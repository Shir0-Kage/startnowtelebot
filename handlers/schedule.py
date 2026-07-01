"""Schedule views: /schedule, /next, /meetups, /engagements."""

from datetime import datetime

from telegram.ext import CommandHandler

import storage
from config import TIMEZONE
from data import events


def _fmt_date(d):
    # e.g. "12 Jul 2026"
    return f"{d.day} {d.strftime('%b %Y')}"


def _fmt_time(t):
    return t.strftime("%H%M") + "H"


def _engagement_line(ev):
    return (
        f"{ev['emoji']} <b>{ev['name']}</b>\n"
        f"    {_fmt_date(ev['date'])}, {_fmt_time(ev['time'])}"
    )


def _meetup_line(ev):
    return (
        f"{ev['emoji']} <b>{ev['name']}</b>\n"
        f"    {_fmt_date(ev['date'])}\n"
        f"    ☀️ AM: {_fmt_time(ev['am_time'])}   🌙 PM: {_fmt_time(ev['pm_time'])}"
    )


def _platform_line(ev):
    return f"{ev['emoji']} {ev['name']} — {_fmt_date(ev['date'])}"


def _now():
    return datetime.now(TIMEZONE)


async def schedule_command(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or "")

    parts = ["📅 <b>StartNOW! 2026 Schedule</b>\n"]

    parts.append("<b>✨ Optional Engagements</b>")
    parts.append("\n".join(_engagement_line(ev) for ev in events.ENGAGEMENTS))

    parts.append("\n<b>🎯 Official Meet-Ups</b>")
    parts.append("\n".join(_meetup_line(ev) for ev in events.MEETUPS))

    parts.append("\n<b>🌐 Gather Town</b>")
    parts.append("\n".join(_platform_line(ev) for ev in events.PLATFORM))

    parts.append(
        "\n<i>Meet-ups run about 1.5–2 hours. AM slots start 1000H SGT, "
        "PM slots start 1900H SGT.</i>"
    )

    await update.effective_message.reply_html("\n".join(parts))


async def next_command(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or "")

    ev, when = events.next_event(_now())
    if ev is None:
        await update.effective_message.reply_text(
            "That's a wrap — no more events coming up. Thanks for being part of "
            "StartNOW!  ❤️"
        )
        return

    if events.is_meetup(ev):
        body = (
            f"⏭️ <b>Next up: {ev['name']}</b>\n\n"
            f"📅 {_fmt_date(ev['date'])}\n"
            f"☀️ AM: {_fmt_time(ev['am_time'])}   🌙 PM: {_fmt_time(ev['pm_time'])}\n"
            f"📍 {ev['where']}\n"
            f"🎮 {ev['what']}"
        )
    else:
        body = (
            f"⏭️ <b>Next up: {ev['name']}</b>\n\n"
            f"📅 {_fmt_date(ev['date'])}, {_fmt_time(ev['time'])} SGT\n"
            f"📍 {ev['where']}\n"
            f"🎮 {ev['what']}"
        )
    await update.effective_message.reply_html(body)


async def meetups_command(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or "")

    text = "🎯 <b>Official StartNOW! Meet-Ups</b>\n\n" + "\n\n".join(
        _meetup_line(ev) for ev in events.MEETUPS
    )
    text += (
        "\n\nEvery meet-up has an AM and a PM slot. Not sure which one your "
        "group is on? Try /slot."
    )
    await update.effective_message.reply_html(text)


async def engagements_command(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or "")

    text = "✨ <b>Optional Engagement Sessions</b>\n\n" + "\n\n".join(
        _engagement_line(ev) for ev in events.ENGAGEMENTS
    )
    text += "\n\nThese are optional and just for fun — drop by if you're free!"
    await update.effective_message.reply_html(text)


def register(app):
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CommandHandler("meetups", meetups_command))
    app.add_handler(CommandHandler("engagements", engagements_command))
