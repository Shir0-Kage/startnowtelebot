"""Manual prize round. DM every card-holder to forward their earliest bingo
submission; relay each forwarded card — with the sender's @handle and the card's
ORIGINAL send time — to the vetter (@zzehao) for manual review; and let the
vetter confirm the winners with /confirm_bingo_winners. No OCR and no automatic
winner selection: the vetter decides, then vets the announcement before posting
it to the channel.
"""

import logging

import config
import storage
from setup import sheets

log = logging.getLogger(__name__)

# The organiser who vets forwarded submissions and confirms the winners.
VETTER_HANDLE = "zzehao"

_START_TEXT = (
    "Congratulations, you have been chosen as a potential candidate for the "
    "winner of StartNOW!'s Social Bingo! 🎉\n\n"
    "Forward your earliest bingo submission to this chat to verify your "
    "submission results!")

_ACK_TEXT = ("Thank you for your submission! Please await while we process your "
             "submission. 🙏")


def _fmt_ts(dt):
    """Portable 'DD Mon YYYY, HH:MM AM/PM' in the programme timezone (avoids the
    platform-specific %-d/%-I strftime flags so it also runs on Windows tests)."""
    if dt is None:
        return "unknown time"
    local = dt.astimezone(config.TIMEZONE) if getattr(dt, "tzinfo", None) else dt
    return local.strftime("%d %b %Y, %I:%M %p")


def _vetter_id():
    return storage.user_id_for_handle(VETTER_HANDLE)


async def begin_round(context):
    """Open the round: DM every card-holder the invite to forward their earliest
    card. Returns the number DM'd, or -1 if a round is already open."""
    phase = storage.forward_phase()
    if phase == "collecting":
        return -1                                  # already collecting
    if phase is not None:
        storage.reset_forward_round()              # clear a released/stale round
    storage.set_forward_phase("collecting")
    n = 0
    for a in storage.all_bingo_allocations():
        try:
            await context.bot.send_message(chat_id=a["user_id"], text=_START_TEXT)
            n += 1
        except Exception:
            pass
    return n


async def on_forwarded_card(update, context):
    """A card-holder forwards their earliest submission while the round is open:
    thank them, and relay the card to the vetter with their @handle and the
    card's original send time."""
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return
    if storage.forward_phase() != "collecting":
        return
    message = update.effective_message
    if not (message.photo or message.document):
        return

    user = update.effective_user
    handle = sheets.normalize_handle(user.username) or (user.username or "unknown")
    forward_origin = message.forward_origin
    original = forward_origin.date if forward_origin else message.date

    await message.reply_text(_ACK_TEXT)

    vetter = _vetter_id()
    if vetter is None:
        log.warning("no vetter (@%s) reachable to relay a bingo submission",
                    VETTER_HANDLE)
        return
    note = (f"📥 Bingo submission from @{handle}\n"
            f"🕒 Originally sent: {_fmt_ts(original)}")
    try:
        # copy (not forward) so it renders as a normal image regardless of the
        # source type; the note carries the handle + original time explicitly.
        await context.bot.copy_message(
            chat_id=vetter, from_chat_id=chat.id, message_id=message.message_id)
        await context.bot.send_message(chat_id=vetter, text=note)
    except Exception as exc:
        log.warning("couldn't relay bingo submission to the vetter: %s", exc)


async def confirm_winners(context, handles):
    """DM each confirmed winner a congratulations, then send the vetter an
    announcement listing the winners (for them to vet + post to the channel).
    Closes the round. Returns (winners, unreachable) where winners is a list of
    (handle, user_id) and unreachable is a list of handles we couldn't DM."""
    seen = []
    winners = []       # (handle, user_id)
    unreachable = []   # handle
    for raw in handles:
        h = sheets.normalize_handle(raw) or (raw or "").lstrip("@").lower()
        if not h or h in seen:
            continue
        seen.append(h)
        uid = storage.user_id_for_handle(h)
        if uid is None:
            unreachable.append(h)
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="🏆 BINGO! Congratulations — you've won StartNOW!'s Social "
                     "Bingo! 🎉 A facil will be in touch about your prize.")
            winners.append((h, uid))
        except Exception as exc:
            log.warning("couldn't DM a confirmed winner: %s", exc)
            unreachable.append(h)

    storage.set_forward_phase("released")           # close the round

    vetter = _vetter_id()
    if vetter is not None and winners:
        listed = "\n".join(f"🏆 @{h}" for h, _ in winners)
        announce = (
            "🎉 Congratulations to the winners of StartNOW!'s Social Bingo! 🎉\n\n"
            f"{listed}\n\n"
            "Amazing work, everyone — thank you for playing! 🥳\n\n"
            "— (Review this, then post it to the channel when you're ready.)")
        try:
            await context.bot.send_message(chat_id=vetter, text=announce)
        except Exception as exc:
            log.warning("couldn't send the winner announcement to the vetter: %s", exc)

    return winners, unreachable


def register(app):
    # The manual flow has no inline callbacks; the forwarded-card MessageHandler
    # and the commands are wired in handlers/bingo.py.
    pass


def rearm(app):
    # Nothing time-based to re-arm in the manual flow.
    pass
