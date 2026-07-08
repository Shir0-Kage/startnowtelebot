"""Bingo submission queue: enqueue, kickoff-at-10, submitter self-confirm, and
rolling replacement, ahead of the existing tagged-people verification.

State (see the plan's STATUS MAPPING): queued -> confirming -> pending(verify)
-> verified(won) | failed. Only 'queued'/'confirming' are owned here; once a
submitter confirms a fully-recognised line, _start_verification flips the row to
'pending' and the existing handlers/bingo.py pipeline takes over unchanged.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bingo_lines as lines
import bingo_text
import config
import storage

log = logging.getLogger(__name__)

# submission_id -> {"read": <cells dict>, "handle": str, "sheet_no": int}
# The submitter's latest parsed read, needed by kickoff/confirm/resend. An
# in-memory miss after a restart just means the confirm message can't be
# re-derived until the user resends, which is acceptable.
_PENDING_READ = {}


def evaluate(read, submitter_handle, sheet_no):
    """Classify a parsed read.
    Returns {"line", "fully_recognised", "unreachable"}."""
    from handlers.bingo import _matched_and_prompts   # lazy: avoid import cycle
    matched, _prompts = _matched_and_prompts(
        read.get("cells", []), submitter_handle, sheet_no)
    candidates = lines.winning_lines(matched, submitter_handle)
    if not candidates:
        return {"line": None, "fully_recognised": False, "unreachable": []}
    line = lines.pick_best_line(candidates)
    unreachable = [h for (_r, _c, h) in line
                   if storage.user_id_for_handle(h) is None]
    return {"line": line, "fully_recognised": not unreachable,
            "unreachable": unreachable}


def _confirm_keyboard(submission_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm",
                             callback_data=f"bingoq:confirm:{submission_id}")
    ]])


async def _send_confirmation(context, sub):
    """DM the submitter their confirmation message: short (winning line + a
    Confirm button) when fully recognised, else the full fill-in template with a
    /start flag for any matched-but-unreachable handle."""
    sid = sub["id"]
    uid = sub["submitter_user_id"]
    pending = _PENDING_READ.get(sid)
    if pending is None:
        log.warning("no pending read for submission %s; can't send confirmation", sid)
        return
    res = evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if res["fully_recognised"]:
        text = ("You're up! 🎉 Here's your winning line — tap Confirm if it's "
                "right:\n\n"
                + bingo_text.build_line_confirm_text(pending["sheet_no"], res["line"]))
        await context.bot.send_message(
            chat_id=uid, text=text, reply_markup=_confirm_keyboard(sid))
        return
    preview = bingo_text.build_prefilled_text(
        pending["sheet_no"], pending["read"].get("cells", []))
    flag = ""
    if res["unreachable"]:
        who = ", ".join(f"@{h}" for h in res["unreachable"])
        flag = (f"\n\n⚠️ {who} hasn't started the bot yet — ask them to send it "
                "/start so I can verify them, then resend your list.")
    await context.bot.send_message(
        chat_id=uid,
        text="You're up! Fill in the @handles below (fix any blanks) and send the "
             "whole list back to me:\n\n" + preview + flag,
    )
