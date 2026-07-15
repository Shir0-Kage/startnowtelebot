"""Tests for handlers/bingo_forward.py — the manual prize round.

The round opens with a broadcast to card-holders; each forwarded card is relayed
to the vetter (@zzehao) with the sender's @handle + original send time; the
vetter confirms winners with /confirm_bingo_winners, which DMs the winners and
sends the vetter an announcement to post. No OCR, no automatic winner selection.
"""

import asyncio
import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import config
    import storage
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fwd.db"))
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "fwd.db"))
    importlib.reload(storage)
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "fwd.db"))
    storage.init_db()
    return storage


@pytest.fixture()
def fwd(store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "storage", store)
    return bingo_forward


def _ctx():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    return ctx


def _fwd_update(uid, username, when):
    upd = MagicMock()
    upd.effective_chat = MagicMock(type="private", id=uid)
    upd.effective_user = MagicMock(id=uid, username=username)
    msg = upd.effective_message
    msg.message_id = 5000 + uid
    msg.photo = [MagicMock(file_id="f")]
    msg.document = None
    msg.forward_origin = MagicMock(date=when)
    msg.reply_text = AsyncMock()
    return upd


# --- begin_round ------------------------------------------------------------

def test_begin_round_dms_all_card_holders_and_sets_phase(fwd, store):
    store.allocate_bingo_sheet(100, "alice")
    store.allocate_bingo_sheet(200, "bob")
    ctx = _ctx()

    n = asyncio.run(fwd.begin_round(ctx))

    assert n == 2
    assert store.forward_phase() == "collecting"
    targets = {c.kwargs["chat_id"] for c in ctx.bot.send_message.call_args_list}
    assert targets == {100, 200}
    body = ctx.bot.send_message.call_args_list[0].kwargs["text"]
    assert "potential candidate" in body.lower()


def test_begin_round_noop_when_already_collecting(fwd, store):
    store.set_forward_phase("collecting")
    ctx = _ctx()

    n = asyncio.run(fwd.begin_round(ctx))

    assert n == -1
    ctx.bot.send_message.assert_not_called()


def test_begin_round_can_reopen_after_released(fwd, store):
    store.set_forward_phase("released")
    store.allocate_bingo_sheet(100, "alice")
    ctx = _ctx()

    n = asyncio.run(fwd.begin_round(ctx))

    assert n == 1
    assert store.forward_phase() == "collecting"


# --- on_forwarded_card ------------------------------------------------------

def test_forwarded_card_acks_sender_and_relays_to_vetter(fwd, store):
    store.set_forward_phase("collecting")
    store.mark_started(9, "zzehao", "Z")          # vetter is reachable
    when = datetime(2026, 6, 9, 15, 4, tzinfo=timezone.utc)
    upd = _fwd_update(100, "alice", when)
    ctx = _ctx()

    asyncio.run(fwd.on_forwarded_card(upd, ctx))

    # sender thanked
    upd.effective_message.reply_text.assert_awaited_once()
    assert "thank you" in upd.effective_message.reply_text.call_args[0][0].lower()
    # card copied to the vetter + a note with handle and original time
    ctx.bot.copy_message.assert_awaited_once()
    assert ctx.bot.copy_message.call_args.kwargs["chat_id"] == 9
    note = ctx.bot.send_message.call_args.kwargs["text"]
    assert "@alice" in note
    assert "2026" in note and ("Jun" in note or "06" in note)


def test_forwarded_card_ignored_when_not_collecting(fwd, store):
    store.mark_started(9, "zzehao", "Z")
    upd = _fwd_update(100, "alice", datetime.now(timezone.utc))
    ctx = _ctx()

    asyncio.run(fwd.on_forwarded_card(upd, ctx))

    upd.effective_message.reply_text.assert_not_awaited()
    ctx.bot.copy_message.assert_not_called()


def test_forwarded_card_ignored_outside_private_chat(fwd, store):
    store.set_forward_phase("collecting")
    upd = _fwd_update(100, "alice", datetime.now(timezone.utc))
    upd.effective_chat = MagicMock(type="group", id=-1)
    ctx = _ctx()

    asyncio.run(fwd.on_forwarded_card(upd, ctx))

    ctx.bot.copy_message.assert_not_called()


def test_forwarded_card_still_acks_when_vetter_unreachable(fwd, store):
    store.set_forward_phase("collecting")           # nobody marked as zzehao
    upd = _fwd_update(100, "alice", datetime.now(timezone.utc))
    ctx = _ctx()

    asyncio.run(fwd.on_forwarded_card(upd, ctx))

    upd.effective_message.reply_text.assert_awaited_once()   # sender still thanked
    ctx.bot.copy_message.assert_not_called()                 # but nothing relayed


# --- confirm_winners --------------------------------------------------------

def test_confirm_winners_dms_winners_and_announces_to_vetter(fwd, store):
    store.mark_started(9, "zzehao", "Z")
    store.mark_started(100, "alice", "Alice")
    store.mark_started(200, "bob", "Bob")
    ctx = _ctx()

    winners, unreachable = asyncio.run(
        fwd.confirm_winners(ctx, ["@alice", "bob"]))

    assert {h for h, _ in winners} == {"alice", "bob"}
    assert unreachable == []
    # each winner DMed a congratulations
    winner_msgs = [c for c in ctx.bot.send_message.call_args_list
                   if c.kwargs["chat_id"] in (100, 200)]
    assert len(winner_msgs) == 2
    assert all("won" in c.kwargs["text"].lower() for c in winner_msgs)
    # vetter gets an announcement listing both
    vetter_msg = next(c for c in ctx.bot.send_message.call_args_list
                      if c.kwargs["chat_id"] == 9)
    assert "@alice" in vetter_msg.kwargs["text"]
    assert "@bob" in vetter_msg.kwargs["text"]
    # round closed
    assert store.forward_phase() == "released"


def test_confirm_winners_reports_unreachable_handles(fwd, store):
    store.mark_started(9, "zzehao", "Z")
    store.mark_started(100, "alice", "Alice")       # bob never /started
    ctx = _ctx()

    winners, unreachable = asyncio.run(
        fwd.confirm_winners(ctx, ["alice", "bob"]))

    assert {h for h, _ in winners} == {"alice"}
    assert unreachable == ["bob"]


def test_confirm_winners_dedupes_repeated_handles(fwd, store):
    store.mark_started(9, "zzehao", "Z")
    store.mark_started(100, "alice", "Alice")
    ctx = _ctx()

    winners, _ = asyncio.run(fwd.confirm_winners(ctx, ["@alice", "alice", "ALICE"]))

    assert winners == [("alice", 100)]
    winner_msgs = [c for c in ctx.bot.send_message.call_args_list
                   if c.kwargs["chat_id"] == 100]
    assert len(winner_msgs) == 1                     # DMed once, not thrice
