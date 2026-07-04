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

from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
from telethon.tl.types import ChatAdminRights

from setup import manifest, sheets
from setup.client import PHONE, build_client

THROTTLE = 3
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


def match_facils(facils, handles):
    """Join facils (name+group) to handle rows (name+handle) by fuzzy name.
    Returns a row per facil with a status."""
    rows = []
    for f in facils:
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


async def _commit(rows):
    groups = manifest.load()  # title -> {chat_id, ...}
    added = _load_added()
    matched = [r for r in rows if r["status"] == "matched"]
    print(f"{len(matched)} matched facil(s) to add.")

    client = build_client()
    await client.start(**({"phone": PHONE} if PHONE else {}))
    try:
        for r in matched:
            title = f"StartNOW! {r['group']}"
            entry = groups.get(title)
            if not entry or not entry.get("chat_id"):
                print(f"  no group created for {title} yet — skipping {r['name']}")
                continue
            chat_id = str(entry["chat_id"])
            if r["handle"] in added.get(chat_id, []):
                continue
            try:
                channel = await client.get_entity(entry["chat_id"])
                user = await client.get_input_entity(r["handle"])
                try:
                    await client(InviteToChannelRequest(channel, [user]))
                except UserAlreadyParticipantError:
                    pass
                await client(EditAdminRequest(channel, user, FACIL_RIGHTS, rank="Facil"))
                added.setdefault(chat_id, []).append(r["handle"])
                _save_added(added)
                print(f"  added @{r['handle']} to {title} as admin")
            except FloodWaitError as e:
                print(f"  flood wait {e.seconds}s — pausing")
                await asyncio.sleep(e.seconds + 5)
            except Exception as exc:
                print(f"  couldn't add {r['name']} (@{r['handle']}) to {title} ({exc})")
            await asyncio.sleep(THROTTLE)
    finally:
        await client.disconnect()


async def run(commit):
    rows = build_rows()
    write_report(rows)
    print(f"wrote {REPORT_PATH}")
    print("status counts:", _summary(rows))

    if not commit:
        print("\nReview the report, then re-run with --commit to add the "
              "'matched' facils. ('ambiguous' / 'no_handle' need a handle or a "
              "manual fix first.)")
        return
    await _commit(rows)


def main():
    p = argparse.ArgumentParser(description="Add facils to their groups.")
    p.add_argument("--commit", action="store_true", help="actually add the matched facils")
    args = p.parse_args()
    asyncio.run(run(args.commit))


if __name__ == "__main__":
    main()
