"""Tests for handlers/bingo.py — the human-bingo Telegram flow.

Telegram Bot calls and the OCR pipeline are monkeypatched so these run fully
offline and deterministically. Async handlers are invoked with asyncio.run so
the suite needs no pytest-asyncio plugin.
"""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- DB-backed storage on a temp file --------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bingo_test.db"))
    for mod in ("storage",):
        sys.modules.pop(mod, None)
    import storage
    importlib.reload(storage)
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "bingo_test.db"))
    storage.init_db()
    return storage


@pytest.fixture()
def bingo(store, monkeypatch):
    """handlers.bingo with a fixed 2-OG roster and no network."""
    # bingo_queue is imported at collection time (by test_bingo_queue.py) and
    # caches a module-level `storage` reference. The `store` fixture pops+reloads
    # `storage` into a fresh, initialised module, so reload bingo_queue too to
    # re-bind its `storage` to that same module -- otherwise the submit path
    # (on_bingo_text / OCR-confirm -> bingo_queue.enqueue) would write to a stale,
    # unconnected storage instead of the test DB handlers.bingo uses.
    import handlers.bingo_queue as bingo_queue
    importlib.reload(bingo_queue)
    import handlers.bingo as bingo
    importlib.reload(bingo)

    roster = {
        "AM1": [
            {"name": "Alice Tan", "handle": "alice", "email": "a@x.com", "addable": True},
            {"name": "Bob Lee", "handle": "bob", "email": "b@x.com", "addable": True},
            {"name": "Cara Ng", "handle": "cara", "email": "c@x.com", "addable": True},
            {"name": "Dan Ong", "handle": "dan", "email": "d@x.com", "addable": True},
            {"name": "Eve Sim", "handle": "eve", "email": "e@x.com", "addable": True},
        ],
    }
    monkeypatch.setattr(bingo.sheets, "load_year1_members", lambda: roster)
    bingo._ROSTER_INDEX = None  # reset the module cache
    return bingo


def _update(user_id=100, username="submitter", text="/get_bingo"):
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    msg.reply_photo = AsyncMock()
    msg.reply_document = AsyncMock()
    upd = MagicMock()
    upd.effective_user = SimpleNamespace(id=user_id, username=username, full_name="Sub Mitter")
    upd.effective_chat = SimpleNamespace(id=user_id, type="private")
    upd.effective_message = msg
    return upd


def _context():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.user_data = {}
    ctx.job_queue = MagicMock()
    return ctx


def _text_update(user_id=100, username="alice", text=""):
    upd = _update(user_id=user_id, username=username, text=text)
    upd.effective_message.text = text
    upd.effective_message.photo = None
    upd.effective_message.document = None
    return upd


def _confirm_keyboard_sent(ctx):
    """True if any bot.send_message carried a bingoq:confirm: inline button."""
    for c in ctx.bot.send_message.await_args_list:
        rm = c.kwargs.get("reply_markup")
        if rm is None:
            continue
        for row in rm.inline_keyboard:
            for b in row:
                if str(getattr(b, "callback_data", "")).startswith("bingoq:confirm:"):
                    return True
    return False


def _tap_mode(bingo, ctx, uid, mode):
    q = AsyncMock()
    q.data = f"bingomode:{mode}"
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.from_user = SimpleNamespace(id=uid)
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    upd = MagicMock()
    upd.callback_query = q
    upd.effective_user = SimpleNamespace(id=uid)
    asyncio.run(bingo.bingo_mode_button(upd, ctx))
    return q


def _tap_ocr_confirm(bingo, ctx, uid, ans, username="alice"):
    q = AsyncMock()
    q.data = f"bingoocr:{ans}"
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.from_user = SimpleNamespace(id=uid, username=username)
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    upd = MagicMock()
    upd.callback_query = q
    upd.effective_user = SimpleNamespace(id=uid, username=username)
    # mirrors real PTB: Update.effective_message falls back to the callback
    # query's own message when there's no top-level message on the update.
    upd.effective_message = q.message
    asyncio.run(bingo.bingo_ocr_confirm_button(upd, ctx))
    return q


# --- helper: matched dict + prompt map from OCR cells ----------------------

def test_matched_and_prompts_drops_non_match_and_self(bingo, monkeypatch):
    # read_submission already applies the score/margin cutoff, so any non-None
    # handle here is confident; _matched_and_prompts only drops no-match (None
    # handle) and the submitter's own handle (no self-cheese). prompts come from
    # templates.prompt_for(sheet_no, r, c) given an explicit sheet_no.
    monkeypatch.setattr(
        bingo.templates, "prompt_for",
        lambda s, r, c: f"P{r}{c}",
    )
    cells = [
        {"row": 0, "col": 0, "handle": "alice", "score": 91.0},
        {"row": 0, "col": 2, "handle": None, "score": 0.0},       # no match -> dropped
        {"row": 0, "col": 3, "handle": "submitter", "score": 99}, # self -> dropped
        {"row": 0, "col": 4, "handle": "cara", "score": 88.0},
    ]
    matched, prompts = bingo._matched_and_prompts(cells, "submitter", sheet_no=1)
    assert matched == {(0, 0): "alice", (0, 4): "cara"}
    assert prompts[(0, 0)] == "P00" and prompts[(0, 4)] == "P04"
    assert (0, 2) not in matched and (0, 3) not in matched


# --- helper: per-line verdict ----------------------------------------------

def test_line_verdict_pass_fail_pending(bingo):
    line = [(0, 0, "alice"), (0, 1, "bob"), (0, 3, "dan"), (0, 4, "eve")]  # 4 real cells
    # all yes -> pass
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes", "dan": "yes", "eve": "yes"}) == "pass"
    # one no, rest yes -> still pass (one allowed miss == required_yes met)
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes", "dan": "yes", "eve": "no"}) == "pass"
    # two misses -> fail
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes", "dan": "no", "eve": "no"}) == "fail"
    # unanswered cells and not yet failable -> pending
    assert bingo._line_verdict(line, {"alice": "yes", "bob": "yes"}) == "pending"


# --- get_bingo -------------------------------------------------------------

def test_get_bingo_non_roster_also_gets_a_card(bingo, store):
    # A Year 1 who isn't on the sheet still gets a card (roster gate removed).
    upd, ctx = _update(user_id=999, username="stranger"), _context()
    asyncio.run(bingo.get_bingo(upd, ctx))
    upd.effective_message.reply_document.assert_awaited()
    assert store.get_bingo_sheet(999) is not None


def test_get_bingo_roster_sends_sheet_and_freezes(bingo, store):
    upd, ctx = _update(user_id=100, username="alice"), _context()
    asyncio.run(bingo.get_bingo(upd, ctx))
    # existing implementation uses reply_document (not reply_photo)
    upd.effective_message.reply_document.assert_awaited()
    first = store.get_bingo_sheet(100)
    assert first is not None
    # calling again must not reallocate
    asyncio.run(bingo.get_bingo(upd, ctx))
    assert store.get_bingo_sheet(100) == first


# --- submit_bingo gating ---------------------------------------------------

def test_submit_bingo_shows_mode_choice_when_open(bingo, store):
    store.allocate_bingo_sheet(100, "alice")  # must have a card first
    upd, ctx = _update(user_id=100, username="alice", text="/submit_bingo"), _context()
    asyncio.run(bingo.submit_bingo(upd, ctx))
    # neither mode is armed yet -- the user still has to tap a button
    assert not ctx.user_data.get("awaiting_bingo")
    assert not ctx.user_data.get("awaiting_bingo_text")
    upd.effective_message.reply_text.assert_awaited()
    _, kwargs = upd.effective_message.reply_text.call_args
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert {b.callback_data for b in buttons} == {"bingomode:photo", "bingomode:text"}


def test_bingo_mode_button_photo_arms_awaiting_bingo(bingo, store):
    store.allocate_bingo_sheet(100, "alice")
    ctx = _context()
    _tap_mode(bingo, ctx, 100, "photo")
    assert ctx.user_data.get("awaiting_bingo") is True
    assert not ctx.user_data.get("awaiting_bingo_text")
    # sent via the bot API, not query.message.reply_text -- the tapped
    # message may be too old/inaccessible to reply through directly
    ctx.bot.send_message.assert_awaited()


def test_bingo_mode_button_text_arms_awaiting_bingo_text_and_sends_template(bingo, store):
    store.allocate_bingo_sheet(100, "alice")
    ctx = _context()
    _tap_mode(bingo, ctx, 100, "text")
    assert ctx.user_data.get("awaiting_bingo_text") is True
    assert not ctx.user_data.get("awaiting_bingo")
    sent_text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "R1C1" in sent_text and "R3C3" not in sent_text


def test_bingo_mode_button_regates_at_click_time(bingo, store, monkeypatch):
    # state can go stale between the buttons rendering and the tap
    store.allocate_bingo_sheet(100, "alice")
    monkeypatch.setattr(store, "has_bingo_prize", lambda uid: True)
    ctx = _context()
    _tap_mode(bingo, ctx, 100, "photo")
    assert not ctx.user_data.get("awaiting_bingo")
    assert not ctx.user_data.get("awaiting_bingo_text")
    ctx.bot.send_message.assert_awaited()  # rejection message, not the photo prompt


def test_bingo_mode_button_arming_one_mode_clears_the_other(bingo, store):
    # regression: a user who taps "Photo" on one /submit_bingo prompt and
    # "Text" on an earlier one (both gates passed since neither had a
    # submission yet) must not end up with BOTH flags armed -- whichever is
    # tapped last must win outright.
    store.allocate_bingo_sheet(100, "alice")
    ctx = _context()
    _tap_mode(bingo, ctx, 100, "text")
    assert ctx.user_data.get("awaiting_bingo_text") is True
    _tap_mode(bingo, ctx, 100, "photo")
    assert ctx.user_data.get("awaiting_bingo") is True
    assert ctx.user_data.get("awaiting_bingo_text") is False


def test_submit_bingo_blocked_when_closed(bingo, store):
    store.set_bingo_closed()
    upd, ctx = _update(user_id=100, username="alice", text="/submit_bingo"), _context()
    asyncio.run(bingo.submit_bingo(upd, ctx))
    assert not ctx.user_data.get("awaiting_bingo")


def test_submit_bingo_blocked_when_already_won(bingo, store, monkeypatch):
    monkeypatch.setattr(store, "has_bingo_prize", lambda uid: True)
    upd, ctx = _update(user_id=100, username="alice", text="/submit_bingo"), _context()
    asyncio.run(bingo.submit_bingo(upd, ctx))
    assert not ctx.user_data.get("awaiting_bingo")


# --- on_bingo_image full pipeline -> pending + DMs subjects -----------------

def _photo_update(user_id=100, username="alice"):
    upd = _update(user_id=user_id, username=username)
    photo = SimpleNamespace(file_id="F")
    upd.effective_message.photo = [photo]
    upd.effective_message.document = None
    return upd


def test_on_bingo_image_shows_preview_and_waits_for_confirmation(bingo, store, monkeypatch):
    # allocate sheet 1 for the submitter (id 100)
    store.allocate_bingo_sheet(100, "alice")

    async def fake_ocr(sheet_no, image_bytes):
        return {"corner": sheet_no, "cells": [
            {"row": 0, "col": 0, "handle": "bob", "score": 95.0},
        ]}
    monkeypatch.setattr(bingo, "_run_ocr", fake_ocr)

    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)

    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))

    # nothing acted on yet -- no submission, no subject DMs
    assert store.active_submission(100) is None
    ctx.bot.send_message.assert_not_awaited()
    pending = ctx.user_data.get("bingo_ocr_pending")
    assert pending is not None
    assert pending["sheet_no"] == store.get_bingo_sheet(100)
    # last reply is the preview + confirm keyboard, with the matched handle shown
    last_call = upd.effective_message.reply_text.call_args
    assert "@bob" in last_call.args[0]
    assert {b.callback_data for row in last_call.kwargs["reply_markup"].inline_keyboard for b in row} \
        == {"bingoocr:yes", "bingoocr:no"}


def test_ocr_confirm_yes_queues_without_messaging_until_round_opens(bingo, store, monkeypatch):
    # New round-open flow: a confirmed OCR read is ENQUEUED, and because the
    # round stays closed after a single submit, it just sits 'queued' -- nothing
    # is messaged to confirm and no timeout is armed. Only once the round opens
    # (10 queued, or a facil command) is it promoted to 'confirming' and, since
    # the line is fully recognised (all subjects have /started), the submitter
    # gets the SHORT confirmation with a Confirm button. Subjects are never DM'd
    # here -- that waits until the submitter confirms.
    from handlers import bingo_queue
    store.allocate_bingo_sheet(100, "alice")
    for uid, h in [(1, "bob"), (2, "cara"), (3, "dan"), (4, "eve")]:
        store.mark_started(uid, h, h.title())

    # OCR (isolated subprocess) returns a full top-row line of 4 confident handles
    async def fake_ocr(sheet_no, image_bytes):
        return {"cells": [
            {"row": 0, "col": 0, "handle": "bob", "score": 95.0},
            {"row": 0, "col": 1, "handle": "cara", "score": 95.0},
            {"row": 0, "col": 3, "handle": "dan", "score": 95.0},
            {"row": 0, "col": 4, "handle": "eve", "score": 95.0},
        ]}
    monkeypatch.setattr(bingo, "_run_ocr", fake_ocr)
    monkeypatch.setattr(bingo.templates, "prompt_for", lambda s, r, c: f"prompt-{r}-{c}")
    # winning_lines: top row (row 0) complete (centre free auto-fills col 2)
    monkeypatch.setattr(bingo.lines, "winning_lines",
                        lambda matched, sub: [[(0, 0, "bob"), (0, 1, "cara"), (0, 3, "dan"), (0, 4, "eve")]])

    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)
    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))
    assert store.active_submission(100) is None  # still just a preview

    _tap_ocr_confirm(bingo, ctx, 100, "yes")

    # (a) round closed after a single submit -> the read is QUEUED, not promoted.
    # confirming/active see nothing, no Confirm keyboard was sent, no timeout
    # armed; only the "you're in the queue" DM went out.
    queued = store.queued_in_order()
    assert len(queued) == 1 and queued[0]["submitter_user_id"] == 100
    assert store.confirming_submissions() == []
    assert store.active_submission(100) is None
    assert not _confirm_keyboard_sent(ctx)
    assert any("queue" in c.kwargs.get("text", "").lower()
               for c in ctx.bot.send_message.await_args_list)   # the in-queue DM
    ctx.job_queue.run_once.assert_not_called()   # nothing armed while closed
    assert ctx.user_data.get("bingo_ocr_pending") is None

    # (b) opening the round promotes the read to 'confirming' and fires the SHORT
    # confirmation (fully recognised -> a Confirm button).
    store.set_queue_open()
    asyncio.run(bingo_queue.maybe_kickoff(ctx))

    confirming = store.confirming_submissions()
    assert len(confirming) == 1 and confirming[0]["submitter_user_id"] == 100
    assert store.active_submission(100) is None
    last = ctx.bot.send_message.await_args
    assert last.kwargs["chat_id"] == 100
    buttons = [b for row in last.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data.startswith("bingoq:confirm:") for b in buttons)
    # subjects (chat_ids 1-4) are still NOT messaged
    sent_chat_ids = {c.kwargs.get("chat_id") for c in ctx.bot.send_message.await_args_list}
    assert not (sent_chat_ids & {1, 2, 3, 4})
    ctx.job_queue.run_once.assert_called_once()  # confirm-timeout armed on promote


def test_ocr_confirm_no_arms_text_mode_with_prefilled_list(bingo, store, monkeypatch):
    store.allocate_bingo_sheet(100, "alice")

    async def fake_ocr(sheet_no, image_bytes):
        return {"corner": sheet_no, "cells": [
            {"row": 0, "col": 0, "handle": "bob", "score": 95.0},
        ]}
    monkeypatch.setattr(bingo, "_run_ocr", fake_ocr)

    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)
    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))

    _tap_ocr_confirm(bingo, ctx, 100, "no")

    assert ctx.user_data.get("awaiting_bingo_text") is True
    assert ctx.user_data.get("bingo_ocr_pending") is None
    assert store.active_submission(100) is None  # nothing recorded yet
    sent_text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "@bob" in sent_text and "R1C1" in sent_text


def test_ocr_confirm_yes_no_line_queues_full_template_when_round_opens(bingo, store, monkeypatch):
    # EVERY confirmed submission is queued -- even one with no recognised line --
    # but nothing is messaged while the round is closed. Once the round opens and
    # the read is promoted to 'confirming', a not-fully-recognised read gets the
    # FULL fill-in template (no Confirm button) so the submitter can fix blanks
    # and resend. (The old "no bingo / no submission" path is gone.)
    from handlers import bingo_queue
    store.allocate_bingo_sheet(100, "alice")

    async def _empty(sheet_no, image_bytes):
        return {"cells": []}
    monkeypatch.setattr(bingo, "_run_ocr", _empty)
    monkeypatch.setattr(bingo.lines, "winning_lines", lambda matched, sub: [])
    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)
    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))

    _tap_ocr_confirm(bingo, ctx, 100, "yes")

    # (a) round closed -> queued only, no template/Confirm DM, no timeout armed
    queued = store.queued_in_order()
    assert len(queued) == 1 and queued[0]["submitter_user_id"] == 100
    assert store.confirming_submissions() == []
    assert store.active_submission(100) is None
    assert not _confirm_keyboard_sent(ctx)
    ctx.job_queue.run_once.assert_not_called()

    # (b) opening the round promotes it and sends the full fill-in template with
    # NO Confirm button (not fully recognised).
    store.set_queue_open()
    asyncio.run(bingo_queue.maybe_kickoff(ctx))

    confirming = store.confirming_submissions()
    assert len(confirming) == 1 and confirming[0]["submitter_user_id"] == 100
    assert store.active_submission(100) is None
    last = ctx.bot.send_message.await_args
    assert last.kwargs["chat_id"] == 100
    assert "R1C1" in last.kwargs["text"]
    assert last.kwargs.get("reply_markup") is None


def test_ocr_confirm_stale_button_is_a_no_op(bingo, store):
    # bingo_ocr_pending already consumed (or bot restarted) -- must not crash
    # or act on a phantom read.
    store.allocate_bingo_sheet(100, "alice")
    ctx = _context()
    q = _tap_ocr_confirm(bingo, ctx, 100, "yes")
    ctx.bot.send_message.assert_not_awaited()
    assert store.active_submission(100) is None


# --- on_bingo_text full pipeline -> pending + DMs subjects ------------------

def test_on_bingo_text_queues_without_messaging_until_round_opens(bingo, store, monkeypatch):
    # New round-open flow for the text path: a typed submission is enqueued but,
    # with the round closed after a single submit, it sits 'queued' -- nothing is
    # messaged to confirm. Opening the round promotes it to 'confirming'; all four
    # tagged subjects have /started so the line is fully recognised -> the
    # submitter then gets the SHORT confirmation carrying the line and a Confirm
    # button; subjects are never DM'd here.
    #
    # real Telegram usernames are 5-32 chars; use a roster with realistic
    # handles so normalize_handle doesn't reject a typed handle for being
    # too short (unlike the default short-handle roster used elsewhere,
    # which OCR mode can get away with since it never runs typed input
    # through normalize_handle).
    from handlers import bingo_queue
    long_roster = {
        "AM1": [
            {"name": "Alice Tan", "handle": "alice", "email": "a@x.com", "addable": True},
            {"name": "Bobby Lee", "handle": "bobby", "email": "b@x.com", "addable": True},
            {"name": "Carol Ng", "handle": "carol", "email": "c@x.com", "addable": True},
            {"name": "Daniel Ong", "handle": "daniel", "email": "d@x.com", "addable": True},
            {"name": "Evelyn Sim", "handle": "evelyn", "email": "e@x.com", "addable": True},
        ],
    }
    monkeypatch.setattr(bingo.sheets, "load_year1_members", lambda: long_roster)
    bingo._ROSTER_INDEX = None

    store.allocate_bingo_sheet(100, "alice")
    for uid, h in [(1, "bobby"), (2, "carol"), (3, "daniel"), (4, "evelyn")]:
        store.mark_started(uid, h, h.title())

    monkeypatch.setattr(
        bingo.lines, "winning_lines",
        lambda matched, sub: [[(0, 0, "bobby"), (0, 1, "carol"), (0, 3, "daniel"), (0, 4, "evelyn")]],
    )

    ctx = _context()
    ctx.user_data["awaiting_bingo_text"] = True
    body = "\n".join([
        "R1C1: p - @bobby",
        "R1C2: p - @carol",
        "R1C4: p - @daniel",
        "R1C5: p - @evelyn",
    ])
    upd = _text_update(100, "alice", body)
    asyncio.run(bingo.on_bingo_text(upd, ctx))

    # (a) round closed after a single submit -> QUEUED, nothing messaged to
    # confirm, no timeout armed; only the in-queue DM went out. The awaiting flag
    # is cleared regardless.
    queued = store.queued_in_order()
    assert len(queued) == 1 and queued[0]["submitter_user_id"] == 100
    assert store.confirming_submissions() == []
    assert store.active_submission(100) is None
    assert not _confirm_keyboard_sent(ctx)
    assert any("queue" in c.kwargs.get("text", "").lower()
               for c in ctx.bot.send_message.await_args_list)
    ctx.job_queue.run_once.assert_not_called()
    assert ctx.user_data.get("awaiting_bingo_text") is not True

    # (b) opening the round promotes it and fires the SHORT confirmation carrying
    # the line and a Confirm button.
    store.set_queue_open()
    asyncio.run(bingo_queue.maybe_kickoff(ctx))

    confirming = store.confirming_submissions()
    assert len(confirming) == 1 and confirming[0]["submitter_user_id"] == 100
    assert store.active_submission(100) is None
    last = ctx.bot.send_message.await_args
    assert last.kwargs["chat_id"] == 100
    assert "@bobby" in last.kwargs["text"]
    buttons = [b for row in last.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data.startswith("bingoq:confirm:") for b in buttons)
    # subjects (chat_ids 1-4) are still NOT messaged
    sent_chat_ids = {c.kwargs.get("chat_id") for c in ctx.bot.send_message.await_args_list}
    assert not (sent_chat_ids & {1, 2, 3, 4})
    ctx.job_queue.run_once.assert_called_once()  # confirm-timeout armed on promote


def test_on_bingo_text_exact_match_miss_queues_until_round_opens(bingo, store, monkeypatch):
    # a typed handle that isn't on the roster is not fuzzy-rescued -- the cell
    # just stays unmatched, same as a low-confidence OCR read. There's no
    # recognised line, but the submission is still queued. While the round is
    # closed nothing is messaged to confirm; opening the round then sends the
    # full fill-in template (via bot.send_message) to correct and resend.
    from handlers import bingo_queue
    store.allocate_bingo_sheet(100, "alice")
    monkeypatch.setattr(bingo.lines, "winning_lines", lambda matched, sub: [])
    ctx = _context()
    ctx.user_data["awaiting_bingo_text"] = True
    upd = _text_update(100, "alice", "R1C1: p - @ghostwriter")
    asyncio.run(bingo.on_bingo_text(upd, ctx))

    # (a) round closed -> queued only; no confirmation/template DM, none armed
    queued = store.queued_in_order()
    assert len(queued) == 1 and queued[0]["submitter_user_id"] == 100
    assert store.confirming_submissions() == []
    assert store.active_submission(100) is None
    assert not _confirm_keyboard_sent(ctx)
    ctx.job_queue.run_once.assert_not_called()

    # (b) opening the round promotes it; the submitter's DM is the NOT-fully-
    # recognised FULL fill-in template: it carries the R1C1 template line and has
    # NO confirm button (unlike the short "tap Confirm" DM sent when a line IS
    # fully recognised).
    store.set_queue_open()
    asyncio.run(bingo_queue.maybe_kickoff(ctx))

    confirming = store.confirming_submissions()
    assert len(confirming) == 1 and confirming[0]["submitter_user_id"] == 100
    assert store.active_submission(100) is None
    to_submitter = [c for c in ctx.bot.send_message.await_args_list
                    if c.kwargs.get("chat_id") == 100]
    assert any("R1C1" in c.kwargs.get("text", "")
               and c.kwargs.get("reply_markup") is None
               for c in to_submitter), "expected the full fill-in template DM"


def test_on_bingo_text_fresh_submit_regates_active_submission(bingo, store, monkeypatch):
    # FIX A: the fresh text-submit branch must re-apply the FULL submit gate
    # (closed / already-won / already-being-verified / no-sheet), same as the
    # photo path -- not just the closed check. A user who already has an ACTIVE
    # ('pending') submission being verified, but whose awaiting_bingo_text flag
    # was armed earlier (before that submission existed), must be GATED: no new
    # 'queued' duplicate may slip through.
    store.allocate_bingo_sheet(100, "alice")
    store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100))
    # neutralise the retry cooldown so the GATE is unambiguously what blocks:
    # before the fix nothing would, and a duplicate 'queued' row is created.
    monkeypatch.setattr(store, "last_bingo_activity", lambda uid: None)
    ctx = _context()
    ctx.user_data["awaiting_bingo_text"] = True
    upd = _text_update(100, "alice", "R1C1: p - @bob")
    asyncio.run(bingo.on_bingo_text(upd, ctx))
    # no NEW submission created -- the duplicate was gated. (Before the fix
    # enqueue ran and maybe_kickoff promoted the fresh row straight to
    # 'confirming', so check that queue too, not just 'queued'.)
    assert store.queued_in_order() == []
    assert store.confirming_submissions() == []
    # and the submitter was told their card is already being checked
    upd.effective_message.reply_text.assert_awaited()
    replied = " ".join(str(c.args[0]) for c in
                       upd.effective_message.reply_text.await_args_list if c.args)
    assert "being checked" in replied


def test_on_bingo_text_ignores_when_flag_not_set(bingo, store):
    ctx = _context()  # awaiting_bingo_text never armed
    upd = _text_update(100, "alice", "R1C1: p - @alice")
    asyncio.run(bingo.on_bingo_text(upd, ctx))
    upd.effective_message.reply_text.assert_not_awaited()
    assert store.active_submission(100) is None


def test_on_bingo_text_confirming_user_routes_to_resend(bingo, store, monkeypatch):
    from handlers import bingo_queue
    store.allocate_bingo_sheet(100, "alice")
    sid = store.queue_submission(100, "alice", store.get_bingo_sheet(100))
    store.set_submission_status(sid, "confirming")
    monkeypatch.setattr(bingo_queue, "on_resend", AsyncMock())
    monkeypatch.setattr(bingo_queue, "enqueue", AsyncMock())
    ctx = _context()                              # awaiting_bingo_text NOT set
    upd = _text_update(100, "alice", "R1C1: p - @bob")
    asyncio.run(bingo.on_bingo_text(upd, ctx))
    bingo_queue.on_resend.assert_awaited_once()   # confirming -> resend path
    bingo_queue.enqueue.assert_not_awaited()      # not a fresh submission


def test_on_bingo_text_non_confirming_non_awaiting_is_ignored(bingo, store, monkeypatch):
    from handlers import bingo_queue
    monkeypatch.setattr(bingo_queue, "on_resend", AsyncMock())
    monkeypatch.setattr(bingo_queue, "enqueue", AsyncMock())
    ctx = _context()                              # awaiting flag unset, no confirming sub
    upd = _text_update(200, "nobody", "R1C1: p - @bob")
    asyncio.run(bingo.on_bingo_text(upd, ctx))
    upd.effective_message.reply_text.assert_not_awaited()
    bingo_queue.on_resend.assert_not_awaited()
    bingo_queue.enqueue.assert_not_awaited()


# --- group-collision regression: text handler must not shadow provisioning --

def test_bingo_text_handler_does_not_shadow_provisioning_on_text(store, monkeypatch):
    """handlers/provisioning.py registers an identically-filtered private-text
    MessageHandler in the default group for its awaiting_email flow. If
    handlers.bingo's text handler were registered in that same default group
    (instead of group=1), PTB would only ever invoke the first-registered
    handler for a private text message and provisioning.on_text would never
    run again. This exercises real PTB dispatch (not just calling the handler
    functions directly) so it would actually catch that regression."""
    import asyncio as _asyncio
    from datetime import datetime as _datetime

    from telegram import Chat, Message, Update, User
    from telegram.ext import Application

    import handlers.bingo as bingo_mod
    import handlers.provisioning as provisioning_mod
    importlib.reload(bingo_mod)
    importlib.reload(provisioning_mod)

    bingo_mock = AsyncMock()
    provisioning_mock = AsyncMock()
    monkeypatch.setattr(bingo_mod, "on_bingo_text", bingo_mock)
    monkeypatch.setattr(provisioning_mod, "on_text", provisioning_mock)

    app = Application.builder().token("123456789:AAEexampletesttoken0000000000000000").build()
    # same registration order as main.py: bingo before provisioning
    bingo_mod.register(app)
    provisioning_mod.register(app)
    # process_update() requires Application.initialize(), but the real thing
    # calls Bot.get_me() over the network for a fake token. We only need
    # dispatch (routing an update to the right handlers), not bot identity,
    # so mark it initialized directly rather than hitting the network.
    app._initialized = True

    user = User(id=100, first_name="Test", is_bot=False, username="alice")
    chat = Chat(id=100, type="private")
    message = Message(message_id=1, date=_datetime.now(), chat=chat, from_user=user,
                       text="not a command, not R1C1, just plain text")
    update = Update(update_id=1, message=message)

    _asyncio.run(app.process_update(update))

    bingo_mock.assert_awaited()
    provisioning_mock.assert_awaited()


def test_on_bingo_image_ocr_timeout_asks_retry_without_stranding(bingo, store, monkeypatch):
    # the isolated OCR times out / fails -> _run_ocr returns None; the player is
    # asked to retry and NO pending submission is left behind (which would lock
    # them out), and the bot never blocked (the whole point of the subprocess).
    store.allocate_bingo_sheet(100, "alice")

    async def _fail(sheet_no, image_bytes):
        return None
    monkeypatch.setattr(bingo, "_run_ocr", _fail)
    ctx = _context()
    ctx.user_data["awaiting_bingo"] = True
    tg_file = AsyncMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)
    upd = _photo_update(100, "alice")
    asyncio.run(bingo.on_bingo_image(upd, ctx))
    assert store.active_submission(100) is None
    upd.effective_message.reply_text.assert_awaited()


# --- confirm_button -> pass -> claim + announce + DM ------------------------

def test_confirm_button_pass_awards_and_posts(bingo, store, monkeypatch):
    store.allocate_bingo_sheet(100, "alice")
    sub_id = store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100), 1)
    line_members = [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "p0", "target_user_id": 1},
        {"row": 0, "col": 1, "handle": "cara", "prompt": "p1", "target_user_id": 2},
        {"row": 0, "col": 3, "handle": "dan", "prompt": "p3", "target_user_id": 3},
        {"row": 0, "col": 4, "handle": "eve", "prompt": "p4", "target_user_id": 4},
    ]
    store.record_winning_members(sub_id, line_members)

    ctx = _context()

    def tap(uid, row, col, ans):
        q = AsyncMock()
        q.data = f"bingoconf:{sub_id}:{row}:{col}:{ans}"
        q.answer = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        q.from_user = SimpleNamespace(id=uid)
        upd = MagicMock()
        upd.callback_query = q
        upd.effective_user = SimpleNamespace(id=uid)
        asyncio.run(bingo.confirm_button(upd, ctx))

    tap(1, 0, 0, "yes")
    tap(2, 0, 1, "yes")
    tap(3, 0, 3, "yes")
    # 3 of 4 yes votes already satisfies required_yes (4-1=3), so prize claimed
    # after the 3rd tap. The 4th is a no-op (already verified).
    tap(4, 0, 4, "yes")  # redundant tap; submission already resolved

    assert store.has_bingo_prize(100) is True
    assert store.bingo_prizes_claimed() == 1
    # channel post to ANNOUNCE_CHAT_ID happened
    import config
    posted = [c for c in ctx.bot.send_message.await_args_list
              if c.kwargs.get("chat_id") == config.ANNOUNCE_CHAT_ID]
    assert posted, "expected a channel announcement"
    sub = store.active_submission(100)
    assert sub is None  # no longer pending (verified)


def test_confirm_button_caches_answer_game_wide(bingo, store):
    store.allocate_bingo_sheet(100, "alice")
    sub_id = store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100), 1)
    store.record_winning_members(sub_id, [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "likes cats", "target_user_id": 1},
    ])
    ctx = _context()
    q = AsyncMock()
    q.data = f"bingoconf:{sub_id}:0:0:yes"
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.from_user = SimpleNamespace(id=1)
    upd = MagicMock()
    upd.callback_query = q
    upd.effective_user = SimpleNamespace(id=1)
    asyncio.run(bingo.confirm_button(upd, ctx))
    assert store.get_cached_confirmation(1, "likes cats") == "yes"


# --- closing the game cancels outstanding timeout jobs ---------------------

def test_award_at_limit_cancels_outstanding_timeouts(bingo, store, monkeypatch):
    # Fill 9 prizes so the next claim is the 10th and closes the game.
    # Mark those submissions verified so they don't appear in pending_submissions()
    # (otherwise _cancel_outstanding_timeouts would try to cancel their jobs too).
    for uid in range(200, 209):
        s = store.start_bingo_submission(uid, f"w{uid}", 1, None)
        store.claim_bingo_prize(uid, f"w{uid}", s)
        store.set_submission_status(s, "verified", verified_at=store._now_iso())
    assert store.bingo_prizes_claimed() == 9

    store.allocate_bingo_sheet(100, "alice")
    sub_id = store.start_bingo_submission(100, "alice", store.get_bingo_sheet(100), 1)
    store.record_winning_members(sub_id, [
        {"row": 0, "col": 0, "handle": "bob", "prompt": "p0", "target_user_id": 1},
        {"row": 0, "col": 1, "handle": "cara", "prompt": "p1", "target_user_id": 2},
        {"row": 0, "col": 3, "handle": "dan", "prompt": "p3", "target_user_id": 3},
        {"row": 0, "col": 4, "handle": "eve", "prompt": "p4", "target_user_id": 4},
    ])
    for uid, prompt in [(1, "p0"), (2, "p1"), (3, "p3"), (4, "p4")]:
        store.record_bingo_confirmation(uid, prompt, "yes")

    ctx = _context()
    # one outstanding timeout job the close should cancel
    job = MagicMock()
    ctx.job_queue.get_jobs_by_name = MagicMock(return_value=[job])

    asyncio.run(bingo._finalize(ctx, sub_id))

    assert store.bingo_is_closed() is True
    job.schedule_removal.assert_called_once()


# --- register wires the handlers --------------------------------------

def test_register_adds_handlers(bingo):
    app = MagicMock()
    bingo.register(app)
    assert app.add_handler.call_count >= 7
    ocr_confirm_calls = [
        c for c in app.add_handler.call_args_list
        if c.args and getattr(c.args[0], "callback", None) is bingo.bingo_ocr_confirm_button
    ]
    assert ocr_confirm_calls, "bingo_ocr_confirm_button handler was not registered"
    # the text-submission handler MUST be in group=1, not the default group,
    # or it will shadow handlers/provisioning.py's identically-filtered
    # awaiting_email MessageHandler (see the dedicated regression test above)
    text_handler_calls = [
        c for c in app.add_handler.call_args_list
        if c.args and getattr(c.args[0], "callback", None) is bingo.on_bingo_text
    ]
    assert text_handler_calls, "on_bingo_text handler was not registered"
    call = text_handler_calls[0]
    group = call.kwargs.get("group", call.args[1] if len(call.args) > 1 else 0)
    assert group == 1


# --- rolling replacement on verification failure ---------------------------

def test_failed_verification_promotes_next_queued(bingo, store, monkeypatch):
    from handlers import bingo_queue
    # `a` is in tagged-people verification ('pending') with a 5-cell line whose
    # subjects are ALL unreachable (target None) -> it FAILS evaluation (needs
    # len-1 = 4 yeses, has 0). A 1-cell line would spuriously PASS (required_yes 0).
    a = store.queue_submission(1, "a", 1)
    store.set_submission_status(a, "pending")
    store.record_winning_members(a, [
        {"row": 0, "col": c, "handle": h, "prompt": "p", "target_user_id": None}
        for c, h in enumerate(["v", "w", "x", "y", "z"])])
    b = store.queue_submission(2, "b", 1)                   # queued behind
    bingo_queue._PENDING_READ[b] = {"read": {"cells": []}, "handle": "b", "sheet_no": 1}
    monkeypatch.setattr(bingo_queue, "_send_confirmation", AsyncMock())
    monkeypatch.setattr(bingo_queue, "_arm_confirm_timeout", MagicMock())
    ctx = _context()
    store.set_queue_open()                                  # a pending sub implies the round is open
    asyncio.run(bingo._finalize(ctx, a, final=True))        # no yeses -> fail
    assert store.submission_status(a) == "failed"
    assert store.submission_status(b) == "confirming"       # next promoted
    bingo_queue._PENDING_READ.pop(b, None)


# --- /import_bingo_queue facil command --------------------------------------

def test_import_bingo_queue_command_is_facil_only_and_reports(bingo, store, monkeypatch):
    from handlers import bingo_queue
    monkeypatch.setattr(bingo_queue, "import_queue", AsyncMock(return_value=3))
    # is_facilitator is async in utils.auth; make the caller a facil
    monkeypatch.setattr("handlers.bingo.is_facilitator", AsyncMock(return_value=True), raising=False)
    from utils import auth
    monkeypatch.setattr(auth, "is_facilitator", AsyncMock(return_value=True))
    ctx = _context()
    upd = _text_update(100, "aria", "/import_bingo_queue")
    asyncio.run(bingo.import_bingo_queue(upd, ctx))
    bingo_queue.import_queue.assert_awaited_once()
    sent = upd.effective_message.reply_text.await_args.args[0]
    assert "3" in sent and "queue" in sent.lower()


# --- /start_forward_round facil command -------------------------------------

def test_start_forward_round_command_is_facil_only_and_reports(bingo, store, monkeypatch):
    from handlers import bingo_forward
    monkeypatch.setattr(bingo_forward, "begin_round", AsyncMock(return_value=7))
    # is_facilitator is async in utils.auth; make the caller a facil
    monkeypatch.setattr("handlers.bingo.is_facilitator", AsyncMock(return_value=True), raising=False)
    from utils import auth
    monkeypatch.setattr(auth, "is_facilitator", AsyncMock(return_value=True))
    ctx = _context()
    upd = _text_update(100, "aria", "/start_forward_round")
    asyncio.run(bingo.start_forward_round(upd, ctx))
    bingo_forward.begin_round.assert_awaited_once()
    sent = upd.effective_message.reply_text.await_args.args[0]
    assert "7" in sent


# --- on_bingo_text routes forward-round resends before the live queue -------

def test_on_bingo_text_forward_collecting_routes_to_forward_resend(bingo, store, monkeypatch):
    # A collecting-phase user who already has a 'fwd_confirming' row is routed to
    # bingo_forward.on_resend, and must NOT fall through to the live-queue path.
    from handlers import bingo_forward, bingo_queue
    monkeypatch.setattr(bingo_forward, "storage", store)
    monkeypatch.setattr(bingo_forward, "on_resend", AsyncMock())
    monkeypatch.setattr(bingo_queue, "on_resend", AsyncMock())
    monkeypatch.setattr(bingo_queue, "enqueue", AsyncMock())
    store.allocate_bingo_sheet(100, "alice")
    store.set_forward_phase("collecting")
    store.queue_forwarded_submission(
        100, "alice", store.get_bingo_sheet(100), "2026-01-01T09:00:00")
    ctx = _context()
    upd = _text_update(100, "alice", "R1C1: p - @bob")
    asyncio.run(bingo.on_bingo_text(upd, ctx))
    bingo_forward.on_resend.assert_awaited_once()   # forward resend path taken
    bingo_queue.on_resend.assert_not_awaited()      # NOT the live-queue resend
    bingo_queue.enqueue.assert_not_awaited()        # NOT a fresh live submission


# --- DM the facil admin(s) about each winner --------------------------------

def test_dm_admins_of_winner_dms_zzehao_and_marks(bingo, store, monkeypatch):
    store.mark_started(999, "zzehao", "Zhou")            # admin has /started
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"zzehao"})
    store.allocate_bingo_sheet(1, "alice")
    sub = store.start_bingo_submission(1, "alice", 1)
    store.claim_bingo_prize(1, "alice", sub)
    ctx = _context()
    asyncio.run(bingo._dm_admins_of_winner(
        ctx, {"winner_user_id": 1, "handle": "alice", "claim_no": 1}))
    dm = [c for c in ctx.bot.send_message.await_args_list if c.kwargs.get("chat_id") == 999]
    assert dm and "alice" in dm[0].kwargs["text"]
    assert store.winners_pending_admin_notice() == []    # marked


def test_dm_admins_no_recipient_does_not_mark(bingo, store, monkeypatch):
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"nobody_started"})
    store.allocate_bingo_sheet(1, "alice")
    sub = store.start_bingo_submission(1, "alice", 1)
    store.claim_bingo_prize(1, "alice", sub)
    ctx = _context()
    asyncio.run(bingo._dm_admins_of_winner(
        ctx, {"winner_user_id": 1, "handle": "alice", "claim_no": 1}))
    ctx.bot.send_message.assert_not_awaited()
    assert [w["winner_user_id"] for w in store.winners_pending_admin_notice()] == [1]  # NOT marked


def test_notify_pending_winners_job_sweeps_all(bingo, store, monkeypatch):
    store.mark_started(999, "zzehao", "Zhou")
    monkeypatch.setattr(bingo.config, "FACILITATOR_HANDLES", {"zzehao"})
    for uid, h in [(1, "alice"), (2, "bob")]:
        store.allocate_bingo_sheet(uid, h)
        sub = store.start_bingo_submission(uid, h, 1)
        store.claim_bingo_prize(uid, h, sub)
    ctx = _context(); ctx.job = MagicMock()
    asyncio.run(bingo._notify_pending_winners_job(ctx))
    assert store.winners_pending_admin_notice() == []    # all marked
    dmd = {c.kwargs.get("chat_id") for c in ctx.bot.send_message.await_args_list}
    assert dmd == {999}                                  # both winners announced to zzehao
