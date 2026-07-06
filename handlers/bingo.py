"""Human Bingo.

For now this only ships /get_bingo — it hands each Year 1 their allocated bingo
card so the game can go live ahead of the checker. The /submit_bingo flow (OCR,
winning-line detection and confirmations) is added on top of this module later.

The full flow: gate on bingo_is_closed / has_bingo_prize / active_submission +
cooldown; two-step image upload (context.user_data["awaiting_bingo"]); OCR via
bingo_ocr.read_submission; corner-number wrong-sheet reject; winning_lines ->
pick_best_line; record submission + winning_members; DM each reachable,
non-self, non-cached subject a Yes/No inline keyboard, reusing cached
confirmations, and arm a config.BINGO_CONFIRM_TIMEOUT job; confirm_button
records the vote, re-evaluates via line_passes, and on pass does the atomic
claim + best-effort channel post + submitter DM; close at 10. Unreachable
subjects count as a miss.
"""

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import bingo_lines as lines
import bingo_ocr as ocr
import config
import storage
from data import bingo_templates as templates
from setup import sheets

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Roster index — used by the submit pipeline to fuzzy-match typed handles
# against the known Year 1 handles. /get_bingo does NOT gate on the roster:
# anyone who DMs the bot gets a card, so a Year 1 who's missing from the sheet
# can still play.
# ---------------------------------------------------------------------------

_ROSTER_INDEX = None     # full index dict built by bingo_ocr.build_roster_index


def _roster_index():
    """Build (and cache) the rich roster index that bingo_ocr uses for fuzzy
    handle matching. Also initialises _ROSTER_INDEX with a 'handles' set so
    _is_year1 can share the same data when called indirectly."""
    global _ROSTER_INDEX
    if _ROSTER_INDEX is None:
        members = []
        try:
            for og_members in sheets.load_year1_members().values():
                members.extend(og_members)
        except Exception as exc:
            log.warning("couldn't load Year 1 roster for bingo: %s", exc)
        _ROSTER_INDEX = ocr.build_roster_index(members)
    return _ROSTER_INDEX


# ---------------------------------------------------------------------------
# /get_bingo — resolve the caller, freeze their sheet, DM the template
# ---------------------------------------------------------------------------

async def get_bingo(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me /get_bingo and I'll send you your Human Bingo card 🎉"
        )
        return

    user = update.effective_user
    if user is None:
        return

    # Open to anyone who DMs the bot — a Year 1 who isn't on the sheet still
    # gets a (deterministically allocated, even) card so they can play.
    sheet_no = storage.allocate_bingo_sheet(user.id, (user.username or "").lower())
    path = templates.template_path(sheet_no)
    caption = (
        f"Here's your Human Bingo card (#{sheet_no})! 🌟\n\n"
        "Find fellow Year 1s who match each square and type their @handle in it. "
        "Get 5 in a row — the ⭐ centre is a free space — then send your filled "
        "card back with /submit_bingo to claim a prize! 🎉"
    )
    try:
        with open(path, "rb") as fh:
            await update.effective_message.reply_document(document=fh, caption=caption)
    except FileNotFoundError:
        log.error("bingo card %s missing at %s", sheet_no, path)
        await update.effective_message.reply_text(
            "Your card isn't quite ready yet — please try again in a little bit. 🙏"
        )


# ---------------------------------------------------------------------------
# Pure-ish helpers (unit-tested)
# ---------------------------------------------------------------------------

def _matched_and_prompts(cells, submitter_handle, sheet_no):
    """Turn read_submission cells into (matched, prompts) for a known sheet.

    matched: {(row, col): handle} for CONFIDENT cells only — handle non-None,
    lowercased, and never the submitter (no self-cheese). read_submission has
    already applied the score/margin cutoff, so any non-None handle here is
    confident. prompts: {(row, col): prompt} pulled from
    templates.prompt_for(sheet_no, row, col), so the Yes/No button text is stable
    regardless of later template edits.
    """
    submitter = (submitter_handle or "").lower()
    matched, prompts = {}, {}
    for cell in cells:
        handle = cell.get("handle")
        if not handle:
            continue
        handle = handle.lower()
        if handle == submitter:
            continue
        r, c = cell["row"], cell["col"]
        matched[(r, c)] = handle
        prompts[(r, c)] = templates.prompt_for(sheet_no, r, c)
    return matched, prompts


def _line_verdict(line, answers):
    """'pass' | 'fail' | 'pending' for one candidate line.

    line: [(row, col, handle), ...] real cells. answers: handle -> 'yes'|'no'
    (missing = unanswered/unreachable). Delegates the pass threshold to
    bingo_lines.line_passes (yes >= len-1, i.e. at most one miss). 'fail' once
    it is arithmetically impossible to still pass; otherwise 'pending'.
    """
    if lines.line_passes(line, answers):
        return "pass"
    handles = [h for _, _, h in line]
    need = lines.required_yes(line)
    yes = sum(1 for h in handles if answers.get(h) == "yes")
    undecided = sum(1 for h in handles if h not in answers)
    if yes + undecided < need:
        return "fail"
    return "pending"


# ---------------------------------------------------------------------------
# /submit_bingo — gate, then arm the one-shot image handler
# ---------------------------------------------------------------------------

async def submit_bingo(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "Send your filled card to me in a private chat 🙂"
        )
        return

    uid = update.effective_user.id
    if storage.bingo_is_closed():
        await update.effective_message.reply_text(
            "All 10 prizes have been claimed — thanks for playing! 🎉"
        )
        return
    if storage.has_bingo_prize(uid):
        await update.effective_message.reply_text(
            "You've already won — give someone else a shot! 🏆"
        )
        return
    if storage.active_submission(uid):
        await update.effective_message.reply_text(
            "You already have a card being checked. Hang tight — I'll message you "
            "the moment it's verified. ⏳"
        )
        return
    if storage.get_bingo_sheet(uid) is None:
        await update.effective_message.reply_text(
            "Grab your card first with /get_bingo, then send it back here 🙂"
        )
        return

    context.user_data["awaiting_bingo"] = True
    await update.effective_message.reply_text(
        "Send me a photo of your filled bingo card (as a photo or an image "
        "file) and I'll check it. 📸"
    )


# ---------------------------------------------------------------------------
# The filled-card image — only when we asked for it
# ---------------------------------------------------------------------------

def _cooldown_remaining(uid):
    """Seconds still to wait after a recent attempt, or 0."""
    last = storage.last_bingo_activity(uid)
    if not last:
        return 0
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return 0
    now = datetime.now(config.TIMEZONE)
    # make sure both datetimes are timezone-aware for comparison
    if then.tzinfo is None:
        from zoneinfo import ZoneInfo
        then = then.replace(tzinfo=ZoneInfo("Asia/Singapore"))
    elapsed = (now - then).total_seconds()
    remaining = config.BINGO_RETRY_COOLDOWN.total_seconds() - elapsed
    return int(remaining) if remaining > 0 else 0


async def _download_image(update, context):
    msg = update.effective_message
    if msg.photo:
        tg_file = await context.bot.get_file(msg.photo[-1].file_id)
    elif msg.document:
        tg_file = await context.bot.get_file(msg.document.file_id)
    else:
        return None
    return bytes(await tg_file.download_as_bytearray())


async def on_bingo_image(update, context):
    if not context.user_data.get("awaiting_bingo"):
        return  # not expecting an image from this user; ignore
    context.user_data["awaiting_bingo"] = False

    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return

    uid = update.effective_user.id
    handle = sheets.normalize_handle(update.effective_user.username) or ""

    if storage.bingo_is_closed():
        await update.effective_message.reply_text(
            "All 10 prizes have been claimed — thanks for playing! 🎉"
        )
        return
    wait = _cooldown_remaining(uid)
    if wait:
        await update.effective_message.reply_text(
            f"Hold on {wait}s before trying again 🙂"
        )
        return

    sheet_no = storage.get_bingo_sheet(uid)
    if sheet_no is None:
        await update.effective_message.reply_text(
            "Grab your card first with /get_bingo 🙂"
        )
        return

    image_bytes = await _download_image(update, context)
    if not image_bytes:
        await update.effective_message.reply_text(
            "I couldn't read that — send it as a photo or an image file 📸"
        )
        return

    # OCR scans each cell one at a time and can take up to a minute, so let the
    # player know their card arrived and is being worked on.
    await update.effective_message.reply_text(
        "Got your card! 🔍 Scanning it now — this can take up to a minute…"
    )

    read = ocr.read_submission(sheet_no, image_bytes, _roster_index())

    # wrong-sheet defence: a confident, mismatched corner number rejects
    corner = read.get("corner")
    if corner is not None and corner != sheet_no:
        # Record the attempt (so the retry cooldown still applies) but resolve it
        # immediately — leaving it 'pending' would block the user's next submit.
        rejected = storage.start_bingo_submission(uid, handle, sheet_no, corner)
        storage.set_submission_status(rejected, "rejected")
        await update.effective_message.reply_text(
            f"This looks like sheet #{corner}, but you were given sheet "
            f"#{sheet_no}. Please send your own card 🙂"
        )
        return

    matched, prompts = _matched_and_prompts(read.get("cells", []), handle, sheet_no)

    candidate_lines = lines.winning_lines(matched, handle)
    if not candidate_lines:
        await update.effective_message.reply_text(
            "No bingo yet — I couldn't find 5 in a row of confirmed Year 1s. "
            "Double-check the handles and try again in a minute! 🔁"
        )
        return

    line = lines.pick_best_line(candidate_lines)
    submission_id = storage.start_bingo_submission(uid, handle, sheet_no, corner)

    members = []
    for (r, c, h) in line:
        members.append({
            "row": r, "col": c, "handle": h,
            "prompt": prompts.get((r, c)) or templates.prompt_for(sheet_no, r, c),
            "target_user_id": storage.user_id_for_handle(h),
        })
    storage.record_winning_members(submission_id, members)

    await update.effective_message.reply_text(
        "Nice line! 🎯 I'm checking with the people you tagged — I'll message you "
        "the moment it's verified (they have 12 hours to respond)."
    )

    await _dm_subjects(context, submission_id, members)

    # arm the 12h finaliser
    if context.job_queue is not None:
        context.job_queue.run_once(
            _confirmation_timeout,
            when=config.BINGO_CONFIRM_TIMEOUT,
            data={"submission_id": submission_id},
            name=f"bingo:timeout:{submission_id}",
        )

    # re-evaluate immediately in case cached answers already clinch it
    await _finalize(context, submission_id)


def _confirm_keyboard(submission_id, row, col):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"bingoconf:{submission_id}:{row}:{col}:yes"),
        InlineKeyboardButton("❌ No", callback_data=f"bingoconf:{submission_id}:{row}:{col}:no"),
    ]])


async def _dm_subjects(context, submission_id, members):
    """DM each reachable subject a Yes/No question, using cached answers where
    present (so a popular person is asked at most once per distinct prompt, and
    duplicate prompts shared across cells in this same line are de-duplicated).
    An unreachable subject is silently left unanswered and treated as a miss."""
    asked = set()  # (target_user_id, prompt) already sent this call -> de-dupe
    for m in members:
        target = m.get("target_user_id")
        if target is None:
            continue  # unmapped handle -> counts as a miss at evaluation
        key = (target, m["prompt"])
        if key in asked:
            continue  # de-dup shared cell within this chosen line
        cached = storage.get_cached_confirmation(target, m["prompt"])
        if cached is not None:
            asked.add(key)
            continue  # already answered this prompt game-wide; reuse silently
        try:
            await context.bot.send_message(
                chat_id=target,
                text=f"👋 Quick one for Human Bingo:\n\n<b>{m['prompt']}</b> — does "
                     "this describe you?",
                parse_mode="HTML",
                reply_markup=_confirm_keyboard(submission_id, m["row"], m["col"]),
            )
            asked.add(key)
        except Exception as exc:
            # unreachable (never /started, blocked us, etc.) -> one allowed miss
            log.info("couldn't DM subject %s for submission %s: %s",
                     target, submission_id, exc)


# ---------------------------------------------------------------------------
# Confirmation button
# ---------------------------------------------------------------------------

async def confirm_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, sub_s, row_s, col_s, ans = query.data.split(":")
        submission_id, row, col = int(sub_s), int(row_s), int(col_s)
    except (ValueError, AttributeError):
        return
    if ans not in ("yes", "no"):
        return

    subject_id = query.from_user.id

    # find the prompt for this (submission,row,col) so we can cache by prompt
    prompt = None
    for m in storage.winning_members(submission_id):
        if m["row"] == row and m["col"] == col:
            prompt = m["prompt"]
            break
    if prompt is None:
        return

    storage.record_bingo_confirmation(subject_id, prompt, ans)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may be too old to edit; the cache is what matters

    await _finalize(context, submission_id)


# ---------------------------------------------------------------------------
# Evaluation / finalisation
# ---------------------------------------------------------------------------

def _answers_for(submission_id):
    """handle -> 'yes'|'no' from the cache, for this submission's real cells.
    Unreachable/unmapped/unanswered handles are simply absent (a miss)."""
    answers = {}
    for m in storage.winning_members(submission_id):
        target = m.get("target_user_id")
        if target is None:
            continue
        cached = storage.get_cached_confirmation(target, m["prompt"])
        if cached is not None:
            answers[m["handle"]] = cached
    return answers


def _line_from_members(members):
    return [(m["row"], m["col"], m["handle"]) for m in members]


async def _finalize(context, submission_id, final=False):
    """Evaluate a pending submission once. On pass, atomically claim a prize and
    (best-effort) announce + DM. On a definite fail (or at the 12h timeout with
    still-missing answers) mark it failed so the submitter can retry."""
    sub = storage.submission_by_id(submission_id)
    if not sub or sub["status"] != "pending":
        return  # already resolved
    members = storage.winning_members(submission_id)
    if not members:
        return
    line = _line_from_members(members)
    answers = _answers_for(submission_id)
    verdict = _line_verdict(line, answers)

    if verdict == "pending" and not final:
        return
    if verdict != "pass":
        # timeout or two misses -> failed, retry allowed after cooldown
        storage.set_submission_status(submission_id, "failed")
        await _notify_submitter_failed(context, submission_id)
        return

    await _award(context, submission_id)


async def _award(context, submission_id):
    members = storage.winning_members(submission_id)
    if not members:
        return
    submitter_id, submitter_handle = _submitter_of(submission_id)
    if submitter_id is None or storage.has_bingo_prize(submitter_id):
        storage.set_submission_status(submission_id, "verified",
                                      verified_at=storage._now_iso())
        return

    claim_no = storage.claim_bingo_prize(submitter_id, submitter_handle, submission_id)
    storage.set_submission_status(submission_id, "verified",
                                  verified_at=storage._now_iso())
    if claim_no is None:
        # someone else took the last slot between check and claim
        try:
            await context.bot.send_message(
                chat_id=submitter_id,
                text="So close! All 10 prizes were just claimed — thanks for "
                     "playing! 🎉",
            )
        except Exception:
            pass
        return

    if claim_no >= config.BINGO_PRIZE_LIMIT:
        storage.set_bingo_closed()
        # Cancel the just-resolved submission's own timeout job first (it's
        # already verified, so pending_submissions() won't include it).
        _cancel_job(context, f"bingo:timeout:{submission_id}")
        # Then cancel any remaining pending subs' jobs.
        _cancel_outstanding_timeouts(context)

    # channel post — best-effort, gated to once per slot
    try:
        await context.bot.send_message(
            chat_id=config.ANNOUNCE_CHAT_ID,
            text=f"🎉 <b>{claim_no}/{config.BINGO_PRIZE_LIMIT} bingo prizes "
                 "claimed!</b>",
            parse_mode="HTML",
        )
        storage.mark_prize_posted(submitter_id)
    except Exception as exc:
        log.warning("couldn't post bingo prize announcement: %s", exc)

    try:
        await context.bot.send_message(
            chat_id=submitter_id,
            text=f"🏆 <b>BINGO!</b> Your line is verified — you're prize "
                 f"#{claim_no} of {config.BINGO_PRIZE_LIMIT}! A facil will be in "
                 "touch. Congrats! 🎉",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("couldn't DM bingo winner %s: %s", submitter_id, exc)


def _cancel_job(context, name):
    """Cancel a single named job (best-effort)."""
    jq = getattr(context, "job_queue", None)
    if jq is None or not hasattr(jq, "get_jobs_by_name"):
        return
    try:
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()
    except Exception as exc:
        log.warning("couldn't cancel job %s: %s", name, exc)


def _cancel_outstanding_timeouts(context):
    """When the game closes at the 10th prize, cancel every still-armed 12h
    confirmation-timeout job (spec §8/§9). Each job also self-guards on the
    closed flag, but we remove them so nothing lingers on the queue."""
    jq = getattr(context, "job_queue", None)
    if jq is None or not hasattr(jq, "get_jobs_by_name"):
        return
    # job names are 'bingo:timeout:<submission_id>'; we don't know the ids here,
    # so cancel via the pending-submission list (their timeouts are still armed).
    try:
        for sub in storage.pending_submissions():
            for job in jq.get_jobs_by_name(f"bingo:timeout:{sub['id']}"):
                job.schedule_removal()
    except Exception as exc:
        log.warning("couldn't cancel outstanding bingo timeouts: %s", exc)


async def _notify_submitter_failed(context, submission_id):
    submitter_id, _ = _submitter_of(submission_id)
    if submitter_id is None:
        return
    try:
        await context.bot.send_message(
            chat_id=submitter_id,
            text="Your bingo line didn't get enough confirmations this time. "
                 "You can try another line in a minute — good luck! 🔁",
        )
    except Exception:
        pass


def _submitter_of(submission_id):
    sub = storage.submission_by_id(submission_id)
    if not sub:
        return None, None
    return sub["submitter_user_id"], sub["submitter_handle"]


async def _confirmation_timeout(context):
    """12h job: evaluate one final time, counting still-missing answers as
    misses. No-op if the submission already resolved or the game is closed."""
    submission_id = context.job.data["submission_id"]
    sub = storage.submission_by_id(submission_id)
    if not sub or sub["status"] != "pending":
        return
    if storage.bingo_is_closed():
        storage.set_submission_status(submission_id, "failed")
        await _notify_submitter_failed(context, submission_id)
        return
    await _finalize(context, submission_id, final=True)


# ---------------------------------------------------------------------------
# Startup + registration
# ---------------------------------------------------------------------------

def rearm_bingo_timeouts(app):
    """Re-arm a 12h finaliser for every still-pending submission after a
    restart, so a crash mid-verification never strands a submission. Uses the
    original submitted_at so long-pending ones fire promptly."""
    jq = app.job_queue
    if jq is None:
        log.warning("JobQueue not available — bingo timeouts won't re-arm.")
        return
    now = datetime.now(config.TIMEZONE)
    rearmed = 0
    for sub in storage.pending_submissions():
        try:
            submitted = datetime.fromisoformat(sub["submitted_at"])
        except (ValueError, KeyError, TypeError):
            submitted = now
        # ensure timezone-aware for arithmetic
        if submitted.tzinfo is None:
            from zoneinfo import ZoneInfo
            submitted = submitted.replace(tzinfo=ZoneInfo("Asia/Singapore"))
        deadline = submitted + config.BINGO_CONFIRM_TIMEOUT
        delay = (deadline - now).total_seconds()
        when = max(delay, 5)  # give an overdue one a short grace, don't fire at 0
        jq.run_once(
            _confirmation_timeout,
            when=when,
            data={"submission_id": sub["id"]},
            name=f"bingo:timeout:{sub['id']}",
        )
        rearmed += 1
    log.info("re-armed %d bingo confirmation timeout(s)", rearmed)


def register(app):
    app.add_handler(CommandHandler("get_bingo", get_bingo))
    app.add_handler(CommandHandler("submit_bingo", submit_bingo))
    app.add_handler(CallbackQueryHandler(confirm_button, pattern=r"^bingoconf:"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE),
        on_bingo_image,
    ))
