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
import json
import os
from datetime import datetime

from telethon.errors import PeerFloodError

import storage
from config import TIMEZONE
from setup import add_facils, add_members, add_year_ones
from setup.client import start_client

INTERVAL = 20
PEERFLOOD_BACKOFF = 3600  # if Telegram flags us for spam, back off an hour

# Run the facil add + promote once a day at this SGT hour.
FACIL_HOUR = 6
_STATE = os.path.join(os.path.dirname(__file__), "facil_daily_state.json")

# (label, one-pass coroutine) — reuses each feature's existing cycle
JOBS = [
    ("add_members", add_members._cycle),
    ("add_year_ones", add_year_ones._cycle),
]


def _facils_ran_on():
    try:
        with open(_STATE) as f:
            return json.load(f).get("last_run")
    except Exception:
        return None


def _set_facils_ran(day):
    with open(_STATE, "w") as f:
        json.dump({"last_run": day}, f)


async def _maybe_daily_facils(client):
    """Once a day at ~0600 SGT, add facils and promote whoever's joined."""
    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d")
    if now.hour != FACIL_HOUR or _facils_ran_on() == today:
        return
    _set_facils_ran(today)  # mark first, so a mid-run restart doesn't repeat
    print(f"[{now:%H:%M} SGT] daily facil add + promote")
    await add_facils.run_facils(client)


async def run():
    storage.init_db()
    client = await start_client()
    print(f"worker running (add_members + add_year_ones; facils daily at "
          f"{FACIL_HOUR:02d}00 SGT) — Ctrl-c to stop")
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
            try:
                await _maybe_daily_facils(client)
            except Exception as exc:
                print(f"daily facil task error ({exc}); will try again tomorrow")
            await asyncio.sleep(INTERVAL)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("stopped.")
    finally:
        await client.disconnect()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
