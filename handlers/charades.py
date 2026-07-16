"""/charades — hand the player a random word to act out.

The word always goes to the player privately: in a group the bot DMs it and only
posts a no-spoilers confirmation, so nobody else sees what they have to act.
"""

import html
import logging
import random

from telegram.ext import CommandHandler

from data.charades_words import WORDS

log = logging.getLogger(__name__)


def _word_text(word):
    return (f"🎭 Your charades word:\n\n<b>{html.escape(word)}</b>\n\n"
            "Act it out — no talking, no pointing at objects, no spelling! 🤫")


async def charades(update, context):
    chat = update.effective_chat
    user = update.effective_user
    word = random.choice(WORDS)

    # in a DM there's no one to hide it from — just show it
    if chat is None or chat.type == "private":
        await update.effective_message.reply_html(_word_text(word))
        return

    # in a group: DM the word so the rest of the group can still guess
    try:
        await context.bot.send_message(
            chat_id=user.id, text=_word_text(word), parse_mode="HTML")
    except Exception as exc:
        log.warning("couldn't DM a charades word: %s", exc)
        await update.effective_message.reply_text(
            "I couldn't DM you — send me /start privately first, then try "
            "/charades again 🙈")
        return
    await update.effective_message.reply_html(
        f"🎭 Sent {html.escape(user.full_name or 'you')} a word — act it out! 🤫")


def register(app):
    app.add_handler(CommandHandler("charades", charades))
