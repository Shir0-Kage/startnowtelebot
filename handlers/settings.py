"""Per-group settings: AM/PM slot and reminder toggle."""

from telegram.ext import CommandHandler

import storage
from utils.auth import facil_only

SLOT_LABEL = {"AM": "AM ☀️", "PM": "PM 🌙", "unset": "not set yet"}


def _group_only(update):
    chat = update.effective_chat
    return chat is not None and chat.type in ("group", "supergroup")


@facil_only
async def setslot_command(update, context):
    chat = update.effective_chat
    if not _group_only(update):
        await update.effective_message.reply_text(
            "This only makes sense inside a group chat 🙂"
        )
        return

    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("am", "pm"):
        await update.effective_message.reply_text(
            "Usage: /setslot am  or  /setslot pm"
        )
        return

    slot = arg.upper()
    storage.ensure_group(chat.id, chat.title or "")
    storage.set_slot(chat.id, slot)
    await update.effective_message.reply_text(
        f"Got it — this group is now the <b>{slot}</b> slot. "
        f"Meet-up reminders will use the {slot} timings. ✅",
        parse_mode="HTML",
    )


async def slot_command(update, context):
    chat = update.effective_chat
    if not _group_only(update):
        await update.effective_message.reply_text(
            "Slots are a group-chat thing — add me to your orientation group 🙂"
        )
        return

    storage.ensure_group(chat.id, chat.title or "")
    slot = storage.get_slot(chat.id)
    if slot == "unset":
        await update.effective_message.reply_text(
            "This group doesn't have a slot yet. A facil can set one with "
            "/setslot am or /setslot pm."
        )
    else:
        await update.effective_message.reply_text(
            f"This group is on the <b>{SLOT_LABEL[slot]}</b> slot.",
            parse_mode="HTML",
        )


@facil_only
async def reminders_command(update, context):
    chat = update.effective_chat
    if not _group_only(update):
        await update.effective_message.reply_text(
            "Reminders are set per group chat 🙂"
        )
        return

    arg = (context.args[0].lower() if context.args else "")
    storage.ensure_group(chat.id, chat.title or "")

    if arg not in ("on", "off"):
        g = storage.get_group(chat.id)
        state = "on ✅" if g and g["reminders_enabled"] else "off 🔕"
        await update.effective_message.reply_text(
            f"Reminders are currently {state}.\nUse /reminders on or "
            f"/reminders off to change it."
        )
        return

    storage.set_reminders(chat.id, arg == "on")
    if arg == "on":
        await update.effective_message.reply_text("Reminders are on ✅")
    else:
        await update.effective_message.reply_text("Reminders are off 🔕")


def register(app):
    app.add_handler(CommandHandler("setslot", setslot_command))
    app.add_handler(CommandHandler("slot", slot_command))
    app.add_handler(CommandHandler("reminders", reminders_command))
