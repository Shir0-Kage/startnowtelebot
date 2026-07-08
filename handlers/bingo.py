"""Human Bingo.

For now this only ships /get_bingo — it hands each Year 1 their allocated bingo
card so the game can go live ahead of the checker. The /submit_bingo flow (OCR,
winning-line detection and confirmations) is added on top of this module later.

The full flow: gate on bingo_is_closed / has_bingo_prize / active_submission +
cooldown; two-step image upload (context.user_data["awaiting_bingo"]); OCR in an
isolated ocr_worker subprocess (so onnxruntime can't freeze the bot). The OCR
read is never acted on straight away -- it's shown back to the player as a text
preview (bingo_text.build_prefilled_text) with a Yes/No "does this look right?"
keyboard (bingo_ocr_confirm_button). Yes proceeds with the original read; No
re-arms awaiting_bingo_text with that same prefilled list so the player
corrects it as plain text instead of re-photographing the card. From there
(confirmed OCR or a from-scratch/edited text submission): winning_lines ->
pick_best_line; record submission + winning_members; DM each reachable,
non-self, non-cached subject a Yes/No inline keyboard, reusing cached
confirmations, and arm a config.BINGO_CONFIRM_TIMEOUT job; confirm_button
records the vote, re-evaluates via line_passes, and on pass does the atomic
claim + best-effort channel post + submitter DM; close at 10. Unreachable
subjects count as a miss.
"""

import asyncio
import json
import logging
import pathlib
import sys
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
import bingo_text
import config
import storage
from data import bingo_templates as templates
from setup import sheets
# Module-level import is safe: bingo_queue imports handlers.bingo only lazily
# (inside its functions), so there's no top-level circular import.
from handlers import bingo_queue

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCR runs in an ISOLATED subprocess (ocr_worker.py). RapidOCR/onnxruntime holds
# the GIL while building its models and spins up a CPU-sized thread pool, which
# on a busy box can freeze a whole Python process for seconds — so it must NOT
# run in the bot's process. A subprocess has its own GIL/interpreter; if it hangs
# we kill it, so the bot's event loop and watchdog can never freeze. This is the
# freeze guarantee. /get_bingo does NOT gate on the roster: anyone who DMs the
# bot gets a card and can play; the worker builds the roster index itself.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_OCR_WORKER = str(_REPO_ROOT / "ocr_worker.py")
_OCR_SEMAPHORE = asyncio.Semaphore(1)   # one scan at a time (bounds subprocesses)


async def _run_ocr(sheet_no, image_bytes):
    """OCR one card in an isolated subprocess. Returns read_submission's dict, or
    None on timeout/failure (the caller then asks the player to retry). A hung
    scan is killed, so the bot always stays responsive."""
    async with _OCR_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, _OCR_WORKER, str(sheet_no),
                cwd=str(_REPO_ROOT),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            log.warning("couldn't start OCR worker: %s", exc)
            return None
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=image_bytes),
                timeout=config.BINGO_OCR_TIMEOUT.total_seconds(),
            )
        except asyncio.TimeoutError:
            log.warning("OCR worker exceeded the timeout — killing it")
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return None
        if proc.returncode != 0 or not out:
            log.warning("OCR worker failed (rc=%s): %s", proc.returncode,
                        (err or b"")[:400].decode("utf-8", "replace"))
            return None
        try:
            return json.loads(out.decode("utf-8"))
        except Exception as exc:
            log.warning("OCR worker returned unparseable output: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Roster index — used by the TEXT submission path to fuzzy-match typed
# handles against the known Year 1 handles (the image/OCR path builds its own
# index inside the isolated ocr_worker subprocess, see above). /get_bingo does
# NOT gate on the roster: anyone who DMs the bot gets a card, so a Year 1 who's
# missing from the sheet can still play.
# ---------------------------------------------------------------------------

_ROSTER_INDEX = None     # full index dict built by bingo_ocr.build_roster_index


def _roster_index():
    """Build (and cache) the rich roster index that bingo_text uses for fuzzy
    handle matching when a player submits by typing instead of a photo."""
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

def _bingo_gate_message(uid):
    """None if the four submit-gates all pass, else the user-facing text for
    whichever one didn't. Shared by submit_bingo (render time) and
    bingo_mode_button (click time, since state can go stale in between)."""
    if storage.bingo_is_closed():
        return "All 10 prizes have been claimed — thanks for playing! 🎉"
    if storage.has_bingo_prize(uid):
        return "You've already won — give someone else a shot! 🏆"
    if storage.active_submission(uid):
        return (
            "You already have a card being checked. Hang tight — I'll message "
            "you the moment it's verified. ⏳"
        )
    if storage.get_bingo_sheet(uid) is None:
        return "Grab your card first with /get_bingo, then send it back here 🙂"
    return None


def _ocr_confirm_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Looks right", callback_data="bingoocr:yes"),
        InlineKeyboardButton("✏️ Let me fix it", callback_data="bingoocr:no"),
    ]])


def _mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Photo", callback_data="bingomode:photo"),
        InlineKeyboardButton("✍️ Text", callback_data="bingomode:text"),
    ]])


async def submit_bingo(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "Send your filled card to me in a private chat 🙂"
        )
        return

    uid = update.effective_user.id
    gate_message = _bingo_gate_message(uid)
    if gate_message:
        await update.effective_message.reply_text(gate_message)
        return

    await update.effective_message.reply_text(
        "How would you like to submit your filled card?",
        reply_markup=_mode_keyboard(),
    )


async def bingo_mode_button(update, context):
    query = update.callback_query
    await query.answer()
    try:
        _, mode = query.data.split(":", 1)
    except (ValueError, AttributeError):
        return
    if mode not in ("photo", "text"):
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may be too old to edit

    uid = query.from_user.id
    gate_message = _bingo_gate_message(uid)
    if gate_message:
        # send via the bot API rather than query.message.reply_text: the
        # tapped message may be too old for Telegram to consider "accessible"
        # (same case the edit above already guards), and an inaccessible
        # message has no reply_text method at all.
        await context.bot.send_message(chat_id=uid, text=gate_message)
        return

    # arming one mode must clear the other -- otherwise a user who taps
    # "Photo" on one /submit_bingo prompt and "Text" on an earlier one (both
    # gates passed since neither had created a submission yet) ends up with
    # both flags armed, and a later unrelated message -- or, after the retry
    # cooldown lapses, a second real submission -- gets silently processed by
    # the stale mode's handler.
    if mode == "photo":
        context.user_data["awaiting_bingo"] = True
        context.user_data["awaiting_bingo_text"] = False
        await context.bot.send_message(
            chat_id=uid,
            text="Send me a photo of your filled bingo card (as a photo or an "
                 "image file) and I'll check it. 📸",
        )
    else:
        context.user_data["awaiting_bingo_text"] = True
        context.user_data["awaiting_bingo"] = False
        sheet_no = storage.get_bingo_sheet(uid)
        await context.bot.send_message(
            chat_id=uid,
            text="Reply with this list, adding an @handle after the dash for "
                 "every square you've matched (leave the rest blank):\n\n"
                 + bingo_text.build_template_text(sheet_no),
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

    # OCR can take up to a minute, so let the player know their card arrived.
    await update.effective_message.reply_text(
        "Got your card! 🔍 Scanning it now — this can take up to a minute…"
    )

    # Scan in an isolated subprocess so onnxruntime can never freeze the bot; a
    # timeout/kill just asks the player to retry.
    read = await _run_ocr(sheet_no, image_bytes)
    if read is None:
        await update.effective_message.reply_text(
            "I couldn't finish scanning that in time — please try again in a minute 🙏"
        )
        return

    # Never act on a raw OCR read -- show it back as text and let the player
    # confirm or correct it first (bingo_ocr_confirm_button).
    context.user_data["bingo_ocr_pending"] = {
        "sheet_no": sheet_no, "read": read, "handle": handle,
    }
    preview = bingo_text.build_prefilled_text(sheet_no, read.get("cells", []))
    await update.effective_message.reply_text(
        "Here's what I read off your card — only the squares I'm confident "
        "about are filled in:\n\n" + preview + "\n\nDoes that look right?",
        reply_markup=_ocr_confirm_keyboard(),
    )


async def on_bingo_text(update, context):
    if not context.user_data.get("awaiting_bingo_text"):
        return  # not expecting typed handles from this user; ignore
    context.user_data["awaiting_bingo_text"] = False

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

    read = bingo_text.parse_submission(
        sheet_no, update.effective_message.text or "", _roster_index()
    )
    await bingo_queue.enqueue(context, uid, handle, sheet_no, read)


# ---------------------------------------------------------------------------
# OCR preview confirmation — "does this look right?" before acting on a read
# ---------------------------------------------------------------------------

async def bingo_ocr_confirm_button(update, context):
    """Yes/No on the OCR preview shown after on_bingo_image.

    Yes proceeds with the original OCR read exactly as scanned (via
    bingo_queue.enqueue, same as the text path). No discards it and re-arms
    awaiting_bingo_text with that same read pre-filled as text (see
    bingo_text.build_prefilled_text), so the player corrects only the wrong
    or missing squares instead of re-photographing the whole card.
    """
    query = update.callback_query
    await query.answer()
    try:
        _, ans = query.data.split(":", 1)
    except (ValueError, AttributeError):
        return
    if ans not in ("yes", "no"):
        return

    pending = context.user_data.pop("bingo_ocr_pending", None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may be too old to edit

    if pending is None:
        return  # stale button -- already actioned, or user_data reset since

    uid = query.from_user.id
    # state (closed/won/another submission started meanwhile) can go stale
    # between the OCR preview rendering and the tap, same as bingo_mode_button.
    gate_message = _bingo_gate_message(uid)
    if gate_message:
        await context.bot.send_message(chat_id=uid, text=gate_message)
        return

    if ans == "no":
        context.user_data["awaiting_bingo_text"] = True
        preview = bingo_text.build_prefilled_text(
            pending["sheet_no"], pending["read"].get("cells", [])
        )
        await context.bot.send_message(
            chat_id=uid,
            text="No worries — fix the @handles below and send the whole "
                 "list back to me:\n\n" + preview,
        )
        return

    await bingo_queue.enqueue(
        context, uid, pending["handle"], pending["sheet_no"], pending["read"])


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
        from handlers import bingo_queue
        await bingo_queue.maybe_kickoff(context)
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
    app.add_handler(CallbackQueryHandler(bingo_mode_button, pattern=r"^bingomode:"))
    app.add_handler(CallbackQueryHandler(bingo_ocr_confirm_button, pattern=r"^bingoocr:"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE),
        on_bingo_image,
    ))
    # group=1, NOT the default group 0: handlers/provisioning.py registers a
    # MessageHandler with the IDENTICAL filter (private text, non-command) in
    # the default group for its awaiting_email flow, and main.py registers
    # bingo.register(app) before provisioning.register(app). Within one PTB
    # group, only the first handler whose filter matches gets invoked, and
    # that group's dispatch stops there for the update -- a handler's own
    # "return early if my flag isn't set" guard does NOT let a later handler
    # in the SAME group also run. Putting this in group=1 makes PTB evaluate
    # both handlers independently for every private text message; each still
    # no-ops via its own user_data flag when not relevant.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        on_bingo_text,
    ), group=1)
