"""Fulfils /add_year_ones requests: adds each group's Year 1s from the sheet.

A facil runs /add_year_ones in their group; the bot queues it; this worker
(running as the owner account) adds that group's Year 1s. Anyone who can't be
added directly gets a DM with the group's invite link.

    python -m setup.add_year_ones             # process everything queued now
    python -m setup.add_year_ones --watch      # keep processing as requests arrive
    python -m setup.add_year_ones --dry-run    # show what would happen
"""

import argparse
import asyncio

from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest

import storage
from setup import sheets
from setup.client import PHONE, build_client

THROTTLE = 3
WATCH_INTERVAL = 20

DM_TEMPLATE = (
    "Hi! 🌟 You've been placed in your StartNOW! 2026 orientation group ({og}).\n\n"
    "Join your OG here: {link}\n\nSee you there! ❤️"
)


async def _invite_link(client, channel):
    return (await client(ExportChatInviteRequest(channel))).link


async def _process(client, req, roster):
    """Handle one queued request. `roster` is {OG: [members]} fetched this pass."""
    chat_id, og = req["chat_id"], req["og"]
    members = roster.get(og, [])
    if not members:
        print(f"  {og}: no Year 1s found in the sheet — skipping")
        return

    channel = await client.get_entity(chat_id)
    link = None          # exported lazily, only if someone needs it
    added, dmed, unreachable = 0, 0, []

    for m in members:
        handle, name = m["handle"], m["name"]
        if handle and storage.already_added(chat_id, handle):
            continue

        # 1) try to add directly
        if m["addable"]:
            try:
                user = await client.get_input_entity(handle)
                try:
                    await client(InviteToChannelRequest(channel, [user]))
                except UserAlreadyParticipantError:
                    pass
                storage.record_added(chat_id, handle)
                added += 1
                print(f"  added {name} (@{handle}) to {og}")
                await asyncio.sleep(THROTTLE)
                continue
            except UserPrivacyRestrictedError:
                pass  # fall through to the DM path
            except FloodWaitError as e:
                print(f"  flood wait {e.seconds}s — pausing")
                await asyncio.sleep(e.seconds + 5)
                continue
            except Exception as exc:
                print(f"  couldn't add {name} (@{handle}) directly ({exc})")

        # 2) DM them the invite link instead
        try:
            if link is None:
                link = await _invite_link(client, channel)
            target = handle if handle else m["raw_handle"]
            if not target:
                raise ValueError("no handle to message")
            await client.send_message(target, DM_TEMPLATE.format(og=og, link=link))
            dmed += 1
            print(f"  DM'd invite to {name} ({target})")
        except Exception as exc:
            unreachable.append(f"{name} ({m['raw_handle'] or 'no handle'})")
            print(f"  couldn't reach {name} ({exc})")
        await asyncio.sleep(THROTTLE)

    print(f"{og}: added {added}, DM'd {dmed}, couldn't reach {len(unreachable)}")
    if unreachable:
        print("  facil follow-up needed for: " + "; ".join(unreachable))


async def _cycle(client):
    reqs = storage.pending_requests("year_ones")
    if not reqs:
        return 0
    roster = sheets.load_year1_members()  # fetch the sheet once per pass
    for req in reqs:
        try:
            await _process(client, req, roster)
        except Exception as exc:
            print(f"request {req['id']} ({req['og']}) failed ({exc}); will retry")
            continue
        storage.mark_request_done(req["id"])
    return len(reqs)


async def run(dry_run, watch):
    storage.init_db()

    if dry_run:
        reqs = storage.pending_requests("year_ones")
        if not reqs:
            print("no pending /add_year_ones requests.")
            return
        roster = sheets.load_year1_members()
        for req in reqs:
            members = roster.get(req["og"], [])
            addable = sum(1 for m in members if m["addable"])
            print(
                f"{req['og']} (chat {req['chat_id']}): {len(members)} Year 1s — "
                f"{addable} addable, {len(members) - addable} need an invite/DM"
            )
        print("dry run — nothing changed.")
        return

    client = build_client()
    await client.start(**({"phone": PHONE} if PHONE else {}))
    try:
        if watch:
            print("watching for /add_year_ones requests — Ctrl-c to stop")
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
            print(f"processed {n} request(s)." if n else "nothing queued.")
    finally:
        await client.disconnect()


def main():
    p = argparse.ArgumentParser(description="Add Year 1s for queued groups.")
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--watch", action="store_true", help="keep processing new requests")
    args = p.parse_args()
    asyncio.run(run(args.dry_run, args.watch))


if __name__ == "__main__":
    main()
