"""Bot-side onboarding: DM people their group's invite link.

The bot can't add anyone to a group, but it CAN message users who've messaged
it — so on /start it sends the person their group's join link (one tap to join).
Their group comes from a deep-link payload (t.me/<bot>?start=AM3) or, failing
that, from matching their @username against the Year 1 sheet.

/add_year_ones lets a facil DM the link to all of their group's Year 1s who've
already started the bot.
"""

import logging
import re

from telegram.ext import CommandHandler

import storage
from setup import manifest, sheets
from utils.auth import facil_only

log = logging.getLogger(__name__)

_OG_RE = re.compile(r"(?i)\b(AM|PM)\s*(10|[1-9])\b")     # inside a title
_OG_EXACT = re.compile(r"(?i)^(AM|PM)(10|[1-9])$")        # a bare payload

_year1_map = None    # username (lower) -> OG, built from the sheet on first use
_link_cache = {}     # OG -> invite link


def _og_from_title(title):
    if not title:
        return None
    m = _OG_RE.search(title)
    return (m.group(1).upper() + m.group(2)) if m else None


def _year1_og_map():
    global _year1_map
    if _year1_map is None:
        _year1_map = {}
        try:
            for og, members in sheets.load_year1_members().items():
                for m in members:
                    if m.get("handle"):
                        _year1_map[m["handle"].lower()] = og
        except Exception as exc:
            log.warning("couldn't load Year 1 roster: %s", exc)
    return _year1_map


def _og_for(args, user):
    # 1) deep-link payload, e.g. /start AM3
    if args:
        m = _OG_EXACT.match(args[0].strip())
        if m:
            return m.group(1).upper() + m.group(2)
    # 2) match their @username against the Year 1 sheet
    if user and user.username:
        return _year1_og_map().get(user.username.lower())
    return None


async def _group_link(bot, og):
    if og in _link_cache:
        return _link_cache[og]
    entry = manifest.load().get(f"StartNOW! {og}")
    if not entry or not entry.get("chat_id"):
        return None
    link = entry.get("invite_link")  # reuse one made by setup.invite_links
    if not link:
        try:
            link = (await bot.create_chat_invite_link(entry["chat_id"])).invite_link
        except Exception as exc:
            log.warning("couldn't make an invite link for %s: %s", og, exc)
            return None
    _link_cache[og] = link
    return link


def _welcome(og, link):
    return (
        f"Welcome to StartNOW! 2026 🌟\n\n"
        f"Here's your orientation group ({og}) — tap to join:\n{link}\n\n"
        "See you there! ❤️"
    )


async def try_send_group_link(update, context):
    """On /start in a DM, send the person their group's invite link if we can
    figure out which group they're in. Returns True if a link was sent."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return False
    og = _og_for(context.args, update.effective_user)
    if not og:
        return False
    link = await _group_link(context.bot, og)
    if not link:
        return False
    await update.effective_message.reply_text(_welcome(og, link))
    storage.mark_link_sent(update.effective_user.id, og)
    return True


@facil_only
async def add_year_ones(update, context):
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("Run this inside your orientation group 🙂")
        return
    og = _og_from_title(chat.title)
    if not og:
        await update.effective_message.reply_text(
            "I couldn't tell which OG this is from the group name (expected "
            "something like 'StartNOW! AM3')."
        )
        return

    link = await _group_link(context.bot, og)
    if not link:
        await update.effective_message.reply_text(
            "I couldn't make an invite link — is the group set up and am I an "
            "admin with invite rights?"
        )
        return

    # DM the link to this OG's Year 1s who've started the bot (skip already-sent)
    want = {u for u, g in _year1_og_map().items() if g == og}
    sent = 0
    for su in storage.get_started():
        uname = (su.get("username") or "").lower()
        if uname in want and storage.link_sent_to(su["user_id"]) != og:
            try:
                await context.bot.send_message(su["user_id"], _welcome(og, link))
                storage.mark_link_sent(su["user_id"], og)
                sent += 1
            except Exception as exc:
                log.warning("couldn't DM %s: %s", su["user_id"], exc)

    await update.effective_message.reply_text(
        f"Sent the {og} join link to {sent} Year 1(s) who've started the bot. "
        "Anyone who hasn't can message me (or use their emailed link) to get theirs. 🌟"
    )


def register(app):
    app.add_handler(CommandHandler("add_year_ones", add_year_ones))
