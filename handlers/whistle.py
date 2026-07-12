"""Anonymous whistleblowing. Anyone DMs  /whistle <text>  and the bot relays the
report privately to each organiser in config.WHISTLE_RECIPIENTS — WITHOUT ever
revealing or logging who sent it. /undo_whistle deletes a reporter's last report
from those DMs (best-effort, right after sending).

Anonymity: the sender's id/username/name is never logged, stored, or included in
the relayed message. Recipients only see the report text, sent by the bot."""

import logging

from telegram.ext import CommandHandler

import config
import storage

log = logging.getLogger(__name__)

_REPORT_PREFIX = "🔔 Anonymous report:\n\n"


def _recipient_ids():
    """user_ids of the configured recipients we can actually reach (they must
    have /started the bot). Deduped; order irrelevant."""
    ids = []
    for handle in config.WHISTLE_RECIPIENTS:
        uid = storage.user_id_for_handle(handle)
        if uid is not None and uid not in ids:
            ids.append(uid)
    return ids


async def whistle(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me privately so no one sees you reporting 🙏")
        return
    # everything after the command word; split(maxsplit=1) keeps newlines in the body
    parts = (update.effective_message.text or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.effective_message.reply_text(
            "Send it like:  /whistle <your message>")
        return

    delivered = []
    for uid in _recipient_ids():
        try:
            sent = await context.bot.send_message(
                chat_id=uid, text=_REPORT_PREFIX + text)
        except Exception as exc:
            # NOTE: never log the sender — anonymity. Only the failure reason.
            log.warning("couldn't deliver anonymous whistle to a recipient: %s", exc)
            continue
        delivered.append({"chat_id": uid, "message_id": sent.message_id})

    if not delivered:
        await update.effective_message.reply_text(
            "I couldn't reach any organiser right now — please tell one directly. "
            "(They may need to open a chat with me first.)")
        return

    # Remember only the delivered message ids (never the sender) in PTB's
    # in-memory user_data so this reporter can undo. No persistence backend is
    # configured, so it never touches disk, our DB, or the logs, and it clears
    # on restart.
    if context.user_data is not None:
        context.user_data["last_whistle"] = delivered
    await update.effective_message.reply_text(
        "Sent anonymously ✅\n\nChanged your mind? Send /undo_whistle to take it back.")


async def undo_whistle(update, context):
    """Delete the sender's most recent whistle from the organisers' DMs. Works
    only right after sending (the undo handle is in-memory and cleared on
    restart). No sender identity is ever stored or logged."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me privately to undo a report 🙏")
        return
    delivered = (context.user_data or {}).get("last_whistle")
    if not delivered:
        await update.effective_message.reply_text(
            "Nothing to undo — I don't have a recent report from you to remove. "
            "(Undo only works right after sending, and not once I've restarted.)")
        return
    removed = 0
    for item in delivered:
        try:
            await context.bot.delete_message(
                chat_id=item["chat_id"], message_id=item["message_id"])
            removed += 1
        except Exception as exc:
            # never log the sender — only the failure reason.
            log.warning("couldn't undo a whistle copy: %s", exc)
    context.user_data.pop("last_whistle", None)
    if removed:
        await update.effective_message.reply_text("Your last report has been removed ✅")
    else:
        await update.effective_message.reply_text(
            "Couldn't remove it — it may already be gone.")


def register(app):
    app.add_handler(CommandHandler("whistle", whistle))
    app.add_handler(CommandHandler("undo_whistle", undo_whistle))
