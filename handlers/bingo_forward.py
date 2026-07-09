"""Forward round: read a forwarded filled card's ORIGINAL send time, OCR it,
queue it, and DM the submitter a confirmation — the forward-round analogue of
handlers/bingo_queue.py's submitter-confirm step, but keyed off a forwarded
message's forward_origin.date instead of a live submission.

Mirrors bingo_queue._send_confirmation (evaluate -> short line + Confirm
button if fully recognised, else the full fill-in template + a /start flag
for any matched-but-unreachable handle), with its OWN _PENDING_READ and its
own 'bingofwd:confirm:<id>' keyboard callback -- entirely separate from
bingo_queue's in-flight queue/confirm state.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bingo_text
import storage
from handlers import bingo_queue
from setup import sheets

log = logging.getLogger(__name__)

# submission_id -> {"read": <cells dict>, "handle": str, "sheet_no": int}
# This module's own pending-read cache -- separate from bingo_queue's, since
# forward-round submissions live in their own status lane ('fwd_confirming').
_PENDING_READ = {}


def _confirm_keyboard(submission_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm",
                             callback_data=f"bingofwd:confirm:{submission_id}")
    ]])


async def _send_confirmation(context, submission_id, uid, sheet_no):
    """DM the submitter their confirmation message: short (winning line + a
    Confirm button) when fully recognised, else the full fill-in template with
    a /start flag for any matched-but-unreachable handle."""
    pending = _PENDING_READ.get(submission_id)
    if pending is None:
        log.warning("no pending read for forward submission %s; can't send "
                    "confirmation", submission_id)
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="Please forward your filled bingo card again so I can "
                     "check it. 🔁",
            )
        except Exception:
            pass
        return
    res = bingo_queue.evaluate(pending["read"], pending["handle"], sheet_no)
    if res["fully_recognised"]:
        text = ("Got it! 🎉 Here's your winning line — tap Confirm if it's "
                "right:\n\n"
                + bingo_text.build_line_confirm_text(sheet_no, res["line"]))
        await context.bot.send_message(
            chat_id=uid, text=text,
            reply_markup=_confirm_keyboard(submission_id))
        return
    preview = bingo_text.build_prefilled_text(
        sheet_no, pending["read"].get("cells", []))
    flag = ""
    if res["unreachable"]:
        who = ", ".join(f"@{h}" for h in res["unreachable"])
        flag = (f"\n\n⚠️ {who} hasn't started the bot yet — ask them to send it "
                "/start so I can verify them, then resend your list.")
    await context.bot.send_message(
        chat_id=uid,
        text="Got your card! Fill in the @handles below (fix any blanks) and "
             "send the whole list back to me:\n\n" + preview + flag,
    )


async def _download_image(update, context):
    from handlers import bingo   # lazy: avoid import cycle
    return await bingo._download_image(update, context)


async def on_forwarded_card(update, context):
    """Only acted on in a private chat, while the forward round is collecting,
    and when the message actually carries a photo/document-image."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return
    if storage.forward_phase() != "collecting":
        return
    message = update.effective_message
    if not (message.photo or message.document):
        return

    from handlers import bingo   # lazy: avoid import cycle

    user = update.effective_user
    uid = user.id
    handle = sheets.normalize_handle(user.username) or ""

    sheet_no = storage.get_bingo_sheet(uid)
    if sheet_no is None:
        await message.reply_text("Grab your card first with /get_bingo 🙂")
        return

    forward_origin = message.forward_origin
    original = forward_origin.date if forward_origin else message.date

    image_bytes = await _download_image(update, context)
    if not image_bytes:
        await message.reply_text(
            "I couldn't read that — send it as a photo or an image file 📸"
        )
        return

    read = await bingo._run_ocr(sheet_no, image_bytes)
    if read is None:
        await message.reply_text(
            "I couldn't finish scanning that in time — please try again in a "
            "minute 🙏"
        )
        return

    sid = storage.queue_forwarded_submission(
        uid, handle, sheet_no, original.isoformat())
    _PENDING_READ[sid] = {"read": read, "handle": handle, "sheet_no": sheet_no}

    if forward_origin is None:
        await message.reply_text(
            "Couldn't tell this was forwarded, so I used the time you sent it "
            "instead of the original send time. ℹ️"
        )

    await _send_confirmation(context, sid, uid, sheet_no)
