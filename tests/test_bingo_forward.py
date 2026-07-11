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


# --- confirm / resend -------------------------------------------------------

_TOP_ROW = {(0, 0): "alice", (0, 1): "bob", (0, 2): "carol", (0, 3): "dan",
            (0, 4): "eve"}


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


def _ctx():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    return ctx


@pytest.fixture(autouse=True)
def _clear_pending_read():
    """_PENDING_READ is a module-global dict; clear it after every test so
    one test's leftovers can't leak into the next."""
    yield
    from handlers import bingo_forward
    bingo_forward._PENDING_READ.clear()


def test_confirm_button_marks_ready_when_fully_recognised(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(store, "user_id_for_handle", lambda h: 1, raising=False)
    sid = store.queue_forwarded_submission(100, "submitter", 1, "2026-01-01T09:00:00")
    bingo_forward._PENDING_READ[sid] = {
        "read": {"cells": _cells(_TOP_ROW)}, "handle": "submitter", "sheet_no": 1}
    q = AsyncMock(); q.data = f"bingofwd:confirm:{sid}"; q.from_user = MagicMock(id=100)
    upd = MagicMock(); upd.callback_query = q
    ctx = _ctx()
    asyncio.run(bingo_forward.confirm_button(upd, ctx))
    assert store.submission_status(sid) == "ready"
    members = store.winning_members(sid)
    assert len(members) == 5
    assert {m["handle"] for m in members} == set(_TOP_ROW.values())
    ctx.bot.send_message.assert_awaited_once()
    kwargs = ctx.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 100
    assert "released together" in kwargs["text"].lower()


def test_confirm_button_marks_ready_even_when_handle_unreachable(store, monkeypatch):
    """Reachability no longer matters: a complete roster-matched line is a
    win even when one of its handles has never /started the bot."""
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    unreachable = {"dan"}
    monkeypatch.setattr(store, "user_id_for_handle",
                         lambda h: None if h in unreachable else 1, raising=False)
    sid = store.queue_forwarded_submission(105, "submitter", 1, "2026-01-01T09:00:00")
    bingo_forward._PENDING_READ[sid] = {
        "read": {"cells": _cells(_TOP_ROW)}, "handle": "submitter", "sheet_no": 1}
    q = AsyncMock(); q.data = f"bingofwd:confirm:{sid}"; q.from_user = MagicMock(id=105)
    upd = MagicMock(); upd.callback_query = q
    ctx = _ctx()
    asyncio.run(bingo_forward.confirm_button(upd, ctx))
    assert store.submission_status(sid) == "ready"
    members = store.winning_members(sid)
    assert len(members) == 5
    assert {m["handle"] for m in members} == set(_TOP_ROW.values())


def test_confirm_button_incomplete_read_stays_confirming(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(store, "user_id_for_handle", lambda h: 1, raising=False)
    sid = store.queue_forwarded_submission(101, "submitter", 1, "2026-01-01T09:00:00")
    bingo_forward._PENDING_READ[sid] = {
        "read": {"cells": _cells({(0, 0): "alice"})},   # no full line
        "handle": "submitter", "sheet_no": 1}
    q = AsyncMock(); q.data = f"bingofwd:confirm:{sid}"; q.from_user = MagicMock(id=101)
    upd = MagicMock(); upd.callback_query = q
    asyncio.run(bingo_forward.confirm_button(upd, _ctx()))
    assert store.submission_status(sid) == "fwd_confirming"
    assert store.winning_members(sid) == []


def test_confirm_button_ignored_when_not_fwd_confirming(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    sid = store.queue_forwarded_submission(102, "submitter", 1, "2026-01-01T09:00:00")
    store.set_forward_ready(sid)                        # already resolved
    q = AsyncMock(); q.data = f"bingofwd:confirm:{sid}"; q.from_user = MagicMock(id=102)
    upd = MagicMock(); upd.callback_query = q
    ctx = _ctx()
    asyncio.run(bingo_forward.confirm_button(upd, ctx))
    ctx.bot.send_message.assert_not_awaited()


def test_on_resend_routes_fwd_confirming_user_to_ready(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(store, "user_id_for_handle", lambda h: 1, raising=False)
    sid = store.queue_forwarded_submission(103, "submitter", 1, "2026-01-01T09:00:00")
    handled = asyncio.run(bingo_forward.on_resend(
        _ctx(), 103, {"cells": _cells(_TOP_ROW)}))
    assert handled is True
    assert store.submission_status(sid) == "ready"
    assert len(store.winning_members(sid)) == 5


def test_on_resend_routes_to_ready_even_when_handle_unreachable(store, monkeypatch):
    """Same line-exists rule applies on resend: an unreachable handle in an
    otherwise complete line still marks the entry ready."""
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    unreachable = {"eve"}
    monkeypatch.setattr(store, "user_id_for_handle",
                         lambda h: None if h in unreachable else 1, raising=False)
    sid = store.queue_forwarded_submission(106, "submitter", 1, "2026-01-01T09:00:00")
    handled = asyncio.run(bingo_forward.on_resend(
        _ctx(), 106, {"cells": _cells(_TOP_ROW)}))
    assert handled is True
    assert store.submission_status(sid) == "ready"
    assert len(store.winning_members(sid)) == 5


def test_on_resend_incomplete_resends_confirmation(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(store, "user_id_for_handle", lambda h: 1, raising=False)
    sid = store.queue_forwarded_submission(104, "submitter", 1, "2026-01-01T09:00:00")
    monkeypatch.setattr(bingo_forward, "_send_confirmation", AsyncMock())
    handled = asyncio.run(bingo_forward.on_resend(
        _ctx(), 104, {"cells": _cells({(0, 0): "alice"})}))
    assert handled is True
    assert store.submission_status(sid) == "fwd_confirming"
    bingo_forward._send_confirmation.assert_awaited_once()


def test_on_resend_returns_false_for_unrelated_user(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    handled = asyncio.run(bingo_forward.on_resend(
        _ctx(), 999, {"cells": _cells(_TOP_ROW)}))
    assert handled is False


# --- collection close + direct winner selection ----------------------------

def test_maybe_close_collection_closes_at_target(store, monkeypatch):
    import config
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    store.set_forward_phase("collecting")
    # TARGET 'ready' entries -> closing claims the earliest 10 directly.
    for i in range(config.FORWARD_ROUND_TARGET):
        sid = store.queue_forwarded_submission(
            100 + i, f"u{i}", 1, f"2026-01-01T09:00:{i:02d}")
        store.set_forward_ready(sid)
    asyncio.run(bingo_forward.maybe_close_collection(_ctx()))
    assert store.forward_phase() == "released"
    assert store.bingo_prizes_claimed() == config.BINGO_PRIZE_LIMIT


def test_maybe_close_collection_noop_below_target(store, monkeypatch):
    import config
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo_forward, "_release_results", AsyncMock())
    store.set_forward_phase("collecting")
    for uid in range(1, config.FORWARD_ROUND_TARGET):        # one short
        store.queue_forwarded_submission(uid, f"u{uid}", 1, "2026-01-01T09:00:00")
    asyncio.run(bingo_forward.maybe_close_collection(_ctx()))
    assert store.forward_phase() == "collecting"
    bingo_forward._release_results.assert_not_awaited()


def test_select_winners_claims_ten_earliest_of_twelve(store, monkeypatch):
    import config
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    store.set_forward_phase("verifying")
    sids = []
    for i in range(12):
        sid = store.queue_forwarded_submission(
            200 + i, f"u{i}", 1, f"2026-01-01T09:00:{i:02d}")
        store.set_forward_ready(sid)
        sids.append(sid)
    asyncio.run(bingo_forward.select_winners(_ctx()))
    claimed = [sid for sid in sids if store.submission_status(sid) == "verified"]
    still_ready = [sid for sid in sids if store.submission_status(sid) == "ready"]
    # earliest-first order, capped at the 10-prize limit
    assert claimed == sids[:config.BINGO_PRIZE_LIMIT]
    assert still_ready == sids[config.BINGO_PRIZE_LIMIT:]
    assert store.bingo_prizes_claimed() == config.BINGO_PRIZE_LIMIT


def test_close_collection_claims_winners_and_dms(store, monkeypatch):
    from handlers import bingo, bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo, "_admin_recipient_ids", lambda: set())
    store.set_forward_phase("verifying")
    for i, uid in enumerate([301, 302, 303]):
        sid = store.queue_forwarded_submission(
            uid, f"w{i}", 1, f"2026-01-01T09:00:{i:02d}")
        store.set_forward_ready(sid)
    ctx = _ctx()
    asyncio.run(bingo_forward.close_collection(ctx))
    assert store.forward_phase() == "released"
    assert store.bingo_prizes_claimed() == 3
    winners = {w["winner_user_id"] for w in store.all_bingo_prizes()}
    assert winners == {301, 302, 303}
    dmd = {c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list}
    assert {301, 302, 303} <= dmd     # each winner DM'd


def test_close_collection_idempotent_once_released(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo_forward, "select_winners", AsyncMock())
    monkeypatch.setattr(bingo_forward, "_release_results", AsyncMock())
    store.set_forward_phase("released")
    asyncio.run(bingo_forward.close_collection(_ctx()))
    bingo_forward.select_winners.assert_not_awaited()
    bingo_forward._release_results.assert_not_awaited()


# --- batch results: hold announcements, release together ------------------

def test_release_results_dms_all_winners_and_admin_summary(store, monkeypatch):
    from handlers import bingo, bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"zzehao"})
    store.mark_started(999, "zzehao", "Zhou")
    store.set_forward_phase("verifying")
    store.claim_bingo_prize(1, "alice", 101)
    store.claim_bingo_prize(2, "bob", 102)
    ctx = _ctx()
    asyncio.run(bingo_forward._release_results(ctx))

    assert store.forward_phase() == "released"
    dm_calls = {c.kwargs["chat_id"]: c.kwargs["text"]
                for c in ctx.bot.send_message.await_args_list}
    assert "winners" in dm_calls[1].lower()
    assert "winners" in dm_calls[2].lower()
    admin_text = dm_calls[999]
    assert "alice" in admin_text and "bob" in admin_text
    assert store.winners_pending_admin_notice() == []   # all marked notified


def test_release_results_no_admin_reachable_leaves_winners_unmarked(store, monkeypatch):
    from handlers import bingo, bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo, "_admin_recipient_ids", lambda: set())
    store.set_forward_phase("verifying")
    store.claim_bingo_prize(1, "alice", 101)
    store.claim_bingo_prize(2, "bob", 102)
    ctx = _ctx()
    asyncio.run(bingo_forward._release_results(ctx))

    assert store.forward_phase() == "released"
    dm_calls = {c.kwargs["chat_id"]: c.kwargs["text"]
                for c in ctx.bot.send_message.await_args_list}
    assert "winners" in dm_calls[1].lower()
    assert "winners" in dm_calls[2].lower()
    assert len(dm_calls) == 2   # no summary DM sent to anyone
    pending = {w["winner_user_id"] for w in store.winners_pending_admin_notice()}
    assert pending == {1, 2}   # both left unmarked for the WN sweep to retry


def test_release_results_with_no_winners_sets_released(store, monkeypatch):
    """An empty/late round: _release_results may be called with 0 winners; it
    just DMs nobody and marks the round released."""
    from handlers import bingo, bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo, "_admin_recipient_ids", lambda: set())
    store.set_forward_phase("verifying")
    ctx = _ctx()
    asyncio.run(bingo_forward._release_results(ctx))
    assert store.forward_phase() == "released"
    ctx.bot.send_message.assert_not_awaited()


# --- begin_round: broadcast + phase + deadline timer -----------------------

def test_begin_round_dms_all_allocations_sets_phase_returns_count(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    for uid, h in [(1, "alice"), (2, "bob"), (3, "carol")]:
        store.allocate_bingo_sheet(uid, h)
    ctx = MagicMock(); ctx.bot = AsyncMock(); ctx.job_queue = MagicMock()
    n = asyncio.run(bingo_forward.begin_round(ctx))
    assert n == 3
    assert store.forward_phase() == "collecting"
    dmd = {c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list}
    assert dmd == {1, 2, 3}
    # the 2-day deadline job is armed exactly once
    ctx.job_queue.run_once.assert_called_once()
    assert ctx.job_queue.run_once.call_args.kwargs["name"] == "bingo:forward_deadline"


def test_begin_round_noop_when_round_already_in_progress(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    store.set_forward_phase("verifying")            # a round is already live
    ctx = MagicMock(); ctx.bot = AsyncMock(); ctx.job_queue = MagicMock()
    n = asyncio.run(bingo_forward.begin_round(ctx))
    assert n == -1                                  # sentinel
    ctx.bot.send_message.assert_not_awaited()       # nothing re-broadcast
    ctx.job_queue.run_once.assert_not_called()      # deadline not re-armed


# --- late confirm mid-'verifying' still claims directly + releases ----------

def test_late_confirm_during_verifying_claims_and_releases(store, monkeypatch):
    """A confirm that lands while the round is 'verifying' (a slot still free)
    claims the entry directly as a winner and releases the results — no
    tagged-people verification."""
    from handlers import bingo, bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(store, "user_id_for_handle", lambda h: 1, raising=False)
    monkeypatch.setattr(bingo, "_admin_recipient_ids", lambda: set())
    store.set_forward_phase("verifying")
    sid = store.queue_forwarded_submission(100, "submitter", 1, "2026-01-01T09:00:00")
    bingo_forward._PENDING_READ[sid] = {
        "read": {"cells": _cells(_TOP_ROW)}, "handle": "submitter", "sheet_no": 1}
    q = AsyncMock(); q.data = f"bingofwd:confirm:{sid}"; q.from_user = MagicMock(id=100)
    upd = MagicMock(); upd.callback_query = q
    ctx = _ctx(); ctx.job_queue = MagicMock()
    asyncio.run(bingo_forward.confirm_button(upd, ctx))
    assert store.submission_status(sid) == "verified"   # claimed directly
    assert store.bingo_prizes_claimed() == 1
    assert store.forward_phase() == "released"           # released after claim
    assert sid not in bingo_forward._PENDING_READ         # cleaned up


def test_forward_timeout_job_releases_when_nothing_ready(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    # Already 'verifying' with nothing ready: the deadline backstop must settle
    # the round instead of leaving it stuck forever.
    store.set_forward_phase("verifying")
    asyncio.run(bingo_forward._forward_timeout_job(_ctx()))
    assert store.forward_phase() == "released"
