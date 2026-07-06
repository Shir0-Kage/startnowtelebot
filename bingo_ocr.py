"""OCR + fuzzy handle matching for the Human Bingo submissions.

Given a filled bingo sheet (png/jpg bytes) and the sheet number the player was
allocated, OCR the whole sheet in a single pass, fuzzy-match each detected text
box against the closed roster, and attribute every confident handle to the cell
whose centre it sits *nearest*. Nearest-box (rather than a fixed per-cell crop)
tolerates handles that overhang a cell edge or float in the gutter between
cells, which is how players actually place their name tags.

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


def _encode(pil_img):
    """PNG-encode a PIL image to bytes for the OCR engine.

    RapidOCR's callable accepts str/Path/bytes/ndarray but NOT a PIL image on
    recent (<2) versions, so we hand it bytes — the one form every version reads.
    """
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _ocr_text(eng, image):
    """Run *eng* (a RapidOCR-compatible callable) on a PIL image.

    Returns the concatenated text (may be '').  The caller obtains *eng* once
    via _engine() so we share the stateful singleton across the crops in a
    single submission; tests monkeypatch _engine() with a scripted fake.
    """
    result, _elapse = eng(_encode(image))
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
def _candidates(text):
    """Pull handle-like substrings out of a noisy cell OCR string.

    A filled cell usually contains the printed PROMPT *plus* the player's typed
    "@handle" (e.g. "Favourite colour is purple @BFanL"), so matching the whole
    string dilutes the handle. We try, in priority order: any @-prefixed token
    (the strong signal), then each individual word (the handle may be typed
    without an @), then the whole cleaned string as a last resort.
    """
    cands = []
    # 1) @-prefixed tokens, straight from the raw text
    for tok in re.findall(r"@\s*([A-Za-z0-9_]{2,})", text or ""):
        cands.append(tok.lower())
    # 2) individual word tokens (a handle may be typed without an @)
    for w in re.sub(r"[^a-z0-9_@ ]", " ", (text or "").lower()).split():
        w = w.lstrip("@")
        if len(w) >= 3:
            cands.append(w)
    # 3) the whole cleaned string, as a fallback
    whole = _clean(text)
    if whole:
        cands.append(whole)
    # de-duplicate while preserving priority order
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def match_handle(text, index):
    """Return (result_handle, score) for the best confident match, else (None, 0.0).

    Confident = best score >= effective threshold AND best beats second-best by
    >= BINGO_MATCH_MARGIN. The threshold is LENGTH-AWARE: for a short best key
    (<= 6 chars) it is raised by 7 points (spec §4 step 5), because a one-character
    OCR slip on a short handle is far more likely to land on the wrong person.
    Candidate handle substrings are pulled from the cell text (@-token, words,
    whole) and matched against handles + name tokens, trying cheap OCR-confusion
    variants. Rejected reads are logged so facils can audit misses.
    """
    keys = index["keys"]
    if not keys:
        return None, 0.0

    best_key = None
    best_score = 0.0
    second_score = 0.0
    for cand in _candidates(text):
        for variant in _variants(cand):
            if not variant:
                continue
            results = process.extract(variant, keys, scorer=fuzz.WRatio, limit=2)
            if not results:
                continue
            top_key, top_score, _ = results[0]
            runner = results[1][1] if len(results) > 1 else 0.0
            if top_score > best_score:
                best_score = top_score
                best_key = top_key
                # margin is measured within this candidate's own ranking
                second_score = runner

    if best_key is None:
        return None, 0.0

    # length-aware threshold: short handles must clear a higher bar
    threshold = BINGO_MATCH_THRESHOLD + (7 if len(best_key) <= 6 else 0)
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
_REF_WIDTH = 1000       # normalize a corner crop's source to this width
_DETECT_WIDTH = 1600    # normalize the whole sheet to this width for the OCR pass
_INSET = 0.08           # trim 8% off each side of a cell to drop prompt/gridlines
_UPSCALE = 3            # LANCZOS upscaling factor for the cropped cell
_DARK_MEAN = 110        # crops darker than this get inverted to dark-on-light


def _load_image(image_bytes, ref_width=_REF_WIDTH):
    im = Image.open(io.BytesIO(image_bytes))
    im = ImageOps.exif_transpose(im)          # fix phone rotation
    im = im.convert("RGB")
    w, h = im.size
    if w != ref_width and w > 0:
        scale = ref_width / w
        im = im.resize((ref_width, max(1, int(h * scale))), Image.LANCZOS)
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


# --- Nearest-box attribution ----------------------------------------------
def _cell_centres():
    """Fractional (cx, cy) centre of every non-free cell, keyed by (row, col)."""
    centres = {}
    for (r, c), (x0, y0, x1, y1) in bt.CELL_BOXES.items():
        if bt.is_free(r, c):
            continue
        centres[(r, c)] = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    return centres


def _grid_bounds():
    """Fractional (x_min, y_min, x_max, y_max) of the whole grid, padded by half
    a cell, so detections well outside the 5x5 grid (title banner, footer, the
    corner number) are ignored instead of snapping to an edge cell."""
    boxes = bt.CELL_BOXES.values()
    pad_x, pad_y = bt.CELL_W_F * 0.5, bt.CELL_H_F * 0.5
    return (min(b[0] for b in boxes) - pad_x, min(b[1] for b in boxes) - pad_y,
            max(b[2] for b in boxes) + pad_x, max(b[3] for b in boxes) + pad_y)


def _nearest_cell(fx, fy, centres):
    """(row, col) of the cell whose centre is nearest the fractional point,
    measured in cell-step units so the different x/y scales don't skew it."""
    step_x, step_y = bt.CELL_W_F + bt.GUTTER_F, bt.CELL_H_F + bt.GUTTER_F
    best, best_d = None, None
    for rc, (cx, cy) in centres.items():
        dx, dy = (fx - cx) / step_x, (fy - cy) / step_y
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best_d, best = d, rc
    return best


def _detections(eng, image):
    """OCR the whole image once; yield (fx, fy, text) per detected box, where
    (fx, fy) is the box centroid as a fraction of the image size."""
    w, h = image.size
    result, _elapse = eng(_encode(image))
    out = []
    for line in (result or []):
        if len(line) < 2 or not line[1]:
            continue
        box, text = line[0], line[1]
        try:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
        except (TypeError, IndexError):
            continue
        if not xs or not ys or w <= 0 or h <= 0:
            continue
        out.append(((sum(xs) / len(xs)) / w, (sum(ys) / len(ys)) / h, str(text)))
    return out


# --- Public pipeline -------------------------------------------------------
def read_submission(sheet_no, image_bytes, index):
    """OCR one filled sheet and attribute each handle to its NEAREST cell.

    Returns:
        {"corner": int|None,
         "cells": [{"row":int,"col":int,"handle":str|None,"score":float}, ...]}
    with exactly 24 cell dicts (the FREE centre is skipped).

    The whole sheet is OCR'd in a single pass; each detected text box is fuzzy-
    matched to the roster and, if it's a confident handle, assigned to the cell
    whose centre it sits nearest (when two land on one cell, the higher score
    wins). This tolerates handles that overhang a cell edge or float in the
    gutter, which a fixed per-cell crop could not. The sheet number is read from
    its own small, reliable corner crop.

    The OCR engine is obtained once per call so a monkeypatched _engine() that
    returns a scripted fake stays in sync across the sheet pass and corner crop.
    """
    im = _load_image(image_bytes, _DETECT_WIDTH)
    eng = _engine()     # acquire once; tests replace this with a scripted fake

    centres = _cell_centres()
    x_min, y_min, x_max, y_max = _grid_bounds()
    cells = {rc: {"row": rc[0], "col": rc[1], "handle": None, "score": 0.0}
             for rc in centres}

    for fx, fy, text in _detections(eng, im):
        if not (x_min <= fx <= x_max and y_min <= fy <= y_max):
            continue  # outside the grid: banner, footer, corner number, etc.
        handle, score = match_handle(text, index)
        if not handle:
            continue
        rc = _nearest_cell(fx, fy, centres)
        if rc is not None and score > cells[rc]["score"]:
            cells[rc]["handle"], cells[rc]["score"] = handle, score

    corner_text = _ocr_text(eng, _prep_for_ocr(_crop_box(im, bt.CORNER_BOX)))
    corner = _read_int(corner_text)

    ordered = [cells[(r, c)]
               for r in range(bt.GRID) for c in range(bt.GRID)
               if not bt.is_free(r, c)]
    return {"corner": corner, "cells": ordered}


def _read_int(text):
    """First run of digits in the OCR text as an int, else None."""
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None
