# Bingo Submission Queue + Submitter-Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace immediate bingo processing with a submission queue where the 10 earliest submitters self-confirm/complete their card before the existing tagged-people check awards prizes, with rolling replacement.

**Architecture:** Submissions become a state machine on the existing `bingo_submissions` table. A new module `handlers/bingo_queue.py` owns the queue / kickoff / rolling logic and the submitter-confirmation UX; `handlers/bingo.py`'s submit paths route into it instead of the old immediate `_process_read`. Once a submitter confirms a fully-recognised line, `bingo_queue` flips the row to `pending` and hands off to the **existing, unchanged** tagged-people pipeline (`_dm_subjects` → `bingoconf` → `_finalize` → `_award`).

**Tech Stack:** python-telegram-bot v22 (asyncio, JobQueue), sqlite3 (WAL, shared `_conn`+`_lock`), pytest via `asyncio.run` (no pytest-asyncio plugin), existing `bingo_lines`, `bingo_text`, `data.bingo_templates`.

## Global Constraints

- Python 3.12; python-telegram-bot[job-queue] >=21.4,<23. Tests run offline.
- No hardcoded secrets. Commits authored human-style, NO `Co-Authored-By` trailer.
- All Google-Sheets/roster loads run OFF the event loop; OCR stays in the `ocr_worker.py` subprocess. Never add a blocking fetch on the loop. (Pre-existing exception: `_roster_index()` in the text path — do not make it worse.)
- **STATUS MAPPING — the spec's conceptual state machine maps onto existing `bingo_submissions.status` values; only two values are new. Every task MUST use the DB value, not the spec word:**

  | Spec concept | DB `status` value | New? | Who owns it |
  |---|---|---|---|
  | queued | `queued` | **new** | `bingo_queue` only |
  | confirming | `confirming` | **new** | `bingo_queue` only |
  | verifying | `pending` | existing | existing tagged-people pipeline (unchanged) |
  | won | `verified` | existing | existing `_award` (unchanged) |
  | failed | `failed` | existing | existing `_finalize` + new confirm-timeout |

- **Reuse, do not modify, the tagged-people pipeline**: `_dm_subjects`, `confirm_button`, `_finalize`, `_award`, `_confirmation_timeout`, `_answers_for`, `_line_verdict` stay as-is except for the single `maybe_kickoff` hook added in Task 7. They all key off `status == "pending"`; `_start_verification` (Task 6) sets exactly that.
- **Queue every submission.** A completed photo/text submit enqueues regardless of whether a winning line is present (subject only to the existing gates: game not closed, user hasn't already won, cooldown elapsed, sheet allocated). "No complete line" is a *not-fully-recognised* case per the spec — it still queues and receives the full fill-in template. The old immediate "No bingo yet" reply is removed.
- **Fully recognised** = `bingo_lines.winning_lines` yields a complete line AND every handle in the chosen line is reachable (`storage.user_id_for_handle(handle) is not None`).
- Active-slot cap = `config.BINGO_PRIZE_LIMIT` (10). Both 12h timeouts use `config.BINGO_CONFIRM_TIMEOUT`.
- The bot's shared DB connection is module-level `storage._conn` guarded by `storage._lock`; timestamps via `storage._now_iso()`. New storage functions MUST follow this exact pattern (see existing `start_bingo_submission`).

## File Structure

- `storage.py` (modify) — add queue state helpers; do NOT change existing bingo functions.
- `bingo_text.py` (modify) — cache the 15 blank templates; add `build_line_confirm_text`.
- `handlers/bingo_queue.py` (create) — `evaluate`, `enqueue`, `maybe_kickoff`, `_send_confirmation`, `_confirm_keyboard`, `_arm_confirm_timeout`, `confirm_button`, `on_resend`, `_start_verification`, `_confirm_timeout_job`, `close_round`, `register`, `rearm_confirm_timeouts`, module dict `_PENDING_READ`.
- `handlers/bingo.py` (modify) — submit paths call `bingo_queue.enqueue`; remove old `_process_read`; route `on_bingo_text` resends; add `maybe_kickoff` hook in `_finalize`/`_award`; register queue handlers + facil close + re-arm on startup.
- Tests: `tests/test_bingo_queue_storage.py` (create), `tests/test_bingo_text.py` (modify), `tests/test_bingo_queue.py` (create), `tests/test_bingo_handlers.py` (modify — the immediate-processing tests change to queue behavior).

---

### Task 1: Queue state helpers in storage

**Files:**
- Modify: `storage.py` (append near the other Human Bingo functions, after `pending_submissions`)
- Test: `tests/test_bingo_storage.py` (append — REUSE the existing `store` fixture defined at the top of that file; do NOT redefine it)

**Interfaces:**
- Consumes: existing `storage._conn`, `storage._lock`, `storage._now_iso`, `set_submission_status`.
- Produces:
  - `queue_submission(user_id, handle, sheet_no) -> int` — in ONE locked transaction: `DELETE FROM bingo_submissions WHERE submitter_user_id = ? AND status IN ('queued','confirming')`, then INSERT a row with `status='queued'`, `submitted_at=_now_iso()`, `corner_read=NULL`, `verified_at=NULL`; return `lastrowid`. (Does NOT delete `pending`/`verified` rows — a submission already in tagged-people verification is protected, and the submit gate blocks a new submit while one is `pending`.)
  - `queued_in_order() -> list[dict]` — `status='queued'`, `ORDER BY submitted_at, id`.
  - `confirming_submissions() -> list[dict]` — `status='confirming'`, `ORDER BY submitted_at, id`.
  - `active_slot_count() -> int` — `COUNT(*)` of `status IN ('confirming','pending','verified')` (the in-flight slots: confirming + verifying + won).
  - `submission_status(submission_id) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_storage.py — append. Reuses the existing `store` fixture
# (fresh storage module bound to an isolated temp DB) already defined at the top
# of this file. Do NOT redefine the fixture or re-import pytest.

def test_queue_dedupes_queued_and_confirming_for_one_user(store):
    a = store.queue_submission(1, "alice", 3)
    store.set_submission_status(a, "confirming")
    a2 = store.queue_submission(1, "alice", 3)          # replaces the confirming row
    ids = [r["id"] for r in store.queued_in_order()]
    assert a2 in ids and a not in ids
    assert store.submission_status(a) is None           # old row gone


def test_queue_does_not_touch_pending_or_verified(store):
    p = store.queue_submission(1, "alice", 3)
    store.set_submission_status(p, "pending")           # in tagged-people verify
    q = store.queue_submission(1, "alice", 3)           # must NOT delete the pending row
    assert store.submission_status(p) == "pending"
    assert store.submission_status(q) == "queued"


def test_ordering_is_by_time_then_id(store):
    a = store.queue_submission(1, "a", 1)
    b = store.queue_submission(2, "b", 1)
    ids = [r["id"] for r in store.queued_in_order()]
    assert ids == sorted(ids)                            # same-second inserts fall back to id


def test_active_slot_count_counts_confirming_pending_verified(store):
    s = store.queue_submission(1, "alice", 3)
    assert store.active_slot_count() == 0               # queued is not a slot
    for status, expected in [("confirming", 1), ("pending", 1), ("verified", 1),
                             ("failed", 0)]:
        store.set_submission_status(s, status)
        assert store.active_slot_count() == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`
Expected: the 4 new tests FAIL — `AttributeError: module 'storage' has no attribute 'queue_submission'` (existing tests still pass).

- [ ] **Step 3: Implement the helpers**

```python
# storage.py — append after pending_submissions()

def queue_submission(user_id, handle, sheet_no):
    """Enqueue a fresh submission, replacing this user's existing queued/confirming
    one (never a row already in tagged-people verification). Returns its id."""
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_submissions "
            "WHERE submitter_user_id = ? AND status IN ('queued','confirming')",
            (user_id,),
        )
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, corner_read, "
            " status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, NULL, 'queued', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, _now_iso()),
        )
        _conn.commit()
        return cur.lastrowid


def queued_in_order():
    """All 'queued' submissions, earliest first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'queued' "
            "ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def confirming_submissions():
    """All 'confirming' submissions, earliest first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'confirming' "
            "ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def active_slot_count():
    """In-flight prize slots: confirming + verifying(pending) + won(verified)."""
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('confirming','pending','verified')"
        ).fetchone()
    return row["c"]


def submission_status(submission_id):
    """A submission's current status string, or None if it doesn't exist."""
    with _lock:
        row = _conn.execute(
            "SELECT status FROM bingo_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return row["status"] if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`
Expected: PASS (existing tests + 4 new).

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_bingo_storage.py
git commit -m "Add bingo submission-queue state helpers to storage"
```

---

### Task 2: Pre-generate blank templates + winning-line confirm text

**Files:**
- Modify: `bingo_text.py`
- Test: `tests/test_bingo_text.py` (append — file EXISTS; `import bingo_text` is already at the top, don't duplicate it. The existing `test_build_template_text_has_24_lines...` still passes against the cached string.)

**Interfaces:**
- Consumes: `data.bingo_templates` (`GRID`, `is_free`, `prompt_for`, `SHEETS`).
- Produces:
  - `build_template_text(sheet_no)` — now returns a cached string (built once per sheet at import into `_TEMPLATE_CACHE`). Same text as before.
  - `_TEMPLATE_CACHE` — `{sheet_no: str}` for all sheets in `templates.SHEETS`.
  - `build_line_confirm_text(sheet_no, line) -> str` — `line` is a list of `(row, col, handle)` (0-indexed, as returned by `bingo_lines.winning_lines`). One line per cell: `R{row+1}C{col+1}: {prompt} - @{handle}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_text.py — append (bingo_text is already imported at the top)

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_text.py -q`
Expected: FAIL — `AttributeError: module 'bingo_text' has no attribute '_TEMPLATE_CACHE'`.

- [ ] **Step 3: Implement caching + line confirm**

Replace the existing `build_template_text` in `bingo_text.py` with a cached version and add `build_line_confirm_text`:

```python
# bingo_text.py — replace build_template_text; add cache + build_line_confirm_text

def _build_template_text(sheet_no):
    lines = []
    for row in range(templates.GRID):
        for col in range(templates.GRID):
            if templates.is_free(row, col):
                continue
            prompt = templates.prompt_for(sheet_no, row, col)
            lines.append(f"R{row + 1}C{col + 1}: {prompt} - ")
    return "\n".join(lines)


# Pre-build the 15 blank fill-in templates once at import (they never change).
_TEMPLATE_CACHE = {n: _build_template_text(n) for n in templates.SHEETS}


def build_template_text(sheet_no):
    """The cached fill-in-the-blank list a player replies to, one line per
    non-FREE cell (R1C1..R5C5, FREE centre omitted). Pre-generated at import."""
    return _TEMPLATE_CACHE[sheet_no]


def build_line_confirm_text(sheet_no, line):
    """Render just the winning line's cells for the short confirm message.
    `line`: list of (row, col, handle) 0-indexed, as bingo_lines returns."""
    out = []
    for row, col, handle in line:
        prompt = templates.prompt_for(sheet_no, row, col)
        out.append(f"R{row + 1}C{col + 1}: {prompt} - @{handle}")
    return "\n".join(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_text.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bingo_text.py tests/test_bingo_text.py
git commit -m "Pre-generate blank bingo templates + winning-line confirm text"
```

---

### Task 3: Recognition helper — classify a read

**Files:**
- Create: `handlers/bingo_queue.py`
- Test: `tests/test_bingo_queue.py` (create)

**Interfaces:**
- Consumes: `handlers.bingo._matched_and_prompts(cells, submitter_handle, sheet_no) -> (matched, prompts)` where `matched` is `{(row,col): handle}`; `bingo_lines.winning_lines(matched, submitter_handle) -> list[line]`; `bingo_lines.pick_best_line(lines) -> line`; `storage.user_id_for_handle(handle) -> int|None`. (Import `handlers.bingo` lazily INSIDE `evaluate` to avoid an import cycle — `bingo.py` imports `bingo_queue` at module load.)
- Produces:
  - `evaluate(read, submitter_handle, sheet_no) -> dict` with keys `{"line": [(r,c,h),...] | None, "fully_recognised": bool, "unreachable": [handle,...]}`. `fully_recognised` is True iff a winning line exists AND every handle in the chosen line is reachable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py
import bingo_lines
from handlers import bingo_queue


def _cells(matched):
    """Build a read_submission-shaped cells list from {(r,c): handle}."""
    cells = []
    for r in range(5):
        for c in range(5):
            if (r, c) == (2, 2):
                continue
            h = matched.get((r, c))
            cells.append({"row": r, "col": c, "handle": h,
                          "score": 100.0 if h else 0.0})
    return cells


_TOP_ROW = {(0, 0): "alice", (0, 1): "bob", (0, 2): "carol", (0, 3): "dan", (0, 4): "eve"}
# Row 0 does NOT cross the FREE centre (2,2), so a COMPLETE row-0 line needs all
# 5 cells matched. (Only row 2, col 2, and the two diagonals lose the centre.)


def test_evaluate_fully_recognised_when_line_all_reachable(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)
    res = bingo_queue.evaluate({"cells": _cells(_TOP_ROW)}, "submitter", 1)
    assert res["fully_recognised"] is True
    assert res["line"] is not None and res["unreachable"] == []


def test_evaluate_flags_unreachable(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle",
                        lambda h: None if h == "dan" else 1)
    res = bingo_queue.evaluate({"cells": _cells(_TOP_ROW)}, "submitter", 1)
    assert res["fully_recognised"] is False
    assert res["unreachable"] == ["dan"]
    assert res["line"] is not None            # a line exists, just not all reachable


def test_evaluate_no_line(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)
    res = bingo_queue.evaluate({"cells": _cells({(0, 0): "alice"})}, "submitter", 1)
    assert res["line"] is None
    assert res["fully_recognised"] is False and res["unreachable"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.bingo_queue'`.

- [ ] **Step 3: Implement `evaluate` + module skeleton**

```python
# handlers/bingo_queue.py
"""Bingo submission queue: enqueue, kickoff-at-10, submitter self-confirm, and
rolling replacement, ahead of the existing tagged-people verification.

State (see the plan's STATUS MAPPING): queued -> confirming -> pending(verify)
-> verified(won) | failed. Only 'queued'/'confirming' are owned here; once a
submitter confirms a fully-recognised line, _start_verification flips the row to
'pending' and the existing handlers/bingo.py pipeline takes over unchanged.
"""

import logging

import bingo_lines as lines
import bingo_text
import config
import storage

log = logging.getLogger(__name__)

# submission_id -> {"read": <cells dict>, "handle": str, "sheet_no": int}
# The submitter's latest parsed read, needed by kickoff/confirm/resend. An
# in-memory miss after a restart just means the confirm message can't be
# re-derived until the user resends, which is acceptable.
_PENDING_READ = {}


def evaluate(read, submitter_handle, sheet_no):
    """Classify a parsed read.
    Returns {"line", "fully_recognised", "unreachable"}."""
    from handlers.bingo import _matched_and_prompts   # lazy: avoid import cycle
    matched, _prompts = _matched_and_prompts(
        read.get("cells", []), submitter_handle, sheet_no)
    candidates = lines.winning_lines(matched, submitter_handle)
    if not candidates:
        return {"line": None, "fully_recognised": False, "unreachable": []}
    line = lines.pick_best_line(candidates)
    unreachable = [h for (_r, _c, h) in line
                   if storage.user_id_for_handle(h) is None]
    return {"line": line, "fully_recognised": not unreachable,
            "unreachable": unreachable}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Add bingo_queue.evaluate: classify a read (line + reachability)"
```

---

### Task 4: Confirmation message builder (short vs full + unreachable flags)

**Files:**
- Modify: `handlers/bingo_queue.py`
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `evaluate`, `_PENDING_READ`, `bingo_text.build_line_confirm_text`, `bingo_text.build_prefilled_text`, `telegram.InlineKeyboardButton/Markup`.
- Produces:
  - `_confirm_keyboard(submission_id) -> InlineKeyboardMarkup` — one "✅ Confirm" button, callback `bingoq:confirm:<id>`.
  - `async _send_confirmation(context, sub) -> None` — `sub` is a submission dict (needs `id`, `submitter_user_id`). Reads `_PENDING_READ[sub["id"]]`; if fully recognised → DM the short line + confirm keyboard; else → DM the full prefilled template + a `/start` flag listing any matched-but-unreachable handles. No-op (with a log warning) if the pending read is missing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py — append
import asyncio
from unittest.mock import AsyncMock, MagicMock


def _ctx():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    return ctx


def test_confirmation_short_when_fully_recognised(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)
    sid = 7
    bingo_queue._PENDING_READ[sid] = {
        "read": {"cells": _cells(_TOP_ROW)}, "handle": "submitter", "sheet_no": 1}
    ctx = _ctx()
    asyncio.run(bingo_queue._send_confirmation(
        ctx, {"id": sid, "submitter_user_id": 99}))
    kwargs = ctx.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 99
    assert "@alice" in kwargs["text"]
    assert kwargs.get("reply_markup") is not None            # confirm button
    bingo_queue._PENDING_READ.pop(sid, None)


def test_confirmation_full_flags_unreachable(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle",
                        lambda h: None if h == "dan" else 1)
    sid = 8
    bingo_queue._PENDING_READ[sid] = {
        "read": {"cells": _cells(_TOP_ROW)}, "handle": "submitter", "sheet_no": 1}
    ctx = _ctx()
    asyncio.run(bingo_queue._send_confirmation(
        ctx, {"id": sid, "submitter_user_id": 99}))
    text = ctx.bot.send_message.await_args.kwargs["text"]
    assert "@dan" in text and "/start" in text
    assert ctx.bot.send_message.await_args.kwargs.get("reply_markup") is None
    bingo_queue._PENDING_READ.pop(sid, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `_send_confirmation` undefined.

- [ ] **Step 3: Implement `_send_confirmation` + keyboard**

```python
# handlers/bingo_queue.py — add this import at the top and these functions
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _confirm_keyboard(submission_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm",
                             callback_data=f"bingoq:confirm:{submission_id}")
    ]])


async def _send_confirmation(context, sub):
    """DM the submitter their confirmation message: short (winning line + a
    Confirm button) when fully recognised, else the full fill-in template with a
    /start flag for any matched-but-unreachable handle."""
    sid = sub["id"]
    uid = sub["submitter_user_id"]
    pending = _PENDING_READ.get(sid)
    if pending is None:
        log.warning("no pending read for submission %s; can't send confirmation", sid)
        return
    res = evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if res["fully_recognised"]:
        text = ("You're up! 🎉 Here's your winning line — tap Confirm if it's "
                "right:\n\n"
                + bingo_text.build_line_confirm_text(pending["sheet_no"], res["line"]))
        await context.bot.send_message(
            chat_id=uid, text=text, reply_markup=_confirm_keyboard(sid))
        return
    preview = bingo_text.build_prefilled_text(
        pending["sheet_no"], pending["read"].get("cells", []))
    flag = ""
    if res["unreachable"]:
        who = ", ".join(f"@{h}" for h in res["unreachable"])
        flag = (f"\n\n⚠️ {who} hasn't started the bot yet — ask them to send it "
                "/start so I can verify them, then resend your list.")
    await context.bot.send_message(
        chat_id=uid,
        text="You're up! Fill in the @handles below (fix any blanks) and send the "
             "whole list back to me:\n\n" + preview + flag,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Build short/full bingo confirmation messages with unreachable flags"
```

---

### Task 5: Enqueue on submit + kickoff at 10

**Files:**
- Modify: `handlers/bingo_queue.py`; `handlers/bingo.py` (submit paths)
- Test: `tests/test_bingo_queue.py`; `tests/test_bingo_handlers.py` (update immediate-processing tests)

**Interfaces:**
- Consumes: `storage.queue_submission`, `storage.queued_in_order`, `storage.active_slot_count`, `storage.set_submission_status`, `storage.submission_status`, `storage.submission_by_id`, `_send_confirmation`, `config.BINGO_CONFIRM_TIMEOUT`.
- Produces:
  - `async enqueue(context, uid, handle, sheet_no, read) -> None` — `queue_submission`, stash `_PENDING_READ[sid]`, DM "You're in the queue (#N)…", then `await maybe_kickoff(context)`.
  - `async maybe_kickoff(context) -> None` — while `active_slot_count() < config.BINGO_PRIZE_LIMIT` and a queued submission exists: take the earliest, `set_submission_status(id, 'confirming')`, `_arm_confirm_timeout(context, id)`, `await _send_confirmation(...)`.
  - `def _arm_confirm_timeout(context, submission_id)` — `context.job_queue.run_once(_confirm_timeout_job, when=config.BINGO_CONFIRM_TIMEOUT, data={"submission_id": id}, name=f"bingo:confirmwait:{id}")` (guard `job_queue is not None`).
  - `async _confirm_timeout_job(context)` — if `submission_status(id) == 'confirming'`: `set_submission_status(id, 'failed')`, DM the submitter their slot passed on, `await maybe_kickoff(context)`. (This is the submitter-confirm 12h deadline; the confirm/resend RESPONSE handlers live in Task 6.)

> Ordering: `maybe_kickoff` is a loop guarded by `active_slot_count()`, so when the 10th submission enqueues it fires confirmations for the earliest 10 in one pass; the 11th enqueue finds the count already at 10 and does nothing. When a slot later frees (Task 6/7), calling `maybe_kickoff` again promotes the next queued.

> Why the timeout machinery is here (not Task 6): `maybe_kickoff` must arm a REAL confirm-timeout (the rewritten handler tests assert `run_once` fires), and `_arm_confirm_timeout` references `_confirm_timeout_job`, so both must be defined in this task. Task 6 adds only the submitter's confirm/resend RESPONSE handlers and reuses these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py — append

class FakeStore:
    """In-memory stand-in for the queue subset of storage."""
    def __init__(self):
        self.rows = {}
        self._id = 0
    def queue_submission(self, uid, handle, sheet_no):
        self.rows = {i: r for i, r in self.rows.items()
                     if not (r["submitter_user_id"] == uid
                             and r["status"] in ("queued", "confirming"))}
        self._id += 1
        self.rows[self._id] = {"id": self._id, "submitter_user_id": uid,
                               "submitter_handle": handle, "sheet_no": sheet_no,
                               "status": "queued", "submitted_at": f"{self._id:05d}"}
        return self._id
    def queued_in_order(self):
        return sorted((dict(r) for r in self.rows.values()
                       if r["status"] == "queued"),
                      key=lambda r: (r["submitted_at"], r["id"]))
    def confirming_submissions(self):
        return sorted((dict(r) for r in self.rows.values()
                       if r["status"] == "confirming"),
                      key=lambda r: (r["submitted_at"], r["id"]))
    def active_slot_count(self):
        return sum(1 for r in self.rows.values()
                   if r["status"] in ("confirming", "pending", "verified"))
    def set_submission_status(self, sid, status):
        self.rows[sid]["status"] = status
    def submission_status(self, sid):
        return self.rows[sid]["status"] if sid in self.rows else None
    def submission_by_id(self, sid):
        return dict(self.rows[sid]) if sid in self.rows else None


def test_kickoff_promotes_only_ten_earliest(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    for uid in range(1, 13):
        fake.queue_submission(uid, f"u{uid}", 1)
    asyncio.run(bingo_queue.maybe_kickoff(_ctx()))
    assert bingo_queue._send_confirmation.await_count == 10
    assert fake.active_slot_count() == 10
    assert len(fake.queued_in_order()) == 2


def test_enqueue_replies_in_queue_then_kicks_off(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    ctx = _ctx()
    asyncio.run(bingo_queue.enqueue(ctx, 1, "alice", 1, {"cells": _cells({})}))
    text = ctx.bot.send_message.await_args_list[0].kwargs["text"]
    assert "queue" in text.lower()
    assert bingo_queue._send_confirmation.await_count == 1   # 1 queued, slot free
    bingo_queue._PENDING_READ.pop(1, None)


def test_confirm_timeout_fails_and_promotes(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    a = fake.queue_submission(1, "a", 1); fake.set_submission_status(a, "confirming")
    b = fake.queue_submission(2, "b", 1)                 # queued behind
    bingo_queue._PENDING_READ[b] = {"read": {"cells": _cells({})},
                                    "handle": "b", "sheet_no": 1}
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    ctx = _ctx(); ctx.job = MagicMock(); ctx.job.data = {"submission_id": a}
    asyncio.run(bingo_queue._confirm_timeout_job(ctx))
    assert fake.submission_status(a) == "failed"
    assert fake.submission_status(b) == "confirming"     # next promoted
    bingo_queue._PENDING_READ.pop(b, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `maybe_kickoff`/`enqueue` undefined.

- [ ] **Step 3: Implement enqueue + maybe_kickoff; rewire bingo.py submit paths**

```python
# handlers/bingo_queue.py — add
async def enqueue(context, uid, handle, sheet_no, read):
    """Record a submission into the queue and tell the user their position, then
    fire kickoff if a slot is free (auto-batches once 10 are queued)."""
    sid = storage.queue_submission(uid, handle, sheet_no)
    _PENDING_READ[sid] = {"read": read, "handle": handle, "sheet_no": sheet_no}
    position = len(storage.queued_in_order())
    await context.bot.send_message(
        chat_id=uid,
        text=f"You're in the queue (#{position})! 📥 I'll message you if I need "
             "you to confirm your squares — hang tight. 🙂",
    )
    await maybe_kickoff(context)


async def maybe_kickoff(context):
    """Promote queued submissions into 'confirming' until the 10 in-flight slots
    are full, sending each its confirmation message."""
    while storage.active_slot_count() < config.BINGO_PRIZE_LIMIT:
        queued = storage.queued_in_order()
        if not queued:
            return
        sub = queued[0]
        storage.set_submission_status(sub["id"], "confirming")
        _arm_confirm_timeout(context, sub["id"])
        await _send_confirmation(context, sub)


def _arm_confirm_timeout(context, submission_id):
    """Arm the submitter-confirm 12h timeout for a 'confirming' submission."""
    jq = getattr(context, "job_queue", None)
    if jq is None:
        return
    jq.run_once(
        _confirm_timeout_job,
        when=config.BINGO_CONFIRM_TIMEOUT,
        data={"submission_id": submission_id},
        name=f"bingo:confirmwait:{submission_id}",
    )


async def _confirm_timeout_job(context):
    """12h submitter-confirm deadline: if still unconfirmed, fail and roll on."""
    sid = context.job.data["submission_id"]
    if storage.submission_status(sid) != "confirming":
        return
    storage.set_submission_status(sid, "failed")
    sub = storage.submission_by_id(sid)
    if sub is not None:
        try:
            await context.bot.send_message(
                chat_id=sub["submitter_user_id"],
                text="Your bingo confirmation timed out, so your slot passed to the "
                     "next player. Submit again anytime to re-join the queue! 🔁",
            )
        except Exception:
            pass
    await maybe_kickoff(context)
```

In `handlers/bingo.py`:
- Add near the other imports: `from handlers import bingo_queue` (module-level; `bingo_queue` imports `bingo` only lazily inside functions, so no cycle).
- Delete `_process_read` (the whole function, lines ~457-502).
- In `on_bingo_text`, replace the final `await _process_read(...)` with:

```python
        await bingo_queue.enqueue(context, uid, handle, sheet_no, read)
```

- In `bingo_ocr_confirm_button`, replace the final `await _process_read(update, context, uid, pending["handle"], pending["sheet_no"], pending["read"])` with:

```python
        await bingo_queue.enqueue(
            context, uid, pending["handle"], pending["sheet_no"], pending["read"])
```

- [ ] **Step 4: Update the four existing handler tests that assert the OLD immediate-processing behavior**

Exactly FOUR tests in `tests/test_bingo_handlers.py` break and must be REWRITTEN to assert the new queue behavior (do NOT delete coverage, do NOT weaken to trivial asserts). All four have an empty queue and no active slots, so `submit → enqueue → queued → maybe_kickoff → confirming → _send_confirmation`.

New-flow facts (true for all four):
- The submission is now status `confirming`, NOT `pending`. `store.active_submission(100)` returns `None` (it only matches `pending`). Find the submission via `store.confirming_submissions()` → one row with `submitter_user_id == 100`.
- All player-facing messages now go through `ctx.bot.send_message` (enqueue sends "You're in the queue (#1)…", then `_send_confirmation` sends the confirmation). The success path does NOT call `update.effective_message.reply_text`.
- Subjects (chat_ids 1-4) are NOT DM'd at submit time — that happens only after the submitter confirms (Task 6). Assert no `send_message` went to chat_ids 1-4.
- `ctx.job_queue.run_once` is called once (arming the confirm-timeout `bingo:confirmwait:<id>`).
- `winning_members` is NOT recorded yet at submit (recorded later in `_start_verification`, Task 6) — do not assert on it here.

Per-test rewrite (rename each to fit the new behavior):
1. `test_ocr_confirm_yes_records_line_and_dms_subjects` → e.g. `test_ocr_confirm_yes_queues_and_sends_short_confirmation`: subjects are `mark_started` (reachable) and `winning_lines` is monkeypatched to a complete line ⇒ fully recognised. After `_tap_ocr_confirm(...,"yes")`: assert exactly one `confirming` submission for user 100; `active_submission(100) is None`; the LAST `ctx.bot.send_message` went to `chat_id=100` and its `reply_markup` contains a button whose `callback_data` starts with `bingoq:confirm:`; assert no `send_message` call used `chat_id` in {1,2,3,4}.
2. `test_ocr_confirm_yes_no_line_reports_and_no_submission` → e.g. `test_ocr_confirm_yes_no_line_still_queues_full_template`: `winning_lines=[]` ⇒ not recognised. After yes: assert one `confirming` submission for user 100; the confirmation `ctx.bot.send_message` to `chat_id=100` is the FULL template (text contains `"R1C1"`) and has NO `reply_markup` (no confirm button). (The old "no bingo / no submission" expectation is intentionally removed — queuing every submission is the new behavior.)
3. `test_on_bingo_text_full_pipeline_success` → e.g. `test_on_bingo_text_queues_and_sends_short_confirmation`: 4 subjects `mark_started`; `winning_lines` monkeypatched to a complete line ⇒ fully recognised. After `on_bingo_text`: one `confirming` submission for user 100; the LAST `ctx.bot.send_message` to `chat_id=100` carries the line and a `bingoq:confirm:` button; no DMs to 1-4; `run_once` called once.
4. `test_on_bingo_text_exact_match_miss_degrades_gracefully`: `winning_lines=[]` ⇒ not recognised. After `on_bingo_text`: one `confirming` submission for user 100; replace the old `upd.effective_message.reply_text.assert_awaited()` with `ctx.bot.send_message.assert_awaited()` (the full template).

Leave `test_on_bingo_text_ignores_when_flag_not_set` unchanged — Task 5 does not touch `on_bingo_text`'s awaiting-flag guard (Task 8 does), and that user has no confirming submission, so it still no-ops.

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py tests/test_bingo_handlers.py -q` then the FULL suite `.venv/Scripts/python.exe -m pytest -q`.
Expected: PASS (the four rewritten tests + everything else). If any OTHER existing test breaks, investigate — only these four should need changes.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py handlers/bingo.py tests/
git commit -m "Queue bingo submissions on submit and kick off the earliest 10"
```

---

### Task 6: Submitter confirm / resend / verification hand-off + confirm timeout

**Files:**
- Modify: `handlers/bingo_queue.py`
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `_PENDING_READ`, `evaluate`, `_send_confirmation`, `storage.confirming_submissions`, `storage.submission_status`, `storage.set_submission_status`, `storage.record_winning_members`, `storage.submission_by_id`, `storage.user_id_for_handle`, `data.bingo_templates.prompt_for`, and (lazily, to avoid cycle) `handlers.bingo._dm_subjects`, `handlers.bingo._finalize`, `handlers.bingo._confirmation_timeout`, `handlers.bingo._cancel_job`, `config.BINGO_CONFIRM_TIMEOUT`. (`_arm_confirm_timeout` and `_confirm_timeout_job` were already defined in Task 5 — do NOT redefine them.)
- Produces:
  - `async confirm_button(update, context)` — handles `bingoq:confirm:<id>`; only acts if `submission_status(id) == 'confirming'`; re-`evaluate`s the pending read; if fully recognised → `_start_verification`; else silently ignore (the full-template path governs).
  - `async on_resend(context, uid, read) -> bool` — if the user has a `confirming` submission: update its `_PENDING_READ`, re-`evaluate`; fully recognised → `_start_verification`; else → `_send_confirmation` again. Returns True if it handled a confirming user, else False.
  - `async _start_verification(context, submission_id, line, handle, sheet_no)` — cancel `bingo:confirmwait:<id>`; build `members` (row,col,handle,prompt,target_user_id); `record_winning_members`; `set_submission_status(id, 'pending')`; DM the submitter "Nice line!…"; arm existing `bingo._confirmation_timeout` (name `bingo:timeout:<id>`); `await bingo._dm_subjects`; `await bingo._finalize`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bingo_queue.py — append

def test_confirm_button_starts_verification_when_fully_recognised(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(fake, "user_id_for_handle", lambda h: 1, raising=False)
    sid = fake.queue_submission(1, "submitter", 1)
    fake.set_submission_status(sid, "confirming")
    bingo_queue._PENDING_READ[sid] = {
        "read": {"cells": _cells(_TOP_ROW)}, "handle": "submitter", "sheet_no": 1}
    started = {}
    monkeypatch.setattr(bingo_queue, "_start_verification",
                        AsyncMock(side_effect=lambda *a, **k: started.setdefault("y", a)))
    q = AsyncMock(); q.data = f"bingoq:confirm:{sid}"; q.from_user = MagicMock(id=1)
    upd = MagicMock(); upd.callback_query = q
    asyncio.run(bingo_queue.confirm_button(upd, _ctx()))
    assert "y" in started
    bingo_queue._PENDING_READ.pop(sid, None)


def test_confirm_button_ignored_when_not_confirming(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    sid = fake.queue_submission(1, "s", 1)               # still 'queued'
    monkeypatch.setattr(bingo_queue, "_start_verification", AsyncMock())
    q = AsyncMock(); q.data = f"bingoq:confirm:{sid}"; q.from_user = MagicMock(id=1)
    upd = MagicMock(); upd.callback_query = q
    asyncio.run(bingo_queue.confirm_button(upd, _ctx()))
    assert bingo_queue._start_verification.await_count == 0
```
(The `_confirm_timeout_job` unit test lives in Task 5, where the function is defined.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `confirm_button` undefined (the confirm-button tests error; `_confirm_timeout_job` already exists from Task 5).

- [ ] **Step 3: Implement**

```python
# handlers/bingo_queue.py — add (reuse _arm_confirm_timeout / _confirm_timeout_job from Task 5; do NOT redefine them)
async def confirm_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, _, sid_s = query.data.split(":")
        sid = int(sid_s)
    except (ValueError, AttributeError):
        return
    if storage.submission_status(sid) != "confirming":
        return                                    # stale / already resolved
    pending = _PENDING_READ.get(sid)
    if pending is None:
        return
    res = evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if not res["fully_recognised"]:
        return                                    # full-template path governs
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _start_verification(
        context, sid, res["line"], pending["handle"], pending["sheet_no"])


async def on_resend(context, uid, read):
    """Handle a typed resend from a submitter in the 'confirming' phase.
    Returns True if the user was confirming (message consumed), else False."""
    mine = [s for s in storage.confirming_submissions()
            if s["submitter_user_id"] == uid]
    if not mine:
        return False
    sub = mine[0]
    handle = sub.get("submitter_handle") or ""
    _PENDING_READ[sub["id"]] = {
        "read": read, "handle": handle, "sheet_no": sub["sheet_no"]}
    res = evaluate(read, handle, sub["sheet_no"])
    if res["fully_recognised"]:
        await _start_verification(
            context, sub["id"], res["line"], handle, sub["sheet_no"])
    else:
        await _send_confirmation(context, sub)    # re-show full + flags
    return True


async def _start_verification(context, submission_id, line, handle, sheet_no):
    """Submitter confirmed a fully-recognised line: hand off to the existing
    tagged-people pipeline (flip to 'pending', DM subjects, arm the 12h timeout,
    evaluate once)."""
    from handlers import bingo                     # lazy: avoid import cycle
    from data import bingo_templates as templates
    bingo._cancel_job(context, f"bingo:confirmwait:{submission_id}")
    members = [{
        "row": r, "col": c, "handle": h,
        "prompt": templates.prompt_for(sheet_no, r, c),
        "target_user_id": storage.user_id_for_handle(h),
    } for (r, c, h) in line]
    storage.record_winning_members(submission_id, members)
    storage.set_submission_status(submission_id, "pending")
    sub = storage.submission_by_id(submission_id)
    if sub is not None:
        try:
            await context.bot.send_message(
                chat_id=sub["submitter_user_id"],
                text="Nice line! 🎯 I'm checking with the people you tagged — I'll "
                     "message you the moment it's verified (they have 12 hours).",
            )
        except Exception:
            pass
    if getattr(context, "job_queue", None) is not None:
        context.job_queue.run_once(
            bingo._confirmation_timeout,
            when=config.BINGO_CONFIRM_TIMEOUT,
            data={"submission_id": submission_id},
            name=f"bingo:timeout:{submission_id}",
        )
    await bingo._dm_subjects(context, submission_id, members)
    await bingo._finalize(context, submission_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Add submitter confirm/resend, verification hand-off, confirm timeout"
```

---

### Task 7: Rolling replacement when a slot frees at verification

**Files:**
- Modify: `handlers/bingo.py` (`_finalize`, `_award`)
- Test: `tests/test_bingo_handlers.py`

**Interfaces:**
- Produces: after the existing tagged-people pipeline reaches a terminal outcome, it pulls the next queued submission into confirming. Concretely: at the end of `_finalize`'s fail branch (`status='failed'`) and at every terminal path in `_award`, call `await bingo_queue.maybe_kickoff(context)` (import `from handlers import bingo_queue` lazily inside the function to avoid a cycle).

> Rationale (approved spec decision): a tagged-people rejection or 12h verify-timeout frees the slot, so the next queued player is promoted. A win occupies its slot permanently (`verified` counts toward `active_slot_count`), so `maybe_kickoff` after `_award` is a safe no-op while 10 are in flight and correctly does nothing once the game closes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_handlers.py — append. Uses the file's existing temp-DB storage
# fixture; follow its established fixture name/style (shown here as `bingo_store`).

def test_failed_verification_promotes_next_queued(monkeypatch, bingo_store):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import storage
    from handlers import bingo, bingo_queue
    a = storage.queue_submission(1, "a", 1)
    storage.set_submission_status(a, "pending")            # in tagged-people verify
    storage.record_winning_members(a, [
        {"row": 0, "col": 0, "handle": "x", "prompt": "p", "target_user_id": None}])
    b = storage.queue_submission(2, "b", 1)                # queued behind
    bingo_queue._PENDING_READ[b] = {"read": {"cells": []}, "handle": "b", "sheet_no": 1}
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    ctx = MagicMock(); ctx.bot = AsyncMock(); ctx.job_queue = None
    asyncio.run(bingo._finalize(ctx, a, final=True))       # no answers -> fail
    assert storage.submission_status(a) == "failed"
    assert storage.submission_status(b) == "confirming"    # promoted
    bingo_queue._PENDING_READ.pop(b, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_handlers.py::test_failed_verification_promotes_next_queued -q`
Expected: FAIL — `b` stays `queued` (no kickoff hook yet).

- [ ] **Step 3: Add the hook**

In `handlers/bingo.py`, `_finalize`, in the `verdict != "pass"` branch, after `await _notify_submitter_failed(context, submission_id)` and before its `return`:

```python
        from handlers import bingo_queue
        await bingo_queue.maybe_kickoff(context)
        return
```

In `handlers/bingo.py`, `_award`: ensure every terminal path ends by promoting the next queued. The cleanest way is to add, as the final statement of the function (reached on the normal win path), and immediately before each early `return` (the already-won branch and the `claim_no is None` branch):

```python
        from handlers import bingo_queue
        await bingo_queue.maybe_kickoff(context)
```

Keep it minimal and correct — every `return` in `_award` must be preceded by this call (or refactored to fall through to a single trailing call).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_handlers.py tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo.py tests/test_bingo_handlers.py
git commit -m "Roll the queue forward when a verification slot frees"
```

---

### Task 8: Register handlers, route resends, facil close, re-arm on startup

**Files:**
- Modify: `handlers/bingo.py` (`on_bingo_text` routing, `register`, and the startup re-arm); `handlers/bingo_queue.py` (`register`, `close_round`, `rearm_confirm_timeouts`); `main.py` if it registers modules there.
- Test: `tests/test_bingo_queue.py`; `tests/test_bingo_handlers.py`

**Interfaces:**
- Produces:
  - `bingo_queue.register(app)` — `app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingoq:confirm:"))`.
  - `bingo_queue.close_round(context)` — `await maybe_kickoff(context)` (facil fallback: fire whoever's queued even if fewer than 10).
  - `bingo_queue.rearm_confirm_timeouts(app)` — for each `storage.confirming_submissions()`, re-arm `_confirm_timeout_job` using `submitted_at + BINGO_CONFIRM_TIMEOUT` (mirror `bingo.rearm_bingo_timeouts` clock math; floor delay at 5s).
  - `on_bingo_text` change: proceed when EITHER `awaiting_bingo_text` is set OR the user has a confirming submission; for a confirming user, parse and delegate to `bingo_queue.on_resend` and return; otherwise the fresh-submission path enqueues.
  - `bingo.close_bingo_round` — a thin `@facil_only` command calling `bingo_queue.close_round(context)` then replying "Kicked off the bingo queue. 🚀". Register `CommandHandler("close_bingo_round", close_bingo_round)`.
  - `bingo.register` also calls `bingo_queue.register(app)`; the startup path that calls `rearm_bingo_timeouts(app)` also calls `bingo_queue.rearm_confirm_timeouts(app)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py — append
def test_close_round_fires_with_fewer_than_ten(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    for uid in (1, 2, 3):
        fake.queue_submission(uid, f"u{uid}", 1)
    asyncio.run(bingo_queue.close_round(_ctx()))
    assert bingo_queue._send_confirmation.await_count == 3
    assert fake.active_slot_count() == 3
```

Also add a routing test in `tests/test_bingo_handlers.py`: a private text message from a user with a `confirming` submission (and `awaiting_bingo_text` unset) is delegated to `bingo_queue.on_resend` (monkeypatch `bingo_queue.on_resend` to an AsyncMock, assert awaited); a user with neither the flag nor a confirming submission is ignored (no reply, `on_resend` not awaited).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py::test_close_round_fires_with_fewer_than_ten -q`
Expected: FAIL — `close_round` undefined.

- [ ] **Step 3: Implement**

```python
# handlers/bingo_queue.py — add
from datetime import datetime
from telegram.ext import CallbackQueryHandler


async def close_round(context):
    """Facil fallback: process whoever is queued even if fewer than 10."""
    await maybe_kickoff(context)


def register(app):
    app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingoq:confirm:"))


def rearm_confirm_timeouts(app):
    """Re-arm the submitter-confirm 12h timeout for every 'confirming' submission
    after a restart (mirror bingo.rearm_bingo_timeouts' clock math)."""
    jq = app.job_queue
    if jq is None:
        return
    now = datetime.now(config.TIMEZONE)
    for sub in storage.confirming_submissions():
        try:
            submitted = datetime.fromisoformat(sub["submitted_at"])
        except (ValueError, KeyError, TypeError):
            submitted = now
        if submitted.tzinfo is None:
            from zoneinfo import ZoneInfo
            submitted = submitted.replace(tzinfo=ZoneInfo("Asia/Singapore"))
        delay = (submitted + config.BINGO_CONFIRM_TIMEOUT - now).total_seconds()
        jq.run_once(_confirm_timeout_job, when=max(delay, 5),
                    data={"submission_id": sub["id"]},
                    name=f"bingo:confirmwait:{sub['id']}")
```

In `handlers/bingo.py`, replace `on_bingo_text`'s opening guard so a confirming user is also served, and route them to `on_resend`:

```python
async def on_bingo_text(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return
    uid = update.effective_user.id
    awaiting = context.user_data.get("awaiting_bingo_text")
    confirming = any(s["submitter_user_id"] == uid
                     for s in storage.confirming_submissions())
    if not awaiting and not confirming:
        return                                     # not for us
    context.user_data["awaiting_bingo_text"] = False

    handle = sheets.normalize_handle(update.effective_user.username) or ""
    sheet_no = storage.get_bingo_sheet(uid)
    if sheet_no is None:
        await update.effective_message.reply_text(
            "Grab your card first with /get_bingo 🙂")
        return
    read = bingo_text.parse_submission(
        sheet_no, update.effective_message.text or "", _roster_index())
    if confirming:
        await bingo_queue.on_resend(context, uid, read)
        return
    # fresh first submission
    if storage.bingo_is_closed():
        await update.effective_message.reply_text(
            "All 10 prizes have been claimed — thanks for playing! 🎉")
        return
    wait = _cooldown_remaining(uid)
    if wait:
        await update.effective_message.reply_text(
            f"Hold on {wait}s before trying again 🙂")
        return
    await bingo_queue.enqueue(context, uid, handle, sheet_no, read)
```

Add the facil command (import `from utils.auth import facil_only` at the top of `handlers/bingo.py`):

```python
@facil_only
async def close_bingo_round(update, context):
    await bingo_queue.close_round(context)
    await update.effective_message.reply_text("Kicked off the bingo queue. 🚀")
```

In `bingo.register(app)`, add:

```python
    app.add_handler(CommandHandler("close_bingo_round", close_bingo_round))
    bingo_queue.register(app)
```

Find the startup call to `rearm_bingo_timeouts(app)` (in `main.py`'s post-init) and add alongside it:

```python
    bingo_queue.rearm_confirm_timeouts(app)
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (all existing + new).

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo.py handlers/bingo_queue.py main.py tests/
git commit -m "Register bingo queue handlers, route resends, facil close, re-arm"
```

---

## Self-Review

- **Spec coverage:** queue on submit (T5) ✓; kickoff at 10 (T5) ✓; short vs full by fully-recognised (T3,T4) ✓; unreachable flags (T3,T4) ✓; unlimited resends + confirm-timeout-only failure (T6) ✓; rolling replacement on fail/timeout (T6 confirm-phase, T7 verify-phase) ✓; fully-recognised = complete line + reachable (T3) ✓; pre-generated templates (T2) ✓; reuse tagged-people verify + prize (T6 hand-off, unchanged pipeline) ✓; fallback facil close (T8) ✓; one-live-submission-per-person (T1 dedup) ✓.
- **Approved open decision:** tagged-people rejection frees the slot → T7 `maybe_kickoff` in `_finalize` fail path. ✓
- **Status mapping consistency:** all tasks use DB values (`queued`/`confirming`/`pending`/`verified`/`failed`); the existing pipeline is untouched except the T7 hook. `active_slot_count` (T1) counts `confirming`+`pending`+`verified`, matching "confirming/verifying/won". ✓
- **Import-cycle discipline:** `bingo.py` imports `bingo_queue` at module level; `bingo_queue` imports `bingo` only lazily inside functions. ✓
- **Behavior change to flag to the user:** submitting now ALWAYS queues (the immediate "No bingo yet" reply is gone); incomplete cards get the full fill-in template during confirmation. This follows the spec ("no complete matched line" is a not-fully-recognised case) and the user's original wording ("if their text cannot be fully recognised … the full message template to fill up").
- **Test-harness note:** existing `tests/test_bingo_handlers.py` immediate-processing assertions are updated in T5/T7, not deleted. New pure-logic tests use a `FakeStore`; storage-integration tests use the file's real temp-DB fixture.
