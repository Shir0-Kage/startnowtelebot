"""Validation for the transcribed bingo templates + measured grid geometry.

Structural geometry (CELL_BOXES / CORNER_BOX ordering + range) is checked
here, plus a crop-accuracy sanity check against a real PNG so a mis-measured
geometry constant is caught in this task rather than surfacing as blank OCR
crops in Task 5.
"""

import pathlib

import pytest
from PIL import Image, ImageStat

from data import bingo_templates as bt


def test_top_level_constants():
    assert bt.GRID == 5
    assert bt.NUM_SHEETS == 15
    assert bt.FREE == (2, 2)


def test_template_dir_has_all_pngs():
    assert isinstance(bt.TEMPLATE_DIR, pathlib.Path)
    assert bt.TEMPLATE_DIR.is_dir()
    for n in range(1, bt.NUM_SHEETS + 1):
        p = bt.template_path(n)
        assert p == bt.TEMPLATE_DIR / f"{n}.png"
        assert p.is_file(), f"missing template {p}"
        assert p.stat().st_size > 0, f"empty template {p}"


def test_template_path_signature():
    assert bt.template_path(7) == bt.TEMPLATE_DIR / "7.png"


def test_sheets_are_5x5_with_free_centre():
    assert set(bt.SHEETS) == set(range(1, bt.NUM_SHEETS + 1))
    for n, grid in bt.SHEETS.items():
        assert len(grid) == bt.GRID, f"sheet {n} has {len(grid)} rows"
        for r, row in enumerate(grid):
            assert len(row) == bt.GRID, f"sheet {n} row {r} not 5 wide"
            for c, prompt in enumerate(row):
                assert isinstance(prompt, str)
                assert prompt.strip(), f"sheet {n} cell {r},{c} is blank"
        assert grid[2][2] == "FREE SPACE", f"sheet {n} centre not FREE"


def test_non_centre_cells_are_not_free_space():
    for n, grid in bt.SHEETS.items():
        for r in range(bt.GRID):
            for c in range(bt.GRID):
                if (r, c) == bt.FREE:
                    continue
                assert grid[r][c] != "FREE SPACE", (
                    f"sheet {n} cell {r},{c} unexpectedly FREE SPACE"
                )


def test_prompts_are_not_placeholders():
    # guards against shipping the "P n r,c" scaffold: no cell may look like a
    # placeholder token, and the sheet must have real, distinct wording.
    import re
    placeholder = re.compile(r"^P\s*\d+\s+\d+\s*,\s*\d+$")
    for n, grid in bt.SHEETS.items():
        for r in range(bt.GRID):
            for c in range(bt.GRID):
                if (r, c) == bt.FREE:
                    continue
                assert not placeholder.match(grid[r][c]), (
                    f"sheet {n} cell {r},{c} is still a placeholder: {grid[r][c]!r}"
                )
        # a real card has many distinct prompts, not 24 copies of one string
        non_free = [grid[r][c] for r in range(bt.GRID) for c in range(bt.GRID)
                    if (r, c) != bt.FREE]
        assert len(set(non_free)) >= 20, f"sheet {n} prompts not distinct enough"


def test_is_free():
    assert bt.is_free(2, 2) is True
    for r in range(bt.GRID):
        for c in range(bt.GRID):
            if (r, c) != (2, 2):
                assert bt.is_free(r, c) is False


def test_cell_boxes_cover_all_25_cells():
    assert len(bt.CELL_BOXES) == bt.GRID * bt.GRID
    assert set(bt.CELL_BOXES) == {
        (r, c) for r in range(bt.GRID) for c in range(bt.GRID)
    }
    # the free centre has a geometry entry but OCR callers skip it
    assert bt.FREE in bt.CELL_BOXES
    ocr_cells = [rc for rc in bt.CELL_BOXES if not bt.is_free(*rc)]
    assert len(ocr_cells) == 24


def test_cell_boxes_are_fractional_and_ordered():
    for (r, c), box in bt.CELL_BOXES.items():
        assert len(box) == 4, f"cell {r},{c} box not a 4-tuple"
        x0, y0, x1, y1 = box
        for v in box:
            assert 0.0 <= v <= 1.0, f"cell {r},{c} coord {v} out of 0..1"
        assert x0 < x1, f"cell {r},{c} x0 !< x1"
        assert y0 < y1, f"cell {r},{c} y0 !< y1"


def test_cell_boxes_do_not_overlap_horizontally_or_vertically():
    # columns advance left->right within a row; rows advance top->bottom
    for r in range(bt.GRID):
        for c in range(bt.GRID - 1):
            assert bt.CELL_BOXES[(r, c)][2] <= bt.CELL_BOXES[(r, c + 1)][0] + 1e-9
    for c in range(bt.GRID):
        for r in range(bt.GRID - 1):
            assert bt.CELL_BOXES[(r, c)][3] <= bt.CELL_BOXES[(r + 1, c)][1] + 1e-9


def test_corner_box_is_fractional_and_top_left():
    x0, y0, x1, y1 = bt.CORNER_BOX
    for v in bt.CORNER_BOX:
        assert 0.0 <= v <= 1.0
    assert x0 < x1 and y0 < y1
    # the sheet number sits above/left of the grid's first cell
    first_x0, first_y0 = bt.CELL_BOXES[(0, 0)][0], bt.CELL_BOXES[(0, 0)][1]
    assert x0 <= first_x0
    assert y0 <= first_y0


def _crop_frac(img, frac):
    w, h = img.size
    x0, y0, x1, y1 = frac
    return img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))


def test_crop_accuracy_boxes_land_on_ink():
    # A correctly-measured box crops a region that contains printed ink, i.e.
    # it is NOT a uniform blank margin. We check that several cell crops and the
    # corner crop have real tonal variation (stddev), catching a geometry that
    # points at empty paper.
    img = Image.open(bt.template_path(1)).convert("L")
    sampled = [(0, 0), (0, 4), (4, 0), (4, 4), (1, 2)]
    inky = 0
    for rc in sampled:
        crop = _crop_frac(img, bt.CELL_BOXES[rc])
        if ImageStat.Stat(crop).stddev[0] > 5.0:
            inky += 1
    assert inky >= 3, "cell boxes appear to land on blank paper — re-measure geometry"
    corner = _crop_frac(img, bt.CORNER_BOX)
    assert ImageStat.Stat(corner).stddev[0] > 5.0, (
        "corner box lands on blank paper — re-measure CORNER_BOX"
    )


def test_prompt_for_matches_sheets():
    assert bt.prompt_for(1, 2, 2) == "FREE SPACE"
    for n, grid in bt.SHEETS.items():
        assert bt.prompt_for(n, 0, 0) == grid[0][0]
        assert bt.prompt_for(n, 4, 4) == grid[4][4]


def test_prompt_for_rejects_bad_sheet():
    with pytest.raises(KeyError):
        bt.prompt_for(99, 0, 0)
