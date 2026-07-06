"""Tests for the /get_bingo command and its wiring."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from handlers import bingo

FAKE_ROSTER = {"AM1": [{"name": "Tan Wei Xuan", "handle": "theoverlord27"}]}


def _dm_update(username, uid=111):
    upd = MagicMock()
    upd.effective_chat.type = "private"
    upd.effective_user.username = username
    upd.effective_user.id = uid
    upd.effective_message.reply_document = AsyncMock()
    upd.effective_message.reply_text = AsyncMock()
    return upd


def test_is_year1_is_case_and_at_tolerant(monkeypatch):
    bingo._roster_handles = None
    monkeypatch.setattr(bingo.sheets, "load_year1_members", lambda: FAKE_ROSTER)
    assert bingo._is_year1("theoverlord27")
    assert bingo._is_year1("@TheOverlord27")
    assert not bingo._is_year1("someone_else")
    assert not bingo._is_year1(None)


def test_get_bingo_declines_non_roster(monkeypatch):
    bingo._roster_handles = None
    monkeypatch.setattr(bingo.sheets, "load_year1_members", lambda: FAKE_ROSTER)
    upd = _dm_update("random_person")
    asyncio.run(bingo.get_bingo(upd, MagicMock()))
    upd.effective_message.reply_text.assert_called_once()
    upd.effective_message.reply_document.assert_not_called()


def test_get_bingo_sends_card_for_roster(monkeypatch):
    bingo._roster_handles = None
    monkeypatch.setattr(bingo.sheets, "load_year1_members", lambda: FAKE_ROSTER)
    monkeypatch.setattr(bingo.storage, "allocate_bingo_sheet", lambda uid, h: 3)
    upd = _dm_update("theoverlord27")
    asyncio.run(bingo.get_bingo(upd, MagicMock()))
    upd.effective_message.reply_document.assert_called_once()
    upd.effective_message.reply_text.assert_not_called()


def test_get_bingo_only_works_in_private(monkeypatch):
    upd = _dm_update("theoverlord27")
    upd.effective_chat.type = "group"
    asyncio.run(bingo.get_bingo(upd, MagicMock()))
    upd.effective_message.reply_document.assert_not_called()
    upd.effective_message.reply_text.assert_called_once()


def test_wiring_registers_get_bingo_in_menu_and_help():
    import main
    from handlers import common
    assert "get_bingo" in [c.command for c in main.MENU_COMMANDS]
    assert "get_bingo" in common.HELP_TEXT
