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


async def enqueue(context, uid, handle, sheet_no, read):
    """Record a submission into the queue and tell the user their position, then
    fire kickoff if a slot is free (auto-batches once 10 are queued)."""
    sid = storage.queue_submission(uid, handle, sheet_no)
    _PENDING_READ[sid] = {"read": read, "handle": handle, "sheet_no": sheet_no}
    position = len(storage.queued_in_order())
    await context.bot.send_message(
        chat_id=uid,
        text=f"You're in the queue (#{position})! 📥 I'll message you if I need "
             "you to confirm your squares — hang tight. 🙂",
    )
    await maybe_kickoff(context)


async def maybe_kickoff(context):
    """Promote queued submissions into 'confirming' until the 10 in-flight slots
    are full, sending each its confirmation message."""
    while storage.active_slot_count() < config.BINGO_PRIZE_LIMIT:
        queued = storage.queued_in_order()
        if not queued:
            return
        sub = queued[0]
        storage.set_submission_status(sub["id"], "confirming")
        _arm_confirm_timeout(context, sub["id"])
        await _send_confirmation(context, sub)


def _arm_confirm_timeout(context, submission_id):
    """Arm the submitter-confirm 12h timeout for a 'confirming' submission."""
    jq = getattr(context, "job_queue", None)
    if jq is None:
        return
    jq.run_once(
        _confirm_timeout_job,
        when=config.BINGO_CONFIRM_TIMEOUT,
        data={"submission_id": submission_id},
        name=f"bingo:confirmwait:{submission_id}",
    )


async def _confirm_timeout_job(context):
    """12h submitter-confirm deadline: if still unconfirmed, fail and roll on."""
    sid = context.job.data["submission_id"]
    if storage.submission_status(sid) != "confirming":
        return
    storage.set_submission_status(sid, "failed")
    sub = storage.submission_by_id(sid)
    if sub is not None:
        try:
            await context.bot.send_message(
                chat_id=sub["submitter_user_id"],
                text="Your bingo confirmation timed out, so your slot passed to the "
                     "next player. Submit again anytime to re-join the queue! 🔁",
            )
        except Exception:
            pass
    await maybe_kickoff(context)
