"""Forward round: read a forwarded filled card's ORIGINAL send time, OCR it,
queue it, and DM the submitter a confirmation — the forward-round analogue of
handlers/bingo_queue.py's submitter-confirm step, but keyed off a forwarded
message's forward_origin.date instead of a live submission.

Mirrors bingo_queue._send_confirmation (evaluate -> short line + Confirm
button when a complete roster-matched line exists, else the full fill-in
template), with its OWN _PENDING_READ and its own 'bingofwd:confirm:<id>'
keyboard callback -- entirely separate from bingo_queue's in-flight
queue/confirm state. Reachability (whether a matched handle has /started
the bot) is irrelevant here: a win is any 5-in-a-row of roster handles.
"""

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bingo_text
import config
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
    Confirm button) when a complete roster-matched line exists, else the full
    fill-in template."""
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
    if res["line"] is not None:
        text = ("Got it! 🎉 Here's your winning line — tap Confirm if it's "
                "right:\n\n"
                + bingo_text.build_line_confirm_text(sheet_no, res["line"]))
        await context.bot.send_message(
            chat_id=uid, text=text,
            reply_markup=_confirm_keyboard(submission_id))
        return
    preview = bingo_text.build_prefilled_text(
        sheet_no, pending["read"].get("cells", []))
    await context.bot.send_message(
        chat_id=uid,
        text="Got your card! Fill in the @handles below (fix any blanks) and "
             "send the whole list back to me:\n\n" + preview,
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
    await maybe_close_collection(context)


def _rebuild_pending(sid):
    """Try to repopulate _PENDING_READ[sid] from the persisted winning line
    (mirrors bingo_queue._rebuild_pending, using THIS module's _PENDING_READ).
    Returns the pending dict or None."""
    members = storage.winning_members(sid)
    if not members:
        return None
    sub = storage.submission_by_id(sid)
    if sub is None:
        return None
    read = {"cells": [{"row": m["row"], "col": m["col"], "handle": m["handle"],
                       "score": 100.0} for m in members]}
    pending = {"read": read, "handle": sub.get("submitter_handle") or "",
               "sheet_no": sub["sheet_no"]}
    _PENDING_READ[sid] = pending
    return pending


async def _mark_ready(context, submission_id, line, sheet_no):
    """A fully-recognised line was confirmed (via button or resend): record the
    winning members and flip the entry to 'ready', then DM the submitter.
    Unlike bingo_queue._start_verification, there's no per-tagged-person
    verification here -- results are released together once the batch closes."""
    from data import bingo_templates as templates
    members = [{
        "row": r, "col": c, "handle": h,
        "prompt": templates.prompt_for(sheet_no, r, c),
        "target_user_id": storage.user_id_for_handle(h),
    } for (r, c, h) in line]
    storage.record_winning_members(submission_id, members)
    storage.set_forward_ready(submission_id)
    sub = storage.submission_by_id(submission_id)
    if sub is not None:
        try:
            await context.bot.send_message(
                chat_id=sub["submitter_user_id"],
                text="You're in — results will be released together soon. 🎉",
            )
        except Exception:
            pass
    # A confirm that lands AFTER collection has closed still has to drive the
    # round: claim this fresh 'ready' entry (if a slot's free) and release.
    # During 'collecting' the phase guard skips this (winners are picked at close).
    if storage.forward_phase() == "verifying":
        await select_winners(context)
        await _release_results(context)
    _PENDING_READ.pop(submission_id, None)


async def confirm_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, _, sid_s = query.data.split(":")
        sid = int(sid_s)
    except (ValueError, AttributeError):
        return
    if storage.submission_status(sid) != "fwd_confirming":
        return                                    # stale / already resolved
    pending = _PENDING_READ.get(sid)
    if pending is None:
        pending = _rebuild_pending(sid)
    if pending is None:
        try:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="I lost track of your card — please re-forward your filled "
                     "bingo card and I'll check it. 🔁",
            )
        except Exception:
            pass
        return
    res = bingo_queue.evaluate(pending["read"], pending["handle"], pending["sheet_no"])
    if res["line"] is None:
        return                                    # full-template/resend path governs
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _mark_ready(context, sid, res["line"], pending["sheet_no"])


async def on_resend(context, uid, read):
    """Handle a re-forwarded card from a submitter in the 'fwd_confirming' phase.
    Returns True if the user was in that phase (message consumed), else False."""
    mine = [s for s in storage.all_bingo_submissions()
            if s["submitter_user_id"] == uid and s["status"] == "fwd_confirming"]
    if not mine:
        return False
    sub = mine[0]
    handle = sub.get("submitter_handle") or ""
    sheet_no = sub["sheet_no"]
    _PENDING_READ[sub["id"]] = {"read": read, "handle": handle, "sheet_no": sheet_no}
    res = bingo_queue.evaluate(read, handle, sheet_no)
    if res["line"] is not None:
        await _mark_ready(context, sub["id"], res["line"], sheet_no)
    else:
        await _send_confirmation(context, sub["id"], uid, sheet_no)  # re-show full + flags
    return True


# ---------------------------------------------------------------------------
# Collection close + winner selection: a confirmed 5-in-a-row (all handles in
# the roster) is a direct win. When the round closes we claim the earliest
# 'ready' entries as prizes -- no tagged-people verification.
# ---------------------------------------------------------------------------

async def maybe_close_collection(context):
    """Close the collecting phase once FORWARD_ROUND_TARGET entries are in
    (fwd_confirming + ready), then select winners and release."""
    if storage.forward_phase() == "collecting" and \
            storage.forward_entry_count() >= config.FORWARD_ROUND_TARGET:
        await close_collection(context)


async def close_collection(context):
    """Facil fallback / 20-entry trigger / 2-day deadline: close collecting,
    claim the earliest-ready entries as winners, and release the results.
    Idempotent -- a no-op once the round is 'released'."""
    if storage.forward_phase() == "released":
        return
    if storage.forward_phase() == "collecting":
        storage.set_forward_phase("verifying")
    await select_winners(context)
    await _release_results(context)   # DMs winners + admin summary, sets 'released'


async def _claim_winner(context, sub):
    """Claim one 'ready' forward submission directly as a prize (no tagged-people
    verification). On success mark it 'verified'; if the cap is already full or
    it's a duplicate winner, mark it 'failed'."""
    sid = sub["id"]
    claim_no = storage.claim_bingo_prize(
        sub["submitter_user_id"], sub.get("submitter_handle") or "", sid)
    if claim_no is not None:
        storage.set_submission_status(sid, "verified", verified_at=storage._now_iso())
    else:
        storage.set_submission_status(sid, "failed")   # cap already full / dup
    _PENDING_READ.pop(sid, None)


async def select_winners(context):
    """Claim the earliest 'ready' entries (by original submit time) as winners,
    up to the 10-prize cap."""
    while storage.bingo_prizes_claimed() < config.BINGO_PRIZE_LIMIT:
        ready = storage.ready_in_order()
        if not ready:
            return
        await _claim_winner(context, ready[0])


async def _forward_timeout_job(context):
    """2-day forward-round deadline: close collecting even if under target,
    select winners and release."""
    await close_collection(context)


# ---------------------------------------------------------------------------
# Batch results: once winners are selected, release them all together
# (channel-free — DMs to winners + one admin summary), instead of _award's
# normal one-at-a-time announcements. May be called with 0 winners (an empty
# or late round); that's fine (it DMs nobody and just sets 'released').
# ---------------------------------------------------------------------------

async def _release_results(context):
    from handlers import bingo
    storage.set_forward_phase("released")
    winners = storage.all_bingo_prizes()
    rank = ("a winner" if len(winners) == 1
            else f"one of the {len(winners)} winners")
    for w in winners:
        try:
            await context.bot.send_message(
                chat_id=w["winner_user_id"],
                text=f"🏆 BINGO! You're {rank} — "
                     "congratulations! 🎉 A facil will be in touch to sort out your prize.")
        except Exception:
            pass
    recipients = bingo._admin_recipient_ids()
    if not recipients:
        # No admin reachable: send no summary and leave every winner unmarked
        # so winners_pending_admin_notice() still lists them and the existing
        # winner-notify startup sweep announces them once an admin is reachable
        # (mirrors bingo._dm_admins_of_winner).
        return
    handles = ", ".join(f"@{w['handle']}" for w in winners) or "(none)"
    summary = (f"🏁 Bingo prize round complete — {len(winners)} winner(s): {handles}")
    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=summary)
        except Exception:
            pass
    for w in winners:
        storage.mark_admin_notified(w["winner_user_id"])   # WN sweep won't double-announce


# ---------------------------------------------------------------------------
# Broadcast start + registration + startup re-arm
# ---------------------------------------------------------------------------

async def begin_round(context):
    """Start the forward round: set phase collecting, DM every card-holder to
    forward their earliest card, and arm the 2-day deadline. Returns the count
    DM'd, or -1 (a sentinel, sending nothing) if a round is already in progress."""
    if storage.forward_phase() is not None:
        return -1                            # already collecting/verifying/released
    storage.set_forward_phase("collecting")
    n = 0
    for a in storage.all_bingo_allocations():
        try:
            await context.bot.send_message(chat_id=a["user_id"],
                text="📸 The Human Bingo prize round is on! Forward me the earliest "
                     "bingo card you sent me and I'll check it. 🎉")
            n += 1
        except Exception:
            pass
    if getattr(context, "job_queue", None) is not None:
        context.job_queue.run_once(_forward_timeout_job,
            when=config.FORWARD_ROUND_WINDOW.total_seconds(),
            name="bingo:forward_deadline")
    return n


def register(app):
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingofwd:confirm:"))


def rearm(app):
    """Re-arm the 2-day deadline if a collecting round was in progress at
    restart; if a round was stuck mid-'verifying', schedule a one-shot that
    calls close_collection so it selects winners, releases, and settles instead
    of staying stuck without a live timer to advance it."""
    jq = app.job_queue
    if jq is None:
        return
    phase = storage.forward_phase()
    if phase == "verifying":
        jq.run_once(close_collection, when=3, name="bingo:forward_resume")
        return
    if phase != "collecting":
        return
    started = storage.forward_started_at()
    from zoneinfo import ZoneInfo
    try:
        base = datetime.fromisoformat(started)
    except (ValueError, TypeError):
        base = datetime.now(config.TIMEZONE)
    if base.tzinfo is None:
        base = base.replace(tzinfo=ZoneInfo("Asia/Singapore"))
    delay = (base + config.FORWARD_ROUND_WINDOW - datetime.now(config.TIMEZONE)).total_seconds()
    jq.run_once(_forward_timeout_job, when=max(delay, 5), name="bingo:forward_deadline")
