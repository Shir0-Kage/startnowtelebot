# Bingo Submission Queue + Submitter-Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace immediate bingo processing with a submission queue where the 10 earliest submitters self-confirm/complete their card before the existing tagged-people check awards prizes, with rolling replacement.

**Architecture:** Submissions become a state machine on the existing `bingo_submissions` table (`queued → confirming → verifying → won | failed`). A new module `handlers/bingo_queue.py` owns the queue/kickoff/rolling logic; `handlers/bingo.py` submit paths route into it instead of `_process_read`; the confirmed-line path then calls the unchanged `_dm_subjects`/`_finalize`.

**Tech Stack:** python-telegram-bot v22, sqlite3 (WAL), pytest (asyncio.run, no plugin), existing `bingo_lines`, `bingo_text`, `data.bingo_templates`.

## Global Constraints

- Python 3.12; python-telegram-bot[job-queue] >=21.4,<23.
- No hardcoded secrets; commits authored human-style, NO `Co-Authored-By` trailer.
- Prize cap = `config.BINGO_PRIZE_LIMIT` (10). Confirm timeout = `config.BINGO_CONFIRM_TIMEOUT` (12h).
- All Google-Sheets/roster loads run OFF the event loop (`asyncio.to_thread`); OCR stays in the `ocr_worker.py` subprocess. Never add a blocking fetch on the loop.
- "Fully recognised" = a complete 5-in-a-row of matched roster handles where every tagged person is reachable (`storage.user_id_for_handle(handle)` is not None).
- Tests run offline: monkeypatch `sheets.load_year1_members`, PTB bot calls, and job queue.

## File Structure

- `storage.py` (modify) — queue state helpers on `bingo_submissions`.
- `bingo_text.py` (modify) — cache the 15 blank templates; add `build_line_confirm_text`.
- `handlers/bingo_queue.py` (create) — enqueue, kickoff-at-10, per-submitter message selection (short/full + unreachable flags), submitter-confirm + resend handling, timeout + rolling replacement, hand-off to tagged-people verify.
- `handlers/bingo.py` (modify) — submit paths call `bingo_queue.enqueue(...)` instead of `_process_read`; register new handlers/jobs; expose `_matched_and_prompts`, `_dm_subjects`, `_finalize` for `bingo_queue`.
- `tests/test_bingo_queue.py` (create), `tests/test_bingo_text.py` (modify), `tests/test_bingo_handlers.py` (modify).

---

### Task 1: Queue state helpers in storage

**Files:**
- Modify: `storage.py`
- Test: `tests/test_bingo_queue_storage.py` (create)

**Interfaces:**
- Produces:
  - `queue_submission(user_id, handle, sheet_no) -> int` — deletes the user's existing non-terminal submission, inserts one with status `'queued'`, returns id.
  - `queued_in_order() -> list[dict]` — status `'queued'`, ordered by `submitted_at, id`.
  - `active_slot_count() -> int` — count of status in (`'confirming'`, `'verifying'`, `'won'`).
  - `confirming_submissions() -> list[dict]` — status `'confirming'`, ordered by `submitted_at, id`.
  - `submission_status(submission_id) -> str | None`.
  - (reuses existing `set_submission_status`, `submission_by_id`, `record_winning_members`, `winning_members`, `user_id_for_handle`.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue_storage.py
import importlib
import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "q.db"))
    import storage
    importlib.reload(storage)
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "q.db"))
    storage.init_db()
    return storage


def test_queue_orders_by_time_and_dedupes_per_user(store):
    a = store.queue_submission(1, "alice", 3)
    b = store.queue_submission(2, "bob", 3)
    a2 = store.queue_submission(1, "alice", 3)   # same user re-submits
    ordered = store.queued_in_order()
    ids = [r["id"] for r in ordered]
    assert b in ids and a2 in ids and a not in ids   # first row for user 1 replaced
    assert ids.index(a2) > ids.index(b)              # b queued earlier stays earlier... see note
    assert store.active_slot_count() == 0


def test_active_slot_count_counts_nonqueued_nonfailed(store):
    s = store.queue_submission(1, "alice", 3)
    assert store.active_slot_count() == 0
    store.set_submission_status(s, "confirming")
    assert store.active_slot_count() == 1
    store.set_submission_status(s, "failed")
    assert store.active_slot_count() == 0
```

> Ordering note: because a re-submit replaces the row, `a2` gets a *new*
> `submitted_at`. If you want a re-submit to keep the user's original queue
> position, carry the old `submitted_at` forward in `queue_submission` (see Step
> 3). The test above assumes carry-forward is NOT done (new time); adjust the
> assertion if you implement carry-forward.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue_storage.py -q`
Expected: FAIL — `AttributeError: module 'storage' has no attribute 'queue_submission'`.

- [ ] **Step 3: Implement the helpers**

```python
# storage.py — add near the other bingo functions
def queue_submission(user_id, handle, sheet_no):
    """Replace the user's existing non-terminal submission and enqueue a new one."""
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_submissions "
            "WHERE submitter_user_id = ? AND status IN ('queued','confirming','verifying')",
            (user_id,),
        )
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, 'queued', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, _now_iso()),
        )
        _conn.commit()
        return cur.lastrowid


def queued_in_order():
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'queued' "
            "ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def confirming_submissions():
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'confirming' "
            "ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def active_slot_count():
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('confirming','verifying','won')"
        ).fetchone()
    return row["c"]


def submission_status(submission_id):
    with _lock:
        row = _conn.execute(
            "SELECT status FROM bingo_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return row["status"] if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue_storage.py -q`
Expected: PASS (adjust the ordering assertion per the note if needed).

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_bingo_queue_storage.py
git commit -m "Add bingo submission-queue state helpers to storage"
```

---

### Task 2: Pre-generate blank templates + winning-line confirm text

**Files:**
- Modify: `bingo_text.py`
- Test: `tests/test_bingo_text.py`

**Interfaces:**
- Produces:
  - `build_template_text(sheet_no)` now returns a cached string (pre-generated for all 15 sheets at import).
  - `build_line_confirm_text(sheet_no, line) -> str` — `line` is a list of `(row, col, handle)`; renders the 4–5 winning-line cells as `R{r}C{c}: {prompt} - @{handle}` for the short confirm message.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_text.py — add
import bingo_text


def test_blank_templates_are_pregenerated_and_cached():
    a = bingo_text.build_template_text(3)
    b = bingo_text.build_template_text(3)
    assert a is b                       # same cached object, not rebuilt
    assert bingo_text._TEMPLATE_CACHE   # populated at import


def test_build_line_confirm_text_lists_only_the_line():
    line = [(0, 0, "alice"), (0, 1, "bob"), (0, 3, "dan"), (0, 4, "eve")]
    out = bingo_text.build_line_confirm_text(1, line)
    assert out.count("\n") == 3         # 4 cells -> 4 lines
    assert "R1C1:" in out and "@alice" in out and "@eve" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_text.py -q`
Expected: FAIL — `AttributeError: module 'bingo_text' has no attribute '_TEMPLATE_CACHE'`.

- [ ] **Step 3: Implement caching + line confirm**

```python
# bingo_text.py — replace build_template_text; add cache + build_line_confirm_text
def _build_template_text(sheet_no):
    lines = []
    for row in range(templates.GRID):
        for col in range(templates.GRID):
            if templates.is_free(row, col):
                continue
            lines.append(f"R{row + 1}C{col + 1}: {templates.prompt_for(sheet_no, row, col)} - ")
    return "\n".join(lines)


_TEMPLATE_CACHE = {n: _build_template_text(n) for n in templates.SHEETS}


def build_template_text(sheet_no):
    """The cached fill-in-the-blank list for a sheet (pre-generated at import)."""
    return _TEMPLATE_CACHE[sheet_no]


def build_line_confirm_text(sheet_no, line):
    """Render just the winning line's cells for the short confirm message.
    `line`: list of (row, col, handle), 0-indexed."""
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

### Task 3: Recognition helper — classify a read as fully-recognised or not

**Files:**
- Create: `handlers/bingo_queue.py`
- Test: `tests/test_bingo_queue.py` (create)

**Interfaces:**
- Consumes: `bingo_lines.winning_lines`, `bingo.pick_best_line` (via `bingo_lines.pick_best_line`), `storage.user_id_for_handle`.
- Produces:
  - `evaluate(read, submitter_handle, sheet_no) -> dict` with keys:
    `{"line": [(r,c,h),...] | None, "fully_recognised": bool, "unreachable": [handle,...]}`.
    `fully_recognised` is True iff a winning line exists AND every handle in it is reachable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py
import bingo_lines
from handlers import bingo_queue


def _cells(matched):
    cells = []
    for r in range(5):
        for c in range(5):
            if (r, c) == (2, 2):
                continue
            cells.append({"row": r, "col": c, "handle": matched.get((r, c)), "score": 100.0 if matched.get((r, c)) else 0.0})
    return cells


def test_evaluate_fully_recognised_when_line_all_reachable(monkeypatch):
    matched = {(0, 0): "alice", (0, 1): "bob", (0, 3): "dan", (0, 4): "eve"}
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)  # all reachable
    res = bingo_queue.evaluate({"cells": _cells(matched)}, "submitter", sheet_no=1)
    assert res["fully_recognised"] is True
    assert res["line"] is not None and res["unreachable"] == []


def test_evaluate_flags_unreachable(monkeypatch):
    matched = {(0, 0): "alice", (0, 1): "bob", (0, 3): "dan", (0, 4): "eve"}
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: None if h == "dan" else 1)
    res = bingo_queue.evaluate({"cells": _cells(matched)}, "submitter", sheet_no=1)
    assert res["fully_recognised"] is False
    assert res["unreachable"] == ["dan"]


def test_evaluate_no_line(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)
    res = bingo_queue.evaluate({"cells": _cells({(0, 0): "alice"})}, "submitter", sheet_no=1)
    assert res["line"] is None and res["fully_recognised"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.bingo_queue'`.

- [ ] **Step 3: Implement `evaluate`**

```python
# handlers/bingo_queue.py
"""Bingo submission queue: enqueue, kickoff-at-10, submitter self-confirm, and
rolling replacement, ahead of the existing tagged-people verification."""

import logging

import bingo_lines as lines
import config
import storage
from setup import sheets

log = logging.getLogger(__name__)


def evaluate(read, submitter_handle, sheet_no):
    """Classify a parsed read. Returns {"line", "fully_recognised", "unreachable"}."""
    from handlers.bingo import _matched_and_prompts
    matched, _prompts = _matched_and_prompts(read.get("cells", []), submitter_handle, sheet_no)
    candidate = lines.winning_lines(matched, submitter_handle)
    if not candidate:
        return {"line": None, "fully_recognised": False, "unreachable": []}
    line = lines.pick_best_line(candidate)
    unreachable = [h for (_r, _c, h) in line if storage.user_id_for_handle(h) is None]
    return {"line": line, "fully_recognised": not unreachable, "unreachable": unreachable}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Add bingo_queue.evaluate: classify a read (line/reachability)"
```

---

### Task 4: Enqueue on submit ("you're in the queue") + kickoff at 10

**Files:**
- Modify: `handlers/bingo_queue.py`, `handlers/bingo.py`
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `storage.queue_submission`, `storage.queued_in_order`, `storage.active_slot_count`, `storage.set_submission_status`, `bingo_queue.evaluate`, `bingo_queue._send_confirmation` (Task 5).
- Produces:
  - `async enqueue(context, uid, handle, sheet_no, read) -> None` — stores the read for the user, calls `storage.queue_submission`, replies "you're in the queue (#N)", then `await maybe_kickoff(context)`.
  - `async maybe_kickoff(context) -> None` — while there are queued submissions AND `active_slot_count() < BINGO_PRIZE_LIMIT`, promote the earliest queued to `confirming` and `await _send_confirmation(...)`. (Naturally fires the batch of 10 once the 10th arrives, and pulls in the next when a slot frees.)
- Stores each user's latest parsed read in module dict `_PENDING_READ[submission_id] = {"read", "handle", "sheet_no"}` (needed later by confirm/resend/finalize).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py — add
import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_kickoff_sends_to_ten_earliest_only(monkeypatch, store_like):
    # store_like: a fake storage with an in-memory queue (see fixture below)
    sent = []
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock(side_effect=lambda ctx, sub: sent.append(sub["id"])))
    monkeypatch.setattr(bingo_queue, "storage", store_like)
    ctx = MagicMock(); ctx.bot = AsyncMock()
    for uid in range(1, 13):   # 12 queue
        store_like.queue_submission(uid, f"u{uid}", 1)
    asyncio.run(bingo_queue.maybe_kickoff(ctx))
    assert len(sent) == 10                      # only 10 slots
    assert store_like.active_slot_count() == 10
```

Add a small in-memory `store_like` fixture in the test file that implements
`queue_submission`, `queued_in_order`, `active_slot_count`, `set_submission_status`,
`submission_status` over a list of dicts.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py::test_kickoff_sends_to_ten_earliest_only -q`
Expected: FAIL — `AttributeError: ... 'maybe_kickoff'`.

- [ ] **Step 3: Implement enqueue + maybe_kickoff**

```python
# handlers/bingo_queue.py — add
_PENDING_READ = {}   # submission_id -> {"read", "handle", "sheet_no"}


async def enqueue(context, uid, handle, sheet_no, read):
    submission_id = storage.queue_submission(uid, handle, sheet_no)
    _PENDING_READ[submission_id] = {"read": read, "handle": handle, "sheet_no": sheet_no}
    position = len(storage.queued_in_order())
    await context.bot.send_message(
        chat_id=uid,
        text=f"You're in the queue (#{position}) 📥 — I'll message you if I need "
             "you to confirm your squares. Hang tight!",
    )
    await maybe_kickoff(context)


async def maybe_kickoff(context):
    while storage.active_slot_count() < config.BINGO_PRIZE_LIMIT:
        queued = storage.queued_in_order()
        if not queued:
            return
        sub = queued[0]
        storage.set_submission_status(sub["id"], "confirming")
        await _send_confirmation(context, sub)
```

Then in `handlers/bingo.py`, change both submit tails to enqueue instead of
`_process_read`. In `_process_read`'s two callers (`bingo_ocr_confirm_button`
"yes" branch and `on_bingo_text`), replace `await _process_read(...)` with:

```python
from handlers import bingo_queue
await bingo_queue.enqueue(context, uid, handle, sheet_no, read)
```

Keep `_process_read`'s "no winning line" reply for the from-scratch text path
(a submission with no line should still be told "no bingo yet"); only enqueue
when `bingo_queue.evaluate(read, handle, sheet_no)["line"]` is not None.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py handlers/bingo.py tests/test_bingo_queue.py
git commit -m "Queue bingo submissions and kick off the earliest 10"
```

---

### Task 5: Send the confirmation message (short vs full + unreachable flags)

**Files:**
- Modify: `handlers/bingo_queue.py`
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `bingo_queue.evaluate`, `bingo_text.build_line_confirm_text`, `bingo_text.build_prefilled_text`, `_PENDING_READ`.
- Produces:
  - `async _send_confirmation(context, sub) -> None` — looks up the user's pending read, evaluates it; if fully recognised → send short (line + ✅ Confirm button, callback `bingoq:confirm:<id>`); else → send the full prefilled list + an "ask these people to /start" flag for any unreachable handles.
  - `_confirm_keyboard(submission_id)` — one ✅ Confirm button.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_queue.py — add
def test_confirmation_short_when_fully_recognised(monkeypatch, store_like):
    monkeypatch.setattr(bingo_queue, "storage", store_like)
    sid = store_like.queue_submission(1, "submitter", 1)
    bingo_queue._PENDING_READ[sid] = {"read": {"cells": _cells({(0,0):"alice",(0,1):"bob",(0,3):"dan",(0,4):"eve"})}, "handle": "submitter", "sheet_no": 1}
    monkeypatch.setattr(store_like, "user_id_for_handle", lambda h: 1, raising=False)
    ctx = MagicMock(); ctx.bot = AsyncMock()
    asyncio.run(bingo_queue._send_confirmation(ctx, {"id": sid, "submitter_user_id": 1}))
    text = ctx.bot.send_message.await_args.kwargs["text"]
    assert "confirm" in text.lower() and "@alice" in text
    assert ctx.bot.send_message.await_args.kwargs.get("reply_markup") is not None


def test_confirmation_full_flags_unreachable(monkeypatch, store_like):
    monkeypatch.setattr(bingo_queue, "storage", store_like)
    sid = store_like.queue_submission(1, "submitter", 1)
    bingo_queue._PENDING_READ[sid] = {"read": {"cells": _cells({(0,0):"alice",(0,1):"bob",(0,3):"dan",(0,4):"eve"})}, "handle": "submitter", "sheet_no": 1}
    monkeypatch.setattr(store_like, "user_id_for_handle", lambda h: None if h == "dan" else 1, raising=False)
    ctx = MagicMock(); ctx.bot = AsyncMock()
    asyncio.run(bingo_queue._send_confirmation(ctx, {"id": sid, "submitter_user_id": 1}))
    text = ctx.bot.send_message.await_args.kwargs["text"]
    assert "@dan" in text and "/start" in text     # flagged unreachable person
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `_send_confirmation` undefined.

- [ ] **Step 3: Implement `_send_confirmation`**

```python
# handlers/bingo_queue.py — add
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import bingo_text


def _confirm_keyboard(submission_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"bingoq:confirm:{submission_id}")
    ]])


async def _send_confirmation(context, sub):
    sid = sub["id"]
    uid = sub["submitter_user_id"]
    pending = _PENDING_READ.get(sid)
    if pending is None:
        return
    res = evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if res["fully_recognised"]:
        text = ("You're up! 🎉 Here's your winning line — tap Confirm if it's "
                "right:\n\n" + bingo_text.build_line_confirm_text(pending["sheet_no"], res["line"]))
        await context.bot.send_message(chat_id=uid, text=text, reply_markup=_confirm_keyboard(sid))
    else:
        preview = bingo_text.build_prefilled_text(pending["sheet_no"], pending["read"].get("cells", []))
        flag = ""
        if res["unreachable"]:
            who = ", ".join(f"@{h}" for h in res["unreachable"])
            flag = (f"\n\n⚠️ {who} hasn't started the bot yet — ask them to send it "
                    "/start so I can verify them, then resend your list.")
        await context.bot.send_message(
            chat_id=uid,
            text="You're up! Fill in the @handles below (fix the blanks) and send "
                 "the whole list back:\n\n" + preview + flag,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Send short/full bingo confirmation with unreachable flags"
```

---

### Task 6: Submitter confirm + resend, arming the timeout

**Files:**
- Modify: `handlers/bingo_queue.py`, `handlers/bingo.py`
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `_PENDING_READ`, `evaluate`, `storage.record_winning_members`, `storage.set_submission_status`, `bingo._dm_subjects`, `bingo._finalize`.
- Produces:
  - `async confirm_button(update, context)` — handles `bingoq:confirm:<id>`; only if status is `confirming`; records the line and calls `_start_verification`.
  - `async on_resend(context, uid, read)` — a `confirming` user resent their text; re-evaluate; if fully recognised → `_start_verification`; else → re-send full + flags.
  - `async _start_verification(context, sub, line, handle, sheet_no)` — status → `verifying`, `record_winning_members`, arm the confirm-timeout job, `await bingo._dm_subjects(...)`, `await bingo._finalize(...)`.
  - A confirm-timeout job callback `_confirm_timeout_job` that, if still `confirming`, marks `failed` and calls `maybe_kickoff`.

- [ ] **Step 1: Write the failing test** (confirm → verification started)

```python
def test_confirm_button_starts_verification(monkeypatch, store_like):
    monkeypatch.setattr(bingo_queue, "storage", store_like)
    monkeypatch.setattr(store_like, "user_id_for_handle", lambda h: 1, raising=False)
    sid = store_like.queue_submission(1, "submitter", 1)
    store_like.set_submission_status(sid, "confirming")
    bingo_queue._PENDING_READ[sid] = {"read": {"cells": _cells({(0,0):"alice",(0,1):"bob",(0,3):"dan",(0,4):"eve"})}, "handle": "submitter", "sheet_no": 1}
    started = {}
    monkeypatch.setattr(bingo_queue, "_start_verification", AsyncMock(side_effect=lambda *a, **k: started.setdefault("x", True)))
    q = AsyncMock(); q.data = f"bingoq:confirm:{sid}"; q.from_user = MagicMock(id=1)
    upd = MagicMock(); upd.callback_query = q
    ctx = MagicMock(); ctx.bot = AsyncMock()
    asyncio.run(bingo_queue.confirm_button(upd, ctx))
    assert started.get("x") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py::test_confirm_button_starts_verification -q`
Expected: FAIL — `confirm_button` undefined.

- [ ] **Step 3: Implement confirm/resend/verification/timeout**

```python
# handlers/bingo_queue.py — add
async def confirm_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, _, sid_s = query.data.split(":")
        sid = int(sid_s)
    except (ValueError, AttributeError):
        return
    if storage.submission_status(sid) != "confirming":
        return
    pending = _PENDING_READ.get(sid)
    if pending is None:
        return
    res = evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if not res["fully_recognised"]:
        return  # stale; the full-template path handles this
    await _start_verification(context, sid, res["line"], pending["handle"], pending["sheet_no"])


async def on_resend(context, uid, read):
    subs = [s for s in storage.confirming_submissions() if s["submitter_user_id"] == uid]
    if not subs:
        return False
    sub = subs[0]
    pending = _PENDING_READ.setdefault(sub["id"], {})
    pending.update(read=read, handle=pending.get("handle") or "", sheet_no=sub["sheet_no"])
    res = evaluate(read, pending["handle"], sub["sheet_no"])
    if res["fully_recognised"]:
        await _start_verification(context, sub["id"], res["line"], pending["handle"], sub["sheet_no"])
    else:
        await _send_confirmation(context, sub)   # re-send full + flags
    return True


async def _start_verification(context, sid, line, handle, sheet_no):
    from handlers import bingo
    from data import bingo_templates as templates
    storage.set_submission_status(sid, "verifying")
    members = [{"row": r, "col": c, "handle": h,
                "prompt": templates.prompt_for(sheet_no, r, c),
                "target_user_id": storage.user_id_for_handle(h)} for (r, c, h) in line]
    storage.record_winning_members(sid, members)
    if context.job_queue is not None:
        context.job_queue.run_once(
            _confirm_timeout_job, when=config.BINGO_CONFIRM_TIMEOUT,
            data={"submission_id": sid}, name=f"bingo:timeout:{sid}")
    await bingo._dm_subjects(context, sid, members)
    await bingo._finalize(context, sid)


async def _confirm_timeout_job(context):
    sid = context.job.data["submission_id"]
    if storage.submission_status(sid) == "confirming":
        storage.set_submission_status(sid, "failed")
        await maybe_kickoff(context)
```

Note: the confirm-*timeout* here fires while `confirming` (submitter silent).
`bingo._finalize` already handles the tagged-people 12h path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Submitter confirm/resend + verification hand-off + confirm timeout"
```

---

### Task 7: Rolling replacement when a submission fails at verification

**Files:**
- Modify: `handlers/bingo.py` (`_finalize`/`_award` fail path), `handlers/bingo_queue.py`
- Test: `tests/test_bingo_queue.py`, `tests/test_bingo_handlers.py`

**Interfaces:**
- Produces: when `bingo._finalize` marks a submission `failed` (tagged-people rejected, per the approved decision), it calls `bingo_queue.maybe_kickoff(context)` to pull in the next queued submission. `won` also calls `maybe_kickoff` (a slot may free if under cap — no-op once 10 won because active_slot_count includes `won`).

- [ ] **Step 1: Write the failing test**

```python
def test_failed_verification_promotes_next(monkeypatch, store_like):
    monkeypatch.setattr(bingo_queue, "storage", store_like)
    a = store_like.queue_submission(1, "a", 1); store_like.set_submission_status(a, "verifying")
    b = store_like.queue_submission(2, "b", 1)   # queued behind
    bingo_queue._PENDING_READ[b] = {"read": {"cells": _cells({})}, "handle": "b", "sheet_no": 1}
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    ctx = MagicMock(); ctx.bot = AsyncMock()
    store_like.set_submission_status(a, "failed")           # simulate verify fail
    asyncio.run(bingo_queue.maybe_kickoff(ctx))
    assert store_like.submission_status(b) == "confirming"  # next pulled in
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py::test_failed_verification_promotes_next -q`
Expected: FAIL (until the fail path calls maybe_kickoff / until statuses wired).

- [ ] **Step 3: Wire the fail/won paths**

In `handlers/bingo.py`, in the branch of `_finalize` that sets a submission to
`failed` (verdict != pass) and in `_award` after a `won`/close, add:

```python
from handlers import bingo_queue
await bingo_queue.maybe_kickoff(context)
```

(Guard the import to avoid a cycle: import inside the function.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py tests/test_bingo_handlers.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo.py handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Roll the queue forward when a submission fails or wins"
```

---

### Task 8: Register handlers/jobs, wire submit paths, re-arm on startup, fallback close

**Files:**
- Modify: `handlers/bingo.py` (`register`, `on_bingo_text`, `bingo_ocr_confirm_button`, `rearm_bingo_timeouts`), `handlers/bingo_queue.py`
- Test: `tests/test_bingo_handlers.py`

**Interfaces:**
- Produces:
  - `bingo_queue.register(app)` adds `CallbackQueryHandler(confirm_button, pattern=r"^bingoq:confirm:")`.
  - `on_bingo_text`: if the user has a `confirming` submission → route to `bingo_queue.on_resend`; else the existing first-submission path (which now enqueues on a valid line).
  - `bingo_queue.close_round(context)` — facil-triggered fallback: run `maybe_kickoff` even with < 10 queued. Wire a facil command `/close_bingo_round` in `bingo.register` guarded by `@facil_only`.
  - `rearm_bingo_timeouts` also re-arms confirm-timeout jobs for `confirming` submissions on startup.

- [ ] **Step 1: Write the failing test**

```python
def test_close_round_fires_with_fewer_than_ten(monkeypatch, store_like):
    monkeypatch.setattr(bingo_queue, "storage", store_like)
    for uid in (1, 2, 3):
        store_like.queue_submission(uid, f"u{uid}", 1)
    sent = []
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock(side_effect=lambda ctx, sub: sent.append(sub["id"])))
    ctx = MagicMock(); ctx.bot = AsyncMock()
    asyncio.run(bingo_queue.close_round(ctx))
    assert len(sent) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py::test_close_round_fires_with_fewer_than_ten -q`
Expected: FAIL — `close_round` undefined.

- [ ] **Step 3: Implement registration + routing + close + re-arm**

```python
# handlers/bingo_queue.py — add
from telegram.ext import CallbackQueryHandler


async def close_round(context):
    """Facil fallback: process whoever is queued even if fewer than 10."""
    await maybe_kickoff(context)


def register(app):
    app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingoq:confirm:"))
```

```python
# handlers/bingo.py — on_bingo_text: route resend vs first submission
async def on_bingo_text(update, context):
    ...
    uid = update.effective_user.id
    from handlers import bingo_queue
    if any(s["submitter_user_id"] == uid for s in storage.confirming_submissions()):
        await bingo_queue.on_resend(context, uid, bingo_text.parse_submission(
            sheet_no, update.effective_message.text or "", _roster_index()))
        return
    # else: first submission -> parse, and if it has a line, enqueue (Task 4)
```

Wire `bingo_queue.register(app)` in `main.py`'s feature registration, and a
facil `/close_bingo_round` command in `bingo.register`. In
`rearm_bingo_timeouts`, for each `confirming` submission re-arm
`_confirm_timeout_job`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (all existing + new).

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo.py handlers/bingo_queue.py main.py tests/
git commit -m "Register bingo queue handlers, route resends, add facil close + re-arm"
```

---

## Self-Review

- **Spec coverage:** on-submit queue (T4) ✓; kickoff at 10 (T4) ✓; short/full + reachability (T3,T5) ✓; unlimited resends + confirm-timeout-only fail (T6) ✓; rolling replacement (T6,T7) ✓; fully-recognised = line + reachable (T3) ✓; pre-generated templates (T2) ✓; tagged-people verify + prize reuse (T6) ✓; fallback close (T8) ✓.
- **Open decision (from spec):** tagged-people rejection frees the slot → implemented in T7 (approved default). If the user wants otherwise, drop the `maybe_kickoff` call in the fail path.
- **Type consistency:** `evaluate` returns `{"line","fully_recognised","unreachable"}` used identically in T4/T5/T6; `_PENDING_READ[sid] = {"read","handle","sheet_no"}` consistent; callback prefix `bingoq:confirm:` consistent T5/T6/T8.
- **Reachability off-loop note:** `_roster_index()` is still called on the loop in `on_bingo_text` (pre-existing). Consider wrapping in `asyncio.to_thread` in T8 (optional, matches the codebase discipline).
