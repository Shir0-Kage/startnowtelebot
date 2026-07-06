"""OCR + fuzzy handle matching for the Human Bingo submissions.

Given a filled bingo sheet (png/jpg bytes) and the sheet number the player was
allocated, crop each of the 24 non-free cells from the known template geometry,
OCR the handwritten/typed handle, and fuzzy-match it against the closed roster.

Design goals (see the spec): a bad OCR read degrades to an *empty* cell (the
line just doesn't count) and never to matching the wrong person. The engine is
RapidOCR (bundled PP-OCRv4 ONNX models, offline, CPU) instantiated once as a
module-level singleton, exactly like the shared storage connection.
"""

import io
import logging
import re

from PIL import Image, ImageOps, ImageStat

from config import BINGO_MATCH_MARGIN, BINGO_MATCH_THRESHOLD
from data import bingo_templates as bt
from setup.sheets import name_tokens, normalize_handle

# rapidfuzz is a tiny dep; import at module load so match_handle is cheap.
from rapidfuzz import fuzz, process

log = logging.getLogger(__name__)


# --- RapidOCR singleton ----------------------------------------------------
# The onnxruntime import + model load is heavy (~100 MB, a couple of seconds),
# so we build it lazily and reuse it. Tests monkeypatch _engine() with a fake,
# so rapidocr_onnxruntime never needs to be installed to run them.
_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # lazy: heavy import
        _ENGINE = RapidOCR()
    return _ENGINE


def _ocr_text(eng, image):
    """Run *eng* (a RapidOCR-compatible callable) on a PIL image.

    Returns the concatenated text (may be '').  The caller obtains *eng* once
    via _engine() so we share the stateful singleton across all crops in a
    single submission; tests monkeypatch _engine() with a scripted fake.
    """
    result, _elapse = eng(image)
    if not result:
        return ""
    # result is a list of [box, text, confidence]; join text fragments.
    return " ".join(str(line[1]) for line in result if len(line) > 1 and line[1])


# --- Handle normalisation (looser than setup.sheets.normalize_handle) ------

def _raw_handle(raw):
    """Strip leading @ and lowercase — no length/character validation.

    setup.sheets.normalize_handle enforces the Telegram regex (>=5 chars), which
    is correct for user-facing validation.  Here we need to index short handles
    (e.g. 'sam') that real members may genuinely have, so we skip the length
    check for the *index* only.  The 'handles' membership set is what gates
    /get_bingo in Task 6, and it is populated from these raw values.
    """
    if not raw:
        return None
    h = raw.strip().lstrip("@").strip().lower()
    return h if h else None


# --- Roster index ----------------------------------------------------------

def build_roster_index(members):
    """Build the search structure match_handle uses.

    members: list of roster dicts with "handle" (may be None) and "name".
    Returns:
        {"keys": [str, ...],                 # searchable handle / name keys
         "key_to_result": {key: handle},     # each key resolves to a REAL @handle
         "handles": {handle, ...}}           # set of real normalized @handles

    A match *result* is ALWAYS a genuine, normalized Telegram @handle — never a
    lowercased full name — so every matched cell is confirmable (its handle can
    resolve to a user_id).

    Index strategy:
    - Handle-bearing members: index the handle itself (only).  Their name is
      NOT split into individual tokens for the main index, because short tokens
      like 'joshua' are length-<=6 and trigger the strict threshold, producing
      false rejections and noisy second-place competitors that break the margin
      check for the correct handle key.
    - Handle-less members: index name tokens only when another member with the
      same name has a real handle (so the result is always confirmable).

    "handles" is the membership set Task 6 uses to gate /get_bingo.
    """
    keys = []
    key_to_result = {}
    handles = set()

    def add(key, result):
        if not key or not result:
            return
        # first writer wins so a real handle isn't shadowed by a later name key
        keys.append(key)
        key_to_result.setdefault(key, result)

    # First pass: collect every real handle and build name->handle mapping so
    # handle-less members' name tokens can resolve to a real handle result.
    name_to_handle = {}
    for m in members:
        raw_h = m.get("handle")
        handle = _raw_handle(raw_h) if raw_h else None
        name = (m.get("name") or "").strip()
        if handle:
            handles.add(handle)
            if name:
                for tok in name_tokens(name):
                    name_to_handle.setdefault(tok, handle)

    # Second pass: index handle keys (result = the handle).  For handle-less
    # members index each name token pointing to the backing handle (if any).
    for m in members:
        raw_h = m.get("handle")
        handle = _raw_handle(raw_h) if raw_h else None
        name = (m.get("name") or "").strip()
        if handle:
            add(handle, handle)
        elif name:
            # handle-less: add name tokens only when there is a real handle result
            for tok in name_tokens(name):
                backing = name_to_handle.get(tok)
                if backing:
                    add(tok, backing)

    # de-duplicate keys while preserving order (rapidfuzz wants a plain list)
    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return {"keys": uniq, "key_to_result": key_to_result, "handles": handles}


# --- OCR-confusion variants ------------------------------------------------
_CONFUSIONS = [
    ("0", "o"), ("o", "0"),
    ("1", "l"), ("l", "1"),
    ("1", "i"), ("i", "1"),
    ("l", "i"), ("i", "l"),
    ("5", "s"), ("s", "5"),
]


def _variants(text):
    """A small set of cheap OCR-confusion rewrites, including the original."""
    out = {text}
    for a, b in _CONFUSIONS:
        if a in text:
            out.add(text.replace(a, b))
    # rn<->m both directions
    if "rn" in text:
        out.add(text.replace("rn", "m"))
    if "m" in text:
        out.add(text.replace("m", "rn"))
    return out


def _clean(text):
    """Normalize an OCR string to the [a-z0-9_] handle alphabet, keeping spaces
    so name tokens still split. normalize_handle handles the @/case; we fall back
    to a raw lowercase strip when it isn't a valid handle shape."""
    h = normalize_handle(text)
    if h:
        return h
    return re.sub(r"[^a-z0-9_ ]", "", (text or "").lower()).strip()


# --- Fuzzy matcher ---------------------------------------------------------
def match_handle(text, index):
    """Return (result_handle, score) for the best confident match, else (None, 0.0).

    Confident = best score >= effective threshold AND best beats second-best by
    >= BINGO_MATCH_MARGIN. The threshold is LENGTH-AWARE: for a short best key
    (<= 6 chars) it is raised by 7 points (spec §4 step 5), because a one-character
    OCR slip on a short handle is far more likely to land on the wrong person.
    Tries cheap OCR-confusion variants and matches against handles + name tokens.
    Rejected reads are logged so facils can audit misses.
    """
    cleaned = _clean(text)
    if not cleaned:
        return None, 0.0

    keys = index["keys"]
    if not keys:
        return None, 0.0

    best_key = None
    best_score = 0.0
    second_score = 0.0
    for variant in _variants(cleaned):
        if not variant:
            continue
        results = process.extract(
            variant, keys, scorer=fuzz.WRatio, limit=2
        )
        if not results:
            continue
        top_key, top_score, _ = results[0]
        runner = results[1][1] if len(results) > 1 else 0.0
        if top_score > best_score:
            best_score = top_score
            best_key = top_key
            # margin is measured within this variant's own ranking
            second_score = runner

    # length-aware threshold: short handles must clear a higher bar
    threshold = BINGO_MATCH_THRESHOLD + (7 if (best_key and len(best_key) <= 6) else 0)

    if best_score < threshold:
        log.info("bingo match rejected (below threshold): text=%r best_key=%r "
                 "best=%.1f threshold=%d", text, best_key, best_score, threshold)
        return None, 0.0
    if best_score - second_score < BINGO_MATCH_MARGIN:
        log.info("bingo match rejected (below margin): text=%r best_key=%r "
                 "best=%.1f second=%.1f margin=%d", text, best_key, best_score,
                 second_score, BINGO_MATCH_MARGIN)
        return None, 0.0
    return index["key_to_result"][best_key], float(best_score)


# --- Image helpers ---------------------------------------------------------
_REF_WIDTH = 1000       # normalize every upload to this width before cropping
_INSET = 0.08           # trim 8% off each side of a cell to drop prompt/gridlines
_UPSCALE = 3            # LANCZOS upscaling factor for the cropped cell
_DARK_MEAN = 110        # crops darker than this get inverted to dark-on-light


def _load_image(image_bytes):
    im = Image.open(io.BytesIO(image_bytes))
    im = ImageOps.exif_transpose(im)          # fix phone rotation
    im = im.convert("RGB")
    w, h = im.size
    if w != _REF_WIDTH and w > 0:
        scale = _REF_WIDTH / w
        im = im.resize((_REF_WIDTH, max(1, int(h * scale))), Image.LANCZOS)
    return im


def _crop_box(im, frac):
    """Crop a fractional (x0,y0,x1,y1) box with an inward inset, upscaled."""
    w, h = im.size
    x0, y0, x1, y1 = frac
    bw, bh = (x1 - x0), (y1 - y0)
    x0 += bw * _INSET
    x1 -= bw * _INSET
    y0 += bh * _INSET
    y1 -= bh * _INSET
    box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
    crop = im.crop(box)
    if crop.width and crop.height:
        crop = crop.resize(
            (crop.width * _UPSCALE, crop.height * _UPSCALE), Image.LANCZOS
        )
    return crop


def _prep_for_ocr(crop):
    """Grayscale, contrast-boost, invert-if-dark. Returns a PIL image."""
    g = ImageOps.grayscale(crop)
    g = ImageOps.autocontrast(g)
    if ImageStat.Stat(g).mean[0] < _DARK_MEAN:
        g = ImageOps.invert(g)
    return g.convert("RGB")


# --- Public pipeline -------------------------------------------------------
def read_submission(sheet_no, image_bytes, index):
    """OCR one filled sheet. Returns:
        {"corner": int|None,
         "cells": [{"row":int,"col":int,"handle":str|None,"score":float}, ...]}
    with exactly 24 cell dicts (the FREE centre is skipped).

    The OCR engine is obtained once per call so a monkeypatched _engine() that
    returns a scripted fake stays in sync across all 25 crops (24 cells + corner).
    """
    im = _load_image(image_bytes)
    eng = _engine()     # acquire once; tests replace this with a scripted fake

    cells = []
    for row in range(bt.GRID):
        for col in range(bt.GRID):
            if bt.is_free(row, col):
                continue
            frac = bt.CELL_BOXES[(row, col)]
            crop = _prep_for_ocr(_crop_box(im, frac))
            text = _ocr_text(eng, crop)
            handle, score = match_handle(text, index)
            cells.append(
                {"row": row, "col": col, "handle": handle, "score": score}
            )

    corner_crop = _prep_for_ocr(_crop_box(im, bt.CORNER_BOX))
    corner_text = _ocr_text(eng, corner_crop)
    corner = _read_int(corner_text)

    return {"corner": corner, "cells": cells}


def _read_int(text):
    """First run of digits in the OCR text as an int, else None."""
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None
