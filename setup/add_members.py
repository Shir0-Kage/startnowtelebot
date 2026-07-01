"""Phase 2: add the teammates who've messaged the bot /start, and give them
their admin titles.

The bot writes everyone who /starts into the 'started_users' table in bot.db.
This script reads that list and, for each roster member who has checked in, adds
them to all 20 groups and promotes them. Idempotent — re-running only does
what's left.

    python -m setup.add_members             # add everyone ready right now
    python -m setup.add_members --watch      # keep adding as people check in
    python -m setup.add_members --dry-run    # show who would be added
"""

import argparse
import asyncio

from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import ChatAdminRights

import storage
from setup import manifest, roster
from setup.client import PHONE, build_client

THROTTLE = 3
WATCH_INTERVAL = 30

MEMBER_RIGHTS = ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=False,
    manage_call=True,
)


def _started_usernames():
    """Lowercased usernames of everyone who's messaged the bot /start."""
    return {r["username"] for r in storage.get_started() if r["username"]}


def pending():
    """Roster members who are ready but not yet in every group.
    Returns (manifest_data, [(admin, [group_title, ...]), ...])."""
    ready = _started_usernames()
    data = manifest.load()
    out = []
    for admin in roster.added_admins():
        uname = admin["username"].lower()
        if uname not in ready:
            continue
        missing = [
            title
            for title, e in data.items()
            if e.get("chat_id")
            and uname not in [m.lower() for m in e.get("members_added", [])]
        ]
        if missing:
            out.append((admin, missing))
    return data, out


async def _invite_link(client, channel):
    res = await client(ExportChatInviteRequest(channel))
    return res.link


async def _cycle(client):
    """One pass: add whoever is ready. Returns number of (person, group) adds."""
    data, todo = pending()
    added = 0
    for admin, groups in todo:
        uname = admin["username"]
        try:
            user = await client.get_input_entity(uname)
        except Exception as exc:
            print(f"  can't resolve @{uname} ({exc}); skipping for now")
            continue

        for title in groups:
            entry = data[title]
            try:
                channel = await client.get_entity(entry["chat_id"])
                try:
                    await client(InviteToChannelRequest(channel, [user]))
                except UserAlreadyParticipantError:
                    pass
                await client(EditAdminRequest(channel, user, MEMBER_RIGHTS, rank=admin["title"]))
                entry.setdefault("members_added", []).append(uname)
                manifest.save(data)
                added += 1
                print(f"added @{uname} to {title}")
            except UserPrivacyRestrictedError:
                link = await _invite_link(client, channel)
                print(f"  @{uname}: privacy blocks a direct add to {title} — send them {link}")
            except FloodWaitError as e:
                print(f"hit a flood wait ({e.seconds}s) — progress saved, pausing")
                manifest.save(data)
                await asyncio.sleep(e.seconds + 5)
            except Exception as exc:
                # one bad group shouldn't abort the run (esp. under --watch).
                # members_added is only written on success, so it retries later.
                print(f"  couldn't add @{uname} to {title} ({exc}); will retry")
            await asyncio.sleep(THROTTLE)
    return added


async def run(dry_run, watch):
    storage.init_db()

    if dry_run:
        _, todo = pending()
        if not todo:
            print("nobody is ready to add yet.")
            return
        for admin, groups in todo:
            print(f"would add @{admin['username']} ({admin['title']}) to {len(groups)} group(s)")
        print("dry run — nothing changed.")
        return

    client = build_client()
    await client.start(**({"phone": PHONE} if PHONE else {}))
    try:
        if watch:
            print("watching for /start check-ins — Ctrl-c to stop")
            try:
                while True:
                    try:
                        await _cycle(client)
                    except Exception as exc:
                        print(f"cycle error ({exc}); will retry next tick")
                    await asyncio.sleep(WATCH_INTERVAL)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("stopped watching.")
        else:
            n = await _cycle(client)
            print(f"done — {n} add(s) this run." if n else "nobody new to add.")
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Add teammates who've /started.")
    parser.add_argument("--dry-run", action="store_true", help="show who would be added")
    parser.add_argument("--watch", action="store_true", help="keep running and add people as they check in")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run, args.watch))


if __name__ == "__main__":
    main()
