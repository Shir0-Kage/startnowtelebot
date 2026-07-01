"""Quest guide: /quests, /quest <name>, and the inline buttons."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import storage
from data.quests import QUESTS, QUESTS_BY_KEY, find_quest


def _overview_text():
    lines = ["🗺️ <b>StartNOW! Quest Locations</b>\n"]
    for q in QUESTS:
        lines.append(f"{q['emoji']} {q['name']} — {q['location']}")
    lines.append(
        "\nAll quests are self-paced, so feel free to explore whenever "
        "you're free!"
    )
    return "\n".join(lines)


def _overview_keyboard():
    # a couple of shortcut buttons to the more-asked-about quests
    rows = [
        [
            InlineKeyboardButton("📚 NUSC Acads", callback_data="quest:acads"),
            InlineKeyboardButton("🎪 Interest Groups", callback_data="quest:interest"),
        ],
        [
            InlineKeyboardButton("🎉 Happenings", callback_data="quest:happenings"),
            InlineKeyboardButton("🏠 Houses", callback_data="quest:houses"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _detail_text(q):
    return (
        f"{q['emoji']} <b>{q['name']}</b>\n\n"
        f"📍 <b>Where:</b> {q['location']}\n\n"
        f"{q['blurb']}"
    )


def _detail_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ All quest locations", callback_data="quest:all")]]
    )


async def quests_command(update, context):
    chat = update.effective_chat
    if chat:
        storage.ensure_group(chat.id, chat.title or "")
    await update.effective_message.reply_html(
        _overview_text(), reply_markup=_overview_keyboard()
    )


async def quest_command(update, context):
    """/quest <name> — details for a single quest."""
    if not context.args:
        await update.effective_message.reply_text(
            "Which quest? Try e.g. /quest acads, or /quests to see them all 🙂"
        )
        return

    q = find_quest(" ".join(context.args))
    if not q:
        await update.effective_message.reply_text(
            "Hmm, I don't recognise that quest. Use /quests to see the full list!"
        )
        return

    await update.effective_message.reply_html(
        _detail_text(q), reply_markup=_detail_keyboard()
    )


async def quest_button(update, context):
    """Handle taps on the quest inline buttons."""
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]

    if key == "all":
        await query.edit_message_text(
            _overview_text(),
            parse_mode="HTML",
            reply_markup=_overview_keyboard(),
        )
        return

    q = QUESTS_BY_KEY.get(key)
    if not q:
        return
    await query.edit_message_text(
        _detail_text(q),
        parse_mode="HTML",
        reply_markup=_detail_keyboard(),
    )


def register(app):
    app.add_handler(CommandHandler("quests", quests_command))
    app.add_handler(CommandHandler("quest", quest_command))
    app.add_handler(CallbackQueryHandler(quest_button, pattern=r"^quest:"))
