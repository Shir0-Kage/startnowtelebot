"""Facilitator announcements: /announce (DM-only, broadcasts verbatim — text
and/or photo — to every group), /edit_announce (edit sent messages by link,
text and/or photo), /remind, /pinannounce."""

import html
import logging
import re

from telegram import InputMediaPhoto
from telegram.ext import (ApplicationHandlerStop, CommandHandler, MessageHandler,
                          filters)

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

# Commands that can arrive as a photo caption (matched against the caption).
_ANNOUNCE_CMD_RE = re.compile(r"(?i)^/announce(@\w+)?(\s|$)")
_EDIT_CMD_RE = re.compile(r"(?i)^/edit_announce(@\w+)?(\s|$)")

# Photo albums arrive as several one-photo messages sharing a media_group_id,
# with the caption on only the first. We buffer an /announce album's photos here,
# keyed by media_group_id, then flush them as one media group after a short delay.
_ALBUM_FLUSH_SECONDS = 2.0
_pending_albums = {}   # media_group_id -> {"items": [file_id], "body": str|None, "chat_id": int}

ANNOUNCE_HEADER = "📣 <b>Group Announcement</b>"
ANNOUNCE_FOOTER = "Please check this chat for any updates. See y'all there ❤️"
REMIND_HEADER = "⏰ <b>Quick Reminder</b>"


def _message_arg(update, context):
    """Everything after the command word, as one string (reads a photo's caption
    too). None if empty."""
    msg = update.effective_message
    raw = msg.text or msg.caption or ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    return parts[1].strip()


def _photo_file_id(message):
    """The largest attached photo's file_id, or None if the message has no photo."""
    return message.photo[-1].file_id if message and message.photo else None


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
    """@zzehao-only, DM-only. Broadcast verbatim (text and/or photo(s), no
    header/footer) to every group. Attach a photo — or an album of photos — with
    the command in the caption to include images. (Album handling lives in
    _on_private_photo; this path covers text and a single photo.)"""
    user = update.effective_user
    handle = (user.username or "").lstrip("@").lower() if user else ""
    if handle != ANNOUNCER_HANDLE:
        await update.effective_message.reply_text(
            f"Only @{ANNOUNCER_HANDLE} can use /announce.")
        return
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        await update.effective_message.reply_text(
            "DM me /announce <message> (optionally with a photo) and I'll send it "
            "to every group.")
        return

    photo = _photo_file_id(update.effective_message)
    body = _message_arg(update, context)
    if not body and not photo:
        await update.effective_message.reply_text(
            "DM me the announcement to broadcast — text, a photo, or both, e.g.\n"
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
            # verbatim: no parse_mode, so text/caption go out exactly as typed
            if photo:
                await context.bot.send_photo(
                    chat_id=g["chat_id"], photo=photo, caption=body or None)
            else:
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

    Attach a photo (command in the caption) to also swap the image — this only
    works on messages the bot originally sent as a photo. Every listed message is
    set to the same new text/photo; the bot can only edit its OWN messages and
    must still be in each chat. (One image per message — a message can't hold an
    album, so extra photos are ignored here.)"""
    user = update.effective_user
    handle = (user.username or "").lstrip("@").lower() if user else ""
    if handle != ANNOUNCER_HANDLE:
        await update.effective_message.reply_text(
            f"Only @{ANNOUNCER_HANDLE} can use /edit_announce.")
        return

    message = update.effective_message
    raw = message.text or message.caption or ""
    matches = list(_MSG_LINK_RE.finditer(raw))
    if not matches:
        await update.effective_message.reply_text(
            "Send message link(s) then the new text (and/or a photo), e.g.\n"
            "/edit_announce https://t.me/c/123/45 <new text>")
        return
    # the new body is everything after the LAST link
    new_text = raw[matches[-1].end():].strip()
    photo = _photo_file_id(message)
    if not new_text and not photo:
        await update.effective_message.reply_text(
            "I see the link(s) but no new text or photo — put the replacement "
            "after the last link, or attach a photo.")
        return

    targets = [_parse_message_link(m) for m in matches]
    edited = failed = 0
    for chat_id, message_id in targets:
        try:
            if photo:
                await context.bot.edit_message_media(
                    chat_id=chat_id, message_id=message_id,
                    media=InputMediaPhoto(media=photo, caption=new_text or None))
            else:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=new_text)
            edited += 1
        except Exception as exc:
            # not my message / unchanged / not in chat / deleted / text<->photo
            log.warning("couldn't edit message %s in %s: %s",
                        message_id, chat_id, exc)
            failed += 1

    summary = f"✏️ Edited {edited} message(s)."
    if failed:
        why = (" (a photo edit only works on messages I first sent as a photo)"
               if photo else
               " (must be my own message, still in the chat, and actually changed)")
        summary += f" Couldn't edit {failed}.{why}"
    await update.effective_message.reply_text(summary)


async def _on_private_photo(update, context):
    """group=-1 router for private photos. A command in a photo caption doesn't
    trigger CommandHandler, so we dispatch it here; we also collect the extra
    photos of an /announce album. Anything that isn't ours falls through (no
    ApplicationHandlerStop) so the bingo card handler in group 0 still runs."""
    message = update.effective_message
    caption = message.caption or ""
    mgid = message.media_group_id
    file_id = _photo_file_id(message)

    # a later photo of an /announce album we're already collecting
    if mgid is not None and mgid in _pending_albums:
        if file_id:
            _pending_albums[mgid]["items"].append(file_id)
        raise ApplicationHandlerStop

    if _ANNOUNCE_CMD_RE.match(caption):
        if mgid is None:
            await announce_command(update, context)      # single photo
        else:
            await _start_announce_album(update, context, mgid, file_id)
        raise ApplicationHandlerStop

    if _EDIT_CMD_RE.match(caption):
        await edit_announce_command(update, context)     # single-image edit
        raise ApplicationHandlerStop

    return   # not an announce photo — let the bingo handler take it


async def _start_announce_album(update, context, mgid, file_id):
    """First photo of an /announce album: gate, buffer it, and schedule the flush
    that broadcasts the whole album once the rest have arrived."""
    user = update.effective_user
    handle = (user.username or "").lstrip("@").lower() if user else ""
    if handle != ANNOUNCER_HANDLE:
        await update.effective_message.reply_text(
            f"Only @{ANNOUNCER_HANDLE} can use /announce.")
        return
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        await update.effective_message.reply_text(
            "DM me /announce <message> (optionally with photos) and I'll send it "
            "to every group.")
        return

    _pending_albums[mgid] = {
        "items": [file_id] if file_id else [],
        "body": _message_arg(update, context),
        "chat_id": chat.id,
    }
    jq = getattr(context, "job_queue", None)
    if jq is not None:
        jq.run_once(_flush_album, when=_ALBUM_FLUSH_SECONDS, data=mgid,
                    name=f"announce_album:{mgid}")
    else:
        # no scheduler (shouldn't happen in prod) — flush what we have now
        await _broadcast_album(context, _pending_albums.pop(mgid))


async def _flush_album(context):
    album = _pending_albums.pop(context.job.data, None)
    if album:
        await _broadcast_album(context, album)


async def _broadcast_album(context, album):
    """Send a collected /announce album to every group as one media group."""
    items = album["items"][:10]          # Telegram albums hold at most 10
    if not items:
        return
    groups = storage.all_groups()
    if not groups:
        await _reply_to(context, album["chat_id"],
                        "I'm not in any groups yet, so there's nothing to announce to.")
        return
    body = album["body"]
    # caption on the first item only (verbatim: no parse_mode)
    media = [InputMediaPhoto(media=fid, caption=(body if i == 0 and body else None))
             for i, fid in enumerate(items)]
    sent = failed = 0
    for g in groups:
        try:
            await context.bot.send_media_group(chat_id=g["chat_id"], media=media)
            sent += 1
        except Exception:
            failed += 1
    summary = f"📣 Announced ({len(items)} photos) to {sent} group(s)."
    if failed:
        summary += f" Couldn't reach {failed} (I may have been removed there)."
    await _reply_to(context, album["chat_id"], summary)


async def _reply_to(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        pass


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
    # A command sent as a PHOTO caption doesn't fire CommandHandler (text only),
    # and album photos after the first carry no caption at all — so route every
    # private photo through _on_private_photo. It sits in group=-1 (ahead of
    # bingo's private-photo handler in group 0) and only stops propagation for
    # our announce/edit photos, so bingo cards still reach group 0.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.PHOTO, _on_private_photo), group=-1)
