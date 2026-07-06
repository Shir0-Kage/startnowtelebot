"""Unit tests for bingo_lines — pure winning-line logic (no Telegram/OCR/DB).

bingo_lines imports GRID/FREE from data.bingo_templates (Task 2). To keep this
test independent of Task 2's heavy module, we inject a minimal stub exposing
just those two constants into sys.modules before importing bingo_lines.
"""

import sys
import types

import pytest

# --- Stub data.bingo_templates so bingo_lines can import GRID/FREE offline ---
if "data.bingo_templates" not in sys.modules:
    _data_pkg = sys.modules.get("data")
    if _data_pkg is None:
        _data_pkg = types.ModuleType("data")
        _data_pkg.__path__ = []  # mark as a package
        sys.modules["data"] = _data_pkg
    _stub = types.ModuleType("data.bingo_templates")
    _stub.GRID = 5
    _stub.FREE = (2, 2)
    sys.modules["data.bingo_templates"] = _stub
    _data_pkg.bingo_templates = _stub

import bingo_lines  # noqa: E402


# --- helpers ---------------------------------------------------------------

def row_cells(r, handles):
    """matched dict for a full row r, five handles left-to-right."""
    return {(r, c): handles[c] for c in range(5)}

def col_cells(c, handles):
    return {(r, c): handles[r] for r in range(5)}

FIVE = ["ann", "ben", "cara", "dan", "eve"]        # 5 distinct handles
# a free-centre line only needs 4 real handles; the centre is skipped
FOUR = ["ann", "ben", "dan", "eve"]


# --- winning_lines: each of the 5 rows -------------------------------------

@pytest.mark.parametrize("r", range(5))
def test_each_row_wins(r):
    matched = row_cells(r, FIVE)
    lines = bingo_lines.winning_lines(matched, submitter_handle="zoe")
    assert len(lines) == 1
    line = lines[0]
    if r == 2:
        # middle row crosses the free centre -> 4 real cells, centre skipped
        assert len(line) == 4
        assert (2, 2) not in {(rr, cc) for rr, cc, _ in line}
    else:
        assert len(line) == 5
        assert sorted(line) == sorted((r, c, FIVE[c]) for c in range(5))


# --- winning_lines: each of the 5 columns ----------------------------------

@pytest.mark.parametrize("c", range(5))
def test_each_col_wins(c):
    matched = col_cells(c, FIVE)
    lines = bingo_lines.winning_lines(matched, submitter_handle="zoe")
    assert len(lines) == 1
    line = lines[0]
    if c == 2:
        assert len(line) == 4
        assert (2, 2) not in {(rr, cc) for rr, cc, _ in line}
    else:
        assert len(line) == 5
        assert sorted(line) == sorted((r, c, FIVE[r]) for r in range(5))


# --- winning_lines: both diagonals (both pass through the free centre) ------

def test_main_diagonal_wins():
    cells = [(0, 0), (1, 1), (3, 3), (4, 4)]  # (2,2) is FREE, skipped
    matched = {rc: FOUR[i] for i, rc in enumerate(cells)}
    lines = bingo_lines.winning_lines(matched, submitter_handle="zoe")
    assert len(lines) == 1
    assert sorted(lines[0]) == sorted((r, c, matched[(r, c)]) for r, c in cells)

def test_anti_diagonal_wins():
    cells = [(0, 4), (1, 3), (3, 1), (4, 0)]  # (2,2) is FREE, skipped
    matched = {rc: FOUR[i] for i, rc in enumerate(cells)}
    lines = bingo_lines.winning_lines(matched, submitter_handle="zoe")
    assert len(lines) == 1
    assert sorted(lines[0]) == sorted((r, c, matched[(r, c)]) for r, c in cells)


# --- free-centre auto-fill: a diagonal wins with only its 4 real cells ------

def test_free_centre_autofills_no_matched_centre():
    cells = [(0, 0), (1, 1), (3, 3), (4, 4)]
    matched = {rc: FOUR[i] for i, rc in enumerate(cells)}
    assert (2, 2) not in matched  # centre never needs a match
    lines = bingo_lines.winning_lines(matched, submitter_handle="zoe")
    assert len(lines) == 1


# --- incomplete line: one missing real cell -> no win ----------------------

def test_incomplete_row_does_not_win():
    matched = row_cells(0, FIVE)
    del matched[(0, 3)]  # drop one cell
    assert bingo_lines.winning_lines(matched, submitter_handle="zoe") == []


# --- distinct-people rule: a repeated handle invalidates the line ----------

def test_repeated_handle_rejects_line():
    handles = ["ann", "ben", "cara", "ben", "eve"]  # 'ben' twice
    matched = row_cells(0, handles)
    assert bingo_lines.winning_lines(matched, submitter_handle="zoe") == []


# --- submitter self-exclusion: a cell matching the submitter breaks it ------

def test_submitter_own_handle_rejects_line():
    handles = ["ann", "ben", "cara", "dan", "zoe"]  # last is the submitter
    matched = row_cells(0, handles)
    assert bingo_lines.winning_lines(matched, submitter_handle="zoe") == []


# --- multiple lines complete at once: all returned -------------------------

def test_multiple_lines_returned():
    # full row 0 (5 real) AND full main diagonal (4 real via free centre),
    # sharing only (0,0); pick distinct handles so both are valid.
    matched = {}
    matched.update(row_cells(0, FIVE))                       # row 0
    for i, rc in enumerate([(1, 1), (3, 3), (4, 4)]):        # rest of diag
        matched[rc] = ["fred", "gina", "hugo"][i]
    lines = bingo_lines.winning_lines(matched, submitter_handle="zoe")
    lengths = sorted(len(ln) for ln in lines)
    assert lengths == [4, 5]  # diagonal (4 real) + row 0 (5 real)


# --- required_yes: 5 real -> 4, 4 real (free centre) -> 3 ------------------

def test_required_yes_five_real():
    line = [(0, c, FIVE[c]) for c in range(5)]
    assert bingo_lines.required_yes(line) == 4

def test_required_yes_four_real():
    line = [(0, 0, "ann"), (1, 1, "ben"), (3, 3, "dan"), (4, 4, "eve")]
    assert bingo_lines.required_yes(line) == 3


# --- line_passes: at most one miss -----------------------------------------

def test_line_passes_five_all_yes():
    line = [(0, c, FIVE[c]) for c in range(5)]
    answers = {h: "yes" for h in FIVE}
    assert bingo_lines.line_passes(line, answers) is True

def test_line_passes_five_one_unanswered():
    line = [(0, c, FIVE[c]) for c in range(5)]
    answers = {h: "yes" for h in FIVE[:4]}  # 'eve' unanswered -> 1 miss
    assert bingo_lines.line_passes(line, answers) is True

def test_line_passes_five_one_no():
    line = [(0, c, FIVE[c]) for c in range(5)]
    answers = {h: "yes" for h in FIVE}
    answers["eve"] = "no"  # a NO counts as a miss -> still >=4 yes
    assert bingo_lines.line_passes(line, answers) is True

def test_line_fails_five_two_misses():
    line = [(0, c, FIVE[c]) for c in range(5)]
    answers = {h: "yes" for h in FIVE}
    answers["dan"] = "no"   # miss 1
    del answers["eve"]      # miss 2 (unanswered)
    assert bingo_lines.line_passes(line, answers) is False

def test_line_passes_four_one_miss():
    line = [(0, 0, "ann"), (1, 1, "ben"), (3, 3, "dan"), (4, 4, "eve")]
    answers = {"ann": "yes", "ben": "yes", "dan": "yes"}  # 'eve' unanswered
    assert bingo_lines.line_passes(line, answers) is True  # 3 yes >= 3

def test_line_fails_four_two_misses():
    line = [(0, 0, "ann"), (1, 1, "ben"), (3, 3, "dan"), (4, 4, "eve")]
    answers = {"ann": "yes", "ben": "yes", "dan": "no"}  # dan NO + eve unanswered
    assert bingo_lines.line_passes(line, answers) is False  # 2 yes < 3


# --- pick_best_line: fewest real cells, deterministic tie-break -------------

def test_pick_best_line_prefers_fewest_cells():
    five = [(0, c, FIVE[c]) for c in range(5)]
    four = [(0, 0, "ann"), (1, 1, "ben"), (3, 3, "dan"), (4, 4, "eve")]
    assert bingo_lines.pick_best_line([five, four]) == four
    # order-independent
    assert bingo_lines.pick_best_line([four, five]) == four

def test_pick_best_line_deterministic_tie_break():
    # two 4-real lines of equal length: same choice regardless of input order
    a = [(0, 0, "ann"), (1, 1, "ben"), (3, 3, "dan"), (4, 4, "eve")]
    b = [(0, 4, "ann"), (1, 3, "ben"), (3, 1, "dan"), (4, 0, "eve")]
    assert bingo_lines.pick_best_line([a, b]) == bingo_lines.pick_best_line([b, a])
