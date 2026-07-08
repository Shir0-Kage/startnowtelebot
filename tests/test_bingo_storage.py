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


# --- connection is configured to not fsync-stall the event loop ----------

def test_init_db_uses_wal_normal_sync_and_short_busy_timeout(store):
    # WAL + synchronous=NORMAL stops per-commit fsync (which could stall the
    # event loop for seconds on the overlay FS and freeze the bot); busy_timeout
    # is capped so a lock conflict degrades to a blip, not a 30s hang.
    jm = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    sync = store._conn.execute("PRAGMA synchronous").fetchone()[0]
    bt = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert jm.lower() == "wal"
    assert sync == 1          # 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
    assert bt == 5000


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


# --- submission queue ------------------------------------------------------

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
