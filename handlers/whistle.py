"""Anonymous whistleblowing. An admin opens a thread (a base post in the linked
channel); anyone DMs /whistle <text> and the bot posts it as a comment under that
post (a reply in the channel's discussion group) WITHOUT ever revealing or logging
the sender. The bot learns the channel id directly from any post in the channel
(it is a channel admin), and the discussion-group id from the auto-forwarded copy
of a post (it is an admin in the discussion group too)."""

import logging
import re

from telegram.ext import CommandHandler, MessageHandler, filters

import storage
from utils.auth import is_admin

log = logging.getLogger(__name__)

_BASE_TEXT = ("🔔 Anonymous whistleblowing is open.\n\n"
             "DM me  /whistle <your message>  and it'll appear here anonymously — "
             "your name is never shown or logged.")


async def on_channel_post(update, context):
    """A post in the channel itself (the bot is a channel admin) — learn the
    channel id directly so /start_whistle can open a thread without waiting for
    an auto-forward. Includes the base message the bot posts (harmless re-store)."""
    chat = update.effective_chat
    if chat is None:
        return
    storage.set_whistle_channel(chat.id)


async def on_channel_autoforward(update, context):
    """A channel post auto-copied into the linked discussion group: learn the
    channel + group ids, and resolve a pending base-post anchor if this is it."""
    msg = update.effective_message
    if msg is None or not getattr(msg, "is_automatic_forward", False):
        return
    origin = msg.forward_from_chat or msg.sender_chat
    if origin is None:
        return
    storage.set_whistle_link(origin.id, msg.chat.id)
    if msg.forward_from_message_id is not None:
        # remember this post's group copy so an admin can later adopt it as the
        # base via its link (/set_whistle_base), and resolve a pending anchor.
        storage.remember_forward(
            msg.forward_from_message_id, msg.chat.id, msg.message_id)
        storage.resolve_whistle_anchor(msg.forward_from_message_id, msg.message_id)


async def start_whistle(update, context):
    if not is_admin(update.effective_user):
        await update.effective_message.reply_text(
            "Only an admin can open a whistle thread.")
        return
    channel_id, _group = storage.get_whistle_link()
    if channel_id is None:
        await update.effective_message.reply_text(
            "I'm not linked to the whistle channel yet — post anything in the "
            "channel once so I can find it, then run /start_whistle again.")
        return
    try:
        post = await context.bot.send_message(chat_id=channel_id, text=_BASE_TEXT)
    except Exception as exc:
        log.warning("couldn't post whistle base message: %s", exc)
        await update.effective_message.reply_text(
            "Couldn't post to the channel — make sure I'm still an admin there.")
        return
    storage.set_whistle_pending(post.message_id)
    await update.effective_message.reply_text(
        "Whistle thread posted 🔔 — anonymous reports will appear as comments under it.")


def _base_msg_id(text):
    """The channel message id from a t.me link (or a bare number) — the last
    run of digits, e.g. https://t.me/c/4292606016/29 -> 29. None if none."""
    nums = re.findall(r"\d+", text or "")
    return int(nums[-1]) if nums else None


async def set_whistle_base(update, context):
    """Admin adopts an existing channel post (by its link) as the whistle base.
    The post must already have auto-forwarded into the discussion group so the
    bot knows its group-side copy to reply under."""
    if not is_admin(update.effective_user):
        await update.effective_message.reply_text(
            "Only an admin can set the whistle base message.")
        return
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me the base message link:  /set_whistle_base <link>")
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    msg_id = _base_msg_id(parts[1]) if len(parts) > 1 else None
    if msg_id is None:
        await update.effective_message.reply_text(
            "Send it like:  /set_whistle_base https://t.me/c/.../<id>")
        return
    group_id, group_msg_id = storage.lookup_forward(msg_id)
    if group_id is None:
        await update.effective_message.reply_text(
            "I haven't seen that post arrive in the discussion group yet. Post the "
            "base message while I'm running (and make sure I'm an admin in the "
            "linked discussion group), then send its link again.")
        return
    storage.set_whistle_anchor(group_id, group_msg_id)
    await update.effective_message.reply_text(
        "Base message set 🔔 — anonymous reports will now appear as comments under it.")


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
    group_id, anchor = storage.get_whistle_anchor()
    if group_id is None or anchor is None:
        await update.effective_message.reply_text(
            "No whistle thread is open right now — ask an admin to run /start_whistle.")
        return
    try:
        posted = await context.bot.send_message(
            chat_id=group_id,
            text="🔔 Anonymous report:\n\n" + text,
            reply_to_message_id=anchor)
    except Exception as exc:
        # NOTE: never log the sender — anonymity. Only the failure reason.
        log.warning("couldn't post anonymous whistle: %s", exc)
        await update.effective_message.reply_text(
            "Something went wrong sending that — please try again in a moment.")
        return
    # Remember just enough for THIS sender to undo THEIR last report. This lives
    # only in PTB's in-memory user_data — no persistence backend is configured,
    # so it is never written to disk, our DB, or the logs, and it disappears on
    # restart. We store only message ids, never the sender's identity, so
    # anonymity toward the channel and the logs is unchanged.
    if context.user_data is not None:
        context.user_data["last_whistle"] = {
            "chat_id": group_id, "message_id": posted.message_id}
    await update.effective_message.reply_text(
        "Sent anonymously ✅\n\nChanged your mind? Send /undo_whistle to take it back.")


async def undo_whistle(update, context):
    """Delete the sender's most recent whistle. Works only right after sending
    (the undo handle is in-memory and cleared on restart), and only for the
    person's own last report — no sender identity is ever stored or logged."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me privately to undo a report 🙏")
        return
    last = (context.user_data or {}).get("last_whistle")
    if not last:
        await update.effective_message.reply_text(
            "Nothing to undo — I don't have a recent report from you to remove. "
            "(Undo only works right after sending, and not once I've restarted.)")
        return
    try:
        await context.bot.delete_message(
            chat_id=last["chat_id"], message_id=last["message_id"])
    except Exception as exc:
        # never log the sender — only the failure reason.
        log.warning("couldn't undo whistle: %s", exc)
        await update.effective_message.reply_text(
            "Couldn't remove it — it may already be gone. If not, try again shortly.")
        return
    context.user_data.pop("last_whistle", None)
    await update.effective_message.reply_text("Your last report has been removed ✅")


def register(app):
    app.add_handler(CommandHandler("start_whistle", start_whistle))
    app.add_handler(CommandHandler("set_whistle_base", set_whistle_base))
    app.add_handler(CommandHandler("whistle", whistle))
    app.add_handler(CommandHandler("undo_whistle", undo_whistle))
    # learn the channel id from posts in the channel itself; and the group id +
    # anchor from the auto-forwarded copy in the discussion group. Distinct update
    # types (channel post vs. supergroup message) so they never collide. group=1
    # so neither shadows the other group handlers.
    app.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, on_channel_post), group=1)
    app.add_handler(
        MessageHandler(filters.IS_AUTOMATIC_FORWARD, on_channel_autoforward),
        group=1)
