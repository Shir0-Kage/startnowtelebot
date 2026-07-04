"""Suggest correct handles for facils whose sheet handle doesn't resolve.

For every facil whose sheet @handle isn't one of the members of a known group
chat, this finds the closest member *by name* and suggests their real username
— or FLAGs anyone with no similar match, for you to sort out by hand.

    python -m setup.find_handles
    python -m setup.find_handles --chat -1003689329730 --threshold 0.45

Read-only: it lists members and matches names. It never adds anyone, so it
won't trip Telegram's add limit. Writes setup/handle_suggestions.csv.
"""

import argparse
import asyncio
import csv
import difflib
import os
import re

from setup.add_facils import build_rows
from setup.client import start_client

# from the link t.me/c/3689329730/4/161  ->  -100 + 3689329730
DEFAULT_CHAT = -1003689329730

SUGGEST_HI = 0.72   # confident enough to just use
REVIEW_LO = 0.45    # below this = no similar member, FLAG it
OUT = os.path.join(os.path.dirname(__file__), "handle_suggestions.csv")


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def _score(name, member):
    """How close a facil name is to a group member (name + username)."""
    fn, mn = _norm(name), _norm(member["name"])
    ftok, mtok = set(fn.split()), set(mn.split())
    jaccard = len(ftok & mtok) / len(ftok | mtok) if (ftok | mtok) else 0
    name_ratio = difflib.SequenceMatcher(None, fn, mn).ratio()
    uname = member.get("username") or ""
    uname_ratio = (
        difflib.SequenceMatcher(None, fn.replace(" ", ""), uname).ratio()
        if uname else 0
    )
    return max(jaccard, name_ratio, 0.9 * uname_ratio)


def _best(name, members):
    best, best_score = None, 0.0
    for m in members:
        s = _score(name, m)
        if s > best_score:
            best, best_score = m, s
    return best, best_score


def suggest(rows, members, threshold=REVIEW_LO):
    """For facils whose handle isn't a member username, propose the closest
    member (or FLAG). Returns a list of report rows."""
    usernames = {m["username"] for m in members if m["username"]}
    out = []
    for r in rows:
        if r["handle"].lower() in usernames:
            continue  # handle already matches someone in the group — fine
        best, score = _best(r["name"], members)
        if best and best.get("username") and score >= threshold:
            status = "SUGGEST" if score >= SUGGEST_HI else "REVIEW"
            suggested, match_name = best["username"], best["name"]
        else:
            status, suggested = "FLAG", ""
            match_name = best["name"] if best else ""
        out.append({
            "name": r["name"], "group": r["group"], "old_handle": r["handle"],
            "suggested": suggested, "match_name": match_name,
            "score": round(score, 2), "status": status,
        })
    return out


async def _members(client, chat):
    entity = await client.get_entity(chat)
    members = []
    async for u in client.iter_participants(entity):
        name = " ".join(filter(None, [u.first_name, u.last_name])).strip()
        members.append({"name": name, "username": (u.username or "").lower(), "id": u.id})
    return members


async def run(chat, threshold):
    rows = [r for r in build_rows() if r["status"] == "matched"]
    client = await start_client()
    try:
        try:
            members = await _members(client, chat)
        except Exception as exc:
            raise SystemExit(
                f"couldn't read members of chat {chat} ({exc}).\n"
                "Make sure the owner account is in that group and the chat id is "
                "right (from t.me/c/<ID>/... use -100<ID>)."
            )
    finally:
        await client.disconnect()

    print(f"group has {len(members)} members "
          f"({sum(1 for m in members if m['username'])} with usernames)")
    report = suggest(rows, members, threshold)

    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "group", "old_handle",
                                           "suggested", "match_name", "score", "status"])
        w.writeheader()
        w.writerows(report)
    print(f"wrote {OUT} ({len(report)} handle(s) to look at)\n")

    for st in ("SUGGEST", "REVIEW", "FLAG"):
        block = [o for o in report if o["status"] == st]
        if not block:
            continue
        print(f"== {st} ({len(block)}) ==")
        for o in block:
            if st == "FLAG":
                print(f"  {o['group']:5} {o['name']:30} @{o['old_handle']}  ->  "
                      "no similar member — sort out by hand")
            else:
                print(f"  {o['group']:5} {o['name']:30} @{o['old_handle']}  ->  "
                      f"@{o['suggested']}  (member '{o['match_name']}', {o['score']})")
        print()


def main():
    p = argparse.ArgumentParser(description="Suggest handles from a group chat's members.")
    p.add_argument("--chat", type=int, default=DEFAULT_CHAT, help="group chat id")
    p.add_argument("--threshold", type=float, default=REVIEW_LO,
                   help="below this similarity, FLAG instead of suggest")
    args = p.parse_args()
    asyncio.run(run(args.chat, args.threshold))


if __name__ == "__main__":
    main()
