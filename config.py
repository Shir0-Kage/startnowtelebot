"""Central configuration. Values that change between machines live in the
environment (.env), everything else is here so it's easy to find and tweak."""

import os
from datetime import timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# pull in .env if it exists (harmless if it doesn't)
load_dotenv()

# --- Bot token -------------------------------------------------------------
# Never commit this. BotFather gives it to you, put it in .env as BOT_TOKEN.
BOT_TOKEN = os.environ.get("BOT_TOKEN")


# --- Facilitators ----------------------------------------------------------
# People allowed to run facil-only commands even when they aren't a Telegram
# group admin. Set FACILITATOR_IDS in .env, e.g. FACILITATOR_IDS=123456,789012
def _parse_ids(raw):
    out = set()
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


FACILITATORS = _parse_ids(os.environ.get("FACILITATOR_IDS"))


# --- Time ------------------------------------------------------------------
# Everything StartNOW! runs on Singapore time.
SGT = ZoneInfo("Asia/Singapore")
TIMEZONE = SGT


# --- Storage ---------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "bot.db")


# --- Reminders -------------------------------------------------------------
# How far before an event each reminder goes out. Order matters only for
# readability; the scheduler handles them independently.
REMINDER_OFFSETS = [
    ("1 day", timedelta(days=1)),
    ("1 hour", timedelta(hours=1)),
    ("10 minutes", timedelta(minutes=10)),
]

# Reminders are on by default for a new group; facils can toggle per chat.
REMINDERS_DEFAULT_ON = True


# --- Human Bingo OCR matching ----------------------------------------------
# Minimum WRatio score (0-100) for a fuzzy handle match to be accepted.
BINGO_MATCH_THRESHOLD = 85
# A winning match must beat the runner-up by at least this many points.
BINGO_MATCH_MARGIN = 8
