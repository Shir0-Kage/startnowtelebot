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
from datetime import datetime, timedelta

from telethon.errors import PeerFloodError

import storage
from config import TIMEZONE
from setup import add_facils, add_members, add_year_ones
from setup.client import start_client

INTERVAL = 20
PEERFLOOD_BACKOFF = 3600  # if Telegram flags us for spam, back off an hour

# Run the facil add + promote ONCE, at this SGT hour on the morning after the
# worker is first started (one-time, not recurring).
FACIL_HOUR = 6
_STATE = os.path.join(os.path.dirname(__file__), "facil_task_state.json")

# (label, one-pass coroutine) — reuses each feature's existing cycle
JOBS = [
    ("add_members", add_members._cycle),
    ("add_year_ones", add_year_ones._cycle),
]


def _load_state():
    try:
        with open(_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    with open(_STATE, "w") as f:
        json.dump(state, f)


async def _maybe_facil_once(client):
    """Run the facil add + promote a single time, at FACIL_HOUR SGT the day
    after the worker first starts. Persisted, so it never repeats."""
    state = _load_state()
    if state.get("done"):
        return
    now = datetime.now(TIMEZONE)
    if "run_on" not in state:
        run_on = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        state["run_on"] = run_on
        _save_state(state)
        print(f"facil task armed for {run_on} {FACIL_HOUR:02d}00 SGT (one-time)")
        return
    target = datetime.strptime(state["run_on"], "%Y-%m-%d").replace(
        hour=FACIL_HOUR, tzinfo=TIMEZONE)
    if now >= target:
        state["done"] = True
        _save_state(state)  # mark first, so a mid-run restart won't repeat it
        print(f"[{now:%Y-%m-%d %H:%M} SGT] one-time facil add + promote")
        await add_facils.run_facils(client)


async def run():
    storage.init_db()
    client = await start_client()
    print(f"worker running (add_members + add_year_ones; facils once at "
          f"{FACIL_HOUR:02d}00 SGT tomorrow) — Ctrl-c to stop")
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
                await _maybe_facil_once(client)
            except Exception as exc:
                print(f"facil task error ({exc})")
            await asyncio.sleep(INTERVAL)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("stopped.")
    finally:
        await client.disconnect()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
