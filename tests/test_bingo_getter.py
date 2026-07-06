"""Tests for the /get_bingo command and its wiring."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from handlers import bingo


def _dm_update(username, uid=111):
    upd = MagicMock()
    upd.effective_chat.type = "private"
    upd.effective_user.username = username
    upd.effective_user.id = uid
    upd.effective_message.reply_document = AsyncMock()
    upd.effective_message.reply_text = AsyncMock()
    return upd


def test_get_bingo_gives_a_card_to_anyone(monkeypatch):
    # Not on the Year 1 roster? Still get a card.
    monkeypatch.setattr(bingo.storage, "allocate_bingo_sheet", lambda uid, h: 3)
    upd = _dm_update("someone_not_on_the_list")
    asyncio.run(bingo.get_bingo(upd, MagicMock()))
    upd.effective_message.reply_document.assert_called_once()
    upd.effective_message.reply_text.assert_not_called()


def test_get_bingo_allocates_by_user_id(monkeypatch):
    seen = {}

    def _alloc(uid, handle):
        seen["uid"], seen["handle"] = uid, handle
        return 7

    monkeypatch.setattr(bingo.storage, "allocate_bingo_sheet", _alloc)
    upd = _dm_update("TheOverlord27", uid=999)
    asyncio.run(bingo.get_bingo(upd, MagicMock()))
    assert seen["uid"] == 999
    assert seen["handle"] == "theoverlord27"
    upd.effective_message.reply_document.assert_called_once()


def test_get_bingo_only_works_in_private():
    upd = _dm_update("anyone")
    upd.effective_chat.type = "group"
    asyncio.run(bingo.get_bingo(upd, MagicMock()))
    upd.effective_message.reply_document.assert_not_called()
    upd.effective_message.reply_text.assert_called_once()


def test_wiring_registers_bingo_commands_in_menu_and_help():
    import main
    from handlers import common
    cmds = [c.command for c in main.MENU_COMMANDS]
    assert "get_bingo" in cmds
    assert "submit_bingo" in cmds
    assert "get_bingo" in common.HELP_TEXT
    assert "submit_bingo" in common.HELP_TEXT
    assert hasattr(bingo, "rearm_bingo_timeouts")
