"""Bot-side onboarding: tell people their group and DM their join link.

The bot can't add anyone, but it CAN message users who message it. On /start it
identifies the person and either sends their group's join link or — for Year 1s
before their facil opens the OG — puts them on a waiting list. It figures out
their group from a facil deep link (facil-<OG>), their @username, or, if the
handle doesn't match, the sign-up email they type in.
"""

import asyncio
import logging
import re

from telegram.ext import CommandHandler, MessageHandler, filters

import storage
from setup import manifest, sheets
from utils.auth import facil_only, is_admin

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


# The roster loaders below hit Google Sheets over BLOCKING urllib (incl. DNS,
# which the socket timeout doesn't cover). They must therefore NEVER run on the
# asyncio event loop — a slow or stalled fetch would freeze the whole bot and
# even swallow Ctrl+C. So: loaders are plain sync functions run off the loop via
# `ensure_rosters_loaded()` (asyncio.to_thread), and the `_og_by_*` accessors are
# pure cache reads that never fetch. Loaders build into locals and assign the
# module cache only on success, so a partial/concurrent load can't be observed
# and a transient failure just retries on the next call.


def _load_year1_maps():
    global _year1_by_handle, _year1_by_email
    if _year1_by_handle is not None:
        return
    try:
        by_handle, by_email = {}, {}
        for og, members in sheets.load_year1_members().items():
            for m in members:
                if m.get("handle"):
                    by_handle[m["handle"].lower()] = og
                if m.get("email"):
                    by_email[m["email"].strip().lower()] = og
        _year1_by_handle, _year1_by_email = by_handle, by_email
    except Exception as exc:
        log.warning("couldn't load Year 1 roster: %s", exc)  # leave unset -> retry


def _load_facil_map():
    global _facil_by_handle
    if _facil_by_handle is not None:
        return
    try:
        by_handle = {}
        for og, members in sheets.load_facil_members().items():
            for m in members:
                if m.get("handle"):
                    by_handle[m["handle"].lower()] = og
        _facil_by_handle = by_handle
    except Exception as exc:
        log.warning("couldn't load facil roster: %s", exc)  # leave unset -> retry


def _reload_year1_maps():
    """Drop the cached Year 1 roster and re-read it — used after new Year 1s are
    added to the sheet mid-programme. Sync (blocking); call off the event loop."""
    global _year1_by_handle, _year1_by_email
    _year1_by_handle = None
    _year1_by_email = None
    _load_year1_maps()


async def ensure_rosters_loaded():
    """Load the Year 1 + facil rosters OFF the event loop, so a blocking Google
    fetch never freezes the bot. Cached after the first success; a failure just
    leaves the cache unset to retry. Cheap once warm. Call before any _og_by_*
    lookup, and once at startup to pre-warm."""
    if _facil_by_handle is None:
        await asyncio.to_thread(_load_facil_map)
    if _year1_by_handle is None:
        await asyncio.to_thread(_load_year1_maps)


def _og_by_handle(username):
    """Pure cache read (no fetch). Returns None if the roster isn't loaded yet —
    callers await ensure_rosters_loaded() first."""
    return (_year1_by_handle or {}).get((username or "").lower())


def _og_by_email(email):
    return (_year1_by_email or {}).get((email or "").strip().lower())


def _og_by_facil_handle(username):
    return (_facil_by_handle or {}).get((username or "").lower())


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

    await ensure_rosters_loaded()  # off the event loop; the lookups below are pure reads

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
    await ensure_rosters_loaded()  # off the event loop
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


@facil_only
async def sync_year_ones(update, context):
    """Re-read the roster and onboard anyone who has already /started but isn't
    placed yet — e.g. Year 1s added to the sheet after the programme began. For
    an OG that's already been opened we DM their join link now; for one that
    hasn't, we hold them for their facil. People who never /started the bot can't
    be reached and are skipped (they'll be onboarded the moment they /start)."""
    await asyncio.to_thread(_reload_year1_maps)  # off the event loop
    sent = held = 0
    for su in storage.get_started():
        uid = su["user_id"]
        if storage.link_sent_to(uid):
            continue  # already has their link
        og = _og_by_handle(su.get("username"))
        if not og:
            continue  # not a Year 1 we can match by @username
        if storage.is_og_opened(og):
            link = await _group_link(context.bot, og)
            if not link:
                continue
            try:
                await context.bot.send_message(uid, _joined(og, link))
                storage.mark_link_sent(uid, og)
                storage.remove_waiting(uid)
                sent += 1
            except Exception as exc:
                log.warning("couldn't DM %s: %s", uid, exc)
        else:
            storage.add_waiting(uid, og)
            held += 1

    # Check layer: surface handles the bot CAN'T match by @username — blank or
    # malformed (can only get in via the email fallback), and ones we auto-cleaned
    # (e.g. spaces removed) so a facil can confirm they're the person's real handle.
    flagged = []
    try:
        for og, members in (await asyncio.to_thread(sheets.load_year1_members)).items():
            for m in members:
                name = m.get("name") or "?"
                raw = (m.get("raw_handle") or "").strip()
                clean = m.get("handle")
                if clean is None:
                    flagged.append((og, f"{name} — '{raw or '(blank)'}' can't be matched; "
                                        "fix the handle or they join via email"))
                elif re.search(r"\s", raw):
                    flagged.append((og, f"{name} — '{raw}' → @{clean}; verify it's their real @username"))
    except Exception as exc:
        log.warning("couldn't audit Year 1 handles: %s", exc)

    msg = (
        f"Synced from the sheet 🌟\nDM'd {sent} Year 1(s) their join link; "
        f"holding {held} until their facil opens the group. Anyone who hasn't "
        "messaged me yet gets theirs the moment they /start."
    )
    if flagged:
        flagged.sort()
        shown = "\n".join(f"• {og}: {note}" for og, note in flagged[:25])
        msg += f"\n\n⚠️ {len(flagged)} handle(s) need a look:\n{shown}"
        if len(flagged) > 25:
            msg += f"\n…and {len(flagged) - 25} more."
    await update.effective_message.reply_text(msg)


def _facil_ogs(update):
    """OG(s) a (non-admin) facil may inspect — their own, from the facil roster."""
    user = update.effective_user
    og = _og_by_facil_handle(user.username) if user is not None else None
    return {og} if og else set()


@facil_only
async def roster_status(update, context):
    """DM-ONLY (student privacy): show, per Year 1 in an OG, whether they've reached
    the bot and been placed. Usage: /roster_status PM1 (in a private chat with the
    bot). Admins may check any OG; a facil may only check their own group."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "For students' privacy I only share this in a private chat — DM me "
            "/roster_status <OG> (e.g. /roster_status PM1) instead 🙏"
        )
        return

    await ensure_rosters_loaded()  # off the loop; needed for the facil-OG check
    og = sheets.og_code(context.args[0]) if context.args else None
    if not og:
        await update.effective_message.reply_text(
            "Usage (DM me): /roster_status <OG> — e.g. /roster_status PM1"
        )
        return

    # admins see any OG; facils are scoped to their own group
    if not is_admin(update.effective_user):
        allowed = _facil_ogs(update)
        if og not in allowed:
            own = ", ".join(sorted(allowed)) if allowed else "your own group"
            await update.effective_message.reply_text(
                f"You can only check {own}. Ask an admin for other OGs 🙏"
            )
            return

    members = (await asyncio.to_thread(sheets.load_year1_members)).get(og, [])
    if not members:
        await update.effective_message.reply_text(f"No Year 1s are listed for {og} in the sheet.")
        return

    # started @usernames (normalized) -> user_id, so we can match a sheet handle
    # to a real person who has messaged the bot
    started = {}
    for su in storage.get_started():
        h = sheets.normalize_handle(su.get("username") or "")
        if h:
            started[h] = su["user_id"]

    in_group = waiting = missing = bad = 0
    rows = []
    for m in members:
        name = m.get("name") or "?"
        handle = m.get("handle")
        email = (m.get("email") or "?").lower()
        if not handle:
            bad += 1
            rows.append(f"⚠️ {name} — unusable handle '{m.get('raw_handle') or '(blank)'}' — email: {email}")
            continue
        uid = started.get(handle)
        if uid is None:
            missing += 1
            rows.append(f"❌ {name} (@{handle}) — hasn't /started, or their real @username "
                        f"differs from the sheet — email: {email}")
        elif storage.link_sent_to(uid):
            in_group += 1
            rows.append(f"✅ {name} (@{handle}) — in the group")
        else:
            waiting += 1
            rows.append(f"⏳ {name} (@{handle}) — /started, waiting for the OG to open")

    head = (f"{og} — {len(members)} in the sheet: {in_group} in group, "
            f"{waiting} waiting, {missing} not reachable"
            + (f", {bad} bad handle" if bad else ""))
    await update.effective_message.reply_text(head + "\n\n" + "\n".join(rows))


def register(app):
    app.add_handler(CommandHandler("add_year_ones", add_year_ones))
    app.add_handler(CommandHandler("sync_year_ones", sync_year_ones))
    app.add_handler(CommandHandler("roster_status", roster_status))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
