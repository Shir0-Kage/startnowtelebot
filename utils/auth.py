"""Facilitator/admin checks.

A user counts as a facilitator if either:
  - their Telegram user id is in the FACILITATOR_IDS list, or
  - they're an admin/owner of the current group chat.

Use the @facil_only decorator on command handlers that should be restricted.
"""

from functools import wraps

from telegram.constants import ChatMemberStatus

import config

FACIL_ONLY_MESSAGE = "Sorry, that command is for facilitators only 🙏"


async def is_facilitator(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if user is None:
        return False

    if user.id in config.FACILITATORS:
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
