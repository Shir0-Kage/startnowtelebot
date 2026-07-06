"""Facilitator/admin checks.

A user counts as a facilitator if any of:
  - their Telegram user id is in the FACILITATOR_IDS list,
  - their Telegram @username is one of the facils in the roster sheet, or
  - they're an admin/owner of the current group chat.

The @username check means facils can run facil commands whether or not they've
been promoted to group admin. Use @facil_only on restricted command handlers.
"""

from functools import wraps

from telegram.constants import ChatMemberStatus

import config
from setup import sheets

FACIL_ONLY_MESSAGE = "Sorry, that command is for facilitators only 🙏"

_facil_handles = None   # cached set of facil @usernames (lowercased, no @)


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

    # recognised by their @username in the facil roster — so a facil can run
    # facil commands even if they were never made a group admin
    if sheets.normalize_handle(user.username or "") in _facil_handles_set():
        return True

    # in a group, fall back to Telegram's own admin list
    if chat is not None and chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(user.id)
            return member.status in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            )
        except Exception:
            # e.g. bot can't fetch members — treat as not a facil
            return False

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
