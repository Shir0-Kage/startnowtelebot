"""Tests for handlers/announcements.py — /announce is DM-only and broadcasts the
message verbatim to every group.

Bot calls are mocked; the @facil_only gate is bypassed by monkeypatching
utils.auth.is_facilitator to True. A temp-DB `store` (same pattern as the other
suites) provides the group list.
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import config
    import storage
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "ann.db"))
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "ann.db"))
    importlib.reload(storage)
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "ann.db"))
    storage.init_db()
    return storage


@pytest.fixture()
def ann(store, monkeypatch):
    import handlers.announcements as ann
    from utils import auth
    monkeypatch.setattr(ann, "storage", store)
    monkeypatch.setattr(auth, "is_facilitator", AsyncMock(return_value=True))
    return ann


def _update(text, chat_type="private"):
    upd = MagicMock()
    upd.effective_user = SimpleNamespace(id=1, username="zzehao", full_name="Z")
    upd.effective_chat = SimpleNamespace(id=1, type=chat_type)
    msg = upd.effective_message
    msg.text = text
    msg.reply_text = AsyncMock()
    return upd


def _ctx():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    return ctx


def test_announce_broadcasts_verbatim_to_every_group(ann, store):
    store.ensure_group(-100, "AM Group")
    store.ensure_group(-200, "PM Group")
    store.ensure_group(777, "A DM'd Person")     # private chat (positive id)
    body = "Meet Up 1 is on <b>tomorrow</b> at 10am!"
    upd = _update("/announce " + body)
    ctx = _ctx()

    asyncio.run(ann.announce_command(upd, ctx))

    targets = {c.kwargs["chat_id"] for c in ctx.bot.send_message.call_args_list}
    assert targets == {-100, -200}               # groups only, never the DM (777)
    for c in ctx.bot.send_message.call_args_list:
        # verbatim: exact text, no header/footer, no HTML parse_mode
        assert c.kwargs["text"] == body
        assert "parse_mode" not in c.kwargs
    assert "2 group" in upd.effective_message.reply_text.call_args[0][0]


def test_announce_rejects_non_zzehao(ann, store):
    store.ensure_group(-100, "AM Group")
    upd = _update("/announce hi")
    upd.effective_user = SimpleNamespace(id=2, username="aria", full_name="Aria")
    ctx = _ctx()

    asyncio.run(ann.announce_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "zzehao" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_announce_refuses_in_group_chat(ann, store):
    store.ensure_group(-100, "AM Group")
    upd = _update("/announce hi", chat_type="supergroup")
    ctx = _ctx()

    asyncio.run(ann.announce_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "dm me" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_announce_requires_a_message(ann, store):
    store.ensure_group(-100, "AM Group")
    upd = _update("/announce")
    ctx = _ctx()

    asyncio.run(ann.announce_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "/announce" in upd.effective_message.reply_text.call_args[0][0]


def test_announce_reports_when_no_groups(ann, store):
    upd = _update("/announce hello everyone")
    ctx = _ctx()

    asyncio.run(ann.announce_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "not in any groups" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_announce_counts_failed_deliveries(ann, store):
    store.ensure_group(-100, "AM Group")
    store.ensure_group(-200, "PM Group")
    upd = _update("/announce ping")
    ctx = _ctx()

    def _send(**kw):
        if kw["chat_id"] == -200:
            raise RuntimeError("bot removed from group")
        return SimpleNamespace(message_id=1)
    ctx.bot.send_message = AsyncMock(side_effect=_send)

    asyncio.run(ann.announce_command(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "1 group" in reply           # one delivered
    assert "couldn't reach 1" in reply.lower()
