"""Facilitator/admin checks.

A user counts as a facilitator if any of:
  - their Telegram user id is in the FACILITATOR_IDS list,
  - their @username is in config.FACILITATOR_HANDLES (e.g. @zzehao),
  - their Telegram @username is one of the facils in the roster sheet,
  - they're an admin/owner of the current group chat, or
  - they're an admin/owner of any group chat we manage (so a group admin
    counts everywhere, including DMs).

The @username checks mean facils can run facil commands whether or not they've
been promoted to group admin. Use @facil_only on restricted command handlers.
"""

import asyncio
import time
from functools import wraps

from telegram.constants import ChatMemberStatus

import config
from setup import manifest, sheets

FACIL_ONLY_MESSAGE = "Sorry, that command is for facilitators only 🙏"

_ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

_facil_handles = None   # cached set of facil @usernames (lowercased, no @)

# Admins across every group chat we manage, so a group admin is recognised as a
# facilitator anywhere — not just inside their own group. Refreshed lazily since
# it costs one API call per chat.
_managed_admins = {"ids": set(), "handles": set(), "at": 0.0}
_MANAGED_ADMINS_TTL = 300  # seconds


async def _managed_admin_sets(bot):
    """(ids, handles) of admins/owners across all managed OG chats, cached."""
    now = time.monotonic()
    if _managed_admins["at"] and now - _managed_admins["at"] < _MANAGED_ADMINS_TTL:
        return _managed_admins["ids"], _managed_admins["handles"]

    chat_ids = [e.get("chat_id") for e in manifest.load().values() if e.get("chat_id")]

    async def admins_of(chat_id):
        try:
            return await bot.get_chat_administrators(chat_id)
        except Exception:
            return []  # bot not in the chat, lost admin, etc. — just skip it

    ids, handles = set(), set()
    for members in await asyncio.gather(*(admins_of(c) for c in chat_ids)):
        for m in members:
            ids.add(m.user.id)
            h = (m.user.username or "").lower()
            if h:
                handles.add(h)
    _managed_admins.update(ids=ids, handles=handles, at=now)
    return ids, handles


def _facil_handles_set():
    """Known facil @usernames from the roster sheet (cached). Returns an empty
    set — and retries on the next call — if the sheet can't be reached, so a
    transient failure just falls back to the id/admin checks."""
    global _facil_handles
    if _facil_handles is not None:
        return _facil_handles
    try:
        handles = set()
        for members in sheets.load_facil_members().values():
            for m in members:
                h = sheets.normalize_handle(m.get("handle") or "")
                if h:
                    handles.add(h)
        _facil_handles = handles
        return _facil_handles
    except Exception:
        return set()


async def is_facilitator(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if user is None:
        return False

    if user.id in config.FACILITATORS:
        return True

    handle = (user.username or "").lstrip("@").lower()
    if handle and handle in config.FACILITATOR_HANDLES:
        return True

    # recognised by their @username in the facil roster — so a facil can run
    # facil commands even if they were never made a group admin. The (cached)
    # roster load hits the network, so run it off the event loop.
    facil_handles = await asyncio.to_thread(_facil_handles_set)
    if sheets.normalize_handle(user.username or "") in facil_handles:
        return True

    # admin/owner of the current group — cheapest way to catch a group admin
    if chat is not None and chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(user.id)
            if member.status in _ADMIN_STATUSES:
                return True
        except Exception:
            pass  # bot can't fetch members — fall through to the wider check

    # admin/owner of ANY managed group chat — so a group admin counts as a
    # facilitator everywhere, including DMs
    bot = getattr(context, "bot", None)
    if bot is not None:
        ids, handles = await _managed_admin_sets(bot)
        if user.id in ids or (handle and handle in handles):
            return True

    return False


def facil_only(handler):
    """Decorator: block the wrapped command unless the caller is a facil."""

    @wraps(handler)
    async def wrapper(update, context, *args, **kwargs):
        if not await is_facilitator(update, context):
            if update.effective_message:
                await update.effective_message.reply_text(FACIL_ONLY_MESSAGE)
            return
        return await handler(update, context, *args, **kwargs)

    return wrapper
