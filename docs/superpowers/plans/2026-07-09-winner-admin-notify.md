# DM Each Bingo Winner to @zzehao — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** DM every bingo winner to the facilitator admin(s) — @zzehao by default — as they're confirmed, and on startup DM any existing winners who haven't been notified yet (idempotent).

**Architecture:** A new `bingo_prizes.admin_notified_at` column tracks who's been announced. `_award` DMs the admin(s) on each fresh win; a startup job sweeps `admin_notified_at IS NULL` winners and DMs them. Recipients are resolved from `config.FACILITATOR_HANDLES` (always includes `"zzehao"`) via `storage.user_id_for_handle`.

**Tech Stack:** python-telegram-bot v22 (JobQueue), sqlite3. Builds on the current `handlers/bingo.py`, `storage.py`.

## Global Constraints

- Python 3.12; tests `.venv/Scripts/python.exe -m pytest`; Git Bash. Human-style commits, NO `Co-Authored-By`/AI trailer.
- Storage follows the module idiom (`with _lock:`, `_now_iso()`, `_conn.commit()`, `dict(row)`). The `bingo_prizes` migration must handle BOTH a fresh DB (column in `SCHEMA`) AND an existing live DB (an `ALTER TABLE` migration in `init_db`, mirroring the existing `attendance.answer` migration).
- **Recipients = every handle in `config.FACILITATOR_HANDLES` resolved to a user_id via `storage.user_id_for_handle` (deduped, skip unresolved).** Default is exactly `@zzehao`. If NONE resolve (e.g. the admin never `/start`ed), send nothing and do NOT mark notified — so the next startup retries once they're reachable.
- Best-effort DMs: a send that raises is swallowed; still mark notified once at least one recipient exists, so a restart never re-spams.
- Do NOT change the tagged-people/prize logic, the channel `ANNOUNCE_CHAT_ID` post, or the winner's own congrats DM.
- Before committing, `git status --short`; restore any deleted `__init__.py` with `git checkout HEAD -- <path>`.

## File Structure

- `storage.py` (modify) — `admin_notified_at` column (SCHEMA + migration); `winners_pending_admin_notice`, `mark_admin_notified`.
- `handlers/bingo.py` (modify) — `_admin_recipient_ids`, `_dm_admins_of_winner`, hook in `_award`, `_notify_pending_winners_job`, schedule it in `rearm_bingo_timeouts`.
- Tests: `tests/test_bingo_storage.py`, `tests/test_bingo_handlers.py`.

---

### Task 1: Storage — `admin_notified_at` tracking

**Files:**
- Modify: `storage.py`
- Test: `tests/test_bingo_storage.py` (append, reuse `store` fixture)

**Interfaces:**
- Produces:
  - `bingo_prizes` gains an `admin_notified_at TEXT` column (fresh via SCHEMA, existing via migration).
  - `winners_pending_admin_notice() -> list[dict]` — `bingo_prizes` rows WHERE `admin_notified_at IS NULL`, `ORDER BY claim_no`.
  - `mark_admin_notified(winner_user_id)` — `UPDATE bingo_prizes SET admin_notified_at=_now_iso() WHERE winner_user_id=? AND admin_notified_at IS NULL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bingo_storage.py — append (reuse `store`)
def test_admin_notified_lifecycle(store):
    store.allocate_bingo_sheet(1, "alice")
    sub = store.start_bingo_submission(1, "alice", 1)
    claim = store.claim_bingo_prize(1, "alice", sub)
    assert claim == 1
    pending = store.winners_pending_admin_notice()
    assert [w["winner_user_id"] for w in pending] == [1]
    store.mark_admin_notified(1)
    assert store.winners_pending_admin_notice() == []
    store.mark_admin_notified(1)                 # idempotent no-op
    assert store.winners_pending_admin_notice() == []


def test_bingo_prizes_has_admin_notified_column(store):
    cols = [r[1] for r in store._conn.execute("PRAGMA table_info(bingo_prizes)")]
    assert "admin_notified_at" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`
Expected: FAIL — `winners_pending_admin_notice` undefined / column missing.

- [ ] **Step 3: Implement**

In `storage.py` `SCHEMA`, add `admin_notified_at TEXT` to the `bingo_prizes` CREATE TABLE (after `posted_at`).

In `init_db`, after the existing `attendance` migration and before `_conn.commit()`, add:

```python
        pcols = [r[1] for r in _conn.execute("PRAGMA table_info(bingo_prizes)")]
        if "admin_notified_at" not in pcols:
            _conn.execute("ALTER TABLE bingo_prizes ADD COLUMN admin_notified_at TEXT")
```

Add the helpers:

```python
def winners_pending_admin_notice():
    """Winners not yet DM'd to the facil admin(s), earliest prize first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_prizes WHERE admin_notified_at IS NULL "
            "ORDER BY claim_no"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_admin_notified(winner_user_id):
    """Record that the admin(s) were told about this winner (once)."""
    with _lock:
        _conn.execute(
            "UPDATE bingo_prizes SET admin_notified_at = ? "
            "WHERE winner_user_id = ? AND admin_notified_at IS NULL",
            (_now_iso(), winner_user_id),
        )
        _conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_bingo_storage.py
git commit -m "Track which bingo winners have been announced to the admin"
```

---

### Task 2: DM the admin(s) on each win + a startup catch-up sweep

**Files:**
- Modify: `handlers/bingo.py` (`_admin_recipient_ids`, `_dm_admins_of_winner`, hook in `_award`, `_notify_pending_winners_job`, schedule in `rearm_bingo_timeouts`)
- Test: `tests/test_bingo_handlers.py`

**Interfaces:**
- Consumes: `config.FACILITATOR_HANDLES`, `config.BINGO_PRIZE_LIMIT`, `storage.user_id_for_handle`, `storage.winners_pending_admin_notice`, `storage.mark_admin_notified`.
- Produces:
  - `_admin_recipient_ids() -> set[int]` — resolve each handle in `config.FACILITATOR_HANDLES` via `storage.user_id_for_handle`; drop `None`.
  - `async _dm_admins_of_winner(context, winner)` — `winner` is a dict with `winner_user_id`, `handle`, `claim_no`. If no recipients resolve → log + return (do NOT mark). Else DM each recipient (best-effort) the winner line, then `storage.mark_admin_notified(winner["winner_user_id"])`.
  - `async _notify_pending_winners_job(context)` — for each `storage.winners_pending_admin_notice()`, `await _dm_admins_of_winner(context, winner)`.
  - `_award` calls `await _dm_admins_of_winner(context, {...})` for the new winner (after the winner's own congrats DM).
  - `rearm_bingo_timeouts` schedules `_notify_pending_winners_job` once at startup.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bingo_handlers.py — append (real bingo+store fixtures, _context helper)
def test_dm_admins_of_winner_dms_zzehao_and_marks(bingo, store, monkeypatch):
    store.mark_started(999, "zzehao", "Zhou")            # admin has /started
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"zzehao"})
    store.allocate_bingo_sheet(1, "alice")
    sub = store.start_bingo_submission(1, "alice", 1)
    store.claim_bingo_prize(1, "alice", sub)
    ctx = _context()
    asyncio.run(bingo._dm_admins_of_winner(
        ctx, {"winner_user_id": 1, "handle": "alice", "claim_no": 1}))
    dm = [c for c in ctx.bot.send_message.await_args_list if c.kwargs.get("chat_id") == 999]
    assert dm and "alice" in dm[0].kwargs["text"]
    assert store.winners_pending_admin_notice() == []    # marked


def test_dm_admins_no_recipient_does_not_mark(bingo, store, monkeypatch):
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"nobody_started"})
    store.allocate_bingo_sheet(1, "alice")
    sub = store.start_bingo_submission(1, "alice", 1)
    store.claim_bingo_prize(1, "alice", sub)
    ctx = _context()
    asyncio.run(bingo._dm_admins_of_winner(
        ctx, {"winner_user_id": 1, "handle": "alice", "claim_no": 1}))
    ctx.bot.send_message.assert_not_awaited()
    assert [w["winner_user_id"] for w in store.winners_pending_admin_notice()] == [1]  # NOT marked


def test_notify_pending_winners_job_sweeps_all(bingo, store, monkeypatch):
    store.mark_started(999, "zzehao", "Zhou")
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"zzehao"})
    for uid, h in [(1, "alice"), (2, "bob")]:
        store.allocate_bingo_sheet(uid, h)
        sub = store.start_bingo_submission(uid, h, 1)
        store.claim_bingo_prize(uid, h, sub)
    ctx = _context(); ctx.job = MagicMock()
    asyncio.run(bingo._notify_pending_winners_job(ctx))
    assert store.winners_pending_admin_notice() == []    # all marked
    dmd = {c.kwargs.get("chat_id") for c in ctx.bot.send_message.await_args_list}
    assert dmd == {999}                                  # both winners announced to zzehao
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_handlers.py -q`
Expected: FAIL — `_dm_admins_of_winner` / `_notify_pending_winners_job` undefined.

- [ ] **Step 3: Implement**

```python
# handlers/bingo.py — add near _award
def _admin_recipient_ids():
    """Resolve the facil-admin handles (always includes @zzehao) to user_ids we
    can DM. A handle whose owner hasn't /started the bot resolves to None and is
    dropped (we can't DM them)."""
    ids = set()
    for handle in config.FACILITATOR_HANDLES:
        uid = storage.user_id_for_handle(handle)
        if uid is not None:
            ids.add(uid)
    return ids


async def _dm_admins_of_winner(context, winner):
    """DM the facil admin(s) that a bingo prize was claimed, then mark this winner
    as announced. If no admin is reachable, send nothing and leave it unmarked so
    the next startup sweep retries."""
    recipients = _admin_recipient_ids()
    if not recipients:
        log.warning("no reachable facil admin to announce bingo winner %s",
                    winner.get("winner_user_id"))
        return
    handle = winner.get("handle") or "?"
    text = (f"🏆 Bingo prize #{winner['claim_no']}/{config.BINGO_PRIZE_LIMIT} "
            f"claimed by @{handle}.")
    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception as exc:
            log.warning("couldn't DM admin %s about bingo winner: %s", uid, exc)
    storage.mark_admin_notified(winner["winner_user_id"])


async def _notify_pending_winners_job(context):
    """Startup catch-up: DM the admin(s) about every winner not yet announced."""
    for winner in storage.winners_pending_admin_notice():
        await _dm_admins_of_winner(context, winner)
```

In `_award`, after the winner's congrats DM (the `try: … "🏆 <b>BINGO!</b> …" … except` block), add:

```python
    await _dm_admins_of_winner(
        context,
        {"winner_user_id": submitter_id, "handle": submitter_handle, "claim_no": claim_no},
    )
```

In `rearm_bingo_timeouts(app)`, after the existing re-arm loop and the `bingo_queue.rearm_confirm_timeouts(app)` call, schedule the sweep:

```python
    if jq is not None:
        jq.run_once(_notify_pending_winners_job, when=3, name="bingo:notify_winners")
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (all existing + new). Sanity: `.venv/Scripts/python.exe -c "import main"` OK.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo.py tests/test_bingo_handlers.py
git commit -m "DM the facil admin each bingo winner, with a startup catch-up sweep"
```

---

## Self-Review

- **Requirement coverage:** DM each winner to @zzehao on confirmation (Task 2 `_award` hook) ✓; DM existing/un-notified winners on startup (Task 2 sweep job) ✓; idempotent via `admin_notified_at` (Task 1) ✓; recipient = @zzehao by default via `FACILITATOR_HANDLES` (Task 2) ✓.
- **"Immediately" caveat:** the startup sweep fires when the bot next boots (on deploy/restart) — the earliest the change can reach the live game, since the controller can't touch the live bot.
- **No-recipient safety:** if @zzehao hasn't `/start`ed, nothing is sent and the winner stays un-notified, so a later boot retries — no silent permanent miss, no spam.
- **Type consistency:** `_dm_admins_of_winner(context, winner)` takes `{winner_user_id, handle, claim_no}` at both call sites (the `_award` dict and the `winners_pending_admin_notice` row, which has those columns).
