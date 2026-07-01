"""Attendance collection.

This is NOT an RSVP. A facil opens an attendance check during/after an event,
participants tap a button, and we record who actually showed up — once each.
"""

import csv
import io

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import CallbackQueryHandler, CommandHandler

import storage
from data import events
from utils.auth import facil_only, is_facilitator
from utils.text import chunk_text, display_name


def _event_label(key):
    ev = events.EVENTS_BY_KEY.get(key)
    return ev["short"] if ev else key


def _check_text(ev):
    return (
        f"📋 <b>Attendance Check: {ev['short']}</b>\n\n"
        "If you're here for today's session, tap the button below to mark your "
        "attendance!"
    )


def _check_keyboard(key):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Mark Attendance", callback_data=f"attmark:{key}")]]
    )


async def _open_check(update, context, ev):
    """Post the attendance check for an event and mark the session open."""
    chat = update.effective_chat
    storage.ensure_group(chat.id, chat.title or "")
    storage.open_attendance(chat.id, ev["key"])
    await context.bot.send_message(
        chat_id=chat.id,
        text=_check_text(ev),
        parse_mode="HTML",
        reply_markup=_check_keyboard(ev["key"]),
    )


# ---------------------------------------------------------------------------
# /attendance
# ---------------------------------------------------------------------------

async def attendance_command(update, context):
    chat = update.effective_chat
    storage.ensure_group(chat.id, chat.title or "")

    # /attendance <event> — facil opens a check straight away
    if context.args:
        if not await is_facilitator(update, context):
            await update.effective_message.reply_text(
                "Only facils can open an attendance check 🙏"
            )
            return
        ev = events.find_event(" ".join(context.args))
        if not ev:
            await update.effective_message.reply_text(
                "I don't recognise that event. Try /attendance to see the list."
            )
            return
        await _open_check(update, context, ev)
        return

    # /attendance — show a picker
    rows = []
    pool = events.MEETUPS + events.ENGAGEMENTS
    for i in range(0, len(pool), 2):
        row = [
            InlineKeyboardButton(ev["short"], callback_data=f"attopen:{ev['key']}")
            for ev in pool[i : i + 2]
        ]
        rows.append(row)

    await update.effective_message.reply_text(
        "📋 <b>Attendance</b>\n\nFacils — pick an event to open an attendance "
        "check for this group:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def attopen_button(update, context):
    """Facil tapped an event in the /attendance picker."""
    query = update.callback_query
    if not await is_facilitator(update, context):
        await query.answer("Only facils can open attendance 🙏", show_alert=True)
        return

    await query.answer()
    key = query.data.split(":", 1)[1]
    ev = events.EVENTS_BY_KEY.get(key)
    if not ev:
        return
    await _open_check(update, context, ev)


# ---------------------------------------------------------------------------
# Marking (button tap by participants)
# ---------------------------------------------------------------------------

async def attmark_button(update, context):
    query = update.callback_query
    key = query.data.split(":", 1)[1]
    chat = update.effective_chat
    user = update.effective_user

    if not storage.is_open(chat.id, key):
        await query.answer(
            "Attendance for this event is closed 🙏", show_alert=True
        )
        return

    slot = storage.get_slot(chat.id)
    is_new = storage.mark_attendance(
        chat_id=chat.id,
        event_key=key,
        user_id=user.id,
        display_name=display_name(user),
        username=user.username or "",
        slot=slot,
    )

    if is_new:
        await query.answer("Attendance marked — thank you! ❤️")
    else:
        await query.answer("You're already marked down — thanks! ❤️")


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

async def attendance_summary_command(update, context):
    chat = update.effective_chat
    storage.ensure_group(chat.id, chat.title or "")

    # /attendance_summary <event> — detailed present list
    if context.args:
        ev = events.find_event(" ".join(context.args))
        if not ev:
            await update.effective_message.reply_text(
                "I don't recognise that event. Try /attendance_summary on its "
                "own to see what's been collected."
            )
            return
        await _send_event_summary(update, chat.id, ev)
        return

    # /attendance_summary — overview across events
    counts = storage.attendance_counts(chat.id)
    if not counts:
        await update.effective_message.reply_text(
            "No attendance collected in this group yet. Facils can start one "
            "with /attendance 🙂"
        )
        return

    lines = ["📊 <b>Attendance so far</b>\n"]
    for key, n in counts:
        lines.append(f"• {_event_label(key)} — {n} marked present")
    lines.append(
        "\nUse /attendance_summary &lt;event&gt; to see the full name list."
    )
    await update.effective_message.reply_html("\n".join(lines))


async def _send_event_summary(update, chat_id, ev):
    records = storage.get_attendance(chat_id, ev["key"])
    header = f"📊 <b>Attendance Summary: {ev['short']}</b>\n\n"

    if not records:
        await update.effective_message.reply_html(
            header + "No one's been marked present yet."
        )
        return

    lines = [header, f"Total marked present: <b>{len(records)}</b>\n", "Present:"]
    for r in records:
        name = r["display_name"] or "Someone"
        handle = f" (@{r['username']})" if r["username"] else ""
        lines.append(f"• {name}{handle}")
    lines.append("\nUse this to help keep track of who attended.")

    # long lists get split across messages
    full = "\n".join(lines)
    for piece in chunk_text(full):
        await update.effective_message.reply_html(piece)


# ---------------------------------------------------------------------------
# Facil admin: close / clear / export
# ---------------------------------------------------------------------------

@facil_only
async def close_attendance_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /close_attendance <event>, e.g. /close_attendance meetup1"
        )
        return
    ev = events.find_event(" ".join(context.args))
    if not ev:
        await update.effective_message.reply_text("I don't recognise that event.")
        return

    chat = update.effective_chat
    storage.close_attendance(chat.id, ev["key"])
    await update.effective_message.reply_text(
        f"Attendance for {ev['short']} is now closed. No more taps will count. ✅"
    )


@facil_only
async def clear_attendance_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /clear_attendance <event>, e.g. /clear_attendance meetup1"
        )
        return
    ev = events.find_event(" ".join(context.args))
    if not ev:
        await update.effective_message.reply_text("I don't recognise that event.")
        return

    chat = update.effective_chat
    removed = storage.clear_attendance(chat.id, ev["key"])
    await update.effective_message.reply_text(
        f"Cleared {removed} record(s) for {ev['short']}. Starting fresh. 🧹"
    )


@facil_only
async def export_attendance_command(update, context):
    chat = update.effective_chat
    storage.ensure_group(chat.id, chat.title or "")
    records = storage.all_attendance(chat.id)

    if not records:
        await update.effective_message.reply_text(
            "Nothing to export yet — no attendance collected in this group."
        )
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["event", "user_id", "display_name", "username", "slot", "marked_at"]
    )
    for r in records:
        writer.writerow(
            [
                _event_label(r["event_key"]),
                r["user_id"],
                r["display_name"],
                r["username"],
                r["slot"],
                r["marked_at"],
            ]
        )

    data = buf.getvalue().encode("utf-8-sig")  # BOM so Excel reads UTF-8 cleanly
    filename = f"attendance_{chat.id}.csv"
    await context.bot.send_document(
        chat_id=chat.id,
        document=InputFile(io.BytesIO(data), filename=filename),
        caption="Here's this group's attendance so far 📎",
    )


def register(app):
    app.add_handler(CommandHandler("attendance", attendance_command))
    app.add_handler(CommandHandler("attendance_summary", attendance_summary_command))
    app.add_handler(CommandHandler("close_attendance", close_attendance_command))
    app.add_handler(CommandHandler("clear_attendance", clear_attendance_command))
    app.add_handler(CommandHandler("export_attendance", export_attendance_command))
    app.add_handler(CallbackQueryHandler(attmark_button, pattern=r"^attmark:"))
    app.add_handler(CallbackQueryHandler(attopen_button, pattern=r"^attopen:"))
