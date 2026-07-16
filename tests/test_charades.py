"""Tests for handlers/charades.py — /charades hands out a random word, privately."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.charades_words import WORDS
from handlers import charades


def _update(chat_type="private", uid=100):
    upd = MagicMock()
    upd.effective_user = SimpleNamespace(id=uid, username="alice", full_name="Alice Tan")
    upd.effective_chat = SimpleNamespace(id=uid if chat_type == "private" else -100,
                                         type=chat_type)
    msg = upd.effective_message
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    return upd


def _ctx():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    return ctx


# --- the word list ----------------------------------------------------------

def test_word_list_is_healthy():
    assert len(WORDS) >= 100                       # plenty of variety
    assert all(isinstance(w, str) and w.strip() for w in WORDS)
    assert len(WORDS) == len(set(WORDS)), "duplicate charades words"
    # HTML-safe: the word is sent inside a <b> tag
    assert not any("<" in w or ">" in w or "&" in w for w in WORDS)


def test_original_words_are_all_present():
    for w in ["Swimming", "Superhero", "Elephant", "Brushing teeth", "Cooking",
              "Astronaut", "Fishing", "Playing guitar", "Sleeping", "Boxing",
              "Photographer", "Penguin", "Driving a car", "Ghost",
              "Playing basketball", "Chef", "Robot", "Riding a horse",
              "Painting", "Sneezing", "Bodybuilder", "Surfing", "Crying baby",
              "Magician", "Chicken", "Typing", "Skateboarding", "Waking up",
              "Ballet dancing"]:
        assert w in WORDS


# --- private chat -----------------------------------------------------------

def test_charades_in_dm_replies_with_a_word(monkeypatch):
    monkeypatch.setattr(charades.random, "choice", lambda seq: "Penguin")
    upd = _update(chat_type="private")
    ctx = _ctx()

    asyncio.run(charades.charades(upd, ctx))

    body = upd.effective_message.reply_html.call_args[0][0]
    assert "Penguin" in body
    ctx.bot.send_message.assert_not_called()       # no DM needed, already in one


def test_charades_picks_from_the_word_list():
    upd = _update(chat_type="private")
    ctx = _ctx()

    asyncio.run(charades.charades(upd, ctx))

    body = upd.effective_message.reply_html.call_args[0][0]
    assert any(w in body for w in WORDS)


# --- group chat: the word must stay secret ----------------------------------

def test_charades_in_group_dms_the_word_and_does_not_reveal_it(monkeypatch):
    monkeypatch.setattr(charades.random, "choice", lambda seq: "Penguin")
    upd = _update(chat_type="supergroup")
    ctx = _ctx()

    asyncio.run(charades.charades(upd, ctx))

    # DMed privately to the player
    ctx.bot.send_message.assert_awaited_once()
    assert ctx.bot.send_message.call_args.kwargs["chat_id"] == 100
    assert "Penguin" in ctx.bot.send_message.call_args.kwargs["text"]
    # the group reply names the player but never the word
    group_reply = upd.effective_message.reply_html.call_args[0][0]
    assert "Penguin" not in group_reply
    assert "Alice Tan" in group_reply


def test_charades_in_group_tells_player_to_start_when_dm_fails(monkeypatch):
    monkeypatch.setattr(charades.random, "choice", lambda seq: "Penguin")
    upd = _update(chat_type="supergroup")
    ctx = _ctx()
    ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("bot can't initiate"))

    asyncio.run(charades.charades(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0]
    assert "/start" in reply
    assert "Penguin" not in reply                  # still no spoiler
    upd.effective_message.reply_html.assert_not_awaited()


# --- registration -----------------------------------------------------------

def test_register_adds_charades_command():
    from telegram.ext import CommandHandler
    app = MagicMock()
    charades.register(app)

    handlers = [c.args[0] for c in app.add_handler.call_args_list
                if c.args and isinstance(c.args[0], CommandHandler)]
    assert any(getattr(h, "callback", None) is charades.charades for h in handlers)
