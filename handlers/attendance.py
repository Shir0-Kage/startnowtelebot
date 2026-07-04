"""Attendance via a group poll.

A facil posts (or the bot auto-posts, 1 day before a meet-up) a non-anonymous
poll — Going / Not going / Maybe. Each vote is recorded as it comes in. Because
the poll is non-anonymous, everyone can see who voted what. It's a headcount,
not an RSVP gate.
"""

import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, PollAnswerHandler

import storage
from config import TIMEZONE
from data import events
from utils.auth import facil_only, is_facilitator
from utils.text import display_name

log = logging.getLogger(__name__)

POLL_OPTIONS = ["✅ Going", "❌ Not going", "🤔 Maybe"]
ANSWER_LABELS = ["Going", "Not going", "Maybe"]  # stored value, by option index


def _time_label(ev, slot):
    if events.is_meetup(ev):
        if slot in ("AM", "PM"):
            return f"{slot} {events.slot_time_str(ev, slot)} SGT"
        return (f"AM {events.slot_time_str(ev, 'AM')} / "
                f"PM {events.slot_time_str(ev, 'PM')} SGT")
    return ev["time"].strftime("%H%M") + "H SGT"


def _poll_question(ev, slot):
    date = f"{ev['date'].day} {ev['date'].strftime('%b')}"
    return f"{ev['short']} — are you coming? ({date}, {_time_label(ev, slot)})"


async def _send_poll(bot, chat_id, ev, slot):
    msg = await bot.send_poll(
        chat_id=chat_id,
        question=_poll_question(ev, slot),
        options=POLL_OPTIONS,
        is_anonymous=False,           # so we get each voter in the PollAnswer
        allows_multiple_answers=False,
    )
    storage.record_poll(msg.poll.id, chat_id, ev["key"], msg.message_id, slot)
    return msg


# ---------------------------------------------------------------------------
# /attendance — post a poll (facil), or show a picker
# ---------------------------------------------------------------------------

async def attendance_command(update, context):
    chat = update.effective_chat
    storage.ensure_group(chat.id, chat.title or "")

    if context.args:  # /attendance <event> — post the poll straight away
        if not await is_facilitator(update, context):
            await update.effective_message.reply_text(
                "Only facils can start an attendance poll 🙏"
            )
            return
        ev = events.find_event(" ".join(context.args))
        if not ev:
            await update.effective_message.reply_text(
                "I don't recognise that event. Try /attendance to see the list."
            )
            return
        await _send_poll(context.bot, chat.id, ev, storage.get_slot(chat.id))
        return

    rows = []
    pool = events.MEETUPS + events.ENGAGEMENTS
    for i in range(0, len(pool), 2):
        rows.append([
            InlineKeyboardButton(ev["short"], callback_data=f"attopen:{ev['key']}")
            for ev in pool[i : i + 2]
        ])
    await update.effective_message.reply_text(
        "📋 <b>Attendance</b>\n\nFacils — pick an event to post an attendance "
        "poll for this group:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def attopen_button(update, context):
    query = update.callback_query
    if not await is_facilitator(update, context):
        await query.answer("Only facils can start attendance 🙏", show_alert=True)
        return
    await query.answer()
    ev = events.EVENTS_BY_KEY.get(query.data.split(":", 1)[1])
    if not ev:
        return
    chat = update.effective_chat
    storage.ensure_group(chat.id, chat.title or "")
    await _send_poll(context.bot, chat.id, ev, storage.get_slot(chat.id))


# ---------------------------------------------------------------------------
# Votes — a PollAnswer only carries the poll id, so we map it back via storage
# ---------------------------------------------------------------------------

async def on_poll_answer(update, context):
    pa = update.poll_answer
    if pa is None or pa.user is None:
        return
    poll = storage.get_poll(pa.poll_id)
    if not poll:
        return
    if not pa.option_ids:  # they retracted their vote
        storage.remove_vote(poll["chat_id"], poll["event_key"], pa.user.id)
        return
    idx = pa.option_ids[0]
    answer = ANSWER_LABELS[idx] if idx < len(ANSWER_LABELS) else "?"
    storage.record_vote(
        poll["chat_id"], poll["event_key"], pa.user.id,
        display_name(pa.user), pa.user.username or "", poll["slot"], answer,
    )


# ---------------------------------------------------------------------------
# Facil admin: close / clear / export
# ---------------------------------------------------------------------------

@facil_only
async def close_attendance_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /close_attendance <event>, e.g. /close_attendance meetup1"
        )
        return
    ev = events.find_event(" ".join(context.args))
    if not ev:
        await update.effective_message.reply_text("I don't recognise that event.")
        return
    chat = update.effective_chat
    for mid in storage.open_poll_messages(chat.id, ev["key"]):
        try:
            await context.bot.stop_poll(chat.id, mid)
        except Exception as exc:
            log.warning("stop_poll failed (%s): %s", mid, exc)
    storage.close_event_polls(chat.id, ev["key"])
    await update.effective_message.reply_text(
        f"Closed the {ev['short']} attendance poll. ✅"
    )


# ---------------------------------------------------------------------------
# Auto-send: a poll 1 day before each meet-up's slot time (slot-aware)
# ---------------------------------------------------------------------------

async def _auto_poll_job(context):
    data = context.job.data
    ev = events.EVENTS_BY_KEY.get(data["key"])
    slot = data["slot"]
    if not ev:
        return
    for g in storage.groups_by_slot(slot):
        try:
            await _send_poll(context.bot, g["chat_id"], ev, slot)
        except Exception as exc:
            log.warning("auto-poll to %s failed: %s", g["chat_id"], exc)


def schedule_attendance_polls(app):
    """Queue an attendance poll 1 day before each meet-up's AM/PM slot time."""
    jq = app.job_queue
    if jq is None:
        return
    now = datetime.now(TIMEZONE)
    queued = 0
    for ev in events.MEETUPS:
        for slot in ("AM", "PM"):
            when = events.meetup_slot_dt(ev, slot) - timedelta(days=1)
            if when <= now:
                continue
            jq.run_once(
                _auto_poll_job, when=when,
                data={"key": ev["key"], "slot": slot},
                name=f"poll:{ev['key']}:{slot}",
            )
            queued += 1
    log.info("scheduled %d attendance poll(s)", queued)


def register(app):
    app.add_handler(CommandHandler("attendance", attendance_command))
    app.add_handler(CommandHandler("close_attendance", close_attendance_command))
    app.add_handler(CallbackQueryHandler(attopen_button, pattern=r"^attopen:"))
    app.add_handler(PollAnswerHandler(on_poll_answer))
