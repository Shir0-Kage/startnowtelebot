"""Set the same profile photo on every StartNOW! group.

Reads the groups from the manifest (created by create_groups.py) and sets the
given image as each one's photo. Run as the owner account.

    python -m setup.set_group_photo                       # uses setup/group_photo.png
    python -m setup.set_group_photo --image path/to.png    # a different file
    python -m setup.set_group_photo --dry-run              # list groups, change nothing

Put the image on the machine first (e.g. scp it to the instance). By default the
image is auto-cropped to its artwork (zooming in) and its transparency is
flattened onto a solid colour — otherwise Telegram renders the transparent
background as a black border. Telegram crops the result to a circle.
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


def _parse_bg(value):
    """'RRGGBB' (or '#RRGGBB') -> (r, g, b); None means auto-detect."""
    if not value:
        return None
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _sample_bg(im):
    """Guess the background colour from the edge midpoints — a circular design
    touches its bounding box there. Falls back to white."""
    w, h = im.size
    for x, y in ((w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)):
        r, g, b, a = im.getpixel((x, y))
        if a > 200:
            return (r, g, b)
    return (255, 255, 255)


def _prepared_path(src):
    root, _ = os.path.splitext(src)
    return root + ".prepared.png"


def prepare_image(src, dst, bg=None, zoom=1.0):
    """Crop away the transparent margin (zoom in) and flatten transparency onto
    a solid colour, so Telegram shows no black border. Writes a square PNG."""
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit(
            "Pillow is needed to process the image — run "
            "'pip install -r requirements.txt' (or pass --raw to skip)."
        )

    im = Image.open(src).convert("RGBA")

    # 1) crop to the artwork's non-transparent bounds — this is the zoom-in
    box = im.getchannel("A").getbbox()
    if box:
        im = im.crop(box)

    # 2) optional extra zoom towards the centre
    if zoom and zoom > 1.0:
        w, h = im.size
        nw, nh = int(w / zoom), int(h / zoom)
        left, top = (w - nw) // 2, (h - nh) // 2
        im = im.crop((left, top, left + nw, top + nh))

    fill = bg or _sample_bg(im)

    # 3) pad to a square and flatten onto the solid colour (no transparency left)
    w, h = im.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), fill)
    canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
    canvas.save(dst, format="PNG")
    return dst


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


async def run(image_path, dry_run, bg, zoom, process):
    targets = _targets()
    if not targets:
        print("no groups in the manifest — run create_groups.py first.")
        return
    print(f"{len(targets)} group(s) to update.")

    if not os.path.exists(image_path):
        raise SystemExit(
            f"image not found: {image_path}\n"
            "Put the photo there (or pass --image path/to/file), then re-run."
        )

    upload_path = image_path
    if process:
        upload_path = _prepared_path(image_path)
        prepare_image(image_path, upload_path, bg, zoom)
        print(f"prepared an opaque, zoomed image (no black border): {upload_path}")

    if dry_run:
        for title, chat_id in targets:
            print(f"  would set photo on {title} ({chat_id})")
        note = f" Preview the result at {upload_path}" if process else ""
        print("dry run — nothing uploaded." + note)
        return

    client = await start_client()
    try:
        done = await apply(client, targets, upload_path)
    finally:
        await client.disconnect()
    print(f"done — updated {done}/{len(targets)} groups.")


def main():
    p = argparse.ArgumentParser(description="Set the group profile photo everywhere.")
    p.add_argument("--image", default=DEFAULT_IMAGE, help="path to the image file")
    p.add_argument("--zoom", type=float, default=1.0,
                   help="extra zoom after auto-cropping the margin, e.g. 1.2")
    p.add_argument("--bg", default=None,
                   help="background colour as RRGGBB (default: auto-detected)")
    p.add_argument("--raw", action="store_true",
                   help="upload as-is, without removing transparency or zooming")
    p.add_argument("--dry-run", action="store_true",
                   help="prepare the image + list groups, upload nothing")
    args = p.parse_args()
    asyncio.run(run(args.image, args.dry_run, _parse_bg(args.bg), args.zoom, not args.raw))


if __name__ == "__main__":
    main()
