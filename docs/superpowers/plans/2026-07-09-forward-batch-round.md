# Forward-Based Batch Prize Round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let players **forward** their original bingo cards to the bot; collect for a window (20 forwarded or 2 days), verify the earliest-by-original-time entries via the existing tagged-people check, and release all winner results at once (winners + a summary to @zzehao).

**Architecture:** A self-contained `handlers/bingo_forward.py` owns the round via a game-wide phase (`collecting → verifying → released`) in `bingo_flags`. Forward entries use dedicated statuses (`fwd_confirming`, `ready`) so they never touch the live `/submit_bingo` queue. The round **reuses** `bingo_queue.evaluate`, `bingo_text` builders, the isolated `_run_ocr`, and the tagged-people verification/prize machinery. `_award` gains one hold-and-batch branch guarded by `storage.forward_batch_active()`.

**Tech Stack:** python-telegram-bot v22 (JobQueue, `message.forward_origin`), sqlite3, pytest via `asyncio.run`.

## Global Constraints

- Python 3.12; tests `.venv/Scripts/python.exe -m pytest`; Git Bash. Human-style commits, NO `Co-Authored-By`/AI trailer. Before committing, `git status --short`; restore any deleted `__init__.py` with `git checkout HEAD -- <path>`.
- Storage follows the module idiom (`with _lock:`, `_now_iso()`, `_conn.commit()`, `dict(row)`); model flags on `set_bingo_closed`/`bingo_is_closed`. New `status` values `fwd_confirming`/`ready` are free-text (no schema change).
- **Isolation:** `fwd_confirming` and `ready` MUST be excluded from `active_slot_count`, `queued_in_order`, `confirming_submissions`, `pending_submissions` (the live queue must never see forward rows, and forward logic must never call the live `maybe_kickoff`).
- **Reuse, don't reimplement:** `bingo_queue.evaluate(read, handle, sheet_no)`, `bingo_text.build_line_confirm_text`/`build_prefilled_text`, `bingo._run_ocr(sheet_no, image_bytes)`, `bingo._dm_subjects`/`_finalize`/`_confirmation_timeout`/`_cancel_job`, `storage.record_winning_members`/`claim_bingo_prize`/`winning_members`. Forward round uses its OWN callback prefix `bingofwd:` and its OWN in-memory `_PENDING_READ` dict (do not touch `bingo_queue._PENDING_READ`).
- Config: `FORWARD_ROUND_TARGET = 20`, `FORWARD_ROUND_WINDOW = timedelta(days=2)`.
- Assumes the round is run with no prior/concurrent live winners (the current live state is 0 submissions, 0 winners); the batch release announces all `bingo_prizes` rows.

## File Structure

- `config.py` (modify) — the two constants.
- `storage.py` (modify) — forward phase flags, `queue_forwarded_submission`, `set_forward_ready`, `ready_in_order`, `forward_entry_count`, `forward_phase`/`set_forward_phase`, `forward_batch_active`, `active_forward_verifying_count`.
- `handlers/bingo_forward.py` (create) — the whole round.
- `handlers/bingo.py` (modify) — `_award` hold branch; register command + forwarded-image handler; re-arm forward timer at startup.
- Tests: `tests/test_bingo_storage.py`, `tests/test_bingo_forward.py` (create), `tests/test_bingo_handlers.py`.

---

### Task 1: Config + storage — forward phase, statuses, helpers

**Files:**
- Modify: `config.py`, `storage.py`
- Test: `tests/test_bingo_storage.py` (append, reuse `store`)

**Interfaces (Produces):**
- `config.FORWARD_ROUND_TARGET = 20`; `config.FORWARD_ROUND_WINDOW = timedelta(days=2)`.
- `storage.set_forward_phase(phase)` — record phase (`'collecting'`/`'verifying'`/`'released'`) as a `bingo_flags` row named `forward_{phase}` (INSERT OR IGNORE, so `set_at` of `forward_collecting` marks the start).
- `storage.forward_phase() -> str | None` — the furthest phase set (`released` > `verifying` > `collecting` > None).
- `storage.forward_started_at() -> str | None` — `set_at` of the `forward_collecting` flag (for the 2-day timer).
- `storage.forward_batch_active() -> bool` — True while phase is `collecting` or `verifying`.
- `storage.queue_forwarded_submission(user_id, handle, sheet_no, submitted_at) -> int` — delete the user's existing `fwd_confirming`/`ready` rows, INSERT a row with the GIVEN `submitted_at`, status `fwd_confirming`; return id.
- `storage.set_forward_ready(submission_id)` — status → `ready`.
- `storage.ready_in_order() -> list[dict]` — status `ready`, `ORDER BY submitted_at, id`.
- `storage.forward_entry_count() -> int` — count of rows with status in (`fwd_confirming`,`ready`).
- `storage.active_forward_verifying_count() -> int` — count of rows with status in (`pending`,`verified`) (forward entries in/through verification, for the 10-slot cap during the round).

> Note on `active_forward_verifying_count`: during a forward round there are no live-queue rows (isolation), so counting `pending`+`verified` is the forward round's in-flight/won count. It is used only by `bingo_forward.kickoff_verification`, never by the live `maybe_kickoff`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_bingo_storage.py`, reuse `store`)

```python
def test_forward_phase_progression(store):
    assert store.forward_phase() is None
    store.set_forward_phase("collecting")
    assert store.forward_phase() == "collecting"
    assert store.forward_batch_active() is True
    assert store.forward_started_at() is not None
    store.set_forward_phase("verifying")
    assert store.forward_phase() == "verifying"
    assert store.forward_batch_active() is True
    store.set_forward_phase("released")
    assert store.forward_phase() == "released"
    assert store.forward_batch_active() is False


def test_queue_forwarded_submission_uses_given_time_and_dedups(store):
    a = store.queue_forwarded_submission(1, "alice", 3, "2026-01-01T09:00:00")
    a2 = store.queue_forwarded_submission(1, "alice", 3, "2026-01-01T10:00:00")
    rows = store.ready_in_order()
    assert rows == []                                  # none ready yet
    assert store.submission_status(a) is None          # replaced
    assert store.submission_by_id(a2)["submitted_at"] == "2026-01-01T10:00:00"
    assert store.forward_entry_count() == 1


def test_ready_ordering_and_isolation_from_live_queue(store):
    b = store.queue_forwarded_submission(2, "bob", 1, "2026-01-01T08:00:00")
    a = store.queue_forwarded_submission(1, "alice", 1, "2026-01-01T07:00:00")
    store.set_forward_ready(a); store.set_forward_ready(b)
    assert [r["submitter_user_id"] for r in store.ready_in_order()] == [1, 2]  # earliest first
    # forward rows never leak into the live-queue views:
    assert store.queued_in_order() == []
    assert store.confirming_submissions() == []
    assert store.active_slot_count() == 0
    assert store.forward_entry_count() == 2
```

- [ ] **Step 2: Run to verify fail** — `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q` → FAIL (helpers undefined).

- [ ] **Step 3: Implement.** In `config.py` (Human Bingo section) add the two constants. In `storage.py` add:

```python
def set_forward_phase(phase):
    """Record the forward round phase ('collecting'|'verifying'|'released')."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES (?, ?)",
            (f"forward_{phase}", _now_iso()),
        )
        _conn.commit()


def forward_phase():
    with _lock:
        rows = {r["name"] for r in _conn.execute(
            "SELECT name FROM bingo_flags WHERE name LIKE 'forward_%'")}
    for phase in ("released", "verifying", "collecting"):
        if f"forward_{phase}" in rows:
            return phase
    return None


def forward_started_at():
    with _lock:
        row = _conn.execute(
            "SELECT set_at FROM bingo_flags WHERE name = 'forward_collecting'").fetchone()
    return row["set_at"] if row else None


def forward_batch_active():
    return forward_phase() in ("collecting", "verifying")


def queue_forwarded_submission(user_id, handle, sheet_no, submitted_at):
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_submissions WHERE submitter_user_id = ? "
            "AND status IN ('fwd_confirming','ready')", (user_id,))
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, corner_read, "
            " status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, NULL, 'fwd_confirming', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, submitted_at))
        _conn.commit()
        return cur.lastrowid


def set_forward_ready(submission_id):
    with _lock:
        _conn.execute(
            "UPDATE bingo_submissions SET status = 'ready' WHERE id = ?",
            (submission_id,))
        _conn.commit()


def ready_in_order():
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'ready' "
            "ORDER BY submitted_at, id").fetchall()
    return [dict(r) for r in rows]


def forward_entry_count():
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('fwd_confirming','ready')").fetchone()
    return row["c"]


def active_forward_verifying_count():
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('pending','verified')").fetchone()
    return row["c"]
```

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `git add config.py storage.py tests/test_bingo_storage.py` / `git commit -m "Add forward-round phase flags, statuses, and storage helpers"`.

---

### Task 2: Forwarded-card handler (read original time → OCR → queue → confirm)

**Files:**
- Create: `handlers/bingo_forward.py`
- Test: `tests/test_bingo_forward.py` (create)

**Interfaces (Produces):**
- module dict `_PENDING_READ = {}` (forward-round's own), `log`.
- `_confirm_keyboard(submission_id)` → InlineKeyboardMarkup, callback `bingofwd:confirm:<id>`.
- `async _send_confirmation(context, submission_id, uid, sheet_no)` — reuse `bingo_queue.evaluate` on `_PENDING_READ[submission_id]`; short line + confirm button if fully recognised, else the full template + `/start` flags. (Mirror `bingo_queue._send_confirmation` but with the forward keyboard and forward `_PENDING_READ`.)
- `async on_forwarded_card(update, context)` — only in a PRIVATE chat while `storage.forward_phase() == 'collecting'` and the message has a photo/document-image. Read `original = message.forward_origin.date if message.forward_origin else message.date`; download + `bingo._run_ocr(sheet_no, image_bytes)`; `sheet_no = storage.get_bingo_sheet(uid)` (skip with a hint if None); `sid = storage.queue_forwarded_submission(uid, handle, sheet_no, original.isoformat())`; stash `_PENDING_READ[sid] = {"read": read, "handle": handle, "sheet_no": sheet_no}`; `await _send_confirmation(...)`. If `forward_origin` was None, append a note that the original time couldn't be read.

- [ ] **Step 1: Write failing tests** (`tests/test_bingo_forward.py`) — create with a `store`-style fixture (reload storage on a temp DB, like `tests/test_bingo_storage.py`'s `store`) plus monkeypatched OCR:

```python
import asyncio, importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import config, storage
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fwd.db"))
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "fwd.db"))
    importlib.reload(storage)
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "fwd.db"))
    storage.init_db()
    return storage


def _fwd_update(uid, username, when):
    upd = MagicMock()
    upd.effective_chat = MagicMock(type="private")
    upd.effective_user = MagicMock(id=uid, username=username)
    msg = upd.effective_message
    msg.photo = [MagicMock(file_id="f")]
    msg.document = None
    msg.forward_origin = MagicMock(date=when)
    return upd


def test_on_forwarded_card_reads_original_time_and_queues(store, monkeypatch):
    from handlers import bingo_forward, bingo
    monkeypatch.setattr(bingo_forward, "storage", store)
    store.set_forward_phase("collecting")
    store.allocate_bingo_sheet(100, "alice")
    monkeypatch.setattr(store, "user_id_for_handle", lambda h: 1, raising=False)
    monkeypatch.setattr(bingo, "_run_ocr",
                        AsyncMock(return_value={"cells": [{"row": 0, "col": 0, "handle": "bob", "score": 95.0}]}))
    monkeypatch.setattr(bingo_forward, "_download_image", AsyncMock(return_value=b"img"))
    monkeypatch.setattr(bingo_forward, "_send_confirmation", AsyncMock())
    when = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    ctx = MagicMock(); ctx.bot = AsyncMock()
    asyncio.run(bingo_forward.on_forwarded_card(_fwd_update(100, "alice", when), ctx))
    rows = [r for r in store.all_bingo_submissions() if r["submitter_user_id"] == 100]
    assert len(rows) == 1 and rows[0]["status"] == "fwd_confirming"
    assert rows[0]["submitted_at"] == when.isoformat()
    bingo_forward._send_confirmation.assert_awaited_once()
    for sid in list(bingo_forward._PENDING_READ):
        bingo_forward._PENDING_READ.pop(sid, None)


def test_on_forwarded_card_ignored_when_not_collecting(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo_forward, "_send_confirmation", AsyncMock())
    ctx = MagicMock(); ctx.bot = AsyncMock()
    asyncio.run(bingo_forward.on_forwarded_card(
        _fwd_update(100, "alice", datetime.now(timezone.utc)), ctx))
    bingo_forward._send_confirmation.assert_not_awaited()
```

- [ ] **Step 2: Run to verify fail** — FAIL (module/functions undefined).

- [ ] **Step 3: Implement `handlers/bingo_forward.py`.** Read `handlers/bingo_queue.py` first and mirror `_send_confirmation`'s structure (evaluate → short/full), swapping the keyboard to `bingofwd:confirm:` and using this module's `_PENDING_READ`. Implement `_download_image` by delegating to `bingo._download_image` (import lazily), and `on_forwarded_card` per the interface above (guard: private chat, `forward_phase()=='collecting'`, has an image; use `sheets.normalize_handle(user.username)`; `original.isoformat()` for `submitted_at`).

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `git add handlers/bingo_forward.py tests/test_bingo_forward.py` / `git commit -m "Add forwarded-card handler: original time, OCR, queue, confirm"`.

---

### Task 3: Forward confirm / resend → `ready`

**Files:**
- Modify: `handlers/bingo_forward.py`
- Test: `tests/test_bingo_forward.py`

**Interfaces (Produces):**
- `async confirm_button(update, context)` — handles `bingofwd:confirm:<id>`; only if `storage.submission_status(id) == 'fwd_confirming'`; re-`evaluate` (`bingo_queue.evaluate`) the `_PENDING_READ` (rebuild from `winning_members` if missing, mirroring `bingo_queue._rebuild_pending`); if fully recognised → `storage.record_winning_members(id, members)` + `storage.set_forward_ready(id)` + DM the submitter *"You're in — results will be released together soon."*; else ignore (full-template path governs).
- `async on_resend(context, uid, read) -> bool` — if the user has a `fwd_confirming` row: update `_PENDING_READ`, re-`evaluate`; fully recognised → set ready (as above); else `_send_confirmation` again; return True. Else False.

> `record_winning_members` is called at confirm (so the line is persisted for the later verification kickoff and for restart rebuild). Build `members` exactly like `bingo_queue._start_verification` does (row,col,handle,prompt via `templates.prompt_for`,target_user_id via `storage.user_id_for_handle`).

- [ ] **Step 1: Write failing tests** — a `fwd_confirming` submission whose `_PENDING_READ` is a fully-recognised top row (use a 5-cell `_TOP_ROW` helper + `user_id_for_handle` → 1): tapping `bingofwd:confirm:<id>` sets it `ready`, records members, and DMs the submitter; a `fwd_confirming` with an incomplete read → stays `fwd_confirming`, `_send_confirmation` re-sent. `on_resend` routes a `fwd_confirming` user. (Mirror `tests/test_bingo_queue.py`'s confirm tests, FakeStore-free — use the temp-DB `store` and real `storage`.)

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** `confirm_button` + `on_resend` in `handlers/bingo_forward.py` per the interface; lazy-import `handlers.bingo`/`data.bingo_templates` inside functions.
- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `git commit -m "Forward round: confirm/resend marks an entry ready"`.

---

### Task 4: Collection close + verification kickoff (20 or 2-day timer, rolling)

**Files:**
- Modify: `handlers/bingo_forward.py`
- Test: `tests/test_bingo_forward.py`

**Interfaces (Produces):**
- `async maybe_close_collection(context)` — if `forward_phase()=='collecting'` and `storage.forward_entry_count() >= config.FORWARD_ROUND_TARGET` → `await close_collection(context)`.
- `async close_collection(context)` — set phase `verifying`; `await kickoff_verification(context)`.
- `async kickoff_verification(context)` — while `storage.active_forward_verifying_count() < config.BINGO_PRIZE_LIMIT` and `storage.ready_in_order()` non-empty: take the earliest `ready`; `await _start_verification(context, sub)` (flip to `pending`, DM subjects, arm the existing `bingo._confirmation_timeout`, evaluate). This mirrors `bingo_queue.maybe_kickoff` but on `ready` rows.
- `async _start_verification(context, sub)` — reuse the tagged-people handoff: build members from `storage.winning_members(sub["id"])`, `storage.set_submission_status(id, 'pending')`, DM submitter the "await results" line, arm `bingo._confirmation_timeout` (name `bingo:timeout:<id>`), `await bingo._dm_subjects`, `await bingo._finalize`.
- `def _forward_timeout_job(context)` job that calls `maybe... close` at the 2-day deadline.
- After a forward `pending` FAILS (rolling): `bingo._finalize`'s existing fail path already calls `bingo_queue.maybe_kickoff`; ADD a call to `bingo_forward.kickoff_verification` there too (guarded by `forward_phase()=='verifying'`), so a freed forward slot promotes the next `ready`. (Coordinate with Task 5's `_award` edit — both are in the `_finalize`/`_award` area.)

- [ ] **Step 1: Write failing tests** — `maybe_close_collection` closes at 20 entries; `kickoff_verification` with 12 `ready` rows promotes exactly 10 earliest to `pending` and DMs subjects (monkeypatch `bingo._dm_subjects`/`_finalize`); a `verifying`-phase `_finalize` fail promotes the next `ready`.
- [ ] **Step 2–4:** red → implement → green.
- [ ] **Step 5: Commit** `git commit -m "Forward round: close collection at 20 and verify earliest-first with rolling"`.

---

### Task 5: Batch results — `_award` hold + `_release_results`

**Files:**
- Modify: `handlers/bingo.py` (`_award`), `handlers/bingo_forward.py` (`_release_results`)
- Test: `tests/test_bingo_forward.py`, `tests/test_bingo_handlers.py`

**Interfaces (Produces):**
- `handlers/bingo.py` `_award`: at the top of the successful-claim path, branch on `storage.forward_batch_active()`. If active: still `claim_bingo_prize` + `set_submission_status(id,'verified')`, but SKIP the channel post, the per-win winner DM, and the `_dm_admins_of_winner` call; then `from handlers import bingo_forward; await bingo_forward.maybe_release(context)`. If not active: the existing behavior unchanged.
- `handlers/bingo_forward.py`:
  - `async maybe_release(context)` — if `forward_phase()=='verifying'` and (`storage.bingo_prizes_claimed() >= config.BINGO_PRIZE_LIMIT` OR no more `ready`/`pending` remain) → `await _release_results(context)`.
  - `async _release_results(context)` — set phase `released`; for each row in `storage.bingo_prizes` (add `storage.all_bingo_prizes()` if needed), DM the winner a congratulations and `storage.mark_admin_notified(winner_user_id)` (so the winner-notify sweep won't double-announce); then DM every `config.FACILITATOR_HANDLES` recipient (reuse `bingo._admin_recipient_ids`) one message listing all winners' `@handles`.

- [ ] **Step 1: Write failing tests** — while `forward_batch_active()`, `_award` claims but sends NO winner/admin/channel message (assert `ctx.bot.send_message` not called with the winner text) and calls `maybe_release`; `_release_results` DMs all winners + one @zzehao summary containing every handle, sets phase `released`, and marks winners notified. Guard the two EXISTING award tests: they don't set a forward phase, so `forward_batch_active()` is False and they stay unchanged.
- [ ] **Step 2–4:** red → implement → green (full suite).
- [ ] **Step 5: Commit** `git commit -m "Forward round: hold winner announcements and release them in one batch"`.

---

### Task 6: Broadcast start command + wiring + startup re-arm

**Files:**
- Modify: `handlers/bingo.py` (`register`, `rearm_bingo_timeouts`), `handlers/bingo_forward.py` (`start_forward_round`, `register`, `rearm`), `handlers/common.py` (help)
- Test: `tests/test_bingo_forward.py`, `tests/test_bingo_handlers.py`

**Interfaces (Produces):**
- `bingo_forward.start_forward_round(context) -> int` — set phase `collecting`; for each `bingo_allocation` user (`storage.all_bingo_allocations()` — add if missing), DM *"📸 Forward me the earliest bingo card you sent me and I'll check it for the prize round!"*; arm the 2-day timer job (`_forward_timeout_job`, name `bingo:forward_deadline`); return the number DM'd.
- `handlers/bingo.py`: `@facil_only` command `start_forward_round` calling it + replying with the count; register `CommandHandler("start_forward_round", ...)`, `bingo_forward.register(app)`, and a `MessageHandler(filters.ChatType.PRIVATE & (filters.FORWARDED) & (filters.PHOTO | filters.Document.IMAGE), bingo_forward.on_forwarded_card)` in a group that doesn't shadow the others (use `group=1` like the text handler). Route forward-round confirming users' text resends in `on_bingo_text` (before the live-queue path): if `storage.forward_phase()=='collecting'` and the user has a `fwd_confirming` row → `await bingo_forward.on_resend(...)`.
- `bingo_forward.register(app)` — `CallbackQueryHandler(confirm_button, pattern=r"^bingofwd:confirm:")`.
- `bingo_forward.rearm(app)` — if phase `collecting`, re-arm the 2-day timer from `forward_started_at() + FORWARD_ROUND_WINDOW`; if `verifying`, re-arm confirm-timeouts for `pending` forward rows (the existing `rearm_bingo_timeouts` already re-arms `pending`). Called from `bingo.rearm_bingo_timeouts`.
- `handlers/common.py` HELP_TEXT: one facil line for `/start_forward_round`.

- [ ] **Step 1: Write failing tests** — `start_forward_round` DMs all allocation users, sets phase, returns count; the facil command is `@facil_only` and reports the count (mirror the `/import_bingo_queue` command test); the forwarded-image handler is registered.
- [ ] **Step 2–4:** red → implement → green. Full suite + `import main`.
- [ ] **Step 5: Commit** `git add handlers/ tests/` / `git commit -m "Wire the forward round: /start_forward_round, forwarded-image handler, re-arm"`.

---

## Self-Review

- **Requirement coverage:** broadcast to card-holders (T6) ✓; forward → original time → OCR → confirm-as-they-forward → queue by original time (T2,T3) ✓; close at 20 or 2 days (T4) ✓; verify earliest-first with rolling + tagged-people check (T4) ✓; batch release of all winners + @zzehao summary, per-win DMs suppressed (T5) ✓; isolation from the live queue via dedicated statuses (T1 constraint) ✓; restart re-arm (T6) ✓.
- **Isolation:** `fwd_confirming`/`ready` are excluded from every live-queue query (T1 tests assert it); forward round uses `bingofwd:` callbacks and its own `_PENDING_READ`.
- **`_award` risk:** the only change to the shared award path is one `forward_batch_active()` branch that suppresses announcements + triggers release; the two existing award tests don't set a forward phase, so they're unaffected (T5 guards this).
- **Ordering:** verification starts at collection close and consumes `ready_in_order()` (earliest `submitted_at`, which is the original forward time), with rolling replacement — so winners are the earliest-original-time entries that pass.
- **Type consistency:** forward `_PENDING_READ[id] = {"read","handle","sheet_no"}` (same shape as `bingo_queue`); `_send_confirmation`/`confirm_button`/`on_resend`/`_start_verification` mirror the `bingo_queue` equivalents with `bingofwd:` callbacks and `ready` status.
