"""Human Bingo.

For now this only ships /get_bingo — it hands each Year 1 their allocated bingo
card so the game can go live ahead of the checker. The /submit_bingo flow (OCR,
winning-line detection and confirmations) is added on top of this module later.
"""

import logging

from telegram.ext import CommandHandler

import storage
from data import bingo_templates
from setup import sheets

log = logging.getLogger(__name__)

_roster_handles = None   # set of Year 1 @handles (lowercased, no @)


def _load_roster_handles():
    """Load the Year 1 handle set once (cached). Leaves the cache empty and
    retries next time if the sheet can't be reached, so a transient failure
    doesn't lock everyone out permanently."""
    global _roster_handles
    if _roster_handles is not None:
        return
    try:
        handles = set()
        for members in sheets.load_year1_members().values():
            for m in members:
                if m.get("handle"):
                    handles.add(m["handle"])
        _roster_handles = handles
    except Exception as exc:
        log.warning("couldn't load Year 1 roster for bingo: %s", exc)


def _is_year1(username):
    _load_roster_handles()
    return sheets.normalize_handle(username or "") in (_roster_handles or set())


async def get_bingo(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me /get_bingo and I'll send you your Human Bingo card 🎉"
        )
        return

    user = update.effective_user
    if not _is_year1(user.username if user else None):
        await update.effective_message.reply_text(
            "Hmm, I couldn't find you on the Year 1 list, so I can't hand you a "
            "bingo card 😕\n\nMake sure your Telegram @username matches the one you "
            "signed up with, or check with a facil."
        )
        return

    sheet_no = storage.allocate_bingo_sheet(user.id, (user.username or "").lower())
    path = bingo_templates.template_path(sheet_no)
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


def register(app):
    app.add_handler(CommandHandler("get_bingo", get_bingo))
