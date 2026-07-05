"""Add facils to their AM/PM groups and make them admins.

Handles and group assignments live in two different sheets that only share
names, so this first writes a reconciliation report for you to eyeball, then
(with --commit) adds everyone it matched confidently.

    python -m setup.add_facils              # write the match report, add nobody
    python -m setup.add_facils --commit      # add the matched facils + promote

Review setup/facil_match_report.csv between those two steps.
"""

import argparse
import asyncio
import csv
import json
import os
from itertools import groupby

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import ChatAdminRights

from setup import manifest, sheets
from setup.client import start_client

# adding users is heavily rate-limited; go slow to avoid tripping PeerFloodError
THROTTLE = 10           # seconds between adds within a group
GROUP_DELAY = 60        # seconds to wait between groups
FLOOD_CAP = 3           # consecutive non-contact rejections before we stop adding
REPORT_PATH = os.path.join(os.path.dirname(__file__), "facil_match_report.csv")
ADDED_PATH = os.path.join(os.path.dirname(__file__), "facil_added.json")

INVITE_MSG = (
    "Hi {name}! 🌟 You're a StartNOW! 2026 facil for {og}. "
    "Join your group here: {link}"
)

FACIL_RIGHTS = ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=False,
    manage_call=True,
)


# The facil name→handle join (and the hand-filled overrides for the few names
# it can't resolve) lives in setup.sheets, so the bot can reuse it on /start
# without importing this Telethon-heavy module. See sheets.match_facils and
# sheets.FACIL_HANDLE_OVERRIDES.


def write_report(rows):
    with open(REPORT_PATH, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "group", "house", "handle", "status"])
        w.writeheader()
        w.writerows(rows)


def _summary(rows):
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def _load_added():
    if os.path.exists(ADDED_PATH):
        with open(ADDED_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _save_added(data):
    with open(ADDED_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def build_rows():
    facils = sheets.parse_facil_groups(
        sheets.fetch_csv(sheets.FACIL_GROUP_SHEET_ID, sheets.FACIL_GROUP_TAB)
    )
    handles = sheets.load_facil_handles()
    return sheets.match_facils(facils, handles)


def _og_order(group):
    return (0 if group[:2].upper() == "AM" else 1, int(group[2:]))


def _mark_added(added, chat_id, handle):
    added.setdefault(chat_id, []).append(handle)
    _save_added(added)


async def _get_link(client, groups, title):
    """Group invite link — reuse the stored one, else make + remember it."""
    entry = groups[title]
    if entry.get("invite_link"):
        return entry["invite_link"]
    peer = await client.get_entity(entry["chat_id"])
    link = (await client(ExportChatInviteRequest(peer))).link
    entry["invite_link"] = link
    manifest.save(groups)
    return link


async def _try_add(client, r, groups, added):
    """Try to add + promote one facil directly. Returns a status string."""
    title = f"StartNOW! {r['group']}"
    entry = groups.get(title)
    if not entry or not entry.get("chat_id"):
        print(f"  no group created for {title} yet — skipping {r['name']}")
        return "no_group"
    chat_id = str(entry["chat_id"])
    if r["handle"] in added.get(chat_id, []):
        return "skipped"

    # resolve the username first — a bad handle is a data issue, not flood
    try:
        user = await client.get_input_entity(r["handle"])
    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
        print(f"  @{r['handle']} ({r['name']}) — no such username; fix the handle")
        return "bad_handle"

    try:
        channel = await client.get_entity(entry["chat_id"])
        try:
            result = await client(InviteToChannelRequest(channel, [user]))
        except UserAlreadyParticipantError:
            _mark_added(added, chat_id, r["handle"])
            return "added"
        # newer Telegram reports privacy-blocked users here instead of raising
        if getattr(result, "missing_invitees", None):
            print(f"  @{r['handle']} ({r['name']}) — privacy blocks a direct add")
            return "cant_add"
        await client(EditAdminRequest(channel, user, FACIL_RIGHTS, rank="Facil"))
        _mark_added(added, chat_id, r["handle"])
        print(f"  added @{r['handle']} to {title} as admin")
        return "added"
    except UserPrivacyRestrictedError:
        print(f"  @{r['handle']} ({r['name']}) — privacy blocks a direct add")
        return "cant_add"
    except PeerFloodError:
        return "flood"
    except FloodWaitError as e:
        print(f"  flood wait {e.seconds}s — pausing")
        await asyncio.sleep(e.seconds + 5)
        return "floodwait"
    except Exception as exc:
        print(f"  couldn't add {r['name']} (@{r['handle']}) to {title} ({exc})")
        return "error"


async def _dm_invite(client, r, groups):
    """DM a facil their group's invite link. On Telegram Premium this reaches
    non-contacts too. Returns 'invited' or 'unreachable'."""
    title = f"StartNOW! {r['group']}"
    entry = groups.get(title)
    if not entry or not entry.get("chat_id"):
        return "unreachable"
    try:
        link = await _get_link(client, groups, title)
        await client.send_message(
            r["handle"], INVITE_MSG.format(name=r["name"], og=r["group"], link=link)
        )
        print(f"  DM'd invite link to @{r['handle']} ({r['name']})")
        return "invited"
    except PeerFloodError:
        # messaging non-contacts is rate-limited too (Premium doesn't lift this)
        return "dm_flood"
    except Exception as exc:
        print(f"  couldn't DM @{r['handle']} ({exc})")
        return "unreachable"


async def _commit(rows, group_delay, only, client=None):
    groups = manifest.load()  # title -> {chat_id, ...}
    added = _load_added()
    matched = [r for r in rows if r["status"] == "matched"]
    if only:
        matched = [r for r in matched if r["group"].upper() == only.upper()]
        if not matched:
            print(f"no matched facils for {only}.")
            return
    matched.sort(key=lambda r: _og_order(r["group"]))
    by_group = [(og, list(it)) for og, it in groupby(matched, key=lambda r: r["group"])]
    print(f"{len(matched)} facil(s) across {len(by_group)} group(s); "
          f"{group_delay}s between groups.")

    n_added, n_invited, bad, needs_link = 0, 0, set(), []
    consecutive, add_stopped, dm_stopped = 0, False, False
    own_client = client is None
    if own_client:
        client = await start_client()
    try:
        for i, (og, facils) in enumerate(by_group):
            print(f"\n-- {og} ({len(facils)} facil(s)) --")
            for r in facils:
                if add_stopped:
                    status = "cant_add"  # skip the add attempt, go straight to invite
                else:
                    status = await _try_add(client, r, groups, added)
                    if status == "added":
                        consecutive = 0
                    elif status == "flood":
                        consecutive += 1
                        if consecutive >= FLOOD_CAP:
                            add_stopped = True
                            print("  repeated add-limit hits — sending invite links "
                                  "only from here to protect the account.")

                if status == "added":
                    n_added += 1
                    await asyncio.sleep(THROTTLE)
                    continue
                if status == "bad_handle":
                    bad.add(r["handle"].lower())
                    continue
                if status in ("skipped", "no_group"):
                    continue

                # needs an invite link
                who = f"{r['name']} ({r['group']}) @{r['handle']}"
                if dm_stopped:
                    needs_link.append(who)  # no API call — just list it
                    continue
                result = await _dm_invite(client, r, groups)
                if result == "invited":
                    n_invited += 1
                else:
                    needs_link.append(who)
                    if result == "dm_flood":
                        dm_stopped = True
                        print("  DMing is being rate-limited — stopping DMs to "
                              "protect the account; the rest are just listed below.")
                await asyncio.sleep(THROTTLE)
            if not (add_stopped and dm_stopped) and i < len(by_group) - 1:
                print(f"   waiting {group_delay}s before the next group…")
                await asyncio.sleep(group_delay)
    finally:
        if own_client:
            await client.disconnect()

    print(f"\nadded {n_added} directly; DM'd invite links to {n_invited}.")
    if bad:
        print(f"{len(bad)} handle(s) don't resolve — setup.find_handles can suggest fixes.")
    if needs_link:
        print(f"\n{len(needs_link)} facil(s) still need their invite link (can't be "
              "added, and DMing is rate-limited/blocked). Post the links where they "
              "already are (e.g. the facil group):")
        for u in needs_link[:60]:
            print("  -", u)
        print("\n  python -m setup.invite_links   # prints each group's link to post")
    if n_invited:
        print("\nOnce the invited facils have joined: "
              "python -m setup.add_facils --promote")


async def _promote(only, client=None):
    """Promote matched facils who are already IN their group (e.g. joined via an
    invite link) to admin. Promoting a member isn't rate-limited."""
    groups = manifest.load()
    rows = [r for r in build_rows() if r["status"] == "matched"]
    if only:
        rows = [r for r in rows if r["group"].upper() == only.upper()]
    rows.sort(key=lambda r: _og_order(r["group"]))

    n = 0
    own_client = client is None
    if own_client:
        client = await start_client()
    try:
        for og, facils in groupby(rows, key=lambda r: r["group"]):
            want = {r["handle"].lower(): r for r in facils if r["handle"]}
            entry = groups.get(f"StartNOW! {og}")
            if not entry or not entry.get("chat_id"):
                continue
            channel = await client.get_entity(entry["chat_id"])
            async for u in client.iter_participants(channel):
                uname = (u.username or "").lower()
                if uname not in want:
                    continue
                try:
                    await client(EditAdminRequest(channel, u, FACIL_RIGHTS, rank="Facil"))
                    n += 1
                    print(f"  promoted @{uname} in StartNOW! {og}")
                except Exception as exc:
                    print(f"  couldn't promote @{uname} ({exc})")
                await asyncio.sleep(2)
    finally:
        if own_client:
            await client.disconnect()
    print(f"\npromoted {n} facil(s) who have joined.")


async def run_facils(client):
    """Daily task (used by the worker): add facils, then promote whoever's
    joined. Idempotent — skips anyone already added/promoted, so it's safe to
    run every morning until everyone's in."""
    rows = build_rows()
    matched = sum(1 for r in rows if r["status"] == "matched")
    print(f"facil task: {matched} matched facil(s)")
    await _commit(rows, GROUP_DELAY, None, client=client)
    await _promote(None, client=client)


async def run(commit, promote, group_delay, only):
    if promote:
        await _promote(only)
        return

    rows = build_rows()
    write_report(rows)
    print(f"wrote {REPORT_PATH}")
    print("status counts:", _summary(rows))

    if not commit:
        print("\nReview the report, then re-run with --commit to add the "
              "'matched' facils. ('ambiguous' / 'no_handle' need a handle or a "
              "manual fix first.)")
        return
    await _commit(rows, group_delay, only)


def main():
    p = argparse.ArgumentParser(description="Add facils to their groups, group by group.")
    p.add_argument("--commit", action="store_true", help="add the matched facils (contacts)")
    p.add_argument("--promote", action="store_true",
                   help="promote matched facils who've already joined (e.g. via invite link)")
    p.add_argument("--group-delay", type=int, default=GROUP_DELAY,
                   help="seconds to wait between groups (default %(default)s)")
    p.add_argument("--only", default=None, help="just one group, e.g. --only AM1")
    args = p.parse_args()
    asyncio.run(run(args.commit, args.promote, args.group_delay, args.only))


if __name__ == "__main__":
    main()
