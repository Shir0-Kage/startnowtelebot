"""Facilitator announcements: /announce (DM-only, broadcasts verbatim to every
group), /edit_announce (edit messages by link), /remind, /pinannounce."""

import html
import logging
import re

from telegram.ext import CommandHandler

import storage
from utils.auth import facil_only
from utils.text import chunk_text

log = logging.getLogger(__name__)

# Telegram message links. Private groups/channels: t.me/c/<internal>/<msg> (or
# .../<internal>/<thread>/<msg>); public: t.me/<username>/<msg>. We turn each into
# a (chat_id, message_id) the Bot API can edit.
_MSG_LINK_RE = re.compile(
    r"t\.me/(?:c/(\d+)(?:/\d+)*/(\d+)|([A-Za-z]\w{3,})/(\d+))")


def _parse_message_link(match):
    """A regex match from _MSG_LINK_RE -> (chat_id, message_id)."""
    if match.group(1):                              # private: -100 + internal id
        return int("-100" + match.group(1)), int(match.group(2))
    return "@" + match.group(3), int(match.group(4))  # public: @username

# /announce broadcasts to every group, so it's locked to the lead organiser only.
ANNOUNCER_HANDLE = "zzehao"

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


async def announce_command(update, context):
    """@zzehao-only, DM-only. Send the message verbatim (word for word, no
    header/footer) to every group the bot is in."""
    user = update.effective_user
    handle = (user.username or "").lstrip("@").lower() if user else ""
    if handle != ANNOUNCER_HANDLE:
        await update.effective_message.reply_text(
            f"Only @{ANNOUNCER_HANDLE} can use /announce.")
        return
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        await update.effective_message.reply_text(
            "DM me /announce <message> and I'll send it to every group.")
        return

    body = _message_arg(update, context)
    if not body:
        await update.effective_message.reply_text(
            "DM me the announcement to broadcast, e.g.\n"
            "/announce Meet Up 1 is on tomorrow at 10am!"
        )
        return

    groups = storage.all_groups()
    if not groups:
        await update.effective_message.reply_text(
            "I'm not in any groups yet, so there's nothing to announce to.")
        return

    sent = failed = 0
    for g in groups:
        try:
            # verbatim: plain text, no parse_mode, so it goes out exactly as typed
            await context.bot.send_message(chat_id=g["chat_id"], text=body)
            sent += 1
        except Exception:
            failed += 1                            # removed from that group, etc.

    summary = f"📣 Announced to {sent} group(s)."
    if failed:
        summary += f" Couldn't reach {failed} (I may have been removed there)."
    await update.effective_message.reply_text(summary)


async def edit_announce_command(update, context):
    """@zzehao-only. Rewrite one or more already-sent messages by their links.
    Format: paste the message link(s), then the new text after the last link —
    e.g.

        /edit_announce
        https://t.me/c/4292606016/29
        https://t.me/c/1802003400/54
        Updated announcement text (can span many lines)

    Every listed message is set to the same new text. The bot can only edit its
    OWN messages, and must still be in each chat."""
    user = update.effective_user
    handle = (user.username or "").lstrip("@").lower() if user else ""
    if handle != ANNOUNCER_HANDLE:
        await update.effective_message.reply_text(
            f"Only @{ANNOUNCER_HANDLE} can use /edit_announce.")
        return

    text = update.effective_message.text or ""
    matches = list(_MSG_LINK_RE.finditer(text))
    if not matches:
        await update.effective_message.reply_text(
            "Send message link(s) then the new text, e.g.\n"
            "/edit_announce https://t.me/c/123/45 <new text>")
        return
    # the new body is everything after the LAST link
    new_text = text[matches[-1].end():].strip()
    if not new_text:
        await update.effective_message.reply_text(
            "I see the link(s) but no new text — put the replacement message "
            "after the last link.")
        return

    targets = [_parse_message_link(m) for m in matches]
    edited = failed = 0
    for chat_id, message_id in targets:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=new_text)
            edited += 1
        except Exception as exc:
            # not my message / unchanged text / not in chat / deleted
            log.warning("couldn't edit message %s in %s: %s",
                        message_id, chat_id, exc)
            failed += 1

    summary = f"✏️ Edited {edited} message(s)."
    if failed:
        summary += (f" Couldn't edit {failed} (must be my own message, still in "
                    "the chat, and actually changed).")
    await update.effective_message.reply_text(summary)


async def purge_dm_messages_command(update, context):
    """@zzehao-only: delete the most recent bot message from every individual DM
    the bot recorded (i.e. everyone a pre-fix /announce reached one-on-one), so a
    stray announcement is removed from people's DMs. Group chats are never
    touched. Optional count (default 1, max 5) removes that many recent bot
    messages per DM. Best-effort: it targets the latest message(s), so run it
    promptly and be aware it can catch another recent bot message if the person
    interacted after the announcement."""
    user = update.effective_user
    handle = (user.username or "").lstrip("@").lower() if user else ""
    if handle != ANNOUNCER_HANDLE:
        await update.effective_message.reply_text(
            f"Only @{ANNOUNCER_HANDLE} can use /purge_dm_messages.")
        return

    parts = (update.effective_message.text or "").split()
    count = 1
    if len(parts) > 1 and parts[1].isdigit():
        count = max(1, min(int(parts[1]), 5))

    dm_ids = storage.dm_chat_ids()
    if not dm_ids:
        await update.effective_message.reply_text(
            "No individual DM chats on record to clean up.")
        return

    swept = deleted = 0
    for cid in dm_ids:
        try:
            # a probe is the only way to learn the current latest message id in
            # a chat; we delete it again right after.
            probe = await context.bot.send_message(chat_id=cid, text="🧹")
        except Exception:
            continue                                # can't reach this DM
        swept += 1
        latest = probe.message_id
        for mid in range(latest - 1, latest - 1 - count, -1):
            if mid <= 0:
                break
            try:
                await context.bot.delete_message(chat_id=cid, message_id=mid)
                deleted += 1
            except Exception:
                pass                                # their message / gone / >48h
        try:
            await context.bot.delete_message(chat_id=cid, message_id=latest)
        except Exception:
            pass

    await update.effective_message.reply_text(
        f"🧹 Swept {swept} DM(s); deleted {deleted} recent bot message(s). "
        "Group chats were left untouched.")


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
    app.add_handler(CommandHandler("edit_announce", edit_announce_command))
    app.add_handler(CommandHandler("purge_dm_messages", purge_dm_messages_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("pinannounce", pinannounce_command))
