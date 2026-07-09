"""Bingo submission queue: enqueue, kickoff-at-10, submitter self-confirm, and
rolling replacement, ahead of the existing tagged-people verification.

State (see the plan's STATUS MAPPING): queued -> confirming -> pending(verify)
-> verified(won) | failed. Only 'queued'/'confirming' are owned here; once a
submitter confirms a fully-recognised line, _start_verification flips the row to
'pending' and the existing handlers/bingo.py pipeline takes over unchanged.
"""

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

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


def _read_from_members(submission_id):
    """Rebuild a minimal read from a submission's recorded winning line, or None.
    Used to seed _PENDING_READ for imported past submissions and as a restart
    fallback (winning_members is persisted; _PENDING_READ is in-memory)."""
    members = storage.winning_members(submission_id)
    if not members:
        return None
    cells = [{"row": m["row"], "col": m["col"], "handle": m["handle"], "score": 100.0}
             for m in members]
    return {"cells": cells}


def _rebuild_pending(sid):
    """Try to repopulate _PENDING_READ[sid] from the persisted winning line.
    Returns the pending dict or None."""
    read = _read_from_members(sid)
    if read is None:
        return None
    sub = storage.submission_by_id(sid)
    if sub is None:
        return None
    pending = {"read": read, "handle": sub.get("submitter_handle") or "",
               "sheet_no": sub["sheet_no"]}
    _PENDING_READ[sid] = pending
    return pending


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
        pending = _rebuild_pending(sid)
    if pending is None:
        # in-memory read lost (e.g. restart between kickoff and confirm) and no
        # persisted winning line to rebuild from: ask the submitter to resend so
        # we can rebuild it, instead of leaving them with no confirmation at all.
        log.warning("no pending read for submission %s; can't send confirmation", sid)
        try:
            await context.bot.send_message(
                chat_id=sub["submitter_user_id"],
                text="Please resend your filled bingo card so I can check it. 🔁",
            )
        except Exception:
            pass
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
    """Record a submission into the queue and tell the user their position. The
    confirmation round opens automatically once BINGO_PRIZE_LIMIT are queued
    (or earlier via a facil command)."""
    sid = storage.queue_submission(uid, handle, sheet_no)
    _PENDING_READ[sid] = {"read": read, "handle": handle, "sheet_no": sheet_no}
    position = len(storage.queued_in_order())
    await context.bot.send_message(
        chat_id=uid,
        text=f"You're in the queue (#{position})! 📥 I'll message you when it's "
             "your turn to confirm your squares — hang tight. 🙂",
    )
    if not storage.is_queue_open() and \
            len(storage.queued_in_order()) >= config.BINGO_PRIZE_LIMIT:
        storage.set_queue_open()
    await maybe_kickoff(context)


async def maybe_kickoff(context):
    """Promote queued submissions into 'confirming' until the 10 in-flight slots
    are full — but only once the round is open (10 queued, or a facil command)."""
    if not storage.is_queue_open():
        return
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
    _PENDING_READ.pop(sid, None)                   # terminal state: bound growth
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


async def confirm_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, _, sid_s = query.data.split(":")
        sid = int(sid_s)
    except (ValueError, AttributeError):
        return
    if storage.submission_status(sid) != "confirming":
        return                                    # stale / already resolved
    pending = _PENDING_READ.get(sid)
    if pending is None:
        pending = _rebuild_pending(sid)
    if pending is None:
        # _PENDING_READ is in-memory and empty after a restart, and there's no
        # persisted winning line to rebuild from either. Rather than silently
        # no-op'ing the tap, ask the submitter to resend so we can re-derive and
        # check their card (rearm_confirm_timeouts re-arms the timeout but can't
        # reconstruct the read).
        try:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="I lost track of your card after a restart — please resend "
                     "your filled list and I'll check it. 🔁",
            )
        except Exception:
            pass
        return
    res = evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if not res["fully_recognised"]:
        return                                    # full-template path governs
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _start_verification(
        context, sid, res["line"], pending["handle"], pending["sheet_no"])


async def on_resend(context, uid, read):
    """Handle a typed resend from a submitter in the 'confirming' phase.
    Returns True if the user was confirming (message consumed), else False."""
    mine = [s for s in storage.confirming_submissions()
            if s["submitter_user_id"] == uid]
    if not mine:
        return False
    sub = mine[0]
    handle = sub.get("submitter_handle") or ""
    _PENDING_READ[sub["id"]] = {
        "read": read, "handle": handle, "sheet_no": sub["sheet_no"]}
    res = evaluate(read, handle, sub["sheet_no"])
    if res["fully_recognised"]:
        await _start_verification(
            context, sub["id"], res["line"], handle, sub["sheet_no"])
    else:
        await _send_confirmation(context, sub)    # re-show full + flags
    return True


async def _start_verification(context, submission_id, line, handle, sheet_no):
    """Submitter confirmed a fully-recognised line: hand off to the existing
    tagged-people pipeline (flip to 'pending', DM subjects, arm the 12h timeout,
    evaluate once)."""
    from handlers import bingo                     # lazy: avoid import cycle
    from data import bingo_templates as templates
    bingo._cancel_job(context, f"bingo:confirmwait:{submission_id}")
    members = [{
        "row": r, "col": c, "handle": h,
        "prompt": templates.prompt_for(sheet_no, r, c),
        "target_user_id": storage.user_id_for_handle(h),
    } for (r, c, h) in line]
    storage.record_winning_members(submission_id, members)
    storage.set_submission_status(submission_id, "pending")
    sub = storage.submission_by_id(submission_id)
    if sub is not None:
        try:
            await context.bot.send_message(
                chat_id=sub["submitter_user_id"],
                text="Nice line! 🎯 I'm checking with the people you tagged — I'll "
                     "message you the moment it's verified (they have 12 hours).",
            )
        except Exception:
            pass
    if getattr(context, "job_queue", None) is not None:
        context.job_queue.run_once(
            bingo._confirmation_timeout,
            when=config.BINGO_CONFIRM_TIMEOUT,
            data={"submission_id": submission_id},
            name=f"bingo:timeout:{submission_id}",
        )
    await bingo._dm_subjects(context, submission_id, members)
    await bingo._finalize(context, submission_id)
    # handed off to the tagged-people pipeline; the queue no longer needs the
    # cached read, so evict it to bound _PENDING_READ growth.
    _PENDING_READ.pop(submission_id, None)


# ---------------------------------------------------------------------------
# Startup + registration
# ---------------------------------------------------------------------------

async def close_round(context):
    """Facil fallback: open the round now (even with fewer than 10 queued) and
    process whoever is waiting."""
    storage.set_queue_open()
    await maybe_kickoff(context)


def register(app):
    app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingoq:confirm:"))


def rearm_confirm_timeouts(app):
    """Re-arm the submitter-confirm 12h timeout for every 'confirming' submission
    after a restart (mirror bingo.rearm_bingo_timeouts' clock math)."""
    jq = app.job_queue
    if jq is None:
        return
    now = datetime.now(config.TIMEZONE)
    for sub in storage.confirming_submissions():
        try:
            submitted = datetime.fromisoformat(sub["submitted_at"])
        except (ValueError, KeyError, TypeError):
            submitted = now
        if submitted.tzinfo is None:
            from zoneinfo import ZoneInfo
            submitted = submitted.replace(tzinfo=ZoneInfo("Asia/Singapore"))
        delay = (submitted + config.BINGO_CONFIRM_TIMEOUT - now).total_seconds()
        jq.run_once(_confirm_timeout_job, when=max(delay, 5),
                    data={"submission_id": sub["id"]},
                    name=f"bingo:confirmwait:{sub['id']}")
