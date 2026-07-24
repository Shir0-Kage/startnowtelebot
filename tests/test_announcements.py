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
    msg.caption = None
    msg.photo = None            # a real message has [] / None, not a truthy mock
    msg.reply_text = AsyncMock()
    return upd


def _photo_update(caption, file_id="newpic", chat_type="private"):
    """A photo message whose caption carries the command."""
    upd = _update(None, chat_type)
    msg = upd.effective_message
    msg.caption = caption
    msg.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=file_id)]
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


def test_purge_dm_messages_deletes_latest_in_each_dm_only(ann, store):
    store.ensure_group(-100, "AM Group")          # a real group — must be left alone
    store.ensure_group(777, "Alice DM")           # individuals (positive ids)
    store.ensure_group(888, "Bob DM")
    upd = _update("/purge_dm_messages")
    ctx = _ctx()
    ctx.bot.send_message = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(message_id=50))
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(ann.purge_dm_messages_command(upd, ctx))

    probed = {c.kwargs["chat_id"] for c in ctx.bot.send_message.call_args_list}
    assert probed == {777, 888}                   # group -100 never probed
    deleted = {(c.kwargs["chat_id"], c.kwargs["message_id"])
               for c in ctx.bot.delete_message.call_args_list}
    # per DM: the message before the probe (49) + the probe itself (50)
    assert (777, 49) in deleted and (777, 50) in deleted
    assert (888, 49) in deleted and (888, 50) in deleted
    assert all(cid > 0 for cid, _ in deleted)     # never a group id


def test_purge_dm_messages_respects_count(ann, store):
    store.ensure_group(777, "Alice DM")
    upd = _update("/purge_dm_messages 3")
    ctx = _ctx()
    ctx.bot.send_message = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(message_id=50))
    ctx.bot.delete_message = AsyncMock()

    asyncio.run(ann.purge_dm_messages_command(upd, ctx))

    ids = {c.kwargs["message_id"] for c in ctx.bot.delete_message.call_args_list}
    assert {47, 48, 49, 50} <= ids                # 3 before the probe + the probe


def test_purge_dm_messages_rejects_non_zzehao(ann, store):
    store.ensure_group(777, "Alice DM")
    upd = _update("/purge_dm_messages")
    upd.effective_user = SimpleNamespace(id=2, username="aria", full_name="Aria")
    ctx = _ctx()

    asyncio.run(ann.purge_dm_messages_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "zzehao" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_purge_dm_messages_when_no_dms(ann, store):
    store.ensure_group(-100, "AM Group")          # only a group on record
    upd = _update("/purge_dm_messages")
    ctx = _ctx()

    asyncio.run(ann.purge_dm_messages_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()
    assert "no individual" in upd.effective_message.reply_text.call_args[0][0].lower()


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


# --- /edit_announce (edit messages by link) ---------------------------------

def test_parse_private_thread_and_public_links(ann):
    import re
    pat = ann._MSG_LINK_RE
    a = pat.search("https://t.me/c/4292606016/29")
    assert ann._parse_message_link(a) == (-1004292606016, 29)
    # forum-thread form: last number is the message id
    b = pat.search("https://t.me/c/1802003400/12/54")
    assert ann._parse_message_link(b) == (-1001802003400, 54)
    # public username form
    c = pat.search("https://t.me/startnowchannel/77")
    assert ann._parse_message_link(c) == ("@startnowchannel", 77)


def test_edit_announce_rewrites_each_linked_message(ann):
    upd = _update("/edit_announce\n"
                  "https://t.me/c/4292606016/29\n"
                  "https://t.me/c/1802003400/54\n"
                  "Updated text\nsecond line")
    ctx = _ctx()

    asyncio.run(ann.edit_announce_command(upd, ctx))

    calls = ctx.bot.edit_message_text.call_args_list
    targets = {(c.kwargs["chat_id"], c.kwargs["message_id"]) for c in calls}
    assert targets == {(-1004292606016, 29), (-1001802003400, 54)}
    for c in calls:
        assert c.kwargs["text"] == "Updated text\nsecond line"   # multi-line body, verbatim
        assert "parse_mode" not in c.kwargs
    assert "edited 2" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_edit_announce_rejects_non_zzehao(ann):
    upd = _update("/edit_announce https://t.me/c/1/2 hi")
    upd.effective_user = SimpleNamespace(id=2, username="aria", full_name="Aria")
    ctx = _ctx()

    asyncio.run(ann.edit_announce_command(upd, ctx))

    ctx.bot.edit_message_text.assert_not_called()
    assert "zzehao" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_edit_announce_needs_a_link(ann):
    upd = _update("/edit_announce just some text no link")
    ctx = _ctx()

    asyncio.run(ann.edit_announce_command(upd, ctx))

    ctx.bot.edit_message_text.assert_not_called()
    assert "link" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_edit_announce_needs_new_text_after_the_link(ann):
    upd = _update("/edit_announce https://t.me/c/4292606016/29")
    ctx = _ctx()

    asyncio.run(ann.edit_announce_command(upd, ctx))

    ctx.bot.edit_message_text.assert_not_called()
    assert "no new text" in upd.effective_message.reply_text.call_args[0][0].lower()


def test_edit_announce_counts_failures(ann):
    upd = _update("/edit_announce\n"
                  "https://t.me/c/4292606016/29\n"
                  "https://t.me/c/1802003400/54\n"
                  "New text")
    ctx = _ctx()

    def _edit(**kw):
        if kw["message_id"] == 54:
            raise RuntimeError("message can't be edited")
        return SimpleNamespace(message_id=kw["message_id"])
    ctx.bot.edit_message_text = AsyncMock(side_effect=_edit)

    asyncio.run(ann.edit_announce_command(upd, ctx))

    reply = upd.effective_message.reply_text.call_args[0][0].lower()
    assert "edited 1" in reply
    assert "couldn't edit 1" in reply


# --- photo announcements & photo edits --------------------------------------

def test_announce_with_photo_sends_photo_to_every_group(ann, store):
    from telegram.ext import ApplicationHandlerStop
    store.ensure_group(-100, "AM Group")
    store.ensure_group(-200, "PM Group")
    upd = _photo_update("/announce Look at this!", file_id="pic123")
    ctx = _ctx()

    with pytest.raises(ApplicationHandlerStop):     # stops the bingo photo handler
        asyncio.run(ann.announce_command(upd, ctx))

    ctx.bot.send_message.assert_not_called()        # photo path, not text
    targets = {c.kwargs["chat_id"] for c in ctx.bot.send_photo.call_args_list}
    assert targets == {-100, -200}
    for c in ctx.bot.send_photo.call_args_list:
        assert c.kwargs["photo"] == "pic123"        # largest photo's file_id
        assert c.kwargs["caption"] == "Look at this!"


def test_announce_photo_with_no_caption_text_still_sends(ann, store):
    from telegram.ext import ApplicationHandlerStop
    store.ensure_group(-100, "AM Group")
    upd = _photo_update("/announce", file_id="pic123")   # photo, no extra text
    ctx = _ctx()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(ann.announce_command(upd, ctx))

    ctx.bot.send_photo.assert_awaited_once()
    assert ctx.bot.send_photo.call_args.kwargs["caption"] is None


def test_edit_announce_with_photo_uses_edit_message_media(ann):
    from telegram import InputMediaPhoto
    from telegram.ext import ApplicationHandlerStop
    upd = _photo_update(
        "/edit_announce\nhttps://t.me/c/4292606016/29\nNew caption", file_id="pic999")
    ctx = _ctx()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(ann.edit_announce_command(upd, ctx))

    ctx.bot.edit_message_text.assert_not_called()   # media path, not text
    ctx.bot.edit_message_media.assert_awaited_once()
    kw = ctx.bot.edit_message_media.call_args.kwargs
    assert kw["chat_id"] == -1004292606016 and kw["message_id"] == 29
    media = kw["media"]
    assert isinstance(media, InputMediaPhoto)
    assert media.media == "pic999"
    assert media.caption == "New caption"


def test_edit_announce_text_only_still_uses_edit_message_text(ann):
    # no photo -> unchanged behaviour, and no ApplicationHandlerStop raised
    upd = _update("/edit_announce https://t.me/c/4292606016/29 Just text")
    ctx = _ctx()

    asyncio.run(ann.edit_announce_command(upd, ctx))

    ctx.bot.edit_message_media.assert_not_called()
    ctx.bot.edit_message_text.assert_awaited_once()
