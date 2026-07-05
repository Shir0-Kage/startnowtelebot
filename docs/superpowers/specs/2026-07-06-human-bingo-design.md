# Human Bingo — design spec

**Date:** 2026-07-06
**Feature:** `/get_bingo` + `/submit_bingo` — an OCR-verified "human bingo" icebreaker game for StartNOW! 2026 Year 1s.

---

## 1. Overview

Year 1s play a 5×5 "human bingo" card: each cell is a printed prompt (e.g. *"Has dyed their hair"*), and they **type** the @handle of a fellow Year 1 who matches into that cell. Getting **5 in a row** (horizontal, vertical, or diagonal, with the centre **FREE SPACE** as a wildcard) is a win.

- There are **15 card templates** (`1.png`–`15.png`), same 5×5 geometry, different prompt arrangements, numbered top-left.
- The roster is the **~68 Year 1s** from `setup/sheets.load_year1_members()` (name + handle + email per member).
- Cards are filled **digitally** (typed text over the template image), then submitted as **png or jpg**.
- The bot OCRs the submission, finds a winning line, DMs the people in that line to confirm the prompts describe them, and — if enough confirm — awards the **submitter** a prize. **10 prizes total**, first-come; each announced in the channel; game closes at 10.

The verification design deliberately keeps OCR errors *safe*: a bad read degrades to "empty cell" (a line just doesn't count), never to awarding the wrong person, and the human Yes/No confirmation is the real backstop.

## 2. Commands & user flow

- **`/get_bingo`** (private chat): resolve the caller's @username against the roster. Non-roster → polite decline (mirrors provisioning's tone). Roster → look up / assign their sheet, DM them `data/bingo_templates/<n>.png`.
- **`/submit_bingo`** (private chat): two-step, because a photo arrives as a separate update from the command text.
  1. `/submit_bingo` sets `context.user_data["awaiting_bingo"] = True` and prompts "send me your filled sheet". Gated first: if the game is closed (10 claimed) → "all 10 prizes claimed"; if the caller already won → "you've already won"; if they have a PENDING submission → "you already have one being checked".
  2. A `MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE))` gated on the flag consumes the next image → runs the pipeline.
- **Confirmation buttons**: `CallbackQueryHandler(pattern=r"^bingoconf:")`; `callback_data = bingoconf:<submission_id>:<row>:<col>:<yes|no>`.

Registration (`handlers/bingo.py::register(app)`):
```
CommandHandler("get_bingo", get_bingo)
CommandHandler("submit_bingo", submit_bingo)
CallbackQueryHandler(confirm_button, pattern=r"^bingoconf:")
MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE), on_bingo_image)
```
No `allowed_updates` change: `main.py` already runs `run_polling(allowed_updates=Update.ALL_TYPES)`.

## 3. Allocation — even + stable

Pure per-handle hashing can't be both **even** and **stable** for 68 people over 15 sheets (hash collisions leave some sheets with 8, others with 1–2). Resolution:

- Deal the roster **round-robin into 15 sheets** so counts differ by at most 1 (even, ~4–5 each), using a deterministic canonical order (sorted by `normalize_handle`).
- **Freeze** the result: persist `user_id → sheet` in `bingo_allocation` on first `/get_bingo`. Never recompute an existing row.
- Key on **`user_id`** (stable), resolved when the caller runs `/get_bingo` in DM (we have their id there). A later @username change does not lose their sheet.
- Roster edits after the game starts: existing handles keep their sheet; a genuinely new handle is appended to the currently-smallest sheet. Log a warning if a handle with an allocation is dropped from the roster (they can still submit).

## 4. OCR pipeline (`bingo_ocr.py`)

Engine: **RapidOCR** via `rapidocr-onnxruntime` (1.x line) — PP-OCRv4 ONNX models **bundled in the wheel** (offline, no system Tesseract), CPU onnxruntime. Instantiated **once** as a module-level singleton (like the shared `storage` connection).

`read_submission(sheet_no, image_bytes) -> {"corner": int|None, "cells": [{row, col, handle, score}, …]}`:

1. Open with Pillow (png/jpg native); `ImageOps.exif_transpose` to fix phone rotation; convert RGB.
2. **Normalize size**: scale to the template's reference width so fractional cell boxes land correctly at any upload resolution.
3. For each of the 25 cells (centre `[2][2]` is FREE — skipped): crop the known box with a small inward inset (~6–10%) to exclude the printed prompt and gridlines; **upscale ~3–4× (LANCZOS)**; grayscale; contrast-boost; **invert to dark-on-light** when the crop is majority-dark (the single biggest reliability lever); optional Otsu binarize only if texture bleeds through.
4. OCR each preprocessed cell individually (no cross-cell bleed).
5. **Fuzzy-match** the text against the closed 68-handle vocabulary with `rapidfuzz`:
   - Clean via `sheets.normalize_handle` (strip `@`, lowercase, `[a-z0-9_]`); try cheap OCR-confusion variants (`O↔0`, `l/I/1`, `rn↔m`).
   - Match against roster **handles** and, separately, `sheets.name_tokens(name)` (so a typed real name also resolves); take the best.
   - Accept only if score ≥ threshold (`BINGO_MATCH_THRESHOLD`, ~85; **length-aware** — stricter for ≤6-char handles) **and** a clear margin over second-best (≥ ~8). Otherwise the cell is **unmatched (empty)**.
   - A cell resolves to at most one roster member.
6. **Corner number**: OCR the known top-left digit region as a sanity check only.

`rapidfuzz` is a tiny (~2 MB) addition; stdlib `difflib` is an acceptable zero-dep fallback but lower quality.

## 5. Winning-line detection (`bingo_lines.py`, pure logic)

- 12 candidate lines: 5 rows, 5 cols, 2 diagonals. Centre auto-counts as filled.
- A line is **complete** iff every non-free cell holds a **confident** match **and** all matched people are **distinct** (icebreaker = 5 different people; a repeated handle invalidates the line).
- A cell matching the **submitter's own** user_id is treated as empty (no self-cheese).
- `winning_lines(cells)` returns each complete line as its list of `(row, col, handle, prompt)` real cells plus `required_yes` (**≥4** for a 5-real line, **≥3** for a free-centre line of 4 real people).
- **One verified line wins** (decision #1). If several lines complete, pick the **easiest candidate** (fewest real people / highest confidence) and stop at the first that verifies. Confirmations are de-duplicated across shared cells.

## 6. Confirmation logic

- For each real cell in the chosen line, DM the person: *"<prompt> — does this describe you?"* with **Yes / No** inline buttons.
- Each cell is `YES | NO | PENDING | UNREACHABLE`. **UNREACHABLE** = the handle maps to no user who has `/start`ed the bot (so `send_message` can't reach them) — treated the same as a miss (decision #3).
- **Pass rule — at most one miss**: verified iff `misses ≤ 1` **and** `YES ≥ required_yes`. A `NO`, a timeout, or an UNREACHABLE each count as one miss; two or more of any mix fails.
- **Confirmation window: 12 hours** (`BINGO_CONFIRM_TIMEOUT`, decision #2). A submission may verify as soon as enough Yes arrive; otherwise, at the 12h timeout it is evaluated once, finally, with still-PENDING cells counted as misses. `verified_at` = the moment it crosses the pass threshold; **prize order is strictly by `verified_at`**. Timeout jobs use `job_queue.run_once` (like reminders); re-armed on startup from PENDING rows.
- **Confirmation cache**: answers are stored per **(subject_user_id, prompt)** and reused game-wide, so a popular person is DMed at most once per distinct prompt (prevents fatigue/griefing). The bot only DMs when there is no cached answer.

## 7. Anti-abuse rules

- Ignore any cell matching the submitter's own user_id.
- A line requires 5 (or 4 + free) **distinct** people.
- A person only counts for the cell whose prompt actually describes them (confirmation keyed by (subject, prompt)); a repeated person can't cover multiple prompts.
- Identity keyed on **user_id**, not @username, everywhere (allocation, prize ownership, confirmation cache).
- **One prize per Year 1** — `UNIQUE(winner_user_id)` on the prize table.
- **One active (PENDING) submission** per person; a second `/submit_bingo` is rejected while one is pending.
- After a **failed** attempt (NO_LINE / UNVERIFIED / REJECTED_IMAGE): retry allowed after a **short cooldown (~60 s)** (decision #4).
- **Wrong-sheet defence**: allocation (not the OCR'd corner number) selects the crop template; a corner-number mismatch **rejects** the upload ("this looks like sheet X but you were given sheet Y").
- Fabricated / non-roster text simply fails the fuzzy match → empty cell. Low-confidence matches are logged for facil audit.

## 8. Prize claim, announcement, closing (race-safe)

- `claim_bingo_prize(submitter_user_id)` is a **single locked transaction**: within one `with _lock:` — `SELECT COUNT(*) FROM bingo_prizes`; if `< 10` and submitter has no row, `INSERT` with `claim_no = count+1` and return the slot, else return `None`. `UNIQUE(winner_user_id)` is the last-line guard. This removes the check-then-act TOCTOU between two concurrent confirmation callbacks (and the separate setup-worker process sharing `bot.db`).
- Only the call that got a real slot posts **"🎉 N/10 prizes claimed!"** to `config.ANNOUNCE_CHAT_ID`. The post is best-effort (`try/except`, like `pinannounce`) and **gated to once per slot** (a `posted_at` column). The counter is always derived from the DB, never an in-memory increment, so a crash between commit and post never double-awards or double-posts.
- Slot **10** also sets a persisted `bingo_closed` flag. Once closed: new `/submit_bingo` short-circuits to "all claimed"; outstanding confirmation timeout jobs are cancelled; late verifiers get "all 10 prizes claimed — thanks for playing!". `/get_bingo` stays open (people can still play for fun).

## 9. Submission state machine

Per submitter, at most one active submission.

```
NONE --/submit_bingo+image--> RECEIVED
RECEIVED --corner mismatch / unmappable--> REJECTED_IMAGE (retry after cooldown)
RECEIVED --no complete line--> NO_LINE (retry after cooldown)
RECEIVED --complete, distinct, non-self line--> PENDING (send confirmations, arm 12h timeout)
PENDING --misses >= 2--> UNVERIFIED (retry after cooldown)
PENDING --YES >= required & misses <= 1--> claim_bingo_prize():
        slot assigned  --> WON (record verified_at, claim_no; post N/10; if 10, set bingo_closed)
        None           --> CLOSED_OR_DUP ("all claimed" / "already won")
Global: bingo_closed set --> any /submit_bingo -> CLOSED
```

## 10. Data model (append to `storage.py` SCHEMA + one function per op, existing `_lock` + `_now_iso()` + WAL pattern)

- **`bingo_allocation`** — `user_id INTEGER PRIMARY KEY`, `handle TEXT`, `sheet_no INTEGER`, `assigned_at TEXT`. Frozen even allocation (§3).
- **`bingo_submissions`** — `id INTEGER PRIMARY KEY AUTOINCREMENT`, `submitter_user_id INTEGER`, `submitter_handle TEXT`, `sheet_no INTEGER`, `corner_read INTEGER NULL`, `status TEXT` (pending/verified/failed/rejected), `submitted_at TEXT`, `verified_at TEXT NULL`.
- **`bingo_winning_members`** — `submission_id INTEGER`, `row INTEGER`, `col INTEGER`, `handle TEXT`, `prompt TEXT`, `target_user_id INTEGER NULL`, `PRIMARY KEY (submission_id,row,col)`. The chosen line's real cells; stores `prompt` so button text is stable.
- **`bingo_confirmations`** — `subject_user_id INTEGER`, `prompt TEXT`, `answer TEXT` (yes/no), `responded_at TEXT`, `PRIMARY KEY (subject_user_id, prompt)`. Game-wide cache (§6). (Submission↔member linkage lets us map an answer back to the pending lines.)
- **`bingo_prizes`** — `winner_user_id INTEGER PRIMARY KEY`, `handle TEXT`, `submission_id INTEGER`, `claim_no INTEGER`, `claimed_at TEXT`, `posted_at TEXT NULL`. Ledger + counter (§8).

Key storage functions (each a single locked block): `allocate_bingo_sheet`, `get_bingo_sheet`, `start_bingo_submission` (enforces one-active), `record_winning_members`, `record_bingo_confirmation` (upsert), `get_cached_confirmation`, `submission_state`, `claim_bingo_prize`, `bingo_prizes_claimed`, `is_bingo_closed`, `set_bingo_closed`, `mark_prize_posted`.

## 11. Files

**New**
- `handlers/bingo.py` — commands, image handler, confirm callback, orchestration only.
- `bingo_ocr.py` — crop + preprocess + OCR + fuzzy-match; module-level RapidOCR singleton.
- `bingo_lines.py` — pure line detection + pass-rule math (no Telegram/OCR imports).
- `data/bingo_templates.py` — per-sheet prompts (5×5) + shared grid geometry → derived crop boxes; `TEMPLATE_DIR` via `pathlib`; `prompt_for` / `cells_for`.
- `data/bingo_templates/1.png … 15.png` — committed static art (non-PII; **not** gitignored).
- `tests/test_bingo_lines.py` — unit tests for the 12 orientations, free-centre auto-fill, distinct-people rule, and the pass thresholds.

**Modified**
- `main.py` — import + `bingo.register(app)`; add `/get_bingo`, `/submit_bingo` to `MENU_COMMANDS`; re-arm PENDING confirmation timeouts on startup.
- `handlers/common.py` — add a "🎉 Human Bingo" section to `HELP_TEXT`.
- `config.py` — `ANNOUNCE_CHAT_ID = int(os.environ.get("ANNOUNCE_CHAT_ID", "-1004292606016"))`, `BINGO_PRIZE_LIMIT = 10`, `BINGO_CONFIRM_TIMEOUT = timedelta(hours=12)`, `BINGO_MATCH_THRESHOLD`.
- `requirements.txt` — promote Pillow to a runtime dep; add `rapidocr-onnxruntime>=1.3,<2`, `rapidfuzz`, `opencv-python-headless`.
- `storage.py` — the 5 tables + helpers above.

## 12. Dependencies & deploy constraint

- `rapidocr-onnxruntime` (1.x, models bundled), `onnxruntime`, `opencv-python-headless`, `rapidfuzz`, `Pillow` (already present). Pure pip, offline, ~120–160 MB.
- ⚠️ **Python 3.10–3.12 required** (rapidocr-onnxruntime 1.4.x is not published for 3.13). Use `opencv-python-headless` (not `opencv-python`) to avoid `libGL.so.1` on slim servers. Document both in the README.

## 13. Build step (owned during implementation, not runtime)

- **Transcribe** the 25 prompts for each of the 15 sheets into `data/bingo_templates.py` (375 strings) by reading `1.png`–`15.png`. No prompts are OCR'd at runtime.
- **Measure the grid geometry once** against a real template PNG (grid origin, cell width/height, gutter) so all 25 boxes derive from one geometry rather than 375 hand-typed boxes.
- Save the 15 PNGs into `data/bingo_templates/`.

## 14. Failure / error handling

- Non-roster or never-`/start`ed caller → guiding decline (consistent with existing `started_users` gating).
- Unmappable / wrong-aspect image → reject with guidance; corner-number mismatch → wrong-sheet reject.
- OCR finds no line → "no bingo yet" with a nudge; retry after cooldown.
- Confirmation DM send failures → caught, that cell = UNREACHABLE (one allowed miss).
- Channel post failure/rate-limit → logged, prize still durable; count re-derives from DB on restart.
- A confirmer tapping Yes then No → last answer wins until the submission finalizes; taps after finalization ignored.
- Process restart mid-verification → PENDING submissions + cached confirmations live in SQLite; timeout jobs re-armed on startup; no double-award (count derived from DB).

## 15. Decisions locked with the user

1. Multiple completed lines → **one verified line wins**; confirm the easiest candidate, stop at first pass.
2. Confirmation window → **12 hours**; unanswered cells then count as misses.
3. Never-`/start`ed confirmers → **count as the one allowed miss** (UNREACHABLE).
4. After a failed attempt → **retry after a ~60 s cooldown, one active attempt at a time**.
5. Announcement channel confirmed: **StartNOW! 2026**, chat_id `-1004292606016`; bot (`@startnow2026_bot` / "NowNow") verified admin with **Post Messages**.
6. Cards are filled **digitally** (typed handles), submitted as png/jpg.

## 16. Out of scope (YAGNI)

No web dashboard, no leaderboard, no editing a submitted card, no cross-sheet analytics, no admin command to manually award (facils can still post in the channel if a channel post fails). `/get_bingo` remains open after the game closes (play-for-fun) but `/submit_bingo` is closed.
