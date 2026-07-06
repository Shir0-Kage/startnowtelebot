"""StartNOW! 2026 Telegram bot — entry point.

Run with:  python main.py
Make sure BOT_TOKEN is set first (see the README).
"""

import logging
import sys

from telegram import BotCommand, Update
from telegram.ext import Application, Defaults

import config
import storage
from handlers import (
    announcements,
    attendance,
    bingo,
    common,
    provisioning,
    quests,
    reminders,
    schedule,
    settings,
)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
# httpx is noisy at INFO — quiet it down a notch
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("startnow")


# Commands shown in Telegram's "/" menu. Facil-only ones are left out to keep
# the menu tidy for students.
MENU_COMMANDS = [
    BotCommand("start", "What this bot does"),
    BotCommand("help", "List all commands"),
    BotCommand("quests", "Quest locations"),
    BotCommand("quest", "Details for one quest"),
    BotCommand("schedule", "Full StartNOW! schedule"),
    BotCommand("next", "Next upcoming event"),
    BotCommand("meetups", "The official meet-ups"),
    BotCommand("engagements", "Optional sessions"),
    BotCommand("slot", "Is this group AM or PM?"),
    BotCommand("attendance", "Post an attendance poll"),
    BotCommand("get_bingo", "Get your Human Bingo card"),
    BotCommand("submit_bingo", "Submit your filled bingo card"),
]


async def _on_startup(app):
    """Runs once after the app is built: set the menu and queue reminders."""
    await app.bot.set_my_commands(MENU_COMMANDS)
    reminders.schedule_reminders(app)
    attendance.schedule_attendance_polls(app)
    bingo.rearm_bingo_timeouts(app)
    log.info("bot is up and running")


async def _on_error(update, context):
    log.exception("error while handling update", exc_info=context.error)


def main():
    if not config.BOT_TOKEN:
        sys.exit(
            "BOT_TOKEN is not set. Put it in a .env file or export it as an "
            "environment variable — see the README."
        )

    storage.init_db()

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        # If someone deletes their message before our reply lands (common in
        # groups, where replies quote by default), send it anyway instead of
        # raising "Message to be replied not found".
        .defaults(Defaults(allow_sending_without_reply=True))
        .post_init(_on_startup)
        .build()
    )

    # wire up each feature
    common.register(app)
    bingo.register(app)
    quests.register(app)
    schedule.register(app)
    settings.register(app)
    attendance.register(app)
    announcements.register(app)
    provisioning.register(app)

    app.add_error_handler(_on_error)

    # long-polling — simplest way to run; no public URL needed.
    # ALL_TYPES so we receive poll_answer updates (attendance votes).
    # drop_pending_updates: on restart, skip the backlog Telegram queued while
    # the bot was down/frozen, so a pile of old /submit_bingo images can't
    # immediately re-overload it. (Updates sent while offline are dropped.)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
