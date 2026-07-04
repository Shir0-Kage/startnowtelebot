"""Print each group's invite link so people can join themselves.

Joining via a link is NOT blocked by Telegram's "you can only add your contacts"
rule, so this is the reliable way to onboard facils / Year 1s who aren't the
owner's contacts. Links are generated once and remembered in the manifest, so
they stay stable across runs (sharing an old link keeps working).

    python -m setup.invite_links
    python -m setup.invite_links --regenerate   # make fresh links (old ones die)

Generating links is an admin action — it does NOT trip the add limit.
"""

import argparse
import asyncio
import csv
import os

from telethon.tl.functions.messages import ExportChatInviteRequest

from setup import manifest
from setup.client import start_client

OUT = os.path.join(os.path.dirname(__file__), "invite_links.csv")


def _og_order(title):
    og = title.replace("StartNOW! ", "")
    return (0 if og[:2].upper() == "AM" else 1, int(og[2:]) if og[2:].isdigit() else 0)


async def run(regenerate):
    data = manifest.load()
    targets = sorted((t for t, e in data.items() if e.get("chat_id")), key=_og_order)
    if not targets:
        print("no groups in the manifest — run create_groups.py first.")
        return

    client = await start_client()
    rows = []
    try:
        for title in targets:
            entry = data[title]
            link = entry.get("invite_link")
            if not link or regenerate:
                try:
                    peer = await client.get_entity(entry["chat_id"])
                    link = (await client(ExportChatInviteRequest(peer))).link
                    entry["invite_link"] = link
                    manifest.save(data)
                except Exception as exc:
                    print(f"{title}: couldn't get link ({exc})")
                    continue
                await asyncio.sleep(1)
            print(f"{title}: {link}")
            rows.append({"group": title, "link": link})
    finally:
        await client.disconnect()

    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["group", "link"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {OUT} — share each link with that group's facils / Year 1s.")


def main():
    p = argparse.ArgumentParser(description="Print each group's invite link.")
    p.add_argument("--regenerate", action="store_true",
                   help="make fresh links (invalidates the old ones)")
    args = p.parse_args()
    asyncio.run(run(args.regenerate))


if __name__ == "__main__":
    main()
