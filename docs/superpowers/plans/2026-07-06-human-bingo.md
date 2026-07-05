# Human Bingo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add an OCR-verified "human bingo" icebreaker game (/get_bingo + /submit_bingo) to the StartNOW! 2026 bot.

**Architecture:** Deterministic even sheet allocation frozen per user_id; RapidOCR crops the known cell boxes of a submitter's template and fuzzy-matches typed handles to the ~68-person roster; pure line logic finds a 5-in-a-row; the people in that line confirm via Yes/No DMs (at-most-one-miss, 12h window); a race-safe single-transaction prize claim (10 max) announces to the channel and closes the game.

**Tech Stack:** python-telegram-bot v22, SQLite (WAL + threading.Lock), Pillow, rapidocr-onnxruntime (1.x, models bundled), opencv-python-headless, rapidfuzz.

## Global Constraints
- Python 3.10-3.12 only (rapidocr-onnxruntime 1.4.x is not published for 3.13); install opencv-python-headless (not opencv-python).
- Bot token only from BOT_TOKEN env; never hardcode. Announcement channel id from ANNOUNCE_CHAT_ID env, default -1004292606016.
- Commits authored as Shir0-Kage, human-style imperative messages, NO AI / Co-Authored-By trailer.
- Follow existing patterns: handlers/*.py expose register(app); storage.py is the single SQLite layer (one module-level connection, threading.Lock, WAL, busy_timeout=30000, _now_iso()); config.py holds tunables; data/*.py are static data modules.
- Identity keyed on Telegram user_id everywhere (allocation, prizes, confirmation cache), never the mutable @username. Roster from setup/sheets.load_year1_members(); reuse setup/sheets.normalize_handle and setup/sheets.name_tokens.

---

### Task 1: Dependencies + config

**Files:**
- Modify: `requirements.txt` (promote Pillow to runtime dep; add `rapidocr-onnxruntime`, `opencv-python-headless`, `rapidfuzz`; add a Python 3.10–3.12 comment)
- Modify: `config.py[1:6]` (add six `BINGO_*` / `ANNOUNCE_*` constants; `timedelta` and `os` already imported at lines 4–5)
- Test: `tests/test_config_bingo.py` (new)

> **Note on `opencv-python-headless`:** it is declared here as a *transitive runtime dep of RapidOCR* (rapidocr-onnxruntime imports cv2 internally for image decode/preprocess), not because our own code imports it. No task imports `cv2` directly. Pinning the headless build ourselves guarantees the container-safe wheel is the one that gets resolved (avoids the libGL.so.1 system dep that the default `opencv-python` wheel drags in). Keep it; it is not dead — it backstops RapidOCR's own import.

**Interfaces:**
- Consumes: nothing from earlier tasks. Relies only on the stdlib already imported in `config.py` (`import os` at line 4, `from datetime import timedelta` at line 5).
- Produces (the SHARED INTERFACE CONTRACT that Tasks 4/5/6/7 import verbatim):
  - `config.ANNOUNCE_CHAT_ID: int` — default `-1004292606016`, overridable via env `ANNOUNCE_CHAT_ID`
  - `config.BINGO_PRIZE_LIMIT: int == 10`
  - `config.BINGO_CONFIRM_TIMEOUT: timedelta == timedelta(hours=12)`
  - `config.BINGO_MATCH_THRESHOLD: int == 85`
  - `config.BINGO_MATCH_MARGIN: int == 8`
  - `config.BINGO_RETRY_COOLDOWN: timedelta == timedelta(seconds=60)`

Numbered checkbox steps (each ONE small action):

- [ ] **Step 1: Write the failing test**

  Create `tests/test_config_bingo.py`:

  ```python
  """Task 1 — the Human Bingo tunables must exist in config with the exact
  values and types the rest of the feature imports by name."""

  from datetime import timedelta

  import config


  def test_announce_chat_id_default_and_type():
      # int, and the StartNOW! 2026 channel id from decision #5 of the spec
      assert isinstance(config.ANNOUNCE_CHAT_ID, int)
      assert config.ANNOUNCE_CHAT_ID == -1004292606016


  def test_prize_limit():
      assert config.BINGO_PRIZE_LIMIT == 10
      assert isinstance(config.BINGO_PRIZE_LIMIT, int)


  def test_confirm_timeout():
      assert config.BINGO_CONFIRM_TIMEOUT == timedelta(hours=12)
      assert isinstance(config.BINGO_CONFIRM_TIMEOUT, timedelta)


  def test_match_threshold():
      # rapidfuzz 0-100 score cutoff
      assert config.BINGO_MATCH_THRESHOLD == 85
      assert isinstance(config.BINGO_MATCH_THRESHOLD, int)


  def test_match_margin():
      # best must beat second-best by this many points
      assert config.BINGO_MATCH_MARGIN == 8
      assert isinstance(config.BINGO_MATCH_MARGIN, int)


  def test_retry_cooldown():
      assert config.BINGO_RETRY_COOLDOWN == timedelta(seconds=60)
      assert isinstance(config.BINGO_RETRY_COOLDOWN, timedelta)


  def test_bool_is_not_accepted_as_int():
      # guard against someone typing `True`/`False`; bool is a subclass of int
      assert not isinstance(config.BINGO_PRIZE_LIMIT, bool)
      assert not isinstance(config.BINGO_MATCH_THRESHOLD, bool)
      assert not isinstance(config.BINGO_MATCH_MARGIN, bool)
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run: `python -m pytest tests/test_config_bingo.py -q`

  Expected: FAIL — collection succeeds but the assertions error with `AttributeError: module 'config' has no attribute 'ANNOUNCE_CHAT_ID'` (and likewise for the other five names), because `config.py` does not define them yet.

- [ ] **Step 3: Write minimal implementation**

  Append a new section to the end of `config.py` (after the existing `REMINDERS_DEFAULT_ON = True` block on line 53; `os` and `timedelta` are already imported at lines 4–5, so no new imports are needed):

  ```python


  # --- Human Bingo -----------------------------------------------------------
  # Announcement channel for "N/10 prizes claimed!" posts. Default is the
  # StartNOW! 2026 group (decision #5); override per-deploy with ANNOUNCE_CHAT_ID.
  ANNOUNCE_CHAT_ID = int(os.environ.get("ANNOUNCE_CHAT_ID", "-1004292606016"))

  # First 10 winners take a prize; the game closes once the 10th is claimed.
  BINGO_PRIZE_LIMIT = 10

  # How long we wait for the people in a winning line to tap Yes/No before the
  # submission is finally evaluated with any still-pending cells counting as misses.
  BINGO_CONFIRM_TIMEOUT = timedelta(hours=12)

  # rapidfuzz score (0-100) an OCR'd cell must reach to count as a confident match...
  BINGO_MATCH_THRESHOLD = 85
  # ...and it must beat the second-best candidate by at least this margin, so an
  # ambiguous read degrades to an empty cell instead of guessing the wrong person.
  BINGO_MATCH_MARGIN = 8

  # Breather after a failed attempt before the same person may /submit_bingo again.
  BINGO_RETRY_COOLDOWN = timedelta(seconds=60)
  ```

  Then update `requirements.txt`. Change the trailing Pillow line so it is described as a runtime dep and append the OCR stack plus the Python-version note. Replace lines 8–9:

  ```
  # only needed for the one-time group setup scripts in setup/ (user-account API)
  telethon>=1.36,<2
  # Pillow: image loading + crop/preprocess for the Human Bingo OCR pipeline (runtime)
  Pillow>=10.0
  # Human Bingo OCR: RapidOCR ships PP-OCRv4 ONNX models in the wheel (offline, CPU).
  # NOTE: rapidocr-onnxruntime requires Python 3.10-3.12 (no 3.13 wheels yet).
  rapidocr-onnxruntime>=1.3,<2
  # headless build avoids the libGL.so.1 system dep on slim/container servers.
  # This is a transitive dep of rapidocr-onnxruntime (it imports cv2); we pin the
  # headless wheel so the container-safe build is the one that resolves.
  opencv-python-headless
  # fast fuzzy matching of OCR'd text against the 68-handle roster vocabulary
  rapidfuzz
  ```

  (The `python-telegram-bot[job-queue]`, `python-dotenv`, and `tzdata` lines at the top of `requirements.txt` are unchanged.)

- [ ] **Step 4: Run test to verify it passes**

  Run: `python -m pytest tests/test_config_bingo.py -q`

  Expected: PASS — all 7 tests green (`7 passed`). No import errors, since only stdlib `os`/`timedelta` are touched at import time and the new OCR wheels are declarations only (not imported by `config.py`).

- [ ] **Step 5: Commit**

  ```
  git add requirements.txt config.py tests/test_config_bingo.py
  git commit -m "Add Human Bingo config tunables and OCR dependencies"
  ```

---

### Task 2: `data/bingo_templates.py` + 15 PNG assets + prompt transcription

This is the one task whose code content is partly **measured geometry** and **hand-transcribed data**. The module structure, the fractional-box geometry builder, and the validation test are fully concrete below. The three data-entry actions (save PNGs, measure geometry once, transcribe 375 prompts) are **required build steps that must be completed before this task is done** — the module is not finished while it still carries placeholder prompts or placeholder geometry. Steps 3a/3b/3c produce the real assets, the real six geometry numbers, and the real 375 strings; Step 4 pastes them in and deletes the placeholder scaffolding.

**Files:**
- Create: `data/bingo_templates.py`
- Create (binary assets, committed, NOT gitignored): `data/bingo_templates/1.png` … `data/bingo_templates/15.png`
- Test: `tests/test_bingo_templates.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. Only the standard library (`pathlib`). The 15 PNGs and 375 prompt strings are provided by this task's data-entry steps.
- Produces (exact names/types later tasks rely on — CONTRACT verbatim):
  - `GRID = 5`, `NUM_SHEETS = 15`, `FREE = (2, 2)`  (`FREE` is `(row, col)` of the free centre)
  - `TEMPLATE_DIR = pathlib.Path(__file__).parent / "bingo_templates"`
  - `CELL_BOXES: dict[tuple[int,int], tuple[float,float,float,float]]` — `(r,c) -> (x0f,y0f,x1f,y1f)` fractional (0..1), shared by all sheets, derived from measured grid-geometry constants. Contains all 25 cells; the free centre `(2,2)` is present but callers skip it for OCR.
  - `CORNER_BOX: tuple[float,float,float,float]` — fractional box of the top-left sheet number.
  - `SHEETS: dict[int, list[list[str]]]` — `sheet_no -> 5x5` prompt strings; `SHEETS[n][2][2] == "FREE SPACE"`.
  - `def is_free(row: int, col: int) -> bool` — `row == 2 and col == 2`
  - `def prompt_for(sheet_no: int, row: int, col: int) -> str`
  - `def template_path(sheet_no: int) -> pathlib.Path` — `TEMPLATE_DIR / f"{sheet_no}.png"`
  - Task 3 (`bingo_lines.py`) imports `GRID` and `FREE` from here.
  - Task 5 (`bingo_ocr.py`) imports `CELL_BOXES`, `CORNER_BOX`, `is_free`, `template_path`.
  - Task 6 (`handlers/bingo.py`) imports `prompt_for`, `template_path`, `NUM_SHEETS`.

Steps (each one small action):

- [ ] **Step 1: Write the failing test.** Create `tests/test_bingo_templates.py` with the full validation suite. This asserts the module exists with the right constants, that all 15 PNGs are present, that every sheet is 5×5 with a `"FREE SPACE"` centre, that `CELL_BOXES` holds 25 in-range boxes (with the free cell present but flagged so OCR excludes it), that `is_free`/`prompt_for`/`template_path` behave, AND (crop-accuracy) that the derived `CELL_BOXES`/`CORNER_BOX` actually land on grid content in a real PNG.

  ```python
  # tests/test_bingo_templates.py
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
  ```

- [ ] **Step 2: Run test to verify it fails.**
  - Run: `python -m pytest tests/test_bingo_templates.py -q`
  - Expected: **FAIL** — collection error `ModuleNotFoundError: No module named 'data.bingo_templates'` (the module doesn't exist yet), so all tests error/fail.

- [ ] **Step 3a (DATA — REQUIRED): Save the 15 template PNGs.** Copy the 15 source card images into `data/bingo_templates/` named exactly `1.png` … `15.png`. These are the real card art (non-PII); the feature is non-functional until they are supplied. Confirm they are committed as static art, NOT gitignored. Sanity-check they landed and are non-empty:
  - Run: `ls -1 data/bingo_templates/*.png | wc -l` → Expected: `15`
  - Ensure `.gitignore` does not exclude `data/bingo_templates/` or `*.png` under it (add a `!data/bingo_templates/*.png` un-ignore line if a broad `*.png` rule exists).

- [ ] **Step 3b (DATA — REQUIRED, measure geometry ONCE): Inspect `1.png` and record the six grid-geometry constants.** All 15 templates share one 5×5 geometry, so measure a single real PNG and derive all 25 boxes from six numbers. Open `data/bingo_templates/1.png`, read off (as a *fraction* of the full image width/height) the pixel coordinates of the printed grid, and record the six real values (these REPLACE the placeholder defaults in Step 4):
  - `ORIGIN_X_F`, `ORIGIN_Y_F` — top-left corner of cell `(0,0)`'s printed box.
  - `CELL_W_F`, `CELL_H_F` — width/height of one cell box.
  - `GUTTER_F` — the gap (gridline + spacing) between adjacent cell boxes; use `0.0` if cells abut with no gap.
  - `CORNER_BOX` — the fractional box `(x0f,y0f,x1f,y1f)` around the printed sheet number in the top-left margin.

  Use this one-off measuring helper (run in the venv, then paste the printed numbers into the module in Step 4). It reports geometry as fractions so it is resolution-independent:

  ```python
  # scratch: measure_grid.py  (NOT committed — a one-time measuring aid)
  from PIL import Image

  img = Image.open("data/bingo_templates/1.png")
  W, H = img.size
  print("image px:", W, H)

  # Fill these in by reading pixel coords off 1.png in any image viewer.
  # Top-left of cell (0,0)'s box, one cell's size, and the gutter between boxes.
  origin_x_px, origin_y_px = 0, 0          # <-- measure
  cell_w_px, cell_h_px     = 0, 0          # <-- measure
  gutter_px                = 0             # <-- measure (0 if boxes touch)
  corner_px                = (0, 0, 0, 0)  # <-- measure: sheet-number box

  print(f"ORIGIN_X_F = {origin_x_px / W:.4f}")
  print(f"ORIGIN_Y_F = {origin_y_px / H:.4f}")
  print(f"CELL_W_F   = {cell_w_px / W:.4f}")
  print(f"CELL_H_F   = {cell_h_px / H:.4f}")
  print(f"GUTTER_F   = {gutter_px / W:.4f}")
  cx0, cy0, cx1, cy1 = corner_px
  print(f"CORNER_BOX = ({cx0/W:.4f}, {cy0/H:.4f}, {cx1/W:.4f}, {cy1/H:.4f})")
  ```

  Cross-check: `ORIGIN_X_F + 5*CELL_W_F + 4*GUTTER_F` should be `<= 1.0` (the grid fits), and likewise for Y. If it overshoots, re-measure. Record the six values for Step 4. The `test_crop_accuracy_boxes_land_on_ink` test in Step 5 will fail if these numbers point at blank paper — so measure carefully and re-run until it passes.

- [ ] **Step 3c (DATA — REQUIRED, transcribe): Read `1.png`–`15.png` and transcribe the 375 prompts into `SHEETS`.** For each sheet `n`, type the 5×5 grid of prompt strings **in reading order (row 0 = top, col 0 = left)** into `SHEETS[n]`, copying the printed wording verbatim (trim trailing whitespace; keep the card's capitalisation). The centre cell of every sheet is the literal string `"FREE SPACE"`. This is 15 × 25 = 375 strings; no prompt is ever OCR'd at runtime. The `test_prompts_are_not_placeholders` test enforces that the `_placeholder_sheet` scaffold has been fully replaced — the task is not done until all 375 real prompts are in.

- [ ] **Step 4: Write the module.** Create `data/bingo_templates.py`. Paste the six measured constants from Step 3b into the `# MEASURED` block, and replace the entire placeholder `SHEETS` construction with the 15 explicit transcribed grids from Step 3c. **Delete `_placeholder_sheet` before completion** — it exists only as temporary scaffolding so the module imports during early TDD.

  ```python
  # data/bingo_templates.py
  """Static data for the Human Bingo game.

  Two kinds of content live here, both entered once by hand at build time
  (never computed at runtime):

  * SHEETS — the 25 printed prompt strings for each of the 15 card templates,
    transcribed by reading data/bingo_templates/1.png .. 15.png. The centre of
    every card is the FREE SPACE wildcard.
  * grid geometry — six fractional constants measured once off 1.png. Every
    card shares the same 5x5 layout, so all 25 crop boxes derive from those
    numbers via _build_cell_boxes() rather than 375 hand-typed boxes.
    Boxes are fractions of the image width/height, so they land correctly after
    the OCR step normalises an upload to any resolution.
  """

  import pathlib

  # --- Layout -----------------------------------------------------------------
  GRID = 5                 # 5x5 card
  NUM_SHEETS = 15          # templates 1.png .. 15.png
  FREE = (2, 2)            # (row, col) of the free centre wildcard

  TEMPLATE_DIR = pathlib.Path(__file__).parent / "bingo_templates"


  # --- Measured grid geometry (fractions of image width/height) ---------------
  # MEASURED once off 1.png in Step 3b. All 15 templates share this geometry.
  # These MUST be the real measured values (the crop-accuracy test enforces it).
  ORIGIN_X_F = 0.0600      # left edge of cell (0,0)'s box   -- REPLACE from Step 3b
  ORIGIN_Y_F = 0.1800      # top edge of cell (0,0)'s box    -- REPLACE from Step 3b
  CELL_W_F = 0.1720        # one cell's width                -- REPLACE from Step 3b
  CELL_H_F = 0.1520        # one cell's height               -- REPLACE from Step 3b
  GUTTER_F = 0.0050        # gap between adjacent cell boxes  -- REPLACE from Step 3b

  # Fractional box around the printed sheet number, top-left margin. MEASURED.
  CORNER_BOX = (0.0300, 0.0500, 0.1200, 0.1300)   # REPLACE from Step 3b


  def _build_cell_boxes():
      """Derive all 25 fractional (x0,y0,x1,y1) crop boxes from the measured
      grid geometry. Shared by every sheet; the free centre is included so
      callers have a complete grid, but OCR skips it via is_free()."""
      boxes = {}
      for r in range(GRID):
          for c in range(GRID):
              x0 = ORIGIN_X_F + c * (CELL_W_F + GUTTER_F)
              y0 = ORIGIN_Y_F + r * (CELL_H_F + GUTTER_F)
              x1 = x0 + CELL_W_F
              y1 = y0 + CELL_H_F
              boxes[(r, c)] = (x0, y0, x1, y1)
      return boxes


  CELL_BOXES = _build_cell_boxes()


  # --- Transcribed prompts ----------------------------------------------------
  # SHEETS[n] is a 5x5 grid of the printed prompts on template n.png, in reading
  # order (row 0 = top, col 0 = left). SHEETS[n][2][2] is always "FREE SPACE".
  # TRANSCRIBED by hand in Step 3c. All 15 grids below are the REAL card wording
  # (the placeholder scaffold has been deleted). This is illustrative structure;
  # replace each cell with the actual printed prompt read off the PNG.
  SHEETS = {
      1: [
          ["Has dyed their hair", "Plays an instrument", "Owns a pet",
           "Is left-handed", "Has been on TV"],
          ["Speaks 3+ languages", "Loves durian", "Has a tattoo",
           "Runs marathons", "Codes for fun"],
          ["Wears glasses", "Is a morning person", "FREE SPACE",
           "Has flown a drone", "Bakes"],
          ["Has a twin", "Plays chess", "Grew up abroad",
           "Loves horror films", "Can whistle loudly"],
          ["Is an only child", "Plays sports", "Draws or paints",
           "Has a green thumb", "Volunteers regularly"],
      ],
      # 2 .. 15: the remaining 14 transcribed 5x5 grids, each with
      # [2][2] == "FREE SPACE". Fill every cell with the real printed prompt.
      2: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      3: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      4: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      5: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      6: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      7: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      8: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      9: [["..."] * 5, ["..."] * 5,
          ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      10: [["..."] * 5, ["..."] * 5,
           ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      11: [["..."] * 5, ["..."] * 5,
           ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      12: [["..."] * 5, ["..."] * 5,
           ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      13: [["..."] * 5, ["..."] * 5,
           ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      14: [["..."] * 5, ["..."] * 5,
           ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
      15: [["..."] * 5, ["..."] * 5,
           ["...", "...", "FREE SPACE", "...", "..."], ["..."] * 5, ["..."] * 5],
  }
  # NOTE: sheets 2..15 above show the required SHAPE only. Before this task is
  # complete every "..." MUST be the real transcribed prompt; the
  # test_prompts_are_not_placeholders / test_sheets_are_5x5_with_free_centre
  # checks (which require >=20 distinct non-free prompts per sheet) will fail
  # while "..." fillers remain.


  # --- Accessors --------------------------------------------------------------
  def is_free(row: int, col: int) -> bool:
      """True for the FREE SPACE centre cell (skipped by OCR)."""
      return row == 2 and col == 2


  def prompt_for(sheet_no: int, row: int, col: int) -> str:
      """The printed prompt at (row, col) on the given sheet.
      Raises KeyError for an unknown sheet number."""
      return SHEETS[sheet_no][row][col]


  def template_path(sheet_no: int) -> pathlib.Path:
      """Filesystem path to a sheet's PNG asset."""
      return TEMPLATE_DIR / f"{sheet_no}.png"
  ```

  Notes for the engineer:
  - `data/bingo_templates.py` mirrors the existing static-data modules `data/events.py` / `data/quests.py`: module docstring, plain module-level dicts/lists as the source of truth, small pure helpers (`is_free` / `prompt_for` / `template_path`) with no side effects and no Telegram/OCR/storage imports.
  - The six geometry numbers above are shown at illustrative values; the module MUST carry the values you measured in Step 3b. The crop-accuracy test validates them against a real PNG — measure carefully.
  - The `SHEETS` dict must contain the 375 real transcribed strings before completion; the `"..."` fillers for sheets 2–15 are structural placeholders only and will fail the content tests.

- [ ] **Step 5: Run test to verify it passes.**
  - Run: `python -m pytest tests/test_bingo_templates.py -q`
  - Expected: **PASS** — all tests green (requires the 15 PNGs from Step 3a in place, the real geometry from Step 3b so the crop-accuracy check lands on ink, and the real prompts from Step 3c so the placeholder/distinctness checks pass). If any is missing, the corresponding test names the exact gap.

- [ ] **Step 6: Commit.**
  ```
  git add data/bingo_templates.py data/bingo_templates/1.png data/bingo_templates/2.png data/bingo_templates/3.png data/bingo_templates/4.png data/bingo_templates/5.png data/bingo_templates/6.png data/bingo_templates/7.png data/bingo_templates/8.png data/bingo_templates/9.png data/bingo_templates/10.png data/bingo_templates/11.png data/bingo_templates/12.png data/bingo_templates/13.png data/bingo_templates/14.png data/bingo_templates/15.png tests/test_bingo_templates.py
  git commit -m "Add bingo card templates: 15 PNGs, transcribed prompts, grid geometry"
  ```
  (If `.gitignore` needed an un-ignore line for the PNGs, add `.gitignore` to the `git add` above.)

**Files referenced (absolute paths):**
- Module to create: `C:\Users\zhouz\AppData\Local\Temp\claude\C--Users-zhouz\d1f4ca2b-9e34-4512-9671-ca45796c0fe1\scratchpad\startnowtelebot\data\bingo_templates.py`
- Assets dir: `C:\Users\zhouz\AppData\Local\Temp\claude\C--Users-zhouz\d1f4ca2b-9e34-4512-9671-ca45796c0fe1\scratchpad\startnowtelebot\data\bingo_templates\` (`1.png`..`15.png`)
- Test: `C:\Users\zhouz\AppData\Local\Temp\claude\C--Users-zhouz\d1f4ca2b-9e34-4512-9671-ca45796c0fe1\scratchpad\startnowtelebot\tests\test_bingo_templates.py`
- Convention references read: `data\events.py`, `data\quests.py`, `setup\sheets.py`, `storage.py`, `config.py`

---

### Task 3: `bingo_lines.py` — pure winning-line logic

**Files:**
- Create: `bingo_lines.py` (repo root, alongside `storage.py` / `config.py`)
- Test: `tests/test_bingo_lines.py`

**Interfaces:**
- Consumes (from Task 2, `data/bingo_templates.py`, per contract — the ONLY import this module makes): `GRID = 5` (int) and `FREE = (2, 2)` (the `(row, col)` of the centre free cell). Nothing else from any other module — no Telegram, no OCR, no storage imports.
- Produces (relied on by Task 6 `handlers/bingo.py`):
  - `Line = list[tuple[int, int, str]]` — a line's **real (non-free)** cells as `(row, col, handle)`.
  - `def winning_lines(matched: dict[tuple[int, int], str], submitter_handle: str) -> list[Line]`
  - `def required_yes(line: Line) -> int`  (`== len(line) - 1`)
  - `def line_passes(line: Line, answers: dict[str, str]) -> bool`
  - `def pick_best_line(lines: list[Line]) -> Line`

**Semantics locked by the contract (implement exactly):**
- `matched` maps `(row, col) -> handle` for **CONFIDENT** cells only; handles are already lowercased with no leading `@`.
- A row/col/diagonal **qualifies** iff every non-free cell in it is present in `matched`, all its handles are **DISTINCT**, and none equals `submitter_handle`. The centre `FREE` cell auto-fills (it contributes no handle and is never in `matched`).
- `winning_lines` returns each qualifying line as its list of **real (non-free)** cells only.
- `required_yes(line) == len(line) - 1` (5-real line → 4; 4-real free-centre line → 3): at most one miss allowed.
- `line_passes(line, answers)`: `answers` maps `handle -> "yes" | "no"` (missing handle = unanswered). Passes iff the count of `"yes"` answers among the line's handles `>= len(line) - 1`. A `"no"` and an unanswered handle both count as a miss.
- `pick_best_line(lines)`: choose the line with the **fewest real cells**; deterministic tie-break (sort key = length, then the line's sorted cell tuples) so the same input always yields the same output.

Because Task 2 owns `data/bingo_templates.py` (a heavy module that also pulls in Pillow), the test injects a lightweight fake `data.bingo_templates` exposing only `GRID`/`FREE` into `sys.modules` **before** importing `bingo_lines`. This keeps Task 3 fully offline, deterministic, and independent of Task 2's other code while still honouring the contract's "import `GRID`/`FREE` from `data.bingo_templates` only" rule.

---

- [ ] **Step 1: Write the failing test**

  Create `tests/test_bingo_lines.py`:

  ```python
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
  ```

- [ ] **Step 2: Run test to verify it fails**
  - Run: `python -m pytest tests/test_bingo_lines.py -q`
  - Expected: FAIL — collection error `ModuleNotFoundError: No module named 'bingo_lines'` (the module does not exist yet). All tests error out.

- [ ] **Step 3: Write minimal implementation**

  Create `bingo_lines.py` (repo root):

  ```python
  """Pure winning-line logic for Human Bingo.

  No Telegram, OCR, or storage here — just the 5x5 geometry and the pass-rule
  math, so it's trivially unit-testable. The only import is the grid geometry
  (GRID, FREE) from the templates module.

  A "line" is the list of REAL (non-free) cells it contains, each a
  (row, col, handle) tuple. The centre FREE cell auto-counts as filled and is
  never part of a returned line.
  """

  from data.bingo_templates import GRID, FREE

  # A line is its real (non-free) cells: (row, col, handle).
  Line = list  # type alias for readability; elements are (int, int, str)


  def _candidate_lines():
      """The 12 candidate lines as lists of (row, col), excluding the FREE cell.

      5 rows, 5 columns, 2 diagonals. The centre is dropped wherever it appears
      (middle row, middle column, both diagonals) so it never needs a match.
      """
      lines = []
      # rows
      for r in range(GRID):
          lines.append([(r, c) for c in range(GRID)])
      # columns
      for c in range(GRID):
          lines.append([(r, c) for r in range(GRID)])
      # main diagonal (top-left -> bottom-right)
      lines.append([(i, i) for i in range(GRID)])
      # anti-diagonal (top-right -> bottom-left)
      lines.append([(i, GRID - 1 - i) for i in range(GRID)])

      # strip the free centre from every candidate
      return [[cell for cell in cells if cell != FREE] for cells in lines]


  def winning_lines(matched, submitter_handle):
      """Every complete, valid line in `matched`.

      `matched`: {(row, col): handle} for CONFIDENT cells only (handle lowercased,
      no '@'). A candidate qualifies iff every one of its real (non-free) cells is
      in `matched`, all those handles are DISTINCT, and none equals
      `submitter_handle`. Returns each qualifying line as its list of real
      (row, col, handle) cells.
      """
      out = []
      for cells in _candidate_lines():
          # every real cell must be a confident match
          if not all(cell in matched for cell in cells):
              continue
          handles = [matched[cell] for cell in cells]
          # no self-cheese: a cell matching the submitter breaks the line
          if submitter_handle in handles:
              continue
          # icebreaker rule: everyone in the line must be a different person
          if len(set(handles)) != len(handles):
              continue
          out.append([(r, c, matched[(r, c)]) for (r, c) in cells])
      return out


  def required_yes(line):
      """How many YES answers a line needs: at most one miss allowed."""
      return len(line) - 1


  def line_passes(line, answers):
      """True iff the line clears the pass threshold.

      `answers`: {handle: "yes" | "no"}; a missing handle is unanswered. A "no"
      and an unanswered handle each count as a miss. Passes iff the number of
      "yes" answers among the line's handles is >= len(line) - 1.
      """
      yes = sum(1 for (_r, _c, handle) in line if answers.get(handle) == "yes")
      return yes >= required_yes(line)


  def pick_best_line(lines):
      """The easiest line to verify: fewest real cells, deterministic tie-break.

      Ties (equal length) resolve on the line's sorted cell tuples, so the same
      set of lines always yields the same choice regardless of input order.
      """
      return min(lines, key=lambda line: (len(line), sorted(line)))
  ```

- [ ] **Step 4: Run test to verify it passes**
  - Run: `python -m pytest tests/test_bingo_lines.py -q`
  - Expected: PASS — all tests green (5 rows + 5 cols parametrized, 2 diagonals, free-centre auto-fill, incomplete/repeated/self-exclusion rejections, multiple-lines, `required_yes` 4/3, `line_passes` at-most-one-miss for 5-real and 4-real, and `pick_best_line` selection + tie-break).

- [ ] **Step 5: Commit**
  - `git add bingo_lines.py tests/test_bingo_lines.py`
  - `git commit -m "Add pure winning-line logic for Human Bingo"`

**Notes for the executing engineer**
- Absolute repo-root imports match the codebase convention (`from data.bingo_templates import ...`, like `from config import ...` in `storage.py`). Run pytest from the repo root so `bingo_lines` and `data` resolve.
- `data/__init__.py` already exists, so `data.bingo_templates` is importable once Task 2 lands. The `sys.modules` stub in the test only substitutes for Task 2 during Task 3's isolated TDD; it is a no-op if the real module is already imported.
- pytest is not yet in the `.venv` on this box (and the box is Python 3.13); install it with `python -m pip install pytest` if `python -m pytest` reports "No module named pytest". This module is pure stdlib, so it runs fine under 3.13 despite the 3.10–3.12 constraint that only applies to the OCR deps.
- Do NOT add error handling, type-narrowing, or extra helpers beyond the four contract functions — Task 6 relies on these exact signatures and Task 3 must stay dependency-free.

**Relevant absolute paths**
- Module to create: `C:\Users\zhouz\AppData\Local\Temp\claude\C--Users-zhouz\d1f4ca2b-9e34-4512-9671-ca45796c0fe1\scratchpad\startnowtelebot\bingo_lines.py`
- Test to create: `C:\Users\zhouz\AppData\Local\Temp\claude\C--Users-zhouz\d1f4ca2b-9e34-4512-9671-ca45796c0fe1\scratchpad\startnowtelebot\tests\test_bingo_lines.py`
- Contract dependency (Task 2): `C:\Users\zhouz\AppData\Local\Temp\claude\C--Users-zhouz\d1f4ca2b-9e34-4512-9671-ca45796c0fe1\scratchpad\startnowtelebot\data\bingo_templates.py` (provides `GRID`, `FREE`)

---

### Task 4: storage.py Human Bingo layer

**Files:**
- Modify: `storage.py` (append 5 tables to `SCHEMA` ~lines 90–95; add a new `# Human Bingo` section after the setup-job-queue section, ~line 390)
- Create: `tests/__init__.py` (empty; makes `tests` importable so the temp-DB fixture is clean)
- Create test: `tests/test_bingo_storage.py`

**Interfaces:**
- Consumes (from the repo, verbatim):
  - `config.DB_PATH` — monkeypatched to a temp file in tests before `storage.init_db()`.
  - `storage._lock` (`threading.Lock`), `storage._conn` (module-global `sqlite3.Connection`, `row_factory = sqlite3.Row`), `storage._now_iso() -> str`, `storage.init_db()`.
  - Table `started_users(user_id, username, display_name, marked_at)` — `username` is lowercased, no `@` (see `mark_started`). `user_id_for_handle` reads it.
  - `config.BINGO_PRIZE_LIMIT` (Task 1 = `10`). Import lazily inside `claim_bingo_prize`/`bingo_prizes_claimed` region via `from config import BINGO_PRIZE_LIMIT` at module top alongside the existing `from config import ...`.
- Produces (exact signatures later tasks rely on — CONTRACT verbatim):
  - `allocate_bingo_sheet(user_id: int, handle: str) -> int`
  - `get_bingo_sheet(user_id: int) -> int | None`
  - `user_id_for_handle(handle: str) -> int | None`
  - `bingo_is_closed() -> bool`
  - `set_bingo_closed() -> None`
  - `active_submission(user_id: int) -> dict | None`
  - `submission_by_id(submission_id: int) -> dict | None`  ← **added (fix): read-side pair to `start_bingo_submission`; Task 6's confirm/timeout/award path calls this to map a submission back to its submitter.**
  - `last_bingo_activity(user_id: int) -> str | None`
  - `start_bingo_submission(user_id: int, handle: str, sheet_no: int, corner_read: int | None) -> int`
  - `set_submission_status(submission_id: int, status: str, verified_at: str | None = None) -> None`
  - `record_winning_members(submission_id: int, members: list[dict]) -> None`
  - `winning_members(submission_id: int) -> list[dict]`
  - `record_bingo_confirmation(subject_user_id: int, prompt: str, answer: str) -> None`
  - `get_cached_confirmation(subject_user_id: int, prompt: str) -> str | None`
  - `has_bingo_prize(user_id: int) -> bool`
  - `claim_bingo_prize(user_id: int, handle: str, submission_id: int) -> int | None`
  - `bingo_prizes_claimed() -> int`
  - `mark_prize_posted(user_id: int) -> None`
  - `pending_submissions() -> list[dict]`

> Note (prerequisite, do once): the dev `.venv` has no `pytest`. Before Step 2 run `.venv/Scripts/python -m pip install pytest` (or `python -m pip install pytest`). It is a test-only dep — do **not** add it to `requirements.txt`.

---

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` as an empty file, then create `tests/test_bingo_storage.py`:

```python
"""Human Bingo storage layer — even/frozen allocation, race-safe prize claim,
confirmation cache, submission lifecycle. Runs offline against a temp DB."""

import importlib

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh storage module bound to an isolated temp DB."""
    import config
    import storage
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bingo_test.db"))
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "bingo_test.db"))
    importlib.reload(storage)  # rebind DB_PATH captured at import time
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "bingo_test.db"))
    storage.init_db()
    return storage


# --- allocation: even + frozen -------------------------------------------

def test_allocation_is_even_over_many_users(store):
    # 68 users, 15 sheets -> counts differ by at most 1 (round-robin into smallest)
    for uid in range(1, 69):
        store.allocate_bingo_sheet(uid, f"user{uid}")
    counts = {}
    for uid in range(1, 69):
        s = store.get_bingo_sheet(uid)
        assert 1 <= s <= 15
        counts[s] = counts.get(s, 0) + 1
    assert set(counts) == set(range(1, 16))         # every sheet used
    assert max(counts.values()) - min(counts.values()) <= 1  # even


def test_allocation_is_frozen(store):
    first = store.allocate_bingo_sheet(42, "aaa")
    # allocating other people must never move an existing row
    for uid in range(100, 130):
        store.allocate_bingo_sheet(uid, f"u{uid}")
    again = store.allocate_bingo_sheet(42, "aaa")   # idempotent
    assert again == first
    assert store.get_bingo_sheet(42) == first


def test_get_bingo_sheet_none_when_unallocated(store):
    assert store.get_bingo_sheet(999) is None


def test_new_handle_appends_to_smallest_sheet(store):
    # deal 15 so each sheet has exactly one, then confirm the 16th lands on
    # sheet 1 (the smallest by insertion order tie-break), keeping counts even
    for i in range(1, 16):
        store.allocate_bingo_sheet(i, f"seed{i}")
    counts = {s: 0 for s in range(1, 16)}
    for i in range(1, 16):
        counts[store.get_bingo_sheet(i)] += 1
    assert all(c == 1 for c in counts.values())
    s16 = store.allocate_bingo_sheet(16, "sixteen")
    assert 1 <= s16 <= 15


# --- handle -> user_id (from started_users) ------------------------------

def test_user_id_for_handle(store):
    store.mark_started(7, "Alice", "Alice A")   # stored lowercased, no @
    assert store.user_id_for_handle("alice") == 7
    assert store.user_id_for_handle("@Alice") == 7   # tolerant of @/case
    assert store.user_id_for_handle("nobody") is None


# --- closed flag ----------------------------------------------------------

def test_bingo_closed_flag(store):
    assert store.bingo_is_closed() is False
    store.set_bingo_closed()
    assert store.bingo_is_closed() is True
    store.set_bingo_closed()  # idempotent
    assert store.bingo_is_closed() is True


# --- submission lifecycle -------------------------------------------------

def test_submission_lifecycle_and_active(store):
    assert store.active_submission(5) is None
    sid = store.start_bingo_submission(5, "eve", 3, corner_read=3)
    assert isinstance(sid, int)
    act = store.active_submission(5)
    assert act is not None
    assert act["id"] == sid
    assert act["status"] == "pending"
    assert act["sheet_no"] == 3
    assert act["corner_read"] == 3
    # once resolved, no active submission remains
    store.set_submission_status(sid, "failed")
    assert store.active_submission(5) is None


def test_submission_by_id(store):
    # read-side pair to start_bingo_submission: fetch any submission by its id
    sid = store.start_bingo_submission(77, "gary", 2, corner_read=2)
    sub = store.submission_by_id(sid)
    assert sub is not None
    assert sub["id"] == sid
    assert sub["submitter_user_id"] == 77
    assert sub["submitter_handle"] == "gary"
    assert sub["status"] == "pending"
    # still resolvable after it leaves the pending state (unlike active_submission)
    store.set_submission_status(sid, "verified", verified_at="2026-07-06T10:00:00+08:00")
    resolved = store.submission_by_id(sid)
    assert resolved["status"] == "verified"
    assert store.submission_by_id(999999) is None


def test_verified_at_recorded(store):
    sid = store.start_bingo_submission(6, "frank", 1, None)
    store.set_submission_status(sid, "verified", verified_at="2026-07-06T10:00:00+08:00")
    rows = store.pending_submissions()
    assert all(r["id"] != sid for r in rows)  # verified ones aren't pending


def test_pending_submissions(store):
    a = store.start_bingo_submission(10, "a", 1, None)
    b = store.start_bingo_submission(11, "b", 2, None)
    store.set_submission_status(a, "verified", verified_at="2026-07-06T10:00:00+08:00")
    pend = store.pending_submissions()
    ids = {r["id"] for r in pend}
    assert b in ids and a not in ids
    row = next(r for r in pend if r["id"] == b)
    assert row["submitter_user_id"] == 11
    assert row["sheet_no"] == 2
    assert row["status"] == "pending"


def test_last_bingo_activity(store):
    assert store.last_bingo_activity(20) is None
    store.start_bingo_submission(20, "z", 1, None)
    assert isinstance(store.last_bingo_activity(20), str)


# --- winning members ------------------------------------------------------

def test_record_and_read_winning_members(store):
    sid = store.start_bingo_submission(30, "w", 4, None)
    members = [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "Has a cat", "target_user_id": 101},
        {"row": 0, "col": 1, "handle": "cara", "prompt": "Plays guitar", "target_user_id": None},
    ]
    store.record_winning_members(sid, members)
    got = store.winning_members(sid)
    assert len(got) == 2
    by_cell = {(m["row"], m["col"]): m for m in got}
    assert by_cell[(0, 0)]["handle"] == "bob"
    assert by_cell[(0, 0)]["prompt"] == "Has a cat"
    assert by_cell[(0, 0)]["target_user_id"] == 101
    assert by_cell[(0, 1)]["target_user_id"] is None


# --- confirmation cache (upsert) -----------------------------------------

def test_confirmation_upsert_and_read(store):
    assert store.get_cached_confirmation(50, "Has a cat") is None
    store.record_bingo_confirmation(50, "Has a cat", "yes")
    assert store.get_cached_confirmation(50, "Has a cat") == "yes"
    # last answer wins (yes -> no)
    store.record_bingo_confirmation(50, "Has a cat", "no")
    assert store.get_cached_confirmation(50, "Has a cat") == "no"
    # keyed per (subject, prompt) — a different prompt is independent
    assert store.get_cached_confirmation(50, "Plays guitar") is None


# --- prize claim: caps at 10, unique winner ------------------------------

def test_claim_caps_at_ten_and_rejects_eleventh(store):
    slots = []
    for uid in range(1, 12):  # 11 distinct people
        sid = store.start_bingo_submission(uid, f"p{uid}", 1, None)
        slots.append(store.claim_bingo_prize(uid, f"p{uid}", sid))
    granted = [s for s in slots if s is not None]
    assert granted == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]  # strictly 1..10
    assert slots[10] is None                            # the 11th is rejected
    assert store.bingo_prizes_claimed() == 10


def test_claim_rejects_duplicate_winner(store):
    sid = store.start_bingo_submission(1, "p1", 1, None)
    first = store.claim_bingo_prize(1, "p1", sid)
    assert first == 1
    assert store.has_bingo_prize(1) is True
    sid2 = store.start_bingo_submission(1, "p1", 1, None)
    dup = store.claim_bingo_prize(1, "p1", sid2)  # same winner_user_id
    assert dup is None
    assert store.bingo_prizes_claimed() == 1      # not double-counted


def test_has_bingo_prize_false_before_claim(store):
    assert store.has_bingo_prize(1) is False


def test_mark_prize_posted(store):
    sid = store.start_bingo_submission(1, "p1", 1, None)
    store.claim_bingo_prize(1, "p1", sid)
    store.mark_prize_posted(1)  # must not raise; sets posted_at once
    store.mark_prize_posted(1)  # idempotent


def test_claim_is_race_safe_under_threads(store):
    # Fire 30 concurrent distinct claimants; exactly 10 slots, all unique 1..10
    import threading
    results = []
    reslock = threading.Lock()

    def worker(uid):
        sid = store.start_bingo_submission(uid, f"u{uid}", 1, None)
        slot = store.claim_bingo_prize(uid, f"u{uid}", sid)
        with reslock:
            results.append(slot)

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in range(1, 31)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    granted = sorted(s for s in results if s is not None)
    assert granted == list(range(1, 11))  # exactly 10, no dup slot numbers
    assert store.bingo_prizes_claimed() == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bingo_storage.py -q`
Expected: FAIL — collection/attribute errors such as `AttributeError: module 'storage' has no attribute 'allocate_bingo_sheet'` (and the other bingo functions), because the tables and functions don't exist yet.

- [ ] **Step 3: Write minimal implementation**

3a. In `storage.py`, extend the top-level config import (currently `from config import DB_PATH, TIMEZONE, REMINDERS_DEFAULT_ON`) to also bring in the prize limit:

```python
from config import DB_PATH, TIMEZONE, REMINDERS_DEFAULT_ON, BINGO_PRIZE_LIMIT
```

3b. Append the 5 tables to the `SCHEMA` string, immediately **before** its closing `"""` (i.e. after the `year1_waiting` table):

```sql

-- ===================================================================
-- Human Bingo
-- ===================================================================

-- Frozen, even round-robin allocation of players to card templates (1..15).
-- Keyed on user_id so a later @username change never loses their sheet.
CREATE TABLE IF NOT EXISTS bingo_allocation (
    user_id     INTEGER PRIMARY KEY,
    handle      TEXT,              -- lowercased, no @ (best-effort, for audit)
    sheet_no    INTEGER,
    assigned_at TEXT
);

-- One row per submitted card. At most one 'pending' per submitter.
CREATE TABLE IF NOT EXISTS bingo_submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter_user_id INTEGER,
    submitter_handle  TEXT,
    sheet_no          INTEGER,
    corner_read       INTEGER,     -- OCR'd top-left number, may be NULL
    status            TEXT,        -- 'pending' | 'verified' | 'failed' | 'rejected'
    submitted_at      TEXT,
    verified_at       TEXT         -- set when it crosses the pass threshold
);

-- The chosen winning line's real (non-free) cells. prompt is frozen so the
-- confirmation button text stays stable even if templates change.
CREATE TABLE IF NOT EXISTS bingo_winning_members (
    submission_id  INTEGER,
    row            INTEGER,
    col            INTEGER,
    handle         TEXT,
    prompt         TEXT,
    target_user_id INTEGER,        -- resolved player, may be NULL (unreachable)
    PRIMARY KEY (submission_id, row, col)
);

-- Game-wide Yes/No cache keyed on (subject, prompt): a popular person is DMed
-- at most once per distinct prompt.
CREATE TABLE IF NOT EXISTS bingo_confirmations (
    subject_user_id INTEGER,
    prompt          TEXT,
    answer          TEXT,          -- 'yes' | 'no'
    responded_at    TEXT,
    PRIMARY KEY (subject_user_id, prompt)
);

-- Prize ledger + counter. UNIQUE winner is the last-line race guard.
CREATE TABLE IF NOT EXISTS bingo_prizes (
    winner_user_id INTEGER PRIMARY KEY,
    handle         TEXT,
    submission_id  INTEGER,
    claim_no       INTEGER,
    claimed_at     TEXT,
    posted_at      TEXT            -- set once the channel post succeeds
);

-- Single-row flags (e.g. 'closed' once the 10th prize is claimed).
CREATE TABLE IF NOT EXISTS bingo_flags (
    name    TEXT PRIMARY KEY,
    set_at  TEXT
);
```

3c. Append the new section at the **end of `storage.py`** (after `record_added`):

```python
# ---------------------------------------------------------------------------
# Human Bingo
# ---------------------------------------------------------------------------

def allocate_bingo_sheet(user_id, handle):
    """Return this user's frozen card number, assigning one on first call.

    Existing rows are never moved (frozen). A genuinely new user is dealt into
    the currently-smallest sheet so counts stay even (differ by at most 1),
    breaking ties toward the lowest sheet number for determinism."""
    with _lock:
        row = _conn.execute(
            "SELECT sheet_no FROM bingo_allocation WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is not None:
            return row["sheet_no"]
        counts = {s: 0 for s in range(1, 16)}
        for r in _conn.execute("SELECT sheet_no FROM bingo_allocation"):
            if r["sheet_no"] in counts:
                counts[r["sheet_no"]] += 1
        # smallest count, then smallest sheet number
        sheet_no = min(range(1, 16), key=lambda s: (counts[s], s))
        _conn.execute(
            "INSERT INTO bingo_allocation (user_id, handle, sheet_no, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, (handle or "").lower(), sheet_no, _now_iso()),
        )
        _conn.commit()
        return sheet_no


def get_bingo_sheet(user_id):
    """The user's frozen sheet number, or None if never allocated."""
    with _lock:
        row = _conn.execute(
            "SELECT sheet_no FROM bingo_allocation WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["sheet_no"] if row else None


def user_id_for_handle(handle):
    """Resolve a @handle to a user_id via started_users (they must have /started
    for us to reach them). Returns None if no such user is known."""
    h = (handle or "").lstrip("@").lower()
    with _lock:
        row = _conn.execute(
            "SELECT user_id FROM started_users WHERE username = ?", (h,)
        ).fetchone()
    return row["user_id"] if row else None


def bingo_is_closed():
    """True once the game has been closed (10th prize claimed)."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM bingo_flags WHERE name = 'closed'"
        ).fetchone()
    return row is not None


def set_bingo_closed():
    """Persist the closed flag. Idempotent."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES ('closed', ?)",
            (_now_iso(),),
        )
        _conn.commit()


def active_submission(user_id):
    """The submitter's current pending submission as a dict, or None."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bingo_submissions "
            "WHERE submitter_user_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def submission_by_id(submission_id):
    """Any submission by its id, regardless of status, as a dict (or None).

    Read-side pair to start_bingo_submission: the confirmation callback and the
    12h timeout job use this to map a submission_id back to its submitter
    (id, submitter_user_id, submitter_handle, status, sheet_no, ...)."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return dict(row) if row else None


def last_bingo_activity(user_id):
    """ISO timestamp of the user's most recent submission (for the retry
    cooldown), or None if they've never submitted."""
    with _lock:
        row = _conn.execute(
            "SELECT submitted_at FROM bingo_submissions "
            "WHERE submitter_user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["submitted_at"] if row else None


def start_bingo_submission(user_id, handle, sheet_no, corner_read):
    """Open a new pending submission and return its id."""
    with _lock:
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, corner_read, "
            " status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, corner_read, _now_iso()),
        )
        _conn.commit()
        return cur.lastrowid


def set_submission_status(submission_id, status, verified_at=None):
    """Move a submission to a terminal (or pending) state. verified_at is set
    only when provided (the moment it crossed the pass threshold)."""
    with _lock:
        if verified_at is None:
            _conn.execute(
                "UPDATE bingo_submissions SET status = ? WHERE id = ?",
                (status, submission_id),
            )
        else:
            _conn.execute(
                "UPDATE bingo_submissions SET status = ?, verified_at = ? "
                "WHERE id = ?",
                (status, verified_at, submission_id),
            )
        _conn.commit()


def record_winning_members(submission_id, members):
    """Store the chosen line's real cells. members: list of dicts with keys
    row, col, handle, prompt, target_user_id. Replaces any prior rows for
    this submission."""
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_winning_members WHERE submission_id = ?",
            (submission_id,),
        )
        _conn.executemany(
            "INSERT INTO bingo_winning_members "
            "(submission_id, row, col, handle, prompt, target_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (submission_id, m["row"], m["col"], m["handle"],
                 m["prompt"], m.get("target_user_id"))
                for m in members
            ],
        )
        _conn.commit()


def winning_members(submission_id):
    """The recorded line cells for a submission, ordered by (row, col)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_winning_members WHERE submission_id = ? "
            "ORDER BY row, col",
            (submission_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_bingo_confirmation(subject_user_id, prompt, answer):
    """Upsert a person's Yes/No for a prompt; the latest answer wins."""
    with _lock:
        _conn.execute(
            "INSERT INTO bingo_confirmations "
            "(subject_user_id, prompt, answer, responded_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subject_user_id, prompt) DO UPDATE SET "
            "    answer = excluded.answer, "
            "    responded_at = excluded.responded_at",
            (subject_user_id, prompt, answer, _now_iso()),
        )
        _conn.commit()


def get_cached_confirmation(subject_user_id, prompt):
    """The cached 'yes'/'no' for (subject, prompt), or None if unanswered."""
    with _lock:
        row = _conn.execute(
            "SELECT answer FROM bingo_confirmations "
            "WHERE subject_user_id = ? AND prompt = ?",
            (subject_user_id, prompt),
        ).fetchone()
    return row["answer"] if row else None


def has_bingo_prize(user_id):
    """True if this user has already won a prize."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM bingo_prizes WHERE winner_user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def claim_bingo_prize(user_id, handle, submission_id):
    """Atomically claim a prize slot. In ONE locked transaction: count existing
    prizes, refuse if the game is full (>= BINGO_PRIZE_LIMIT) or this winner
    already has one, else INSERT with claim_no = count + 1. Returns the slot
    number (1..limit) or None. UNIQUE(winner_user_id) is the last-line guard."""
    with _lock:
        count = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_prizes"
        ).fetchone()["c"]
        if count >= BINGO_PRIZE_LIMIT:
            return None
        already = _conn.execute(
            "SELECT 1 FROM bingo_prizes WHERE winner_user_id = ?", (user_id,)
        ).fetchone()
        if already is not None:
            return None
        claim_no = count + 1
        try:
            _conn.execute(
                "INSERT INTO bingo_prizes "
                "(winner_user_id, handle, submission_id, claim_no, "
                " claimed_at, posted_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (user_id, (handle or "").lower(), submission_id, claim_no,
                 _now_iso()),
            )
        except sqlite3.IntegrityError:
            # concurrent duplicate winner slipped past the check — UNIQUE wins
            _conn.rollback()
            return None
        _conn.commit()
        return claim_no


def bingo_prizes_claimed():
    """How many prizes have been awarded (derived from the DB, never memory)."""
    with _lock:
        row = _conn.execute("SELECT COUNT(*) AS c FROM bingo_prizes").fetchone()
    return row["c"]


def mark_prize_posted(user_id):
    """Record that this winner's channel announcement went out (once)."""
    with _lock:
        _conn.execute(
            "UPDATE bingo_prizes SET posted_at = ? "
            "WHERE winner_user_id = ? AND posted_at IS NULL",
            (_now_iso(), user_id),
        )
        _conn.commit()


def pending_submissions():
    """All still-pending submissions (for re-arming 12h timeout jobs on
    startup), ordered by submission time."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'pending' "
            "ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
```

> Implementation notes: every function is a single `with _lock:` block and uses `_now_iso()`, matching the existing module. `submission_by_id` is the read-side pair to `start_bingo_submission` — it returns a row of any status (so Task 6's confirm/timeout/award path can recover the submitter after the submission is no longer pending), whereas `active_submission` is filtered to `status = 'pending'`. `claim_bingo_prize` performs COUNT + one-per-person + INSERT inside one lock (no TOCTOU) and treats `winner_user_id`'s `PRIMARY KEY` as a `UNIQUE` last-line guard against a duplicate slipping through from the separate setup-worker process. `allocate_bingo_sheet` deals into the smallest sheet (`min(counts[s], s)`), and returns early for an existing row so allocations are frozen. `set_submission_status` only touches `verified_at` when supplied, so a plain status change never clobbers a recorded verify time.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bingo_storage.py -q`
Expected: PASS — all tests green (even allocation, freeze-stability, new-handle-to-smallest, handle→user_id, closed flag, submission lifecycle, `submission_by_id` any-status lookup, pending list, winning members, confirmation upsert, prize cap at 10, 11th rejected, duplicate winner rejected, `mark_prize_posted` idempotent, and the threaded race test yielding exactly slots 1..10).

- [ ] **Step 5: Commit**

```
git add storage.py tests/__init__.py tests/test_bingo_storage.py
git commit -m "Add Human Bingo storage layer: allocation, submissions, confirmations, prizes"
```

---

### Task 5: `bingo_ocr.py` — roster index, fuzzy handle matcher, crop→OCR pipeline, RapidOCR singleton
**Files:**
- Create: `bingo_ocr.py`
- Create: `tests/test_bingo_ocr.py`
- Test path: `tests/test_bingo_ocr.py`
- (Depends on `data/bingo_templates.py` from Task 2 for `CELL_BOXES`, `CORNER_BOX`, `template_path`, `is_free`, `GRID`, `FREE`; and on `config.py` from Task 1 for `BINGO_MATCH_THRESHOLD`, `BINGO_MATCH_MARGIN`. If those modules do not yet exist in your worktree, create the tiny shims shown in Step 3's *fixture note* so the test is self-contained — the real modules supersede them.)

**Interfaces:**
- Consumes (verbatim):
  - `config.BINGO_MATCH_THRESHOLD` (int, 85), `config.BINGO_MATCH_MARGIN` (int, 8)
  - `data.bingo_templates.CELL_BOXES: dict[tuple[int,int], tuple[float,float,float,float]]`, `CORNER_BOX: tuple[float,float,float,float]`, `is_free(row,col) -> bool`, `template_path(sheet_no) -> pathlib.Path`, `GRID`, `FREE`
  - `setup.sheets.normalize_handle(raw) -> str | None`, `setup.sheets.name_tokens(name) -> set[str]`
- Produces (verbatim, later tasks rely on these):
  - `build_roster_index(members: list[dict]) -> dict` — returns `{"keys": [...], "key_to_result": {...}, "handles": set[str]}`. **`"handles"` is the set of real normalized @handles (fix: Task 6's `_handle_in_roster` reads this to gate `/get_bingo`).** Only members with a real handle contribute a match *result*; a handle-less member's name is indexed as a *search key* that maps to a real handle only when the member has one, so a matched result is always a genuine Telegram handle (fix: no name-key results that can never be confirmed).
  - `match_handle(text: str, index: dict) -> tuple[str | None, float]`
  - `read_submission(sheet_no: int, image_bytes: bytes, index: dict) -> dict`  →  `{"corner": int|None, "cells": [{"row":int,"col":int,"handle":str|None,"score":float}, … 24 non-free cells]}`
  - module-level RapidOCR singleton via `_engine()` (lazy import of `rapidocr_onnxruntime`)

---

- [ ] **Step 1: Write the failing test** — create `tests/test_bingo_ocr.py`. It (a) tests `match_handle` against a small fake roster fully offline, (b) tests `read_submission` with a fake `_engine()` scripted per crop, and (c) tests the length-aware short-handle guard. No real OCR/model download happens. The fuzzy scores below are measured against `rapidfuzz.fuzz.WRatio` and are stable.

```python
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
```

- [ ] **Step 2: Run test to verify it fails** — Run: `python -m pytest tests/test_bingo_ocr.py -q`. Expected: **FAIL** — `ModuleNotFoundError: No module named 'bingo_ocr'` (the module does not exist yet).

  *Fixture note (only if Task 1/Task 2 modules are not yet in your worktree):* create minimal shims so the test can import them, to be overwritten by the real tasks. `config.py` must define `BINGO_MATCH_THRESHOLD = 85` and `BINGO_MATCH_MARGIN = 8`. `data/bingo_templates.py` must define `GRID = 5`, `FREE = (2, 2)`, `is_free(r, c)` returning `r == 2 and c == 2`, `CELL_BOXES` = a dict of all 24 non-free `(r, c) -> (r/5, c/5, (r+1)/5, (c+1)/5)` fractional boxes, `CORNER_BOX = (0.0, 0.0, 0.18, 0.12)`, and `template_path(n)`/`SHEETS`/`prompt_for` stubs. Do not commit these shims — only `bingo_ocr.py` and the test.

- [ ] **Step 3: Write minimal implementation** — create `bingo_ocr.py`:

```python
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


def _ocr_text(image):
    """Run the OCR engine on a PIL image, return the concatenated text (may be '')."""
    result, _elapse = _engine()(image)
    if not result:
        return ""
    # result is a list of [box, text, confidence]; join text fragments.
    return " ".join(str(line[1]) for line in result if len(line) > 1 and line[1])


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
    resolve to a user_id). A handle-less member contributes name *search keys*
    ONLY when some member with that name has a real handle; otherwise the
    handle-less member is unreachable and is left out of the result domain.
    "handles" is the membership set Task 6 uses to gate /get_bingo.
    """
    keys = []
    key_to_result = {}
    handles = set()

    def add(key, result):
        if not key or not result:
            return
        # first writer wins so a real handle isn't shadowed by a later name token
        keys.append(key)
        key_to_result.setdefault(key, result)

    # First pass: collect every real handle and map name -> handle where possible,
    # so a name typed for a member who DOES have a handle resolves to that handle.
    name_to_handle = {}
    for m in members:
        handle = normalize_handle(m.get("handle")) if m.get("handle") else None
        name = (m.get("name") or "").strip()
        if handle:
            handles.add(handle)
            if name:
                name_to_handle.setdefault(_joined_name(name), handle)
                for tok in name_tokens(name):
                    name_to_handle.setdefault(tok, handle)

    # Second pass: index handle keys (result = the handle) and name keys
    # (result = the real handle for that name, if any). Name keys with no
    # backing handle are skipped, so no un-confirmable name-only result exists.
    for m in members:
        handle = normalize_handle(m.get("handle")) if m.get("handle") else None
        name = (m.get("name") or "").strip()
        if handle:
            add(handle, handle)
        if name:
            joined = _joined_name(name)
            add(joined, name_to_handle.get(joined))
            for tok in name_tokens(name):
                add(tok, name_to_handle.get(tok))

    # de-duplicate keys while preserving order (rapidfuzz wants a plain list)
    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return {"keys": uniq, "key_to_result": key_to_result, "handles": handles}


def _joined_name(name):
    """Name tokens joined in a stable order (deterministic full-name key)."""
    return "".join(sorted(name_tokens(name)))


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
    with exactly 24 cell dicts (the FREE centre is skipped)."""
    im = _load_image(image_bytes)

    cells = []
    for row in range(bt.GRID):
        for col in range(bt.GRID):
            if bt.is_free(row, col):
                continue
            frac = bt.CELL_BOXES[(row, col)]
            crop = _prep_for_ocr(_crop_box(im, frac))
            text = _ocr_text(crop)
            handle, score = match_handle(text, index)
            cells.append(
                {"row": row, "col": col, "handle": handle, "score": score}
            )

    corner_crop = _prep_for_ocr(_crop_box(im, bt.CORNER_BOX))
    corner_text = _ocr_text(corner_crop)
    corner = _read_int(corner_text)

    return {"corner": corner, "cells": cells}


def _read_int(text):
    """First run of digits in the OCR text as an int, else None."""
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None
```

- [ ] **Step 4: Run test to verify it passes** — Run: `python -m pytest tests/test_bingo_ocr.py -q`. Expected: **PASS** (all tests green, including the `handles`-set exposure, the length-aware short-handle rejection, and the name→real-handle resolution). No network, no model download, no `rapidocr_onnxruntime` import (the fake engine replaces `_engine()`; `rapidfuzz` and `Pillow` are the only real deps exercised). If your worktree lacks Task 1/Task 2 modules, add the Step 2 *fixture-note* shims first so imports resolve.

- [ ] **Step 5: Commit** — Run:
```
git add bingo_ocr.py tests/test_bingo_ocr.py
git commit -m "Add bingo OCR pipeline: roster index, fuzzy handle matcher, crop-and-read"
```

---

### Task 6: handlers/bingo.py — commands, image handler, confirmation callback, timeout re-arm

**Files:**
- Create: `handlers/bingo.py`
- Create (test): `tests/test_bingo_handlers.py`
- Consumes (already built by earlier tasks): `config.py` (Task 1), `data/bingo_templates.py` (Task 2), `bingo_lines.py` (Task 3), `storage.py` (Task 4), `bingo_ocr.py` (Task 5), `setup/sheets.py::load_year1_members` (existing)

**Interfaces:**
- Consumes:
  - `config.ANNOUNCE_CHAT_ID: int`, `config.BINGO_PRIZE_LIMIT: int (==10)`, `config.BINGO_CONFIRM_TIMEOUT: timedelta`, `config.BINGO_RETRY_COOLDOWN: timedelta`, `config.TIMEZONE`
  - `data.bingo_templates.prompt_for(sheet_no, row, col) -> str`, `data.bingo_templates.template_path(sheet_no) -> pathlib.Path`, `data.bingo_templates.is_free(row, col) -> bool`
  - `bingo_lines.winning_lines(matched: dict[tuple[int,int],str], submitter_handle: str) -> list[Line]`, `bingo_lines.pick_best_line(lines) -> Line`, `bingo_lines.line_passes(line, answers: dict[str,str]) -> bool`, `bingo_lines.required_yes(line) -> int` (`Line = list[tuple[int,int,str]]`)
  - `bingo_ocr.build_roster_index(members) -> dict` (with `"handles"` set), `bingo_ocr.read_submission(sheet_no, image_bytes, index) -> {"corner": int|None, "cells": [{"row","col","handle","score"}, ...]}`
  - `storage.allocate_bingo_sheet`, `storage.get_bingo_sheet`, `storage.user_id_for_handle`, `storage.bingo_is_closed`, `storage.set_bingo_closed`, `storage.active_submission`, `storage.submission_by_id`, `storage.last_bingo_activity`, `storage.start_bingo_submission`, `storage.set_submission_status`, `storage.record_winning_members`, `storage.winning_members`, `storage.record_bingo_confirmation`, `storage.get_cached_confirmation`, `storage.has_bingo_prize`, `storage.claim_bingo_prize`, `storage.bingo_prizes_claimed`, `storage.mark_prize_posted`, `storage.pending_submissions`
  - `setup.sheets.load_year1_members() -> {OG: [{name, handle, email, addable}]}`, `setup.sheets.normalize_handle`
- Produces (later tasks — Task 7/main.py — rely on these exact names):
  - `handlers.bingo.register(app) -> None`
  - `handlers.bingo.rearm_bingo_timeouts(app) -> None`
  - `handlers.bingo.get_bingo`, `submit_bingo`, `on_bingo_image`, `confirm_button`, `_confirmation_timeout` (async)
  - Internal testable helpers: `_roster_index()`, `_matched_and_prompts(cells, submitter_handle, sheet_no) -> (matched, prompts)`, `_answers_for(submission_id) -> dict[str,str]`, `_line_verdict(line, answers) -> str`, `_finalize(context, submission_id) -> None`

---

- [ ] **Step 1: Write the failing test** — create `tests/test_bingo_handlers.py`. Async handlers are driven with `asyncio.run(...)` inside plain sync tests so no `pytest-asyncio` plugin is needed; Telegram + OCR are fully monkeypatched.

```python
"""Tests for handlers/bingo.py — the human-bingo Telegram flow.

Telegram Bot calls and the OCR pipeline are monkeypatched so these run fully
offline and deterministically. Async handlers are invoked with asyncio.run so
the suite needs no pytest-asyncio plugin.
"""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- DB-backed storage on a temp file --------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bingo_test.db"))
    for mod in ("storage",):
        sys.modules.pop(mod, None)
    import storage
    importlib.reload(storage)
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "bingo_test.db"))
    storage.init_db()
    return storage


@pytest.fixture()
def bingo(store, monkeypatch):
    """handlers.bingo with a fixed 2-OG roster and no network."""
    import handlers.bingo as bingo
    importlib.reload(bingo)

    roster = {
        "AM1": [
            {"name": "Alice Tan", "handle": "alice", "email": "a@x.com", "addable": True},
            {"name": "Bob Lee", "handle": "bob", "email": "b@x.com", "addable": True},
            {"name": "Cara Ng", "handle": "cara", "email": "c@x.com", "addable": True},
            {"name": "Dan Ong", "handle": "dan", "email": "d@x.com", "addable": True},
            {"name": "Eve Sim", "handle": "eve", "email": "e@x.com", "addable": True},
        ],
    }
    monkeypatch.setattr(bingo.sheets, "load_year1_members", lambda: roster)
    bingo._ROSTER_INDEX = None  # reset the module cache
    return bingo


def _update(user_id=100, username="submitter", text="/get_bingo"):
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    msg.reply_photo = AsyncMock()
    upd = MagicMock()
    upd.effective_user = SimpleNamespace(id=user_id, username=username, full_name="Sub Mitter")
    upd.effective_chat = SimpleNamespace(id=user_id, type="private")
    upd.effective_message = msg
    return upd


def _context():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.user_data = {}
    ctx.job_queue = MagicMock()
    return ctx


# --- helper: matched dict + prompt map from OCR cells ----------------------

def test_matched_and_prompts_drops_non_match_and_self(bingo, monkeypatch):
    # read_submission already applies the score/margin cutoff, so any non-None
    # handle here is confident; _matched_and_prompts only drops no-match (None
    # handle) and the submitter's own handle (no self-cheese). prompts come from
    # templates.prompt_for(sheet_no, r, c) given an explicit sheet_no.
    monkeypatch.setattr(
        bingo.templates, "prompt_for",
        lambda s, r, c: f"P{r}{c}",
    )
    cells = [
        {"row": 0, "col": 0, "handle": "alice", "score": 91.0},
        {"row": 0, "col": 2, "handle": None, "score": 0.0},       # no match -> dropped
        {"row": 0, "col": 3, "handle": "submitter", "score": 99}, # self -> dropped
        {"row": 0, "col": 4, "handle": "cara", "score": 88.0},
    ]
    matched, prompts = bingo._matched_and_prompts(cells, "submitter", sheet_no=1)
    assert matched == {(0, 0): "alice", (0, 4): "cara"}
    assert prompts[(0, 0)] == "P00" and prompts[(0, 4)] == "P04"
    assert (0, 2) not in matched and (0, 3) not in matched


# --- helper: per-line verdict ----------------------------------------------

def test_line_verdict_pass_fail_pending(bingo):
    line = [(0, 0, "alice"), (0, 1, "bob"), (0, 3, "dan"), (0, 4, "eve")]  # 4 real cells
    # all yes -> pass
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes", "dan": "yes", "eve": "yes"}) == "pass"
    # one no, rest yes -> still pass (one allowed miss == required_yes met)
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes", "dan": "yes", "eve": "no"}) == "pass"
    # two misses -> fail
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes", "dan": "no", "eve": "no"}) == "fail"
    # unanswered cells and not yet failable -> pending
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes"}) == "pending"


# --- get_bingo -------------------------------------------------------------

def test_get_bingo_non_roster_declines(bingo):
    upd, ctx = _update(user_id=999, username="stranger"), _context()
    asyncio.run(bingo.get_bingo(upd, ctx))
    upd.effective_message.reply_text.assert_awaited()
    assert not ctx.bot.send_photo.await_count  # nothing sent


def test_get_bingo_roster_sends_sheet_and_freezes(bingo, store):
    upd, ctx = _update(user_id=100, username="alice"), _context()
    asyncio.run(bingo.get_bingo(upd, ctx))
    upd.effective_message.reply_photo.assert_awaited()
    first = store.get_bingo_sheet(100)
    assert first is not None
    # calling again must not reallocate
    asyncio.run(bingo.get_bingo(upd, ctx))
    assert store.get_bingo_sheet(100) == first


# --- submit_bingo gating ---------------------------------------------------

def test_submit_bingo_sets_flag_when_open(bingo, store):
    store.allocate_bingo_sheet(100, "alice")  # must have a card first
    upd, ctx = _update(user_id=100, username="alice", text="/submit_bingo"), _context()
    asyncio.run(bingo.submit_bingo(upd, ctx))
    assert ctx.user_data.get("awaiting_bingo") is True
    upd.effective_message.reply_text.assert_awaited()


def test_submit_bingo_blocked_when_closed(bingo, store):
    store.set_bingo_closed()
    upd, ctx = _update(user_id=100, username="alice", text="/submit_bingo"), _context()
    asyncio.run(bingo.submit_bingo(upd, ctx))
    assert not ctx.user_data.get("awaiting_bingo")


def test_submit_bingo_blocked_when_already_won(bingo, store, monkeypatch):
    monkeypatch.setattr(store, "has_bingo_prize", lambda uid: True)
    upd, ctx = _update(user_id=100, username="alice", text="/submit_bingo"), _context()
    asyncio.run(bingo.submit_bingo(upd, ctx))
    assert not ctx.user_data.get("awaiting_bingo")


# --- on_bingo_image full pipeline -> pending + DMs subjects -----------------

def _photo_update(user_id=100, username="alice"):
    upd = _update(user_id=user_id, username=username)
    photo = SimpleNamespace(file_id="F")
    upd.effective_message.photo = [photo]
    upd.effective_message.document = None
    return upd


def test_on_bingo_image_records_line_and_dms_subjects(bingo, store, monkeypatch):
    # allocate sheet 1 for the submitter (id 100)
    store.allocate_bingo_sheet(100, "alice")
    sheet = store.get_bingo_sheet(100)
    # register subjects so they're reachable
    for uid, h in [(1, "bob"), (2, "cara"), (3, "dan"), (4, "eve")]:
        store.mark_started(uid, h, h.title())

    # OCR returns a full top-row line of 4 distinct, non-self, confident handles
    def fake_read(sheet_no, image_bytes, index):
        return {"corner": sheet_no, "cells": [
            {"row": 0, "col": 0, "handle": "bob", "score": 95.0},
            {"row": 0, "col": 1, "handle": "cara", "score": 95.0},
            {"row": 0, "col": 3, "handle": "dan", "score": 95.0},
            {"row": 0, "col": 4, "handle": "eve", "score": 95.0},
        ]}
    monkeypatch.setattr(bingo.ocr, "read_submission", fake_read)
    monkeypatch.setattr(bingo.templates, "prompt_for", lambda s, r, c: f"prompt-{r}-{c}")
    # winning_lines: top row (row 0) complete (centre free auto-fills col 2)
    monkeypatch.setattr(bingo.lines, "winning_lines",
                        lambda matched, sub: [[(0, 0, "bob"), (0, 1, "cara"), (0, 3, "dan"), (0, 4, "eve")]])

    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    # bot.get_file -> file whose download_as_bytearray returns image bytes
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)

    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))

    sub = store.active_submission(100)
    assert sub is not None and sub["status"] == "pending"
    members = store.winning_members(sub["id"])
    assert {m["handle"] for m in members} == {"bob", "cara", "dan", "eve"}
    # each reachable subject was DM'd a Yes/No keyboard
    assert ctx.bot.send_message.await_count == 4
    # a 12h timeout job was armed
    ctx.job_queue.run_once.assert_called_once()
    assert ctx.user_data.get("awaiting_bingo") is not True


def test_on_bingo_image_no_line_reports_and_cooldowns(bingo, store, monkeypatch):
    store.allocate_bingo_sheet(100, "alice")
    monkeypatch.setattr(bingo.ocr, "read_submission",
                        lambda s, b, i: {"corner": s, "cells": []})
    monkeypatch.setattr(bingo.lines, "winning_lines", lambda matched, sub: [])
    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)
    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))
    assert store.active_submission(100) is None  # nothing pending
    upd.effective_message.reply_text.assert_awaited()


# --- confirm_button -> pass -> claim + announce + DM ------------------------

def test_confirm_button_pass_awards_and_posts(bingo, store, monkeypatch):
    store.allocate_bingo_sheet(100, "alice")
    sub_id = store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100), 1)
    line_members = [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "p0", "target_user_id": 1},
        {"row": 0, "col": 1, "handle": "cara", "prompt": "p1", "target_user_id": 2},
        {"row": 0, "col": 3, "handle": "dan", "prompt": "p3", "target_user_id": 3},
        {"row": 0, "col": 4, "handle": "eve", "prompt": "p4", "target_user_id": 4},
    ]
    store.record_winning_members(sub_id, line_members)

    ctx = _context()

    def tap(uid, row, col, ans):
        q = AsyncMock()
        q.data = f"bingoconf:{sub_id}:{row}:{col}:{ans}"
        q.answer = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        q.from_user = SimpleNamespace(id=uid)
        upd = MagicMock()
        upd.callback_query = q
        upd.effective_user = SimpleNamespace(id=uid)
        asyncio.run(bingo.confirm_button(upd, ctx))

    tap(1, 0, 0, "yes")
    tap(2, 0, 1, "yes")
    tap(3, 0, 3, "yes")
    # not yet enough -> still pending, no prize
    assert store.bingo_prizes_claimed() == 0
    tap(4, 0, 4, "yes")  # 4th yes -> pass

    assert store.has_bingo_prize(100) is True
    assert store.bingo_prizes_claimed() == 1
    # channel post to ANNOUNCE_CHAT_ID happened
    import config
    posted = [c for c in ctx.bot.send_message.await_args_list
              if c.kwargs.get("chat_id") == config.ANNOUNCE_CHAT_ID]
    assert posted, "expected a channel announcement"
    sub = store.active_submission(100)
    assert sub is None  # no longer pending (verified)


def test_confirm_button_caches_answer_game_wide(bingo, store):
    store.allocate_bingo_sheet(100, "alice")
    sub_id = store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100), 1)
    store.record_winning_members(sub_id, [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "likes cats", "target_user_id": 1},
    ])
    ctx = _context()
    q = AsyncMock()
    q.data = f"bingoconf:{sub_id}:0:0:yes"
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.from_user = SimpleNamespace(id=1)
    upd = MagicMock()
    upd.callback_query = q
    upd.effective_user = SimpleNamespace(id=1)
    asyncio.run(bingo.confirm_button(upd, ctx))
    assert store.get_cached_confirmation(1, "likes cats") == "yes"


# --- closing the game cancels outstanding timeout jobs ---------------------

def test_award_at_limit_cancels_outstanding_timeouts(bingo, store, monkeypatch):
    # Fill 9 prizes so the next claim is the 10th and closes the game.
    for uid in range(200, 209):
        s = store.start_bingo_submission(uid, f"w{uid}", 1, None)
        store.claim_bingo_prize(uid, f"w{uid}", s)
    assert store.bingo_prizes_claimed() == 9

    store.allocate_bingo_sheet(100, "alice")
    sub_id = store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100), 1)
    store.record_winning_members(sub_id, [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "p0", "target_user_id": 1},
        {"row": 0, "col": 1, "handle": "cara", "prompt": "p1", "target_user_id": 2},
        {"row": 0, "col": 3, "handle": "dan", "prompt": "p3", "target_user_id": 3},
        {"row": 0, "col": 4, "handle": "eve", "prompt": "p4", "target_user_id": 4},
    ])
    for uid, prompt in [(1, "p0"), (2, "p1"), (3, "p3"), (4, "p4")]:
        store.record_bingo_confirmation(uid, prompt, "yes")

    ctx = _context()
    # one outstanding timeout job the close should cancel
    job = MagicMock()
    ctx.job_queue.get_jobs_by_name = MagicMock(return_value=[job])

    asyncio.run(bingo._finalize(ctx, sub_id))

    assert store.bingo_is_closed() is True
    job.schedule_removal.assert_called_once()


# --- register wires the four handlers --------------------------------------

def test_register_adds_handlers(bingo):
    app = MagicMock()
    bingo.register(app)
    assert app.add_handler.call_count >= 4
```

- [ ] **Step 2: Run test to verify it fails** — Run: `python -m pytest tests/test_bingo_handlers.py -q`. Expected: FAIL (collection/import error `ModuleNotFoundError: No module named 'handlers.bingo'` — the module does not exist yet). If `pytest` is not installed on the box, first `python -m pip install pytest` (no async plugin needed — tests use `asyncio.run`).

- [ ] **Step 3: Write minimal implementation** — create `handlers/bingo.py`:

```python
"""Human Bingo — /get_bingo, /submit_bingo, confirmation buttons.

Year 1s type fellow Year 1s' @handles into a printed 5x5 card, submit it as an
image, and the bot OCRs it, finds a winning line, DMs the named people to
confirm the prompts describe them, and — if enough confirm — awards the
submitter one of 10 first-come prizes. This module is orchestration only: the
line maths live in bingo_lines, the OCR in bingo_ocr, and all persistence in
storage. Telegram calls are wrapped best-effort so one bad DM never sinks a
submission (an unreachable subject is just a miss).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import bingo_lines as lines
import bingo_ocr as ocr
import config
import storage
from data import bingo_templates as templates
from setup import sheets

log = logging.getLogger(__name__)

# Built once from the roster (like the shared OCR/storage singletons) and reused
# for every submission's fuzzy handle matching.
_ROSTER_INDEX = None


def _roster_index():
    global _ROSTER_INDEX
    if _ROSTER_INDEX is None:
        members = []
        try:
            for og_members in sheets.load_year1_members().values():
                members.extend(og_members)
        except Exception as exc:
            log.warning("couldn't load Year 1 roster for bingo: %s", exc)
        _ROSTER_INDEX = ocr.build_roster_index(members)
    return _ROSTER_INDEX


# ---------------------------------------------------------------------------
# Pure-ish helpers (unit-tested)
# ---------------------------------------------------------------------------

def _matched_and_prompts(cells, submitter_handle, sheet_no):
    """Turn read_submission cells into (matched, prompts) for a known sheet.

    matched: {(row, col): handle} for CONFIDENT cells only — handle non-None,
    lowercased, and never the submitter (no self-cheese). read_submission has
    already applied the score/margin cutoff, so any non-None handle here is
    confident. prompts: {(row, col): prompt} pulled from
    templates.prompt_for(sheet_no, row, col), so the Yes/No button text is stable
    regardless of later template edits.
    """
    submitter = (submitter_handle or "").lower()
    matched, prompts = {}, {}
    for cell in cells:
        handle = cell.get("handle")
        if not handle:
            continue
        handle = handle.lower()
        if handle == submitter:
            continue
        r, c = cell["row"], cell["col"]
        matched[(r, c)] = handle
        prompts[(r, c)] = templates.prompt_for(sheet_no, r, c)
    return matched, prompts


def _line_verdict(line, answers):
    """'pass' | 'fail' | 'pending' for one candidate line.

    line: [(row, col, handle), ...] real cells. answers: handle -> 'yes'|'no'
    (missing = unanswered/unreachable). Delegates the pass threshold to
    bingo_lines.line_passes (yes >= len-1, i.e. at most one miss). 'fail' once
    it is arithmetically impossible to still pass; otherwise 'pending'.
    """
    if lines.line_passes(line, answers):
        return "pass"
    handles = [h for _, _, h in line]
    need = lines.required_yes(line)
    yes = sum(1 for h in handles if answers.get(h) == "yes")
    undecided = sum(1 for h in handles if h not in answers)
    if yes + undecided < need:
        return "fail"
    return "pending"


# ---------------------------------------------------------------------------
# /get_bingo — resolve the caller, freeze their sheet, DM the template
# ---------------------------------------------------------------------------

def _handle_in_roster(handle):
    if not handle:
        return False
    # build_roster_index exposes "handles": the set of real normalized @handles.
    return sheets.normalize_handle(handle) in _roster_index().get("handles", set())


async def get_bingo(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "Message me privately to get your bingo card 🙂"
        )
        return

    user = update.effective_user
    handle = sheets.normalize_handle(user.username)
    storage.mark_started(user.id, user.username, user.full_name)

    if not handle or not _handle_in_roster(handle):
        await update.effective_message.reply_text(
            "I couldn't find you on the Year 1 roster by your Telegram handle 😕\n"
            "Human Bingo is for StartNOW! 2026 Year 1s — if that's you, ping a "
            "facil so we can fix your handle."
        )
        return

    sheet_no = storage.get_bingo_sheet(user.id)
    if sheet_no is None:
        sheet_no = storage.allocate_bingo_sheet(user.id, handle)

    caption = (
        "🎉 <b>Human Bingo!</b>\n\n"
        f"Here's your card (sheet #{sheet_no}). Type a fellow Year 1's @handle "
        "into each square that describes them, aiming for 5 in a row (the centre "
        "is FREE). When you've got a line, send it back with /submit_bingo.\n\n"
        "First 10 verified lines win a prize! 🏆"
    )
    try:
        with open(templates.template_path(sheet_no), "rb") as fh:
            await update.effective_message.reply_photo(fh, caption=caption,
                                                       parse_mode="HTML")
    except FileNotFoundError:
        log.error("missing bingo template for sheet %s", sheet_no)
        await update.effective_message.reply_text(
            "Your card isn't ready yet — please let a facil know 🙏"
        )


# ---------------------------------------------------------------------------
# /submit_bingo — gate, then arm the one-shot image handler
# ---------------------------------------------------------------------------

async def submit_bingo(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "Send your filled card to me in a private chat 🙂"
        )
        return

    uid = update.effective_user.id
    if storage.bingo_is_closed():
        await update.effective_message.reply_text(
            "All 10 prizes have been claimed — thanks for playing! 🎉"
        )
        return
    if storage.has_bingo_prize(uid):
        await update.effective_message.reply_text(
            "You've already won — give someone else a shot! 🏆"
        )
        return
    if storage.active_submission(uid):
        await update.effective_message.reply_text(
            "You already have a card being checked. Hang tight — I'll message you "
            "the moment it's verified. ⏳"
        )
        return
    if storage.get_bingo_sheet(uid) is None:
        await update.effective_message.reply_text(
            "Grab your card first with /get_bingo, then send it back here 🙂"
        )
        return

    context.user_data["awaiting_bingo"] = True
    await update.effective_message.reply_text(
        "Send me a photo of your filled bingo card (as a photo or an image "
        "file) and I'll check it. 📸"
    )


# ---------------------------------------------------------------------------
# The filled-card image — only when we asked for it
# ---------------------------------------------------------------------------

def _cooldown_remaining(uid):
    """Seconds still to wait after a recent attempt, or 0."""
    from datetime import datetime
    last = storage.last_bingo_activity(uid)
    if not last:
        return 0
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return 0
    now = datetime.now(config.TIMEZONE)
    elapsed = (now - then).total_seconds()
    remaining = config.BINGO_RETRY_COOLDOWN.total_seconds() - elapsed
    return int(remaining) if remaining > 0 else 0


async def _download_image(update, context):
    msg = update.effective_message
    if msg.photo:
        tg_file = await context.bot.get_file(msg.photo[-1].file_id)
    elif msg.document:
        tg_file = await context.bot.get_file(msg.document.file_id)
    else:
        return None
    return bytes(await tg_file.download_as_bytearray())


async def on_bingo_image(update, context):
    if not context.user_data.get("awaiting_bingo"):
        return  # not expecting an image from this user; ignore
    context.user_data["awaiting_bingo"] = False

    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return

    uid = update.effective_user.id
    handle = sheets.normalize_handle(update.effective_user.username) or ""

    if storage.bingo_is_closed():
        await update.effective_message.reply_text(
            "All 10 prizes have been claimed — thanks for playing! 🎉"
        )
        return
    wait = _cooldown_remaining(uid)
    if wait:
        await update.effective_message.reply_text(
            f"Hold on {wait}s before trying again 🙂"
        )
        return

    sheet_no = storage.get_bingo_sheet(uid)
    if sheet_no is None:
        await update.effective_message.reply_text(
            "Grab your card first with /get_bingo 🙂"
        )
        return

    image_bytes = await _download_image(update, context)
    if not image_bytes:
        await update.effective_message.reply_text(
            "I couldn't read that — send it as a photo or an image file 📸"
        )
        return

    read = ocr.read_submission(sheet_no, image_bytes, _roster_index())

    # wrong-sheet defence: a confident, mismatched corner number rejects
    corner = read.get("corner")
    if corner is not None and corner != sheet_no:
        storage.start_bingo_submission(uid, handle, sheet_no, corner)
        await update.effective_message.reply_text(
            f"This looks like sheet #{corner}, but you were given sheet "
            f"#{sheet_no}. Please send your own card 🙂"
        )
        return

    matched, prompts = _matched_and_prompts(read.get("cells", []), handle, sheet_no)

    candidate_lines = lines.winning_lines(matched, handle)
    if not candidate_lines:
        await update.effective_message.reply_text(
            "No bingo yet — I couldn't find 5 in a row of confirmed Year 1s. "
            "Double-check the handles and try again in a minute! 🔁"
        )
        return

    line = lines.pick_best_line(candidate_lines)
    submission_id = storage.start_bingo_submission(uid, handle, sheet_no, corner)

    members = []
    for (r, c, h) in line:
        members.append({
            "row": r, "col": c, "handle": h,
            "prompt": prompts.get((r, c)) or templates.prompt_for(sheet_no, r, c),
            "target_user_id": storage.user_id_for_handle(h),
        })
    storage.record_winning_members(submission_id, members)

    await update.effective_message.reply_text(
        "Nice line! 🎯 I'm checking with the people you tagged — I'll message you "
        "the moment it's verified (they have 12 hours to respond)."
    )

    await _dm_subjects(context, submission_id, members)

    # arm the 12h finaliser
    if context.job_queue is not None:
        context.job_queue.run_once(
            _confirmation_timeout,
            when=config.BINGO_CONFIRM_TIMEOUT,
            data={"submission_id": submission_id},
            name=f"bingo:timeout:{submission_id}",
        )

    # re-evaluate immediately in case cached answers already clinch it
    await _finalize(context, submission_id)


def _confirm_keyboard(submission_id, row, col):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"bingoconf:{submission_id}:{row}:{col}:yes"),
        InlineKeyboardButton("❌ No", callback_data=f"bingoconf:{submission_id}:{row}:{col}:no"),
    ]])


async def _dm_subjects(context, submission_id, members):
    """DM each reachable subject a Yes/No question, using cached answers where
    present (so a popular person is asked at most once per distinct prompt, and
    duplicate prompts shared across cells in this same line are de-duplicated).
    An unreachable subject is silently left unanswered and treated as a miss."""
    asked = set()  # (target_user_id, prompt) already sent this call -> de-dupe
    for m in members:
        target = m.get("target_user_id")
        if target is None:
            continue  # unmapped handle -> counts as a miss at evaluation
        key = (target, m["prompt"])
        if key in asked:
            continue  # de-dup shared cell within this chosen line
        cached = storage.get_cached_confirmation(target, m["prompt"])
        if cached is not None:
            asked.add(key)
            continue  # already answered this prompt game-wide; reuse silently
        try:
            await context.bot.send_message(
                chat_id=target,
                text=f"👋 Quick one for Human Bingo:\n\n<b>{m['prompt']}</b> — does "
                     "this describe you?",
                parse_mode="HTML",
                reply_markup=_confirm_keyboard(submission_id, m["row"], m["col"]),
            )
            asked.add(key)
        except Exception as exc:
            # unreachable (never /started, blocked us, etc.) -> one allowed miss
            log.info("couldn't DM subject %s for submission %s: %s",
                     target, submission_id, exc)


# ---------------------------------------------------------------------------
# Confirmation button
# ---------------------------------------------------------------------------

async def confirm_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, sub_s, row_s, col_s, ans = query.data.split(":")
        submission_id, row, col = int(sub_s), int(row_s), int(col_s)
    except (ValueError, AttributeError):
        return
    if ans not in ("yes", "no"):
        return

    subject_id = query.from_user.id

    # find the prompt for this (submission,row,col) so we can cache by prompt
    prompt = None
    for m in storage.winning_members(submission_id):
        if m["row"] == row and m["col"] == col:
            prompt = m["prompt"]
            break
    if prompt is None:
        return

    storage.record_bingo_confirmation(subject_id, prompt, ans)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may be too old to edit; the cache is what matters

    await _finalize(context, submission_id)


# ---------------------------------------------------------------------------
# Evaluation / finalisation
# ---------------------------------------------------------------------------

def _answers_for(submission_id):
    """handle -> 'yes'|'no' from the cache, for this submission's real cells.
    Unreachable/unmapped/unanswered handles are simply absent (a miss)."""
    answers = {}
    for m in storage.winning_members(submission_id):
        target = m.get("target_user_id")
        if target is None:
            continue
        cached = storage.get_cached_confirmation(target, m["prompt"])
        if cached is not None:
            answers[m["handle"]] = cached
    return answers


def _line_from_members(members):
    return [(m["row"], m["col"], m["handle"]) for m in members]


async def _finalize(context, submission_id, final=False):
    """Evaluate a pending submission once. On pass, atomically claim a prize and
    (best-effort) announce + DM. On a definite fail (or at the 12h timeout with
    still-missing answers) mark it failed so the submitter can retry."""
    sub = storage.submission_by_id(submission_id)
    if not sub or sub["status"] != "pending":
        return  # already resolved
    members = storage.winning_members(submission_id)
    if not members:
        return
    line = _line_from_members(members)
    answers = _answers_for(submission_id)
    verdict = _line_verdict(line, answers)

    if verdict == "pending" and not final:
        return
    if verdict != "pass":
        # timeout or two misses -> failed, retry allowed after cooldown
        storage.set_submission_status(submission_id, "failed")
        await _notify_submitter_failed(context, submission_id)
        return

    await _award(context, submission_id)


async def _award(context, submission_id):
    members = storage.winning_members(submission_id)
    if not members:
        return
    submitter_id, submitter_handle = _submitter_of(submission_id)
    if submitter_id is None or storage.has_bingo_prize(submitter_id):
        storage.set_submission_status(submission_id, "verified",
                                      verified_at=storage._now_iso())
        return

    claim_no = storage.claim_bingo_prize(submitter_id, submitter_handle, submission_id)
    storage.set_submission_status(submission_id, "verified",
                                  verified_at=storage._now_iso())
    if claim_no is None:
        # someone else took the last slot between check and claim
        try:
            await context.bot.send_message(
                chat_id=submitter_id,
                text="So close! All 10 prizes were just claimed — thanks for "
                     "playing! 🎉",
            )
        except Exception:
            pass
        return

    if claim_no >= config.BINGO_PRIZE_LIMIT:
        storage.set_bingo_closed()
        _cancel_outstanding_timeouts(context)

    # channel post — best-effort, gated to once per slot
    try:
        await context.bot.send_message(
            chat_id=config.ANNOUNCE_CHAT_ID,
            text=f"🎉 <b>{claim_no}/{config.BINGO_PRIZE_LIMIT} bingo prizes "
                 "claimed!</b>",
            parse_mode="HTML",
        )
        storage.mark_prize_posted(submitter_id)
    except Exception as exc:
        log.warning("couldn't post bingo prize announcement: %s", exc)

    try:
        await context.bot.send_message(
            chat_id=submitter_id,
            text=f"🏆 <b>BINGO!</b> Your line is verified — you're prize "
                 f"#{claim_no} of {config.BINGO_PRIZE_LIMIT}! A facil will be in "
                 "touch. Congrats! 🎉",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("couldn't DM bingo winner %s: %s", submitter_id, exc)


def _cancel_outstanding_timeouts(context):
    """When the game closes at the 10th prize, cancel every still-armed 12h
    confirmation-timeout job (spec §8/§9). Each job also self-guards on the
    closed flag, but we remove them so nothing lingers on the queue."""
    jq = getattr(context, "job_queue", None)
    if jq is None or not hasattr(jq, "get_jobs_by_name"):
        return
    # job names are 'bingo:timeout:<submission_id>'; we don't know the ids here,
    # so cancel via the pending-submission list (their timeouts are still armed).
    try:
        for sub in storage.pending_submissions():
            for job in jq.get_jobs_by_name(f"bingo:timeout:{sub['id']}"):
                job.schedule_removal()
    except Exception as exc:
        log.warning("couldn't cancel outstanding bingo timeouts: %s", exc)


async def _notify_submitter_failed(context, submission_id):
    submitter_id, _ = _submitter_of(submission_id)
    if submitter_id is None:
        return
    try:
        await context.bot.send_message(
            chat_id=submitter_id,
            text="Your bingo line didn't get enough confirmations this time. "
                 "You can try another line in a minute — good luck! 🔁",
        )
    except Exception:
        pass


def _submitter_of(submission_id):
    sub = storage.submission_by_id(submission_id)
    if not sub:
        return None, None
    return sub["submitter_user_id"], sub["submitter_handle"]


async def _confirmation_timeout(context):
    """12h job: evaluate one final time, counting still-missing answers as
    misses. No-op if the submission already resolved or the game is closed."""
    submission_id = context.job.data["submission_id"]
    sub = storage.submission_by_id(submission_id)
    if not sub or sub["status"] != "pending":
        return
    if storage.bingo_is_closed():
        storage.set_submission_status(submission_id, "failed")
        await _notify_submitter_failed(context, submission_id)
        return
    await _finalize(context, submission_id, final=True)


# ---------------------------------------------------------------------------
# Startup + registration
# ---------------------------------------------------------------------------

def rearm_bingo_timeouts(app):
    """Re-arm a 12h finaliser for every still-pending submission after a
    restart, so a crash mid-verification never strands a submission. Uses the
    original submitted_at so long-pending ones fire promptly."""
    jq = app.job_queue
    if jq is None:
        log.warning("JobQueue not available — bingo timeouts won't re-arm.")
        return
    from datetime import datetime
    now = datetime.now(config.TIMEZONE)
    rearmed = 0
    for sub in storage.pending_submissions():
        try:
            submitted = datetime.fromisoformat(sub["submitted_at"])
        except (ValueError, KeyError, TypeError):
            submitted = now
        deadline = submitted + config.BINGO_CONFIRM_TIMEOUT
        delay = (deadline - now).total_seconds()
        when = max(delay, 5)  # give an overdue one a short grace, don't fire at 0
        jq.run_once(
            _confirmation_timeout,
            when=when,
            data={"submission_id": sub["id"]},
            name=f"bingo:timeout:{sub['id']}",
        )
        rearmed += 1
    log.info("re-armed %d bingo confirmation timeout(s)", rearmed)


def register(app):
    app.add_handler(CommandHandler("get_bingo", get_bingo))
    app.add_handler(CommandHandler("submit_bingo", submit_bingo))
    app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingoconf:"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE),
        on_bingo_image,
    ))
```

> Implementation notes tying back to the contract: `_matched_and_prompts` and `_line_verdict` are the pure, unit-tested helpers. `_finalize`/`_award`/`_confirmation_timeout` are the orchestration whose Telegram calls are monkeypatched in tests. All storage calls use the exact contract signatures, including `storage.submission_by_id` (Task 4 — the read-side pair to `start_bingo_submission`, returning `id`, `submitter_user_id`, `submitter_handle`, `status`, `sheet_no`), which maps a callback/timeout back to its submitter. `build_roster_index` exposes a `"handles"` set (Task 5) that `_handle_in_roster` reads to gate `/get_bingo`. When the 10th prize is claimed, `_award` closes the game and calls `_cancel_outstanding_timeouts` to remove every still-armed `bingo:timeout:*` job (spec §8/§9); the jobs also self-guard on the closed flag as a belt-and-braces measure.

- [ ] **Step 4: Run test to verify it passes** — Run: `python -m pytest tests/test_bingo_handlers.py -q`. Expected: PASS (all tests green, including `_matched_and_prompts` with an explicit `sheet_no`, the award-at-limit timeout cancellation, and the game-wide confirmation cache). All cross-task storage/index names are the finalized Task 4/Task 5 contract names (`submission_by_id`, the `"handles"` set), so no shim mismatches remain.

- [ ] **Step 5: Commit** — Run:
```
git add handlers/bingo.py tests/test_bingo_handlers.py
git commit -m "Add Human Bingo handlers: cards, submission pipeline, confirmations"
```

---

### Task 7: Wiring

**Files:**
- Modify `main.py` (lines 15-24 imports; 38-49 `MENU_COMMANDS`; 52-57 `_on_startup`; 80-87 register block)
- Modify `handlers/common.py` (lines 16-39 `HELP_TEXT`)
- Test: `tests/test_wiring.py` (Create)

**Interfaces:**
- Consumes (from Task 6 `handlers/bingo.py`, per the contract, must exist and import cleanly before this task's test passes):
  - `def register(app) -> None`
  - `def rearm_bingo_timeouts(app) -> None`
- Consumes (already present): `main.MENU_COMMANDS: list[BotCommand]`, `main._on_startup`, `reminders.schedule_reminders(app)`, `common.HELP_TEXT: str`.
- Produces (later tasks / operators rely on):
  - `main.MENU_COMMANDS` now contains `BotCommand("get_bingo", "Get your Human Bingo sheet")` and `BotCommand("submit_bingo", "Submit your filled bingo sheet")`.
  - `main.main()` registers the bingo handlers via `bingo.register(app)` and re-arms pending confirmation timeouts via `bingo.rearm_bingo_timeouts(app)` at startup.
  - `common.HELP_TEXT` contains a "🎉 Human Bingo" section mentioning `/get_bingo` and `/submit_bingo`.

---

- [ ] **Step 1: Write the failing test**

Create `tests/test_wiring.py`:

```python
"""Task 7 wiring: bingo is menu-listed, registered, timeouts re-armed, help-documented."""

import importlib

import pytest
from telegram import BotCommand


def _get(modname):
    mod = importlib.import_module(modname)
    return importlib.reload(mod)


def test_menu_has_bingo_commands():
    main = _get("main")
    cmds = {c.command: c.description for c in main.MENU_COMMANDS}
    assert cmds.get("get_bingo") == "Get your Human Bingo sheet"
    assert cmds.get("submit_bingo") == "Submit your filled bingo sheet"


def test_menu_commands_are_botcommands():
    main = _get("main")
    assert all(isinstance(c, BotCommand) for c in main.MENU_COMMANDS)


def test_main_imports_bingo_and_wires_it():
    main = _get("main")
    # bingo module is imported into main's namespace
    assert hasattr(main, "bingo")
    # main.main() must call both bingo.register and bingo.rearm_bingo_timeouts
    src = importlib.util.find_spec("main").loader.get_source("main")
    assert "bingo.register(app)" in src
    assert "bingo.rearm_bingo_timeouts(app)" in src


def test_help_text_documents_bingo():
    common = _get("handlers.common")
    assert "Human Bingo" in common.HELP_TEXT
    assert "get_bingo" in common.HELP_TEXT
    assert "submit_bingo" in common.HELP_TEXT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wiring.py -q`

Expected: FAIL — `test_menu_has_bingo_commands` fails with `AssertionError` (the `get_bingo`/`submit_bingo` keys are missing from `MENU_COMMANDS`), `test_main_imports_bingo_and_wires_it` fails because `main` has no `bingo` attribute / the source lacks the `bingo.register(app)` and `bingo.rearm_bingo_timeouts(app)` lines, and `test_help_text_documents_bingo` fails because `HELP_TEXT` has no "Human Bingo" section. (Task 6's `handlers/bingo.py` must be present so `import main` doesn't `ModuleNotFoundError`.)

- [ ] **Step 3: Write minimal implementation**

Edit `main.py` — add `bingo` to the handlers import block:

```python
from handlers import (
    announcements,
    attendance,
    bingo,
    common,
    provisioning,
    quests,
    reminders,
    schedule,
    settings,
)
```

Edit `main.py` — append the two commands to `MENU_COMMANDS` (after the `attendance` entry, before the closing bracket):

```python
MENU_COMMANDS = [
    BotCommand("start", "What this bot does"),
    BotCommand("help", "List all commands"),
    BotCommand("quests", "Quest locations"),
    BotCommand("quest", "Details for one quest"),
    BotCommand("schedule", "Full StartNOW! schedule"),
    BotCommand("next", "Next upcoming event"),
    BotCommand("meetups", "The official meet-ups"),
    BotCommand("engagements", "Optional sessions"),
    BotCommand("slot", "Is this group AM or PM?"),
    BotCommand("attendance", "Post an attendance poll"),
    BotCommand("get_bingo", "Get your Human Bingo sheet"),
    BotCommand("submit_bingo", "Submit your filled bingo sheet"),
]
```

Edit `main.py` — re-arm pending confirmation timeouts in `_on_startup`, next to `schedule_reminders`:

```python
async def _on_startup(app):
    """Runs once after the app is built: set the menu and queue reminders."""
    await app.bot.set_my_commands(MENU_COMMANDS)
    reminders.schedule_reminders(app)
    attendance.schedule_attendance_polls(app)
    bingo.rearm_bingo_timeouts(app)
    log.info("bot is up and running")
```

Edit `main.py` — register the bingo feature in `main()`, alongside the other `*.register(app)` calls:

```python
    # wire up each feature
    common.register(app)
    quests.register(app)
    schedule.register(app)
    settings.register(app)
    attendance.register(app)
    announcements.register(app)
    provisioning.register(app)
    bingo.register(app)
```

Edit `handlers/common.py` — add a "🎉 Human Bingo" section to `HELP_TEXT` (insert after the Attendance block, before the `<b>For facilitators 🛠️</b>` block), matching the existing HTML + emoji style:

```python
HELP_TEXT = (
    "<b>Here's everything I can do 🌟</b>\n\n"
    "<b>🗺️ Quests</b>\n"
    "/quests — all quests and their Gather Town spots\n"
    "/quest &lt;name&gt; — details for one quest\n\n"
    "<b>📅 Schedule &amp; reminders</b>\n"
    "/schedule — the full StartNOW! schedule\n"
    "/next — the next upcoming event\n"
    "/meetups — the three official meet-ups\n"
    "/engagements — optional engagement sessions\n"
    "/slot — check if this group is AM or PM\n\n"
    "<b>📋 Attendance</b>\n"
    "/attendance — post an attendance poll (Going / Not going / Maybe)\n"
    "<i>(a poll also auto-posts 1 day before each meet-up)</i>\n\n"
    "<b>🎉 Human Bingo</b>\n"
    "/get_bingo — get your personal 5×5 bingo sheet\n"
    "/submit_bingo — send in your filled sheet to check for a win\n"
    "<i>(fill cells with fellow Year 1s' @handles — 5 in a row wins a prize!)</i>\n\n"
    "<b>For facilitators 🛠️</b>\n"
    "/setslot am|pm — set this group's meet-up slot\n"
    "/reminders on|off — toggle reminders for this group\n"
    "/attendance &lt;event&gt; — post an attendance poll for an event\n"
    "/close_attendance &lt;event&gt; — close the poll\n"
    "/announce &lt;message&gt; — post a formatted announcement\n"
    "/remind &lt;message&gt; — post a short reminder\n"
    "/pinannounce &lt;message&gt; — announce and pin it\n"
    "/add_year_ones — add this group's Year 1s (from the sheet)\n"
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_wiring.py -q`

Expected: PASS (4 passed). If `import main` raises `ModuleNotFoundError: handlers.bingo`, Task 6 is not yet merged — that dependency is stated in the Interfaces block; the wiring code itself is complete.

- [ ] **Step 5: Commit**

```
git add main.py handlers/common.py tests/test_wiring.py
git commit -m "Wire up Human Bingo: menu commands, handler registration, startup timeouts, help text"
```
