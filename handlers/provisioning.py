"""/add_year_ones — a facil asks the setup account to add this group's Year 1s.

The bot itself can't add members, so this just queues the request; the Telethon
worker running as the owner account picks it up and does the adding.
"""

import re

from telegram.ext import CommandHandler

import storage
from utils.auth import facil_only

# pull "AM3" / "PM7" out of a group title like "StartNOW! AM3".
# tolerant of case and a stray space, e.g. "startnow! am 3" -> "AM3".
_OG_RE = re.compile(r"(?i)\b(AM|PM)\s*(10|[1-9])\b")


def _og_from_title(title):
    if not title:
        return None
    m = _OG_RE.search(title)
    return (m.group(1).upper() + m.group(2)) if m else None


@facil_only
async def add_year_ones(update, context):
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Run this inside your orientation group 🙂"
        )
        return

    og = _og_from_title(chat.title)
    if not og:
        await update.effective_message.reply_text(
            "I couldn't tell which OG this is from the group name. This should "
            "be run in a group named like 'StartNOW! AM3'."
        )
        return

    storage.ensure_group(chat.id, chat.title or "")
    storage.enqueue_request(chat.id, og, "year_ones", update.effective_user.id)
    await update.effective_message.reply_text(
        f"On it — queuing {og}'s Year 1s to be added to this group. "
        "They'll be added shortly, and anyone who can't be added directly will "
        "get an invite link. 🌟"
    )


def register(app):
    app.add_handler(CommandHandler("add_year_ones", add_year_ones))
