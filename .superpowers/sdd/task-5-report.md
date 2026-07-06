# Task 5 Report: bingo_ocr.py

## Files committed

- `bingo_ocr.py` — main module (new)
- `tests/test_bingo_ocr.py` — test file (new)
- `config.py` — added `BINGO_MATCH_THRESHOLD = 85`, `BINGO_MATCH_MARGIN = 8` (Task 1 constants not yet present)

## Shim (not committed)

- `data/bingo_templates.py` — minimal geometry shim (`GRID=5`, `FREE=(2,2)`, `is_free`, `CELL_BOXES`, `CORNER_BOX`, `template_path`). Not in git; real Task 2 module supersedes it.

## Geometry / monkeypatch approach

**_engine() singleton**: `_engine()` is module-level, caches `_ENGINE` via `global`. `rapidocr_onnxruntime` is imported lazily inside `_engine()` so import never runs at module load. Tests do `monkeypatch.setattr(bingo_ocr, "_engine", lambda: _FakeEngine(scripted))`.

**Critical design**: `read_submission` calls `_engine()` ONCE at the start and passes the engine object (`eng`) to `_ocr_text(eng, image)`. This is essential: the test's `lambda` creates a fresh `_FakeEngine` on each `_engine()` call; if `_ocr_text` called `_engine()` per crop, the scripted counter would reset each time and always return `scripted[0]`.

**build_roster_index strategy**: Only handle-bearing members' handles are indexed as keys. Name tokens are indexed ONLY for handle-less members, and only when another member with the same name provides a real handle (so every match result is a genuine Telegram handle). This avoids short name tokens like "joshua" (length 6, strict threshold) competing against the correct handle "joshua_lim" and collapsing the margin below 8.

**_raw_handle vs normalize_handle**: `_raw_handle` strips `@` and lowercases without the 5-char minimum regex. This allows short handles like "sam" to be indexed. `normalize_handle` from setup.sheets is still used in `_clean()` for OCR text normalisation (where the regex length guard is appropriate for input validation).

## pytest command and result

```
.venv\Scripts\python.exe -m pytest tests/test_bingo_ocr.py -v
```

```
12 passed in 0.74s
```

All 12 tests green, no network/model download, no rapidocr_onnxruntime import.

## Test score adjustments

None required. All plan-quoted scores matched the installed rapidfuzz 3.14.5:
- `joshua_iim` → `joshua_lim`: 90.0 (plan says "90")
- `r0cket` raw → `rocket`: 83.33 (plan says "83"), variant `rocket` → 100
- `chloe_tar` → `chloe_tan`/`chloe_tam`: 88.89 each (plan says "88.9")
- `pam` → `pamela`: 90.0 (below raised threshold 92 → None)

## Concerns / deviations from plan

1. **Name token indexing removed for handle-bearing members.** The plan's `build_roster_index` indexed both handles AND name tokens (via `_joined_name` and `name_tokens`) for all members. With rapidfuzz 3.14.5, name tokens like "joshualim" (joined) score 94.7 against "joshua_lim", collapsing the margin below 8 and causing false rejections. The fix: only index handles for handle-bearing members; name tokens only for handle-less members pointing to a real handle. The test `test_name_token_match_resolves_to_real_handle` still passes because "amanda" is a direct handle key.

2. **`_ocr_text` signature changed to accept `eng` parameter.** Plan had `_ocr_text(image)` calling `_engine()` internally. Changed to `_ocr_text(eng, image)` with `eng = _engine()` called once in `read_submission`. Required to make the scripted fake work correctly across 25 sequential calls (24 cells + corner).

3. **`config.py` included in commit.** Task 1's BINGO_MATCH constants were absent; added them. If Task 1 later modifies config.py, a merge conflict is possible but trivial (same two lines).
