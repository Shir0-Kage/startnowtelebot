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

_OG_RE = re.compile(r"(?i)\b(AM|PM)\s*(10|[1-9])\b")           # inside a title
# deep-link payload: 'AM3' (Year 1, held) or 'facil-AM3' (facil, immediate)
_PAYLOAD_RE = re.compile(r"(?i)^(facil-)?(AM|PM)(10|[1-9])$")

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


def _role_og(args, user):
    """(role, OG) from a deep-link payload or @username match. role is 'facil'
    (get the link now) or 'year1' (held until the OG is opened). (None, None)
    if we can't tell."""
    if args:
        m = _PAYLOAD_RE.match(args[0].strip())
        if m:
            role = "facil" if m.group(1) else "year1"
            return role, (m.group(2).upper() + m.group(3))
    if user and user.username:
        og = _year1_og_map().get(user.username.lower())
        if og:
            return "year1", og
    return None, None


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
    """On /start in a DM: facils get their link now; Year 1s are held until
    their facil opens the OG. Returns True if we handled the message."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return False
    role, og = _role_og(context.args, update.effective_user)
    if not og:
        return False
    uid = update.effective_user.id

    # Year 1s wait for the facil's /add_year_ones (unless the OG is already open)
    if role == "year1" and not storage.is_og_opened(og):
        storage.add_waiting(uid, og)
        await update.effective_message.reply_text(
            f"You're on the list for {og}! 🌟 Your facil will let you in shortly "
            "— I'll send your join link the moment they do."
        )
        return True

    link = await _group_link(context.bot, og)
    if not link:
        return False
    await update.effective_message.reply_text(_welcome(og, link))
    storage.mark_link_sent(uid, og)
    storage.remove_waiting(uid)
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

    storage.open_og(og)  # from now on, Year 1s who /start get the link immediately

    # release everyone who was waiting for this OG
    sent = 0
    for uid in storage.waiting_for_og(og):
        try:
            await context.bot.send_message(uid, _welcome(og, link))
            storage.mark_link_sent(uid, og)
            storage.remove_waiting(uid)
            sent += 1
        except Exception as exc:
            log.warning("couldn't DM %s: %s", uid, exc)

    await update.effective_message.reply_text(
        f"Opened {og} — sent the join link to {sent} Year 1(s) who were waiting. "
        "Anyone who messages me now gets theirs immediately. 🌟"
    )


def register(app):
    app.add_handler(CommandHandler("add_year_ones", add_year_ones))
