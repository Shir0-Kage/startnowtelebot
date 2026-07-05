"""Bot-side onboarding: tell people their group and DM their join link.

The bot can't add anyone, but it CAN message users who message it. On /start it
identifies the person and either sends their group's join link or — for Year 1s
before their facil opens the OG — puts them on a waiting list. It figures out
their group from a facil deep link (facil-<OG>), their @username, or, if the
handle doesn't match, the sign-up email they type in.
"""

import logging
import re

from telegram.ext import CommandHandler, MessageHandler, filters

import storage
from setup import manifest, sheets
from utils.auth import facil_only

log = logging.getLogger(__name__)

_OG_RE = re.compile(r"(?i)\b(AM|PM)\s*(10|[1-9])\b")            # inside a title
_PAYLOAD_RE = re.compile(r"(?i)^(facil-)?(AM|PM)(10|[1-9])$")   # deep-link payload

_year1_by_handle = None   # username (lower) -> OG
_year1_by_email = None    # email (lower)    -> OG
_facil_by_handle = None   # username (lower) -> OG
_link_cache = {}          # OG -> invite link


def _og_from_title(title):
    if not title:
        return None
    m = _OG_RE.search(title)
    return (m.group(1).upper() + m.group(2)) if m else None


def _load_year1_maps():
    global _year1_by_handle, _year1_by_email
    if _year1_by_handle is not None:
        return
    _year1_by_handle, _year1_by_email = {}, {}
    try:
        for og, members in sheets.load_year1_members().items():
            for m in members:
                if m.get("handle"):
                    _year1_by_handle[m["handle"].lower()] = og
                if m.get("email"):
                    _year1_by_email[m["email"].strip().lower()] = og
    except Exception as exc:
        log.warning("couldn't load Year 1 roster: %s", exc)


def _og_by_handle(username):
    _load_year1_maps()
    return _year1_by_handle.get((username or "").lower())


def _og_by_email(email):
    _load_year1_maps()
    return _year1_by_email.get((email or "").strip().lower())


def _load_facil_map():
    global _facil_by_handle
    if _facil_by_handle is not None:
        return
    _facil_by_handle = {}
    try:
        for og, members in sheets.load_facil_members().items():
            for m in members:
                if m.get("handle"):
                    _facil_by_handle[m["handle"].lower()] = og
    except Exception as exc:
        log.warning("couldn't load facil roster: %s", exc)


def _og_by_facil_handle(username):
    _load_facil_map()
    return _facil_by_handle.get((username or "").lower())


async def _group_link(bot, og):
    if og in _link_cache:
        return _link_cache[og]
    entry = manifest.load().get(f"StartNOW! {og}")
    if not entry or not entry.get("chat_id"):
        return None
    link = entry.get("invite_link")
    if not link:
        try:
            link = (await bot.create_chat_invite_link(entry["chat_id"])).invite_link
        except Exception as exc:
            log.warning("couldn't make an invite link for %s: %s", og, exc)
            return None
    _link_cache[og] = link
    return link


def _joined(og, link):
    return f"You're in orientation group {og}! 🌟\n\nTap to join:\n{link}"


async def _deliver(update, context, og):
    """Tell a Year 1 their group, then send the link or hold them until their
    facil opens the OG."""
    uid = update.effective_user.id
    if not storage.is_og_opened(og):
        storage.add_waiting(uid, og)
        await update.effective_message.reply_text(
            f"You're in orientation group {og}! 🌟\n\nYou're on the list — your "
            "facil will let you in shortly, and I'll send your join link the "
            "moment they do."
        )
        return
    link = await _group_link(context.bot, og)
    if not link:
        await update.effective_message.reply_text(
            f"You're in orientation group {og}! Your facil will share the join "
            "link shortly. 🌟"
        )
        return
    await update.effective_message.reply_text(_joined(og, link))
    storage.mark_link_sent(uid, og)
    storage.remove_waiting(uid)


async def _send_facil_link(update, context, og):
    """Facils aren't held like Year 1s — send their group link right away."""
    link = await _group_link(context.bot, og)
    if not link:
        await update.effective_message.reply_text(
            f"Welcome, facil! Your {og} group isn't quite ready yet — I'll have "
            "your join link shortly. 🌟"
        )
        return
    await update.effective_message.reply_text(
        f"Welcome, facil! Here's your {og} group — tap to join:\n{link}"
    )
    storage.mark_link_sent(update.effective_user.id, og)


async def try_send_group_link(update, context):
    """Handle /start in a DM. Returns True if we handled it."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return False

    # facil deep link (facil-<OG>) -> their link right away
    if context.args:
        m = _PAYLOAD_RE.match(context.args[0].strip())
        if m and m.group(1):
            await _send_facil_link(update, context, m.group(2).upper() + m.group(3))
            return True

    # facil matched by @username -> their link right away (facils aren't held)
    og = _og_by_facil_handle(update.effective_user.username)
    if og:
        await _send_facil_link(update, context, og)
        return True

    # Year 1 matched by @username
    og = _og_by_handle(update.effective_user.username)
    if og:
        await _deliver(update, context, og)
        return True

    # couldn't match the handle -> ask for the sign-up email
    context.user_data["awaiting_email"] = True
    await update.effective_message.reply_text(
        "Welcome to StartNOW! 2026 🌟\n\nI couldn't find you by your Telegram "
        "handle. Please reply with the email you used to sign up, and I'll find "
        "your group."
    )
    return True


async def on_text(update, context):
    """A plain DM — used to collect the sign-up email once we've asked for it."""
    if not context.user_data.get("awaiting_email"):
        return
    og = _og_by_email(update.effective_message.text)
    if not og:
        await update.effective_message.reply_text(
            "Hmm, I couldn't find that email in our sign-up list 😕\n\nPlease "
            "double-check and reply with the exact email you registered with."
        )
        return  # keep waiting for a valid one
    context.user_data["awaiting_email"] = False
    await _deliver(update, context, og)


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

    sent = 0
    for uid in storage.waiting_for_og(og):
        try:
            await context.bot.send_message(uid, _joined(og, link))
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
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
