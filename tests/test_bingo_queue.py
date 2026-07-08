import asyncio
from unittest.mock import AsyncMock, MagicMock

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
