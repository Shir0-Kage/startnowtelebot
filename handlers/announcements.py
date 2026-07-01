"""Facilitator announcements: /announce, /remind, /pinannounce."""

import html

from telegram.ext import CommandHandler

from utils.auth import facil_only
from utils.text import chunk_text

ANNOUNCE_HEADER = "📣 <b>Group Announcement</b>"
ANNOUNCE_FOOTER = "Please check this chat for any updates. See y'all there ❤️"
REMIND_HEADER = "⏰ <b>Quick Reminder</b>"


def _message_arg(update, context):
    """Everything after the command, as one string. None if empty."""
    text = update.effective_message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    return parts[1].strip()


async def _send_chunks(message, full_text):
    """Send a (possibly long) HTML message in Telegram-safe pieces. Returns the
    first sent message so callers can pin it."""
    first = None
    for piece in chunk_text(full_text):
        sent = await message.reply_html(piece)
        if first is None:
            first = sent
    return first


@facil_only
async def announce_command(update, context):
    body = _message_arg(update, context)
    if not body:
        await update.effective_message.reply_text(
            "Give me something to announce, e.g.\n"
            "/announce Meet Up 1 is on tomorrow at 10am!"
        )
        return

    text = f"{ANNOUNCE_HEADER}\n\n{html.escape(body)}\n\n{ANNOUNCE_FOOTER}"
    await _send_chunks(update.effective_message, text)


@facil_only
async def remind_command(update, context):
    body = _message_arg(update, context)
    if not body:
        await update.effective_message.reply_text(
            "Give me something to remind about, e.g.\n"
            "/remind React with 👍 if you're coming for the dry run."
        )
        return

    text = f"{REMIND_HEADER}\n\n{html.escape(body)}"
    await _send_chunks(update.effective_message, text)


@facil_only
async def pinannounce_command(update, context):
    body = _message_arg(update, context)
    if not body:
        await update.effective_message.reply_text(
            "Give me something to announce and pin, e.g.\n"
            "/pinannounce Meet Up 1 is on tomorrow at 10am!"
        )
        return

    text = f"{ANNOUNCE_HEADER}\n\n{html.escape(body)}\n\n{ANNOUNCE_FOOTER}"
    sent = await _send_chunks(update.effective_message, text)

    # try to pin; the bot needs "pin messages" permission for this to work
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception:
        await update.effective_message.reply_text(
            "Posted it, but I couldn't pin — make sure I'm an admin with "
            "permission to pin messages 🙂"
        )


def register(app):
    app.add_handler(CommandHandler("announce", announce_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("pinannounce", pinannounce_command))
