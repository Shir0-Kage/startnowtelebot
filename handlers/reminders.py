"""Automatic reminders.

On startup we look at every upcoming event and queue jobs for 1 day, 1 hour and
10 minutes before it. Engagement reminders go to every group that wants them;
meet-up reminders are slot-aware — AM groups get the AM timing, PM groups get
the PM timing.
"""

import logging
from datetime import datetime

import storage
from config import REMINDER_OFFSETS, TIMEZONE
from data import events

log = logging.getLogger(__name__)

# turn an offset label into something that reads naturally in a sentence
LEAD_PHRASE = {
    "1 day": "tomorrow",
    "1 hour": "in 1 hour",
    "10 minutes": "in 10 minutes",
}


def _lead(label):
    return LEAD_PHRASE.get(label, f"in {label}")


def _fmt_day(d):
    # "12 July"
    return f"{d.day} {d.strftime('%B')}"


# ---------------------------------------------------------------------------
# Reminder message builders
# ---------------------------------------------------------------------------

def _engagement_message(ev, label):
    return (
        f"⏰ Reminder: <b>{ev['name']}</b> is happening {_lead(label)}!\n\n"
        f"📅 Date: {_fmt_day(ev['date'])}\n"
        f"⏰ Time: {ev['time'].strftime('%H%M')}H SGT\n"
        f"📍 Where: {ev['where']}\n"
        f"🎮 What: {ev['what']}\n\n"
        "See y'all there ❤️"
    )


def _meetup_message(ev, slot, label):
    return (
        f"⏰ Reminder: <b>StartNOW! {ev['short']}</b> is happening {_lead(label)} "
        f"for {slot} groups!\n\n"
        f"📅 Date: {_fmt_day(ev['date'])}\n"
        f"⏰ Time: {events.slot_time_str(ev, slot)} SGT\n"
        f"📍 Where: {ev['where']}\n"
        f"🎮 What: {ev['what']}\n\n"
        "See y'all there ❤️"
    )


# ---------------------------------------------------------------------------
# Job callbacks
# ---------------------------------------------------------------------------

async def _broadcast(context, chats, text):
    for g in chats:
        try:
            await context.bot.send_message(
                chat_id=g["chat_id"], text=text, parse_mode="HTML"
            )
        except Exception as exc:
            # group might have removed the bot, etc. — just skip it
            log.warning("couldn't send reminder to %s: %s", g["chat_id"], exc)


async def engagement_reminder(context):
    data = context.job.data
    ev = events.EVENTS_BY_KEY.get(data["key"])
    if not ev:
        return
    text = _engagement_message(ev, data["label"])
    await _broadcast(context, storage.groups_with_reminders(), text)


async def meetup_reminder(context):
    data = context.job.data
    ev = events.EVENTS_BY_KEY.get(data["key"])
    if not ev:
        return
    slot = data["slot"]
    text = _meetup_message(ev, slot, data["label"])
    await _broadcast(context, storage.groups_by_slot(slot), text)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def schedule_reminders(app):
    """Queue all future reminder jobs. Safe to call once at startup."""
    jq = app.job_queue
    if jq is None:
        log.warning(
            "JobQueue not available — install python-telegram-bot[job-queue] "
            "to enable reminders."
        )
        return

    now = datetime.now(TIMEZONE)
    queued = 0

    for ev in events.ENGAGEMENTS:
        event_dt = events.engagement_dt(ev)
        for label, offset in REMINDER_OFFSETS:
            when = event_dt - offset
            if when <= now:
                continue  # already past, don't bother
            jq.run_once(
                engagement_reminder,
                when=when,
                data={"key": ev["key"], "label": label},
                name=f"rem:{ev['key']}:{label}",
            )
            queued += 1

    for ev in events.MEETUPS:
        for slot in ("AM", "PM"):
            slot_dt = events.meetup_slot_dt(ev, slot)
            for label, offset in REMINDER_OFFSETS:
                when = slot_dt - offset
                if when <= now:
                    continue
                jq.run_once(
                    meetup_reminder,
                    when=when,
                    data={"key": ev["key"], "slot": slot, "label": label},
                    name=f"rem:{ev['key']}:{slot}:{label}",
                )
                queued += 1

    log.info("scheduled %d reminder job(s)", queued)
