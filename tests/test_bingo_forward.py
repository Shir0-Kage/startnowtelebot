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
