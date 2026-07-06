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


# Facilitators listed by @username instead of numeric id — handy for admins
# whose id we don't have. Anyone here is treated as a facilitator everywhere,
# including DMs. @zzehao (lead organiser) is always included; add more with
# FACILITATOR_HANDLES in .env, e.g. FACILITATOR_HANDLES=someone,another.
def _parse_handles(raw):
    out = set()
    for chunk in (raw or "").split(","):
        h = chunk.strip().lstrip("@").lower()
        if h:
            out.add(h)
    return out


FACILITATOR_HANDLES = {"zzehao"} | _parse_handles(os.environ.get("FACILITATOR_HANDLES"))


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


# --- Human Bingo -----------------------------------------------------------
# Announcement channel for "N/10 prizes claimed!" posts. Default is the
# StartNOW! 2026 group (decision #5); override per-deploy with ANNOUNCE_CHAT_ID.
ANNOUNCE_CHAT_ID = int(os.environ.get("ANNOUNCE_CHAT_ID", "-1004292606016"))

# First 10 winners take a prize; the game closes once the 10th is claimed.
BINGO_PRIZE_LIMIT = 10

# How long we wait for the people in a winning line to tap Yes/No before the
# submission is finally evaluated with any still-pending cells counting as misses.
BINGO_CONFIRM_TIMEOUT = timedelta(hours=12)

# rapidfuzz score (0-100) an OCR'd cell must reach to count as a confident match...
BINGO_MATCH_THRESHOLD = 85
# ...and it must beat the second-best candidate by at least this margin, so an
# ambiguous read degrades to an empty cell instead of guessing the wrong person.
BINGO_MATCH_MARGIN = 8

# Breather after a failed attempt before the same person may /submit_bingo again.
BINGO_RETRY_COOLDOWN = timedelta(seconds=60)
