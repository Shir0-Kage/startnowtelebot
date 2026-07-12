"""Tests for handlers/whistle.py — anonymous whistleblowing relayed to the
organisers' DMs.

Telegram Bot calls are mocked so these run fully offline. Async handlers are
invoked with asyncio.run so the suite needs no pytest-asyncio plugin. The
`store` fixture (copied from tests/test_bingo_storage.py) rebinds storage to an
isolated temp DB, handlers.whistle.storage is monkeypatched to it, and three
recipients are marked /started so they're reachable.
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CommandHandler


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh storage module bound to an isolated temp DB."""
    import config
    import storage
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "whistle_test.db"))
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "whistle_test.db"))
    importlib.reload(storage)  # rebind DB_PATH captured at import time
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "whistle_test.db"))
    storage.init_db()
    return storage


@pytest.fixture()
def whistle(store, monkeypatch):
    """handlers.whistle bound to the temp-DB `store`, with three reachable
    recipients (@zzehao -> 11, @jvsoh -> 22, @dxnellek -> 33)."""
    import config
    import handlers.whistle as whistle
    monkeypatch.setattr(whistle, "storage", store)
    monkeypatch.setattr(config, "WHISTLE_RECIPIENTS", {"zzehao", "jvsoh", "dxnellek"})
    store.mark_started(11, "zzehao", "Zee")
    store.mark_started(22, "jvsoh", "Jay")
    store.mark_started(33, "dxnellek", "Dee")
    return whistle


def _msg(text=None):
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    return msg


def _update(user_id=100, username="reporter", chat_type="private", text=None):
    upd = MagicMock()
    upd.effective_user = SimpleNamespace(id=user_id, username=username, full_name="A Person")
    upd.effective_chat = SimpleNamespace(id=user_id, type=chat_type)
    upd.effective_message = _msg(text)
    return upd


def _context():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    # each send returns a message whose id is derived from the recipient chat_id
    ctx.bot.send_message = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(message_id=1000 + kw["chat_id"]))
    ctx.user_data = {}
    return ctx


# --- whistle: fan-out to organiser DMs --------------------------------------

def test_whistle_refuses_outside_private_chat(whistle):
    upd = _update(chat_type="group", text="/whistle something bad happened")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "private" in reply.lower() or "dm" in reply.lower()
    ctx.bot.send_message.assert_not_called()


def test_whistle_rejects_empty_body(whistle):
    upd = _update(chat_type="private", text="/whistle")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "/whistle" in reply
    ctx.bot.send_message.assert_not_called()


def test_whistle_dms_every_recipient_anonymously(whistle):
    report = "Someone is being unsafe near the AV room\nplease help"
    upd = _update(user_id=999, username="secretive_sam", text="/whistle " + report)
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    assert ctx.bot.send_message.await_count == 3
    targets = {c.kwargs["chat_id"] for c in ctx.bot.send_message.call_args_list}
    assert targets == {11, 22, 33}
    for c in ctx.bot.send_message.call_args_list:
        body = c.kwargs["text"]
        assert report in body
        assert "anonymous" in body.lower()
        # anonymity: the sender's id/username must never appear in the relayed text
        assert "999" not in body
        assert "secretive_sam" not in body

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "sent anonymously" in reply.lower()
    assert "/undo_whistle" in reply


def test_whistle_records_undo_handles_for_all_copies(whistle):
    upd = _update(text="/whistle a concern")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    handles = ctx.user_data["last_whistle"]
    assert {h["chat_id"] for h in handles} == {11, 22, 33}
    assert all("message_id" in h for h in handles)


def test_whistle_reports_when_no_recipient_reachable(whistle, monkeypatch):
    import config
    monkeypatch.setattr(config, "WHISTLE_RECIPIENTS", {"nobody_started"})
    upd = _update(text="/whistle a concern")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "last_whistle" not in ctx.user_data
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "couldn't reach" in reply.lower()


def test_whistle_delivers_to_reachable_subset(whistle, monkeypatch):
    import config
    # one known recipient plus one who never /started
    monkeypatch.setattr(config, "WHISTLE_RECIPIENTS", {"zzehao", "ghost"})
    upd = _update(text="/whistle a concern")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    assert ctx.bot.send_message.await_count == 1
    assert ctx.bot.send_message.call_args.kwargs["chat_id"] == 11
    assert {h["chat_id"] for h in ctx.user_data["last_whistle"]} == {11}
    assert "sent anonymously" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_whistle_survives_partial_send_failure(whistle):
    upd = _update(text="/whistle a concern")
    ctx = _context()

    def _send(**kw):
        if kw["chat_id"] == 22:
            raise RuntimeError("blocked the bot")
        return SimpleNamespace(message_id=1000 + kw["chat_id"])
    ctx.bot.send_message = AsyncMock(side_effect=_send)

    asyncio.run(whistle.whistle(upd, ctx))

    # 11 and 33 delivered; 22 failed silently (never surfaced to reporter)
    assert {h["chat_id"] for h in ctx.user_data["last_whistle"]} == {11, 33}
    assert "sent anonymously" in upd.effective_message.reply_text.call_args[0][0].lower()


# --- undo_whistle -----------------------------------------------------------

def test_undo_deletes_all_delivered_copies_and_clears(whistle):
    upd = _update(chat_type="private", text="/undo_whistle")
    ctx = _context()
    ctx.user_data = {"last_whistle": [
        {"chat_id": 11, "message_id": 1011},
        {"chat_id": 33, "message_id": 1033}]}
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(whistle.undo_whistle(upd, ctx))

    deleted = {c.kwargs["chat_id"] for c in ctx.bot.delete_message.call_args_list}
    assert deleted == {11, 33}
    assert "last_whistle" not in ctx.user_data
    assert "removed" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_undo_with_nothing_to_remove(whistle):
    upd = _update(chat_type="private", text="/undo_whistle")
    ctx = _context()
    ctx.user_data = {}
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(whistle.undo_whistle(upd, ctx))

    ctx.bot.delete_message.assert_not_called()
    assert "nothing to undo" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_undo_refuses_outside_private_chat(whistle):
    upd = _update(chat_type="group", text="/undo_whistle")
    ctx = _context()
    ctx.user_data = {"last_whistle": [{"chat_id": 11, "message_id": 1011}]}
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(whistle.undo_whistle(upd, ctx))

    ctx.bot.delete_message.assert_not_called()
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "private" in reply.lower() or "dm" in reply.lower()


# --- register wires the commands --------------------------------------------

def test_register_adds_whistle_commands(whistle):
    app = MagicMock()
    whistle.register(app)

    callbacks = {
        getattr(c.args[0], "callback", None)
        for c in app.add_handler.call_args_list
        if c.args and isinstance(c.args[0], CommandHandler)
    }
    assert whistle.whistle in callbacks
    assert whistle.undo_whistle in callbacks
