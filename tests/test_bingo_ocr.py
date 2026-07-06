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


# --- read_submission with a single-pass scripted fake engine --------------
def _blank_png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), "white").save(buf, format="PNG")
    return buf.getvalue()


def _cell_centre_frac(rc):
    from data import bingo_templates as bt
    x0, y0, x1, y1 = bt.CELL_BOXES[rc]
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


class _FakeEngine:
    """Mimics rapidocr_onnxruntime.RapidOCR.__call__ for the single-pass model.

    Call 1 OCRs the whole sheet: we return each scripted detection as a
    [box, text, conf] positioned at a fractional (fx, fy) of the passed image.
    Call 2 OCRs the corner crop: we return the scripted sheet-number text.
    """

    def __init__(self, detections, corner_text=""):
        self._detections = list(detections)   # (fx, fy, text)
        self._corner = corner_text
        self._call = 0

    def __call__(self, image, **kwargs):
        # the pipeline hands the engine PNG bytes; decode to size the boxes
        self._call += 1
        if self._call == 1:
            w, h = Image.open(io.BytesIO(image)).size
            result = []
            for fx, fy, text in self._detections:
                px, py = fx * w, fy * h
                box = [[px - 6, py - 6], [px + 6, py - 6],
                       [px + 6, py + 6], [px - 6, py + 6]]
                result.append([box, text, 0.99])
            return (result or None), 0.0
        if not self._corner:
            return None, 0.0
        return [[[[0, 0], [1, 0], [1, 1], [0, 1]], self._corner, 0.99]], 0.0


def test_read_submission_maps_handles_to_nearest_cell(monkeypatch, index):
    from data import bingo_templates as bt

    cx00, cy00 = _cell_centre_frac((0, 0))
    cx01, cy01 = _cell_centre_frac((0, 1))
    # a handle placed BELOW cell (0,4)'s centre, overhanging into the gutter —
    # nearest-box must still snap it back to (0,4)
    x0, _y0, x1, y1 = bt.CELL_BOXES[(0, 4)]
    overhang_x, overhang_y = (x0 + x1) / 2.0, y1 + bt.GUTTER_F * 0.4

    detections = [
        (cx00, cy00, "@joshua_lim"),
        (cx01, cy01, "r0cket"),                 # 0->o confusion, still matches
        (overhang_x, overhang_y, "@aqueous27"),  # off-centre, snaps to (0,4)
        (0.5, 0.5, "Has a partner"),             # a prompt line -> matches nothing
    ]
    monkeypatch.setattr(bingo_ocr, "_engine",
                        lambda: _FakeEngine(detections, corner_text="7"))

    out = bingo_ocr.read_submission(7, _blank_png_bytes(), index)

    assert out["corner"] == 7
    assert len(out["cells"]) == 24
    by = {(c["row"], c["col"]): c for c in out["cells"]}
    assert by[(0, 0)]["handle"] == "joshua_lim"
    assert by[(0, 1)]["handle"] == "rocket"
    assert by[(0, 4)]["handle"] == "aqueous27"      # overhang snapped to nearest
    # exactly those three cells are filled; the prompt line stayed empty
    assert {rc for rc, c in by.items() if c["handle"]} == {(0, 0), (0, 1), (0, 4)}
    assert bt.FREE not in by


def test_read_submission_ignores_detections_outside_the_grid(monkeypatch, index):
    # a handle-looking string in the title banner / footer must NOT fill a cell
    detections = [
        (0.5, 0.03, "@joshua_lim"),   # up in the banner
        (0.5, 0.97, "rocket"),        # down in the footer
    ]
    monkeypatch.setattr(bingo_ocr, "_engine",
                        lambda: _FakeEngine(detections, corner_text="7"))
    out = bingo_ocr.read_submission(7, _blank_png_bytes(), index)
    assert all(cell["handle"] is None for cell in out["cells"])


def test_read_submission_corner_none_when_unreadable(monkeypatch, index):
    monkeypatch.setattr(bingo_ocr, "_engine",
                        lambda: _FakeEngine([], corner_text="garble"))
    out = bingo_ocr.read_submission(3, _blank_png_bytes(), index)
    assert out["corner"] is None
    assert len(out["cells"]) == 24
    assert all(cell["handle"] is None for cell in out["cells"])
