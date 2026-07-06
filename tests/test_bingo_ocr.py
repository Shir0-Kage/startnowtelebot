import io
import pathlib

import pytest
from PIL import Image

import bingo_ocr


# --- a small, deterministic roster ----------------------------------------
# scores below verified against rapidfuzz WRatio:
#   'joshua_lim'   -> exact 100
#   'joshua_iim'   -> joshua_lim 90        (one OCR typo l->i, within threshold)
#   'r0cket'       -> raw 83 (< 85) but 'rocket' variant -> 100 (0->o confusion)
#   'chloe_tar'    -> chloe_tan 88.9 == chloe_tam 88.9 (margin 0 -> ambiguous None)
#   'amamda'       -> name token 'amanda' 83? -> use name match path
#   'qwertyzxcvb'  -> best 25 (< threshold -> None)
FAKE_MEMBERS = [
    {"handle": "joshua_lim", "name": "Joshua Lim"},
    {"handle": "rocket", "name": "Rachel Ong"},
    {"handle": "chloe_tan", "name": "Chloe Tan"},
    {"handle": "chloe_tam", "name": "Chloe Tam"},
    {"handle": "aqueous27", "name": "Ma Anqi"},
    {"handle": None, "name": "Amanda Wong Sokleng"},   # no handle -> name-only match
    {"handle": "amanda", "name": "Amanda Wong Sokleng"},  # gives 'amanda' a real handle
]


@pytest.fixture
def index():
    return bingo_ocr.build_roster_index(FAKE_MEMBERS)


def test_index_exposes_handles_set(index):
    # Task 6 gates /get_bingo on this set of real normalized handles.
    assert "handles" in index
    assert "joshua_lim" in index["handles"]
    assert "rocket" in index["handles"]
    assert "amanda" in index["handles"]


def test_exact_handle_matches(index):
    handle, score = bingo_ocr.match_handle("@Joshua_Lim", index)
    assert handle == "joshua_lim"
    assert score >= 99


def test_one_typo_within_threshold(index):
    # OCR read 'l' as 'i'
    handle, score = bingo_ocr.match_handle("joshua_iim", index)
    assert handle == "joshua_lim"
    assert score >= 85


def test_ocr_confusion_variant_rescues_below_threshold(index):
    # raw 'r0cket' scores ~83 (below 85); the 0->o variant 'rocket' scores 100.
    handle, score = bingo_ocr.match_handle("r0cket", index)
    assert handle == "rocket"
    assert score >= 85


def test_at_handle_extracted_from_prompt_text(index):
    # A filled cell is the printed PROMPT plus the typed @handle; the handle must
    # be pulled out even though the prompt text dominates the OCR string.
    handle, _ = bingo_ocr.match_handle("Favourite colour is purple @Joshua_Lim", index)
    assert handle == "joshua_lim"


def test_handle_without_at_extracted_from_prompt_text(index):
    # Same, but the player forgot the @ — the word token still matches.
    handle, _ = bingo_ocr.match_handle("Has a film camera rocket", index)
    assert handle == "rocket"


def test_prompt_only_cell_matches_nothing(index):
    # An unfilled cell (just the printed prompt) must NOT match a handle.
    handle, _ = bingo_ocr.match_handle("Has nephews or nieces", index)
    assert handle is None


def test_ambiguous_below_margin_returns_none(index):
    # 'chloe_tar' is equidistant from chloe_tan and chloe_tam -> no clear winner.
    handle, score = bingo_ocr.match_handle("chloe_tar", index)
    assert handle is None
    assert score == 0.0


def test_name_token_match_resolves_to_real_handle(index):
    # someone typed the person's real name; it resolves to the member's REAL
    # @handle (not the lowercased full name), so the cell is confirmable.
    handle, score = bingo_ocr.match_handle("amanda", index)
    assert handle == "amanda"
    assert score >= 85


def test_garbage_returns_none(index):
    handle, score = bingo_ocr.match_handle("qwertyzxcvb", index)
    assert handle is None
    assert score == 0.0


def test_empty_text_returns_none(index):
    assert bingo_ocr.match_handle("", index) == (None, 0.0)
    assert bingo_ocr.match_handle("   ", index) == (None, 0.0)


# --- length-aware threshold: short handles need a stricter score ----------
def test_short_handle_near_miss_is_rejected():
    # a <=6-char handle must clear a raised bar; a 1-char-off read that would
    # pass at 85 for a long handle is rejected for a short one.
    members = [{"handle": "sam", "name": "Sam Toh"},
               {"handle": "pamela", "name": "Pamela Lee"}]
    idx = bingo_ocr.build_roster_index(members)
    # 'pam' vs 'sam' / 'pamela': for a 3-char best_key the effective threshold
    # is BINGO_MATCH_THRESHOLD + 7 (=92), which an ambiguous short read misses.
    handle, score = bingo_ocr.match_handle("pam", idx)
    assert handle is None
    assert score == 0.0


def test_short_handle_exact_still_matches():
    members = [{"handle": "sam", "name": "Sam Toh"}]
    idx = bingo_ocr.build_roster_index(members)
    handle, score = bingo_ocr.match_handle("@sam", idx)  # exact -> 100, clears 92
    assert handle == "sam"
    assert score >= 99


# --- read_submission with a scripted fake engine --------------------------
def _blank_png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), "white").save(buf, format="PNG")
    return buf.getvalue()


class _FakeEngine:
    """Mimics rapidocr_onnxruntime.RapidOCR.__call__.

    Returns (result, elapse) where result is a list of [box, text, conf]
    or None. We script the *text* per call in submission order:
    24 cell crops (row-major, skipping FREE centre) then the corner box.
    """

    def __init__(self, texts):
        self._texts = list(texts)
        self._i = 0

    def __call__(self, image, **kwargs):
        text = self._texts[self._i] if self._i < len(self._texts) else ""
        self._i += 1
        if not text:
            return None, 0.0
        return [[[[0, 0], [1, 0], [1, 1], [0, 1]], text, 0.99]], 0.0


def test_read_submission_maps_cells_and_reads_corner(monkeypatch, index):
    from data import bingo_templates as bt

    # 24 non-free cells in the exact order read_submission iterates them.
    order = [(r, c) for r in range(bt.GRID) for c in range(bt.GRID)
             if not bt.is_free(r, c)]
    assert len(order) == 24

    # script: first cell -> joshua_lim, second -> rocket, rest blank; corner -> '7'
    scripted = [""] * 24
    scripted[0] = "@joshua_lim"
    scripted[1] = "r0cket"
    scripted.append("7")            # corner box read last

    monkeypatch.setattr(bingo_ocr, "_engine", lambda: _FakeEngine(scripted))

    out = bingo_ocr.read_submission(7, _blank_png_bytes(), index)

    assert out["corner"] == 7
    assert len(out["cells"]) == 24
    by_pos = {(c["row"], c["col"]): c for c in out["cells"]}
    r0, c0 = order[0]
    r1, c1 = order[1]
    assert by_pos[(r0, c0)]["handle"] == "joshua_lim"
    assert by_pos[(r1, c1)]["handle"] == "rocket"
    # a blank cell resolves to no handle (safe-empty), score 0.0
    r2, c2 = order[2]
    assert by_pos[(r2, c2)]["handle"] is None
    assert by_pos[(r2, c2)]["score"] == 0.0
    # never emits the FREE centre
    assert bt.FREE not in by_pos


def test_read_submission_corner_none_when_unreadable(monkeypatch, index):
    scripted = [""] * 24 + ["garble"]   # corner not a number
    monkeypatch.setattr(bingo_ocr, "_engine", lambda: _FakeEngine(scripted))
    out = bingo_ocr.read_submission(3, _blank_png_bytes(), index)
    assert out["corner"] is None
    assert all(cell["handle"] is None for cell in out["cells"])
