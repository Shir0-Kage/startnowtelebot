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
        return "flood"
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

    n_added, bad_handles, flooded = 0, [], False
    client = await start_client()
    try:
        for i, (og, facils) in enumerate(by_group):
            print(f"\n-- {og} ({len(facils)} facil(s)) --")
            for r in facils:
                status = await _add_one(client, r, groups, added)
                if status == "added":
                    n_added += 1
                elif status == "bad_handle":
                    bad_handles.append(f"{r['name']} ({r['group']}) @{r['handle']}")
                elif status == "flood":
                    flooded = True
                    break
                if status not in ("skipped", "no_group"):
                    await asyncio.sleep(THROTTLE)
            if flooded:
                break
            if i < len(by_group) - 1:  # pause between groups, not after the last
                print(f"   waiting {group_delay}s before the next group…")
                await asyncio.sleep(group_delay)
    finally:
        await client.disconnect()

    print(f"\nadded {n_added} facil(s).")
    if bad_handles:
        print(f"{len(bad_handles)} handle(s) don't resolve — fix these and re-run:")
        for b in bad_handles:
            print("  -", b)
    if flooded:
        print(
            "\nSTOPPED: Telegram rate-limited adding (PeerFloodError).\n"
            "   Already-added facils are saved, so wait a few hours and re-run —\n"
            "   it resumes where it left off. Or do one group at a time with\n"
            "   --only AM1, spaced out over the day."
        )


async def run(commit, group_delay, only):
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
    p.add_argument("--commit", action="store_true", help="actually add the matched facils")
    p.add_argument("--group-delay", type=int, default=GROUP_DELAY,
                   help="seconds to wait between groups (default %(default)s)")
    p.add_argument("--only", default=None, help="add just one group, e.g. --only AM1")
    args = p.parse_args()
    asyncio.run(run(args.commit, args.group_delay, args.only))


if __name__ == "__main__":
    main()
