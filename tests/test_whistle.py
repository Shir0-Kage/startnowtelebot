"""Tests for handlers/whistle.py — anonymous whistleblowing capture + commands.

Telegram Bot calls are mocked so these run fully offline. Async handlers are
invoked with asyncio.run so the suite needs no pytest-asyncio plugin. The
`store` fixture (copied from tests/test_bingo_storage.py) rebinds storage to
an isolated temp DB, and handlers.whistle.storage is monkeypatched to it so
the handlers under test hit that same temp DB.
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CommandHandler, MessageHandler


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
    """handlers.whistle bound to the temp-DB `store`."""
    import handlers.whistle as whistle
    monkeypatch.setattr(whistle, "storage", store)
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
    return ctx


# --- on_channel_autoforward -------------------------------------------------

def test_autoforward_captures_link_and_resolves_matching_pending(whistle, store):
    store.set_whistle_pending(77)  # admin already ran /start_whistle

    upd = MagicMock()
    msg = MagicMock()
    msg.is_automatic_forward = True
    msg.forward_from_chat = SimpleNamespace(id=-100123)
    msg.sender_chat = None
    msg.forward_from_message_id = 77
    msg.message_id = 500
    msg.chat = SimpleNamespace(id=-100456)
    upd.effective_message = msg

    asyncio.run(whistle.on_channel_autoforward(upd, _context()))

    assert store.get_whistle_link() == (-100123, -100456)
    assert store.get_whistle_anchor() == (-100456, 500)


def test_autoforward_remembers_forward_map(whistle, store):
    upd = MagicMock()
    msg = MagicMock()
    msg.is_automatic_forward = True
    msg.forward_from_chat = SimpleNamespace(id=-100123)
    msg.sender_chat = None
    msg.forward_from_message_id = 29
    msg.message_id = 812
    msg.chat = SimpleNamespace(id=-100456)
    upd.effective_message = msg

    asyncio.run(whistle.on_channel_autoforward(upd, _context()))

    assert store.lookup_forward(29) == (-100456, 812)


def test_autoforward_ignores_non_forwarded_message(whistle, store):
    upd = MagicMock()
    msg = MagicMock()
    msg.is_automatic_forward = False
    upd.effective_message = msg

    asyncio.run(whistle.on_channel_autoforward(upd, _context()))

    assert store.get_whistle_link() == (None, None)


# --- on_channel_post --------------------------------------------------------

def test_channel_post_captures_channel_id(whistle, store):
    upd = MagicMock()
    upd.effective_chat = SimpleNamespace(id=-100123, type="channel")
    upd.effective_message = MagicMock()

    asyncio.run(whistle.on_channel_post(upd, _context()))

    channel_id, _group = store.get_whistle_link()
    assert channel_id == -100123


def test_channel_post_lets_start_whistle_open_without_autoforward(whistle, store, monkeypatch):
    # the bot only ever saw a channel post (no discussion-group auto-forward yet)
    post = MagicMock()
    post.effective_chat = SimpleNamespace(id=-100123, type="channel")
    post.effective_message = MagicMock()
    asyncio.run(whistle.on_channel_post(post, _context()))

    monkeypatch.setattr(whistle, "is_admin", lambda user: True)
    upd = _update(chat_type="group")
    ctx = _context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=321))

    asyncio.run(whistle.start_whistle(upd, ctx))

    ctx.bot.send_message.assert_awaited_once_with(chat_id=-100123, text=whistle._BASE_TEXT)
    assert store._whistle_row()["pending_channel_msg_id"] == 321


def test_channel_post_preserves_known_group_id(whistle, store):
    store.set_whistle_link(-100123, -100456)  # both already known
    upd = MagicMock()
    upd.effective_chat = SimpleNamespace(id=-100123, type="channel")
    upd.effective_message = MagicMock()

    asyncio.run(whistle.on_channel_post(upd, _context()))

    assert store.get_whistle_link() == (-100123, -100456)  # group_id untouched


# --- start_whistle -----------------------------------------------------------

def test_start_whistle_blocks_non_admin(whistle, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: False)
    upd = _update(chat_type="group")
    ctx = _context()

    asyncio.run(whistle.start_whistle(upd, ctx))

    upd.effective_message.reply_text.assert_awaited_once()
    assert "admin" in upd.effective_message.reply_text.call_args[0][0].lower()
    ctx.bot.send_message.assert_not_called()


def test_start_whistle_not_linked_yet(whistle, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: True)
    upd = _update(chat_type="group")
    ctx = _context()

    asyncio.run(whistle.start_whistle(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "not linked" in reply.lower()
    ctx.bot.send_message.assert_not_called()


def test_start_whistle_linked_posts_base_and_sets_pending(whistle, store, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: True)
    store.set_whistle_link(-100123, -100456)
    upd = _update(chat_type="group")
    ctx = _context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=321))

    asyncio.run(whistle.start_whistle(upd, ctx))

    ctx.bot.send_message.assert_awaited_once_with(chat_id=-100123, text=whistle._BASE_TEXT)
    row = store._whistle_row()
    assert row["pending_channel_msg_id"] == 321
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "posted" in reply.lower()


# --- set_whistle_base --------------------------------------------------------

def test_base_msg_id_parses_link_and_bare_number(whistle):
    assert whistle._base_msg_id("https://t.me/c/4292606016/29") == 29
    assert whistle._base_msg_id("/set_whistle_base 29") == 29
    assert whistle._base_msg_id("no numbers here") is None


def test_set_base_blocks_non_admin(whistle, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: False)
    upd = _update(chat_type="private", text="/set_whistle_base https://t.me/c/4292606016/29")
    ctx = _context()

    asyncio.run(whistle.set_whistle_base(upd, ctx))

    assert "admin" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_set_base_requires_private(whistle, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: True)
    upd = _update(chat_type="group", text="/set_whistle_base https://t.me/c/4292606016/29")
    ctx = _context()

    asyncio.run(whistle.set_whistle_base(upd, ctx))

    assert "dm me" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_set_base_unknown_post_not_seen(whistle, store, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: True)
    upd = _update(chat_type="private", text="/set_whistle_base https://t.me/c/4292606016/29")
    ctx = _context()

    asyncio.run(whistle.set_whistle_base(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0].lower()
    assert "haven't seen" in reply
    assert store.get_whistle_anchor() == (None, None)  # anchor left untouched


def test_set_base_happy_path_adopts_group_copy_as_anchor(whistle, store, monkeypatch):
    monkeypatch.setattr(whistle, "is_admin", lambda user: True)
    store.remember_forward(29, -100456, 812)  # bot saw the auto-forward earlier
    upd = _update(chat_type="private", text="/set_whistle_base https://t.me/c/4292606016/29")
    ctx = _context()

    asyncio.run(whistle.set_whistle_base(upd, ctx))

    assert store.get_whistle_anchor() == (-100456, 812)
    assert "base message set" in upd.effective_message.reply_text.call_args[0][0].lower()


# --- whistle -------------------------------------------------------------

def test_whistle_refuses_outside_private_chat(whistle):
    upd = _update(chat_type="group", text="/whistle something bad happened")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "private" in reply.lower() or "dm" in reply.lower()
    ctx.bot.send_message.assert_not_called()


def test_whistle_refuses_when_no_active_anchor(whistle):
    upd = _update(chat_type="private", text="/whistle something bad happened")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "no whistle thread" in reply.lower()
    ctx.bot.send_message.assert_not_called()


def test_whistle_happy_path_posts_anonymously_and_confirms(whistle, store):
    store.set_whistle_link(-100123, -100456)
    store.set_whistle_pending(77)
    store.resolve_whistle_anchor(77, 500)  # (group_id, anchor) == (-100456, 500)

    report_text = "Someone is being unsafe near the AV room\nplease help"
    upd = _update(user_id=999, username="secretive_sam", text="/whistle " + report_text)
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    ctx.bot.send_message.assert_awaited_once()
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == -100456
    assert kwargs["reply_to_message_id"] == 500
    assert report_text in kwargs["text"]
    assert "anonymous" in kwargs["text"].lower()
    # anonymity: the sender's id/username must never appear in the posted text
    assert "999" not in kwargs["text"]
    assert "secretive_sam" not in kwargs["text"]

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "sent anonymously" in reply.lower()


def _whistle_ready(store):
    store.set_whistle_link(-100123, -100456)
    store.set_whistle_pending(77)
    store.resolve_whistle_anchor(77, 500)  # (group_id, anchor) == (-100456, 500)


def test_whistle_stores_undo_handle_and_mentions_undo(whistle, store):
    _whistle_ready(store)
    upd = _update(chat_type="private", text="/whistle a concern")
    ctx = _context()
    ctx.user_data = {}
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=555))

    asyncio.run(whistle.whistle(upd, ctx))

    # only message ids are remembered (in-memory), never the sender's identity
    assert ctx.user_data["last_whistle"] == {"chat_id": -100456, "message_id": 555}
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "sent anonymously" in reply.lower()
    assert "/undo_whistle" in reply


def test_whistle_rejects_empty_body(whistle, store):
    store.set_whistle_link(-100123, -100456)
    store.set_whistle_pending(77)
    store.resolve_whistle_anchor(77, 500)

    upd = _update(chat_type="private", text="/whistle")
    ctx = _context()

    asyncio.run(whistle.whistle(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "/whistle" in reply
    ctx.bot.send_message.assert_not_called()


# --- undo_whistle -----------------------------------------------------------

def test_undo_deletes_last_report_and_clears(whistle):
    upd = _update(chat_type="private", text="/undo_whistle")
    ctx = _context()
    ctx.user_data = {"last_whistle": {"chat_id": -100456, "message_id": 555}}
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(whistle.undo_whistle(upd, ctx))

    ctx.bot.delete_message.assert_awaited_once_with(chat_id=-100456, message_id=555)
    assert "last_whistle" not in ctx.user_data
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "removed" in reply.lower()


def test_undo_with_nothing_to_remove(whistle):
    upd = _update(chat_type="private", text="/undo_whistle")
    ctx = _context()
    ctx.user_data = {}
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(whistle.undo_whistle(upd, ctx))

    ctx.bot.delete_message.assert_not_called()
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "nothing to undo" in reply.lower()


def test_undo_refuses_outside_private_chat(whistle):
    upd = _update(chat_type="group", text="/undo_whistle")
    ctx = _context()
    ctx.user_data = {"last_whistle": {"chat_id": -100456, "message_id": 555}}
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(whistle.undo_whistle(upd, ctx))

    ctx.bot.delete_message.assert_not_called()
    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "private" in reply.lower() or "dm" in reply.lower()


# --- register wires the handlers --------------------------------------

def test_register_adds_whistle_handlers(whistle):
    app = MagicMock()
    whistle.register(app)

    command_calls = [
        c for c in app.add_handler.call_args_list
        if c.args and isinstance(c.args[0], CommandHandler)
    ]
    callbacks = {getattr(c.args[0], "callback", None) for c in command_calls}
    assert whistle.start_whistle in callbacks
    assert whistle.set_whistle_base in callbacks
    assert whistle.whistle in callbacks
    assert whistle.undo_whistle in callbacks

    forward_calls = [
        c for c in app.add_handler.call_args_list
        if c.args and isinstance(c.args[0], MessageHandler)
        and getattr(c.args[0], "callback", None) is whistle.on_channel_autoforward
    ]
    assert forward_calls, "auto-forward MessageHandler was not registered"

    post_calls = [
        c for c in app.add_handler.call_args_list
        if c.args and isinstance(c.args[0], MessageHandler)
        and getattr(c.args[0], "callback", None) is whistle.on_channel_post
    ]
    assert post_calls, "channel-post MessageHandler was not registered"
