"""Phase 1: create the 20 groups, add the bot, and set each group's slot.

Run once from the owner account. Safe to re-run — already-created groups are
skipped and half-finished ones are repaired via the manifest. This does NOT add
the other teammates; that's add_members.py, gated on /start.

    python -m setup.create_groups             # do it for real
    python -m setup.create_groups --dry-run   # just print the plan
"""

import argparse
import asyncio

from telethon import utils
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditAdminRequest,
    InviteToChannelRequest,
)
from telethon.tl.types import ChatAdminRights

import storage
from setup import manifest, roster
from setup.client import PHONE, build_client

# seconds to wait between creating groups, to stay under Telegram's spam radar
THROTTLE = 4

# what the bot is allowed to do: post/pin/manage, but not add other admins
BOT_RIGHTS = ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=False,
    manage_call=True,
)

# the owner gets the full set
OWNER_RIGHTS = ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=True,
    manage_call=True,
)


async def _ensure_bot(client, channel, entry):
    """Add the bot and promote it, unless we've already done so."""
    if entry.get("bot_added"):
        return
    bot = await client.get_input_entity(roster.BOT_USERNAME)
    try:
        await client(InviteToChannelRequest(channel=channel, users=[bot]))
    except UserAlreadyParticipantError:
        pass
    await client(EditAdminRequest(channel, bot, BOT_RIGHTS, rank=""))
    entry["bot_added"] = True


async def _set_owner_title(client, channel, entry):
    if entry.get("owner_title_set"):
        return
    me = await client.get_me(input_peer=True)
    try:
        await client(EditAdminRequest(channel, me, OWNER_RIGHTS, rank=roster.owner()["title"]))
        entry["owner_title_set"] = True
    except Exception as exc:
        # Telegram sometimes refuses to edit the creator's own admin entry.
        # Not fatal — the owner can set their title in-app once.
        print(f"  note: couldn't set owner title here ({exc}); set it in-app")


async def _provision(client, group, data):
    title, slot = group["title"], group["slot"]
    entry = manifest.group_entry(data, title)

    if not entry.get("chat_id"):
        result = await client(CreateChannelRequest(title=title, about="", megagroup=True))
        channel = result.chats[0]
        entry["channel_id"] = channel.id
        entry["chat_id"] = utils.get_peer_id(channel)  # -100... form the bot uses
        entry["slot"] = slot
        # tell the bot which slot this group is, straight in its database
        storage.ensure_group(entry["chat_id"], title)
        storage.set_slot(entry["chat_id"], slot)
        manifest.save(data)
        print(f"created {title}  (chat_id {entry['chat_id']})")
        await asyncio.sleep(THROTTLE)
    else:
        channel = await client.get_entity(entry["chat_id"])

    await _ensure_bot(client, channel, entry)
    await _set_owner_title(client, channel, entry)
    manifest.save(data)


async def run(dry_run):
    roster.validate()
    data = manifest.load()
    todo = [g for g in roster.GROUPS if not data.get(g["title"], {}).get("chat_id")]
    print(f"{len(roster.GROUPS)} groups total; {len(todo)} still to create.")

    if dry_run:
        for g in todo:
            print(f"  would create {g['title']}  (slot {g['slot']})")
        print("dry run — nothing was created.")
        return

    storage.init_db()  # make sure the shared tables exist
    client = build_client()
    await client.start(**({"phone": PHONE} if PHONE else {}))
    try:
        for g in roster.GROUPS:
            try:
                await _provision(client, g, data)
            except FloodWaitError as e:
                print(f"hit a flood wait ({e.seconds}s) — progress saved, pausing")
                manifest.save(data)
                await asyncio.sleep(e.seconds + 5)
    finally:
        await client.disconnect()
    print("Phase 1 done. Bot is in every group and slots are set.")


def main():
    parser = argparse.ArgumentParser(description="Create the StartNOW! groups.")
    parser.add_argument("--dry-run", action="store_true", help="show the plan only")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
