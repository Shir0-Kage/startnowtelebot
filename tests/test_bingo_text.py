"""Unit tests for bingo_text — text-mode submission parsing (pure, no
Telegram/OCR/DB deps)."""

import bingo_text
from data import bingo_templates as bt

# real Telegram usernames are 5-32 chars; keep fixture handles realistic so
# normalize_handle's length check doesn't reject them for an unrelated reason
INDEX = {"handles": {"alice", "bobby", "carol", "daniel", "evelyn"}}


def test_build_template_text_has_24_lines_1_indexed_free_omitted():
    text = bingo_text.build_template_text(1)
    lines = text.splitlines()
    assert len(lines) == 24
    assert all("R3C3" not in line for line in lines)  # FREE cell omitted
    assert lines[0].startswith("R1C1: ")
    assert lines[0].rstrip().endswith("-")
    # correct prompt text is embedded
    assert bt.prompt_for(1, 0, 0) in lines[0]
    # last cell in row-major order (skipping FREE) is R5C5
    assert lines[-1].startswith("R5C5: ")


def test_build_prefilled_text_fills_matched_cells_only():
    cells = [
        {"row": 0, "col": 0, "handle": "alice", "score": 91.0},
        {"row": 0, "col": 1, "handle": None, "score": 0.0},
    ]
    text = bingo_text.build_prefilled_text(1, cells)
    lines = {ln.split(":", 1)[0]: ln for ln in text.splitlines()}
    assert lines["R1C1"].rstrip().endswith("@alice")
    assert lines["R1C2"].rstrip().endswith("-")  # unmatched -> still blank
    assert len(text.splitlines()) == 24
    assert all("R3C3" not in ln for ln in text.splitlines())


def test_build_prefilled_text_no_cells_matches_blank_template():
    assert bingo_text.build_prefilled_text(1, []) == bingo_text.build_template_text(1)


def test_parse_submission_exact_match():
    text = "R1C1: Studying the same major as you - @alice"
    read = bingo_text.parse_submission(1, text, INDEX)
    cell = next(c for c in read["cells"] if c["row"] == 0 and c["col"] == 0)
    assert cell["handle"] == "alice"
    assert read["corner"] is None


def test_parse_submission_ignores_garbled_restated_prompt():
    # only the R{}C{}: prefix and the text after the LAST dash matter
    text = "R2C1: this prompt got garbled somehow -- ignore-me - @bobby"
    read = bingo_text.parse_submission(1, text, INDEX)
    cell = next(c for c in read["cells"] if c["row"] == 1 and c["col"] == 0)
    assert cell["handle"] == "bobby"


def test_parse_submission_exact_match_miss_degrades_gracefully():
    # a handle not on the roster is not fuzzy-rescued -- cell stays unmatched
    text = "R1C1: Studying the same major as you - @alicee"
    read = bingo_text.parse_submission(1, text, INDEX)
    cell = next(c for c in read["cells"] if c["row"] == 0 and c["col"] == 0)
    assert cell["handle"] is None
    assert cell["score"] == 0.0


def test_parse_submission_malformed_lines_ignored():
    text = "\n".join([
        "not a line at all",
        "R1: missing column - @alice",
        "R1C1 missing colon - @bobby",
        "R1C2: missing dash @carol",
        "R9C9: out of range - @daniel",
        "R3C3: FREE SPACE - @evelyn",  # the FREE cell itself
    ])
    read = bingo_text.parse_submission(1, text, INDEX)
    assert all(c["handle"] is None for c in read["cells"])


def test_parse_submission_duplicate_line_first_wins():
    text = "\n".join([
        "R1C1: p - @alice",
        "R1C1: p - @bobby",
    ])
    read = bingo_text.parse_submission(1, text, INDEX)
    cell = next(c for c in read["cells"] if c["row"] == 0 and c["col"] == 0)
    assert cell["handle"] == "alice"


def test_parse_submission_later_correction_after_malformed_first_line_wins():
    # a malformed or unresolved first attempt at a cell must not permanently
    # block a later, valid correction for that same cell
    text = "\n".join([
        "R1C1: p with no dash at all",
        "R1C1: p - @alice",
    ])
    read = bingo_text.parse_submission(1, text, INDEX)
    cell = next(c for c in read["cells"] if c["row"] == 0 and c["col"] == 0)
    assert cell["handle"] == "alice"


def test_parse_submission_later_correction_after_unresolved_handle_wins():
    text = "\n".join([
        "R1C1: p - @notonroster",
        "R1C1: p - @alice",
    ])
    read = bingo_text.parse_submission(1, text, INDEX)
    cell = next(c for c in read["cells"] if c["row"] == 0 and c["col"] == 0)
    assert cell["handle"] == "alice"


def test_parse_submission_shape_invariant():
    read = bingo_text.parse_submission(1, "", INDEX)
    assert read["corner"] is None
    assert len(read["cells"]) == 24
    assert all(c["handle"] is None and c["score"] == 0.0 for c in read["cells"])
    seen = {(c["row"], c["col"]) for c in read["cells"]}
    assert (2, 2) not in seen  # FREE cell never emitted
    assert len(seen) == 24


def test_blank_templates_are_pregenerated_and_cached():
    a = bingo_text.build_template_text(3)
    b = bingo_text.build_template_text(3)
    assert a is b                                  # cached object, not rebuilt
    assert set(bingo_text._TEMPLATE_CACHE) == set(range(1, 16))


def test_build_line_confirm_text_lists_only_the_line():
    line = [(0, 0, "alice"), (0, 1, "bob"), (0, 3, "dan"), (0, 4, "eve")]
    out = bingo_text.build_line_confirm_text(1, line)
    assert out.count("\n") == 3                    # 4 cells -> 4 lines
    assert out.startswith("R1C1:")
    assert "@alice" in out and "@eve" in out
    assert "@bob" in out and "@dan" in out
