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
)
from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
from telethon.tl.types import ChatAdminRights

from setup import manifest, sheets
from setup.client import start_client

# adding users is heavily rate-limited; go slow to avoid tripping PeerFloodError
THROTTLE = 10           # seconds between adds within a group
GROUP_DELAY = 60        # seconds to wait between groups
FLOOD_CAP = 3           # consecutive non-contact rejections before we stop adding
REPORT_PATH = os.path.join(os.path.dirname(__file__), "facil_match_report.csv")
ADDED_PATH = os.path.join(os.path.dirname(__file__), "facil_added.json")

FACIL_RIGHTS = ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=False,
    manage_call=True,
)


# Handles the name-join can't resolve on its own — people missing from the
# assessment sheet, or names too ambiguous to pick. Filled in by hand; keyed by
# the facil's name exactly as it appears in the grouping tab.
HANDLE_OVERRIDES = {
    "Ma Anqi": "aqueous27",
    "Jian YiXuan": "yeet_suan",
    "Lau Yi Xuan": "itisyixuan",
    "Kong Jing Yee": "jingyeeeeeeee",
    "Mihikaa Singh": "mihikaasingh",
}

_OVERRIDES = {" ".join(k.split()).lower(): v for k, v in HANDLE_OVERRIDES.items()}


def match_facils(facils, handles):
    """Join facils (name+group) to handle rows (name+handle) by fuzzy name.
    Returns a row per facil with a status."""
    rows = []
    for f in facils:
        override = _OVERRIDES.get(" ".join(f["name"].split()).lower())
        if override:
            rows.append({"name": f["name"], "group": f["group"],
                         "house": f["house"], "handle": override,
                         "status": "matched"})
            continue
        ftok = sheets.name_tokens(f["name"])
        cands = {}
        for h in handles:
            htok = sheets.name_tokens(h["name"])
            if htok and (ftok == htok or htok <= ftok or ftok <= htok):
                cands[h.get("handle") or h["raw_handle"]] = h

        if len(cands) == 1:
            h = next(iter(cands.values()))
            status = "matched" if h.get("handle") else "handle_invalid"
            handle = h.get("handle") or h["raw_handle"]
        elif len(cands) > 1:
            status, handle = "ambiguous", " | ".join(sorted(cands))
        else:
            status, handle = "no_handle", ""

        rows.append(
            {"name": f["name"], "group": f["group"], "house": f["house"],
             "handle": handle, "status": status}
        )
    return rows


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
    return match_facils(facils, handles)


def _og_order(group):
    return (0 if group[:2].upper() == "AM" else 1, int(group[2:]))


async def _add_one(client, r, groups, added):
    """Add + promote one facil. Returns a status string."""
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
            await client(InviteToChannelRequest(channel, [user]))
        except UserAlreadyParticipantError:
            pass
        await client(EditAdminRequest(channel, user, FACIL_RIGHTS, rank="Facil"))
        added.setdefault(chat_id, []).append(r["handle"])
        _save_added(added)
        print(f"  added @{r['handle']} to {title} as admin")
        return "added"
    except PeerFloodError:
        # Telegram won't let a user account add non-contacts — not a rate limit.
        return "needs_invite"
    except FloodWaitError as e:
        print(f"  flood wait {e.seconds}s — pausing")
        await asyncio.sleep(e.seconds + 5)
        return "floodwait"
    except Exception as exc:
        print(f"  couldn't add {r['name']} (@{r['handle']}) to {title} ({exc})")
        return "error"


async def _commit(rows, group_delay, only):
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

    n_added, bad = 0, set()
    consecutive, stopped = 0, False
    client = await start_client()
    try:
        for i, (og, facils) in enumerate(by_group):
            if stopped:
                break
            print(f"\n-- {og} ({len(facils)} facil(s)) --")
            for r in facils:
                status = await _add_one(client, r, groups, added)
                if status == "added":
                    n_added += 1
                    consecutive = 0
                elif status == "bad_handle":
                    bad.add(r["handle"].lower())
                elif status == "needs_invite":
                    consecutive += 1
                    print(f"  @{r['handle']} ({r['name']}) isn't a contact — "
                          "can't add directly; they'll need an invite link")
                    if consecutive >= FLOOD_CAP:
                        print(f"\n  hit the non-contact limit {FLOOD_CAP}x in a row — "
                              "stopping direct adds to protect the account.")
                        stopped = True
                        break
                if status not in ("skipped", "no_group"):
                    await asyncio.sleep(THROTTLE)
            if not stopped and i < len(by_group) - 1:
                print(f"   waiting {group_delay}s before the next group…")
                await asyncio.sleep(group_delay)
    finally:
        await client.disconnect()

    # anyone matched but not added (and whose handle resolves) needs an invite
    added_handles = {h.lower() for hs in added.values() for h in hs}
    needs = [r for r in matched
             if r["handle"].lower() not in added_handles
             and r["handle"].lower() not in bad]

    print(f"\nadded {n_added} facil(s) directly (contacts).")
    if bad:
        print(f"{len(bad)} handle(s) don't resolve — fix these "
              "(setup.find_handles can suggest the right ones).")
    if needs:
        print(f"\n{len(needs)} facil(s) aren't the owner's contacts, so Telegram "
              "won't let you add them directly. Onboard them via invite link:")
        for r in needs[:60]:
            print(f"  - {r['name']} ({r['group']}) @{r['handle']}")
        print("\n  1) python -m setup.invite_links   -> share each group's link\n"
              "  2) once they've joined: python -m setup.add_facils --promote")


async def _promote(only):
    """Promote matched facils who are already IN their group (e.g. joined via an
    invite link) to admin. Promoting a member isn't rate-limited."""
    groups = manifest.load()
    rows = [r for r in build_rows() if r["status"] == "matched"]
    if only:
        rows = [r for r in rows if r["group"].upper() == only.upper()]
    rows.sort(key=lambda r: _og_order(r["group"]))

    n = 0
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
        await client.disconnect()
    print(f"\npromoted {n} facil(s) who have joined.")


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
