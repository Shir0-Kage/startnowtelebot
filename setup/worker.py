"""The one always-on setup worker.

Runs both jobs through a single Telethon client, so there's only ever ONE
process using the session (no "database is locked"):
  - adds the directors as they message the bot /start   (add_members)
  - fulfils /add_year_ones requests from facils           (add_year_ones)

Run just this one for continuous operation:

    python -m setup.worker

The one-off scripts (create_groups, add_facils, set_group_photo) still work, but
stop this worker first — only one Telethon script can use the session at a time
(the session lock will remind you if you forget).
"""

import asyncio

from telethon.errors import PeerFloodError

import storage
from setup import add_members, add_year_ones
from setup.client import start_client

INTERVAL = 20
PEERFLOOD_BACKOFF = 3600  # if Telegram flags us for spam, back off an hour

# (label, one-pass coroutine) — reuses each feature's existing cycle
JOBS = [
    ("add_members", add_members._cycle),
    ("add_year_ones", add_year_ones._cycle),
]


async def run():
    storage.init_db()
    client = await start_client()
    print("worker running (add_members + add_year_ones) — Ctrl-c to stop")
    try:
        while True:
            for label, cycle in JOBS:
                try:
                    await cycle(client)
                except PeerFloodError:
                    print(f"{label}: PeerFloodError — Telegram is rate-limiting "
                          f"adds; backing off {PEERFLOOD_BACKOFF // 60} min")
                    await asyncio.sleep(PEERFLOOD_BACKOFF)
                except Exception as exc:
                    print(f"{label} cycle error ({exc}); will retry next tick")
            await asyncio.sleep(INTERVAL)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("stopped.")
    finally:
        await client.disconnect()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
