"""/start and /help — the friendly front door."""

from telegram.ext import CommandHandler

import storage

START_TEXT = (
    "👋 Hey there, welcome to <b>StartNOW! 2026</b>!\n\n"
    "I'm here to help facils and Year 1s stay on top of everything during "
    "orientation — quests, the schedule, reminders, attendance and "
    "announcements.\n\n"
    "Type /help to see what I can do. See y'all around! ❤️"
)

HELP_TEXT = (
    "<b>Here's everything I can do 🌟</b>\n\n"
    "<b>🗺️ Quests</b>\n"
    "/quests — all quests and their Gather Town spots\n"
    "/quest &lt;name&gt; — details for one quest\n\n"
    "<b>📅 Schedule &amp; reminders</b>\n"
    "/schedule — the full StartNOW! schedule\n"
    "/next — the next upcoming event\n"
    "/meetups — the three official meet-ups\n"
    "/engagements — optional engagement sessions\n"
    "/slot — check if this group is AM or PM\n\n"
    "<b>📋 Attendance</b>\n"
    "/attendance — post an attendance poll (Going / Not going / Maybe)\n"
    "<i>(a poll also auto-posts 1 day before each meet-up)</i>\n\n"
    "<b>For facilitators 🛠️</b>\n"
    "/setslot am|pm — set this group's meet-up slot\n"
    "/reminders on|off — toggle reminders for this group\n"
    "/attendance &lt;event&gt; — post an attendance poll for an event\n"
    "/close_attendance &lt;event&gt; — close the poll\n"
    "/announce &lt;message&gt; — post a formatted announcement\n"
    "/remind &lt;message&gt; — post a short reminder\n"
    "/pinannounce &lt;message&gt; — announce and pin it\n"
    "/add_year_ones — add this group's Year 1s (from the sheet)\n"
)


async def start(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or chat.full_name or "")
    # note who's checked in — the group setup scripts use this to know who's
    # ready to be added to the orientation groups
    user = update.effective_user
    if user:
        storage.mark_started(user.id, user.username, user.full_name)
    await update.effective_message.reply_html(START_TEXT)


async def help_command(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or chat.full_name or "")
    await update.effective_message.reply_html(HELP_TEXT)


def register(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
