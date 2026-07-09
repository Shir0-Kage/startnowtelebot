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
