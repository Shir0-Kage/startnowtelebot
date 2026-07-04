"""Set the same profile photo on every StartNOW! group.

Reads the groups from the manifest (created by create_groups.py) and sets the
given image as each one's photo. Run as the owner account.

    python -m setup.set_group_photo                       # uses setup/group_photo.png
    python -m setup.set_group_photo --image path/to.png    # a different file
    python -m setup.set_group_photo --dry-run              # list groups, change nothing

Put the image on the machine first (e.g. scp it to the instance). A roughly
square image works best — Telegram crops it to a circle.
"""

import argparse
import asyncio
import os

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import EditPhotoRequest
from telethon.tl.types import InputChatUploadedPhoto

from setup import manifest
from setup.client import start_client

THROTTLE = 2
DEFAULT_IMAGE = os.path.join(os.path.dirname(__file__), "group_photo.png")


def _targets():
    """(title, chat_id) for every created group in the manifest."""
    out = []
    for title, entry in manifest.load().items():
        if entry.get("chat_id"):
            out.append((title, entry["chat_id"]))
    return out


async def apply(client, targets, image_path):
    done = 0
    for title, chat_id in targets:
        try:
            channel = await client.get_entity(chat_id)
            uploaded = await client.upload_file(image_path)
            await client(EditPhotoRequest(channel, InputChatUploadedPhoto(uploaded)))
            done += 1
            print(f"  set photo on {title}")
        except FloodWaitError as e:
            print(f"  flood wait {e.seconds}s — pausing")
            await asyncio.sleep(e.seconds + 5)
        except Exception as exc:
            print(f"  couldn't set photo on {title} ({exc})")
        await asyncio.sleep(THROTTLE)
    return done


async def run(image_path, dry_run):
    targets = _targets()
    if not targets:
        print("no groups in the manifest — run create_groups.py first.")
        return
    print(f"{len(targets)} group(s) to update.")

    if dry_run:
        for title, chat_id in targets:
            print(f"  would set photo on {title} ({chat_id})")
        print("dry run — nothing changed.")
        return

    if not os.path.exists(image_path):
        raise SystemExit(
            f"image not found: {image_path}\n"
            "Put the photo there (or pass --image path/to/file), then re-run."
        )

    client = await start_client()
    try:
        done = await apply(client, targets, image_path)
    finally:
        await client.disconnect()
    print(f"done — updated {done}/{len(targets)} groups.")


def main():
    p = argparse.ArgumentParser(description="Set the group profile photo everywhere.")
    p.add_argument("--image", default=DEFAULT_IMAGE, help="path to the image file")
    p.add_argument("--dry-run", action="store_true", help="list groups, change nothing")
    args = p.parse_args()
    asyncio.run(run(args.image, args.dry_run))


if __name__ == "__main__":
    main()
