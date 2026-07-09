# Bingo Queue — Round-Open Kickoff + Past-Submission Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the confirmation round open at 10 queued (or on a facil command) instead of messaging the first 10 eagerly, and add a facil `/import_bingo_queue` that folds every non-winner's FIRST past submission into the queue by submission time.

**Architecture:** A persisted `queue_open` flag gates `maybe_kickoff`; `enqueue` opens the round at 10, `close_round`/import open it early. The import scans `bingo_submissions`, re-queues each non-winner's earliest row in place (preserving `submitted_at`, reusing its recorded `winning_members` to rebuild the confirmation), supersedes their other rows, opens the round, and kicks off. The confirmation reconstruction (`winning_members` → read) doubles as a restart fallback.

**Tech Stack:** python-telegram-bot v22, sqlite3 (WAL, shared `_conn`+`_lock`), pytest via `asyncio.run`. Builds on the existing `handlers/bingo_queue.py`, `handlers/bingo.py`, `storage.py`.

## Global Constraints

- Python 3.12; tests run offline with `.venv/Scripts/python.exe -m pytest`. Shell is Git Bash.
- No hardcoded secrets. Commits human-style, NO `Co-Authored-By`/AI trailer.
- Storage follows the existing module pattern: shared `storage._conn` under `with _lock:`, `_now_iso()`, `_conn.commit()`, `dict(row)` reads. Model `set_queue_open`/`is_queue_open` on the existing `set_bingo_closed`/`bingo_is_closed`.
- **STATUS values** on `bingo_submissions.status` (free-text TEXT, no schema change): existing `queued`/`confirming`/`pending`/`verified`/`failed` plus a new terminal **`superseded`** (a non-first past submission of a re-queued player). `superseded` is NOT counted by `active_slot_count` and NOT returned by `queued_in_order`/`confirming_submissions`/`pending_submissions`.
- **queue_open semantics:** `maybe_kickoff` promotes ONLY when `is_queue_open()`. The flag persists in `bingo_flags` (survives restart). Opens automatically when `len(queued_in_order()) >= BINGO_PRIZE_LIMIT` (10), or on `close_round`/`import_queue`. Never auto-closes (the game-over `closed` flag is separate).
- **Import rules (from the approved spec):** re-queue every player who has submitted and has NO prize; one entry per player = their EARLIEST submission (min `submitted_at`, tie-break min `id`); supersede their other submissions; preserve original `submitted_at`; open the round + kickoff; idempotent (skip a player whose earliest is already `queued`/`confirming`).
- Do NOT change the tagged-people pipeline, prize cap, or freeze protections.

## File Structure

- `storage.py` (modify) — `set_queue_open`, `is_queue_open`, `all_bingo_submissions`, `requeue_submission`.
- `handlers/bingo_queue.py` (modify) — gate `maybe_kickoff` on `is_queue_open`; `enqueue` opens at 10; `close_round` opens; add `_read_from_members`, restart fallback in `_send_confirmation`/`confirm_button`, and `import_queue`.
- `handlers/bingo.py` (modify) — `import_bingo_queue` facil command + register + help text.
- Tests: `tests/test_bingo_storage.py`, `tests/test_bingo_queue.py`, `tests/test_bingo_handlers.py` (modify).

---

### Task 1: Storage — `queue_open` flag + import read/requeue helpers

**Files:**
- Modify: `storage.py` (append after the queue helpers / near `bingo_is_closed`)
- Test: `tests/test_bingo_storage.py` (append — reuse the existing `store` fixture)

**Interfaces:**
- Consumes: `storage._conn`, `_lock`, `_now_iso`, existing `bingo_flags` table.
- Produces:
  - `set_queue_open()` — `INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES ('queue_open', ?)`. Idempotent.
  - `is_queue_open() -> bool` — True iff the `queue_open` flag row exists.
  - `all_bingo_submissions() -> list[dict]` — every `bingo_submissions` row, `ORDER BY submitted_at, id`.
  - `requeue_submission(submission_id)` — `UPDATE bingo_submissions SET status='queued', verified_at=NULL WHERE id=?` (keeps `submitted_at` and `id`, so `winning_members` stay linked).

- [ ] **Step 1: Write the failing test** (append to `tests/test_bingo_storage.py`, reuse `store`)

```python
def test_queue_open_flag_roundtrips(store):
    assert store.is_queue_open() is False
    store.set_queue_open()
    assert store.is_queue_open() is True
    store.set_queue_open()                       # idempotent
    assert store.is_queue_open() is True


def test_all_bingo_submissions_ordered_by_time(store):
    a = store.start_bingo_submission(1, "a", 3)   # status 'pending', submitted_at now
    b = store.start_bingo_submission(2, "b", 3)
    rows = store.all_bingo_submissions()
    ids = [r["id"] for r in rows]
    assert set(ids) >= {a, b} and ids == sorted(ids)


def test_requeue_submission_sets_queued_clears_verified(store):
    s = store.start_bingo_submission(1, "a", 3)
    store.set_submission_status(s, "verified", verified_at=store._now_iso())
    store.requeue_submission(s)
    row = store.submission_by_id(s)
    assert row["status"] == "queued" and row["verified_at"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`
Expected: the 3 new tests FAIL — `AttributeError: module 'storage' has no attribute 'set_queue_open'`.

- [ ] **Step 3: Implement**

```python
# storage.py — add near bingo_is_closed / set_bingo_closed

def set_queue_open():
    """Open the confirmation round (persisted). Idempotent."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES ('queue_open', ?)",
            (_now_iso(),),
        )
        _conn.commit()


def is_queue_open():
    """True once the round has been opened (10 queued, or a facil command)."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM bingo_flags WHERE name = 'queue_open'"
        ).fetchone()
    return row is not None


def all_bingo_submissions():
    """Every submission row, earliest first (for the past-submission import)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def requeue_submission(submission_id):
    """Re-queue an existing submission in place: status -> 'queued', clear
    verified_at, keep submitted_at and id (so its winning_members stay linked)."""
    with _lock:
        _conn.execute(
            "UPDATE bingo_submissions SET status = 'queued', verified_at = NULL "
            "WHERE id = ?",
            (submission_id,),
        )
        _conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`
Expected: PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_bingo_storage.py
git commit -m "Add queue-open flag and import read/requeue storage helpers"
```

---

### Task 2: Round-open kickoff (gate `maybe_kickoff`, open at 10 / on facil command)

**Files:**
- Modify: `handlers/bingo_queue.py` (`maybe_kickoff`, `enqueue`, `close_round`)
- Test: `tests/test_bingo_queue.py`, `tests/test_bingo_handlers.py` (update the eager-kickoff assumptions)

**Interfaces:**
- Consumes: `storage.is_queue_open`, `storage.set_queue_open`, existing queue helpers.
- Produces: `maybe_kickoff` no-ops unless the round is open; `enqueue` opens the round when `queued` reaches `BINGO_PRIZE_LIMIT`; `close_round` opens the round then kicks off.

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/test_bingo_queue.py`, extend `FakeStore` with the flag:

```python
    # in FakeStore.__init__: self._open = False
    def is_queue_open(self):
        return self._open
    def set_queue_open(self):
        self._open = True
```

Update the existing kickoff tests to the new behavior and add the open-at-10 test:

```python
def test_maybe_kickoff_noops_until_round_open(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    for uid in range(1, 6):
        fake.queue_submission(uid, f"u{uid}", 1)
    asyncio.run(bingo_queue.maybe_kickoff(_ctx()))        # round closed
    assert bingo_queue._send_confirmation.await_count == 0
    fake.set_queue_open()
    asyncio.run(bingo_queue.maybe_kickoff(_ctx()))        # now fires
    assert bingo_queue._send_confirmation.await_count == 5


def test_enqueue_opens_round_at_ten(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    ctx = _ctx()
    for uid in range(1, 10):                              # 9 queued -> not open
        asyncio.run(bingo_queue.enqueue(ctx, uid, f"u{uid}", 1, {"cells": _cells({})}))
    assert fake.is_queue_open() is False
    assert bingo_queue._send_confirmation.await_count == 0
    asyncio.run(bingo_queue.enqueue(ctx, 10, "u10", 1, {"cells": _cells({})}))  # 10th
    assert fake.is_queue_open() is True
    assert bingo_queue._send_confirmation.await_count == 10
    for sid in list(bingo_queue._PENDING_READ):
        bingo_queue._PENDING_READ.pop(sid, None)
```

Replace the old `test_kickoff_promotes_only_ten_earliest`, `test_enqueue_replies_in_queue_then_kicks_off`, and `test_confirm_timeout_fails_and_promotes` bodies so they `fake.set_queue_open()` before expecting any promotion (a confirming/failing submission implies the round is already open). Keep `test_close_round_fires_with_fewer_than_ten` — it now passes because `close_round` opens the round itself (verify it still asserts 3 confirmations).

In `tests/test_bingo_handlers.py`, the four submit tests and the rolling-replacement test change (the round is closed after a single submit, so a submission stays `queued` and NOTHING is messaged until the round opens):
- `test_ocr_confirm_yes_queues_and_sends_short_confirmation` → rename e.g. `test_ocr_confirm_yes_queues_without_messaging_until_round_opens`: after `_tap_ocr_confirm(...,"yes")`, assert the submission is `queued` (via `store.queued_in_order()` has the user's row; `store.confirming_submissions()==[]`), `active_submission(100) is None`, and NO confirmation `send_message` beyond the "You're in the queue" line (assert no `bingoq:confirm:` keyboard was sent). Then assert that after `store.set_queue_open()` + `asyncio.run(bingo_queue.maybe_kickoff(ctx))` the short confirmation (with a `bingoq:confirm:` button) IS sent.
- `test_ocr_confirm_yes_no_line_still_queues_full_template` → same shape: queued only; opening the round then sends the full template.
- `test_on_bingo_text_queues_and_sends_short_confirmation` and `test_on_bingo_text_exact_match_miss_degrades_gracefully` → same: after a single submit the row is `queued`, no confirmation; do NOT assert a confirmation is sent at submit time. (The "in the queue" DM is still sent.)
- `test_failed_verification_promotes_next_queued` → add `store.set_queue_open()` before `_finalize` (a `pending` submission means the round was already open), so the fail-path `maybe_kickoff` promotes `b`.

Do NOT weaken these — they must still prove the queued state and that opening the round fires the confirmations.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py tests/test_bingo_handlers.py -q`
Expected: new/updated tests FAIL (kickoff still fires eagerly; `is_queue_open` unused).

- [ ] **Step 3: Implement the gating**

```python
# handlers/bingo_queue.py — maybe_kickoff: gate on the open flag
async def maybe_kickoff(context):
    """Promote queued submissions into 'confirming' until the 10 in-flight slots
    are full — but only once the round is open (10 queued, or a facil command)."""
    if not storage.is_queue_open():
        return
    while storage.active_slot_count() < config.BINGO_PRIZE_LIMIT:
        queued = storage.queued_in_order()
        if not queued:
            return
        sub = queued[0]
        storage.set_submission_status(sub["id"], "confirming")
        _arm_confirm_timeout(context, sub["id"])
        await _send_confirmation(context, sub)
```

```python
# handlers/bingo_queue.py — enqueue: open the round once 10 have queued
async def enqueue(context, uid, handle, sheet_no, read):
    """Record a submission into the queue and tell the user their position. The
    confirmation round opens automatically once BINGO_PRIZE_LIMIT are queued
    (or earlier via a facil command)."""
    sid = storage.queue_submission(uid, handle, sheet_no)
    _PENDING_READ[sid] = {"read": read, "handle": handle, "sheet_no": sheet_no}
    position = len(storage.queued_in_order())
    await context.bot.send_message(
        chat_id=uid,
        text=f"You're in the queue (#{position})! 📥 I'll message you when it's "
             "your turn to confirm your squares — hang tight. 🙂",
    )
    if not storage.is_queue_open() and \
            len(storage.queued_in_order()) >= config.BINGO_PRIZE_LIMIT:
        storage.set_queue_open()
    await maybe_kickoff(context)
```

```python
# handlers/bingo_queue.py — close_round: open the round, then process
async def close_round(context):
    """Facil fallback: open the round now (even with fewer than 10 queued) and
    process whoever is waiting."""
    storage.set_queue_open()
    await maybe_kickoff(context)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py tests/test_bingo_handlers.py -q` then the FULL suite `.venv/Scripts/python.exe -m pytest -q`.
Expected: PASS (all updated + existing). Only the tests named above should have needed changes.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/
git commit -m "Open the confirmation round at 10 queued or on a facil command"
```

---

### Task 3: Reconstruction helper + restart fallback

**Files:**
- Modify: `handlers/bingo_queue.py` (`_read_from_members`, `_send_confirmation`, `confirm_button`)
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `storage.winning_members`, `storage.submission_by_id`.
- Produces:
  - `_read_from_members(submission_id) -> dict | None` — rebuild a read `{"cells": [{row,col,handle,score:100.0}, …]}` from recorded `winning_members`, or `None` if none recorded.
  - `_send_confirmation` and `confirm_button`: when `_PENDING_READ` is missing, first try `_read_from_members`; if it yields a read, seed `_PENDING_READ` (handle+sheet_no from the submission row) and proceed; only if that also fails, fall back to the "please resend" DM.

- [ ] **Step 1: Write the failing test**

```python
def test_read_from_members_rebuilds_cells(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    fake.winning_members = lambda sid: [
        {"row": 0, "col": 0, "handle": "alice", "prompt": "p", "target_user_id": 1},
        {"row": 0, "col": 1, "handle": "bob", "prompt": "p", "target_user_id": 2}]
    read = bingo_queue._read_from_members(5)
    assert {(c["row"], c["col"], c["handle"]) for c in read["cells"]} == \
        {(0, 0, "alice"), (0, 1, "bob")}
    fake.winning_members = lambda sid: []
    assert bingo_queue._read_from_members(5) is None


def test_confirm_button_rebuilds_from_members_after_restart(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(fake, "user_id_for_handle", lambda h: 1, raising=False)
    sid = fake.queue_submission(1, "submitter", 1)
    fake.set_submission_status(sid, "confirming")
    fake.winning_members = lambda s: [
        {"row": 0, "col": c, "handle": h} for c, h in
        [(0, "alice"), (1, "bob"), (2, "carol"), (3, "dan"), (4, "eve")]]
    bingo_queue._PENDING_READ.pop(sid, None)             # simulate lost read
    monkeypatch.setattr(bingo_queue, "_start_verification", AsyncMock())
    q = AsyncMock(); q.data = f"bingoq:confirm:{sid}"; q.from_user = MagicMock(id=1)
    upd = MagicMock(); upd.callback_query = q
    asyncio.run(bingo_queue.confirm_button(upd, _ctx()))
    bingo_queue._start_verification.assert_awaited_once()  # rebuilt, not "resend"
    bingo_queue._PENDING_READ.pop(sid, None)
```

(`FakeStore` needs `submission_by_id` — already present — returning a dict with `submitter_handle`/`sheet_no`; ensure the queued row carries those, which `FakeStore.queue_submission` already sets.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `_read_from_members` undefined / `confirm_button` sends the resend DM instead of rebuilding.

- [ ] **Step 3: Implement**

```python
# handlers/bingo_queue.py — add near evaluate()
def _read_from_members(submission_id):
    """Rebuild a minimal read from a submission's recorded winning line, or None.
    Used to seed _PENDING_READ for imported past submissions and as a restart
    fallback (winning_members is persisted; _PENDING_READ is in-memory)."""
    members = storage.winning_members(submission_id)
    if not members:
        return None
    cells = [{"row": m["row"], "col": m["col"], "handle": m["handle"], "score": 100.0}
             for m in members]
    return {"cells": cells}


def _rebuild_pending(sid):
    """Try to repopulate _PENDING_READ[sid] from the persisted winning line.
    Returns the pending dict or None."""
    read = _read_from_members(sid)
    if read is None:
        return None
    sub = storage.submission_by_id(sid)
    if sub is None:
        return None
    pending = {"read": read, "handle": sub.get("submitter_handle") or "",
               "sheet_no": sub["sheet_no"]}
    _PENDING_READ[sid] = pending
    return pending
```

In `_send_confirmation`, replace the `if pending is None:` block so it tries a rebuild first:

```python
    pending = _PENDING_READ.get(sid)
    if pending is None:
        pending = _rebuild_pending(sid)
    if pending is None:
        log.warning("no pending read for submission %s; asking submitter to resend", sid)
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="Please resend your filled bingo card so I can check it. 🔁")
        except Exception:
            pass
        return
```

In `confirm_button`, replace the `if pending is None:` block similarly:

```python
    pending = _PENDING_READ.get(sid)
    if pending is None:
        pending = _rebuild_pending(sid)
    if pending is None:
        try:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="I lost track of your card after a restart — please resend "
                     "your filled list and I'll check it. 🔁")
        except Exception:
            pass
        return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q` then the full suite.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Rebuild a lost confirmation read from the recorded winning line"
```

---

### Task 4: `import_queue` — fold past non-winners into the queue

**Files:**
- Modify: `handlers/bingo_queue.py` (`import_queue`)
- Test: `tests/test_bingo_queue.py`

**Interfaces:**
- Consumes: `storage.all_bingo_submissions`, `storage.has_bingo_prize`, `storage.requeue_submission`, `storage.set_submission_status`, `storage.set_queue_open`, `_read_from_members`, `maybe_kickoff`.
- Produces:
  - `async import_queue(context) -> int` — for each player (grouped from `all_bingo_submissions`, which is time-ordered): skip if `has_bingo_prize`; let `first` = their earliest row; skip if `first["status"]` in `('queued','confirming')` (already imported/in-flight); else `requeue_submission(first["id"])`, seed `_PENDING_READ[first["id"]]` from `_read_from_members`, and `set_submission_status(other["id"], 'superseded')` for every other row of that player. Then `set_queue_open()` + `await maybe_kickoff(context)`. Returns the number of players imported.

- [ ] **Step 1: Write the failing test**

```python
def test_import_queue_dedups_to_first_excludes_winners_supersedes_rest(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(fake, "user_id_for_handle", lambda h: 1, raising=False)
    # helper to add a terminal past submission with a recorded line
    def past(uid, handle, when, status):
        sid = fake.queue_submission(uid, handle, 1)
        fake.rows[sid]["submitted_at"] = when
        fake.rows[sid]["status"] = status
        fake.members[sid] = [{"row": 0, "col": c, "handle": h} for c, h in
                             [(0, "a"), (1, "b"), (2, "c"), (3, "d"), (4, "e")]]
        return sid
    # user 1: two failed submissions -> only the FIRST is re-queued
    s1a = past(1, "u1", "00001", "failed")
    s1b = past(1, "u1", "00009", "failed")
    # user 2: a winner -> excluded entirely
    s2 = past(2, "u2", "00002", "verified")
    fake.prizes.add(2)
    # user 3: one failed -> re-queued
    s3 = past(3, "u3", "00003", "failed")
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    n = asyncio.run(bingo_queue.import_queue(_ctx()))
    assert n == 2                                        # users 1 and 3
    assert fake.submission_status(s1a) == "confirming" or fake.submission_status(s1a) == "queued"
    assert fake.submission_status(s1b) == "superseded"  # later dup superseded
    assert fake.submission_status(s2) == "verified"     # winner untouched
    assert fake.is_queue_open() is True
    # ordering preserved: s1a (00001) ranks before s3 (00003)
    for sid in list(bingo_queue._PENDING_READ):
        bingo_queue._PENDING_READ.pop(sid, None)
```

`FakeStore` needs: `self.members = {}`, `self.prizes = set()`; `winning_members(sid)` returns `self.members.get(sid, [])`; `has_bingo_prize(uid)` returns `uid in self.prizes`; `all_bingo_submissions()` returns rows ordered by `(submitted_at, id)`; `requeue_submission(sid)` sets status `'queued'`. Add these to `FakeStore`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q`
Expected: FAIL — `import_queue` undefined.

- [ ] **Step 3: Implement**

```python
# handlers/bingo_queue.py — add
async def import_queue(context):
    """Facil: fold every non-winner's FIRST past submission into the queue,
    supersede their other submissions, open the round, and start checking. Returns
    how many players were imported. Idempotent (skips players already in-flight)."""
    by_user = {}
    for s in storage.all_bingo_submissions():        # time-ordered (submitted_at, id)
        by_user.setdefault(s["submitter_user_id"], []).append(s)
    imported = 0
    for uid, subs in by_user.items():
        if storage.has_bingo_prize(uid):
            continue                                 # winner: keep their prize
        first = subs[0]
        if first["status"] in ("queued", "confirming"):
            continue                                 # already imported / in-flight
        storage.requeue_submission(first["id"])
        read = _read_from_members(first["id"])
        if read is not None:
            _PENDING_READ[first["id"]] = {
                "read": read,
                "handle": first.get("submitter_handle") or "",
                "sheet_no": first["sheet_no"],
            }
        for other in subs[1:]:
            storage.set_submission_status(other["id"], "superseded")
        imported += 1
    storage.set_queue_open()
    await maybe_kickoff(context)
    return imported
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_queue.py -q` then the full suite.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo_queue.py tests/test_bingo_queue.py
git commit -m "Add import_queue: fold past non-winners into the queue by submit time"
```

---

### Task 5: Wire the `/import_bingo_queue` facil command

**Files:**
- Modify: `handlers/bingo.py` (`import_bingo_queue` command + `register`); wherever the bot's `/help` text lists facil commands.
- Test: `tests/test_bingo_handlers.py`

**Interfaces:**
- Produces:
  - `bingo.import_bingo_queue(update, context)` — `@facil_only`; calls `n = await bingo_queue.import_queue(context)` and replies `f"Imported {n} player(s) into the bingo queue — checking the earliest 10 now. 🎬"`.
  - Registered via `app.add_handler(CommandHandler("import_bingo_queue", import_bingo_queue))` in `bingo.register`.

- [ ] **Step 1: Write the failing test**

```python
def test_import_bingo_queue_command_is_facil_only_and_reports(bingo, store, monkeypatch):
    from handlers import bingo_queue
    monkeypatch.setattr(bingo_queue, "import_queue", AsyncMock(return_value=3))
    # is_facilitator is async in utils.auth; make the caller a facil
    monkeypatch.setattr("handlers.bingo.is_facilitator", AsyncMock(return_value=True), raising=False)
    from utils import auth
    monkeypatch.setattr(auth, "is_facilitator", AsyncMock(return_value=True))
    ctx = _context()
    upd = _text_update(100, "aria", "/import_bingo_queue")
    asyncio.run(bingo.import_bingo_queue(upd, ctx))
    bingo_queue.import_queue.assert_awaited_once()
    sent = upd.effective_message.reply_text.await_args.args[0]
    assert "3" in sent and "queue" in sent.lower()
```

> Note: `facil_only` wraps the handler and calls `utils.auth.is_facilitator`. Patch `utils.auth.is_facilitator` to an `AsyncMock(return_value=True)` so the wrapped handler runs. Follow how the existing facil-command tests in this file (e.g. for `roster_status`/`close_bingo_round`) authorize the caller and mirror that exactly.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bingo_handlers.py -q`
Expected: FAIL — `bingo.import_bingo_queue` undefined.

- [ ] **Step 3: Implement**

```python
# handlers/bingo.py — add near close_bingo_round (facil_only already imported)
@facil_only
async def import_bingo_queue(update, context):
    n = await bingo_queue.import_queue(context)
    await update.effective_message.reply_text(
        f"Imported {n} player(s) into the bingo queue — checking the earliest 10 "
        "now. 🎬"
    )
```

In `bingo.register(app)`, add next to the `close_bingo_round` registration:

```python
    app.add_handler(CommandHandler("import_bingo_queue", import_bingo_queue))
```

If the bot's `/help` output enumerates facil commands (search for where `close_bingo_round` or `roster_status` is described), add a one-line entry for `/import_bingo_queue` ("re-queue everyone who's already submitted"). If `/help` does not list these, skip.

- [ ] **Step 4: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (all existing + new). Sanity: `.venv/Scripts/python.exe -c "import main"` OK.

- [ ] **Step 5: Commit**

```bash
git add handlers/bingo.py tests/test_bingo_handlers.py
git commit -m "Add /import_bingo_queue facil command"
```

---

## Self-Review

- **Spec coverage:** round opens at 10 or on facil command (T2) ✓; `maybe_kickoff` gated on `queue_open` (T2) ✓; import = all non-winners, dedup to FIRST submission, supersede rest, preserve submit order, open + kickoff, idempotent (T4) ✓; winners excluded (T4) ✓; reconstruction from `winning_members` for import + restart fallback (T3) ✓; facil command (T5) ✓; `superseded` terminal status ignored by counts (T1 constraint, used T4) ✓.
- **Behavior change to flag:** submissions no longer message the first 10 eagerly — they wait until the round opens (10 queued or facil command). The four submit handler tests are rewritten (T2) to assert queued-then-opened, not weakened.
- **Idempotency:** `import_queue` skips players whose earliest is already `queued`/`confirming`; a player who later fails is eligible for re-import (fresh shot) — intended.
- **Restart robustness:** imported submissions carry a persisted `winning_members`, so `_rebuild_pending` restores their confirmation after a restart; new-flow queued rows (no members yet) still fall back to the resend prompt.
- **Type consistency:** `_read_from_members` returns `{"cells":[…]}` consumed by `evaluate`/`_send_confirmation`; `import_queue` seeds `_PENDING_READ[id] = {"read","handle","sheet_no"}` (same shape as `enqueue`); `is_queue_open`/`set_queue_open` used identically in `maybe_kickoff`/`enqueue`/`close_round`/`import_queue`.
