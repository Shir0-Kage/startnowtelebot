import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import bingo_lines
from handlers import bingo_queue


@pytest.fixture(autouse=True)
def _clear_pending_read():
    """_PENDING_READ is a module-global dict; a test that fails an assertion
    mid-way can leak its entries into later tests. Clear it after every test so
    each starts from a clean slate (makes the manual .pop() calls redundant)."""
    yield
    bingo_queue._PENDING_READ.clear()


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


_TOP_ROW = {(0, 0): "alice", (0, 1): "bob", (0, 2): "carol", (0, 3): "dan",
            (0, 4): "eve"}


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


class FakeStore:
    """In-memory stand-in for the queue subset of storage."""
    def __init__(self):
        self.rows = {}
        self._id = 0
        self._open = False
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
    def is_queue_open(self):
        return self._open
    def set_queue_open(self):
        self._open = True


def test_kickoff_promotes_only_ten_earliest(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    for uid in range(1, 13):
        fake.queue_submission(uid, f"u{uid}", 1)
    fake.set_queue_open()                                # round already open
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
    fake.set_queue_open()                                # round already open
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
    fake.set_queue_open()                                # a confirming sub implies open
    bingo_queue._PENDING_READ[b] = {"read": {"cells": _cells({})},
                                    "handle": "b", "sheet_no": 1}
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    ctx = _ctx(); ctx.job = MagicMock(); ctx.job.data = {"submission_id": a}
    asyncio.run(bingo_queue._confirm_timeout_job(ctx))
    assert fake.submission_status(a) == "failed"
    assert fake.submission_status(b) == "confirming"     # next promoted
    bingo_queue._PENDING_READ.pop(b, None)


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


def test_confirm_button_dms_resend_when_pending_read_lost(monkeypatch):
    # FIX D: _PENDING_READ is in-memory and empty after a restart. A confirming
    # submitter who taps Confirm must be asked to resend (not silently ignored),
    # and we must NOT start verification off a phantom (missing) read.
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    sid = fake.queue_submission(1, "submitter", 1)
    fake.set_submission_status(sid, "confirming")
    # deliberately NO bingo_queue._PENDING_READ[sid] -- simulates a restart
    monkeypatch.setattr(bingo_queue, "_start_verification", AsyncMock())
    q = AsyncMock(); q.data = f"bingoq:confirm:{sid}"; q.from_user = MagicMock(id=1)
    upd = MagicMock(); upd.callback_query = q
    ctx = _ctx()
    asyncio.run(bingo_queue.confirm_button(upd, ctx))
    assert bingo_queue._start_verification.await_count == 0   # no phantom verify
    ctx.bot.send_message.assert_awaited()                     # not a silent no-op
    dm = ctx.bot.send_message.await_args
    assert dm.kwargs["chat_id"] == 1
    assert "resend" in dm.kwargs["text"].lower()


def test_on_resend_routes_by_recognition(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(fake, "user_id_for_handle", lambda h: 1, raising=False)
    sid = fake.queue_submission(5, "submitter", 1)
    fake.set_submission_status(sid, "confirming")
    monkeypatch.setattr(bingo_queue, "_start_verification", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    # fully recognised resend -> hand off to verification
    handled = asyncio.run(bingo_queue.on_resend(_ctx(), 5, {"cells": _cells(_TOP_ROW)}))
    assert handled is True
    assert bingo_queue._start_verification.await_count == 1
    assert bingo_queue._send_confirmation.await_count == 0
    # still-incomplete resend -> re-show the full confirmation
    asyncio.run(bingo_queue.on_resend(_ctx(), 5, {"cells": _cells({})}))
    assert bingo_queue._send_confirmation.await_count == 1
    # a user with no confirming submission is not handled
    assert asyncio.run(bingo_queue.on_resend(_ctx(), 999, {"cells": _cells(_TOP_ROW)})) is False
    bingo_queue._PENDING_READ.pop(sid, None)


def test_start_verification_hands_off_to_existing_pipeline(monkeypatch):
    from handlers import bingo
    fake = FakeStore()
    monkeypatch.setattr(bingo_queue, "storage", fake)
    monkeypatch.setattr(fake, "user_id_for_handle", lambda h: 1, raising=False)
    fake.record_winning_members = MagicMock()
    sid = fake.queue_submission(1, "submitter", 1)
    fake.set_submission_status(sid, "confirming")
    monkeypatch.setattr(bingo, "_cancel_job", MagicMock())
    monkeypatch.setattr(bingo, "_dm_subjects", AsyncMock())
    monkeypatch.setattr(bingo, "_finalize", AsyncMock())
    line = [(0, 0, "alice"), (0, 1, "bob"), (0, 2, "carol"), (0, 3, "dan"), (0, 4, "eve")]
    ctx = _ctx(); ctx.job_queue = None            # skip the tagged-people timeout arming
    asyncio.run(bingo_queue._start_verification(ctx, sid, line, "submitter", 1))
    assert fake.submission_status(sid) == "pending"      # handed to existing pipeline
    fake.record_winning_members.assert_called_once()
    bingo._cancel_job.assert_called_once()               # confirm-timeout cancelled
    bingo._dm_subjects.assert_awaited_once()
    bingo._finalize.assert_awaited_once()


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
