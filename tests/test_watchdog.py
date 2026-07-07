"""The watchdog must capture a synchronously-blocked event loop — that's the
whole point: when the loop can't log or honour Ctrl+C, a separate thread dumps
the stacks anyway."""

import asyncio
import time

import pytest

from utils import watchdog


def test_heartbeat_bumps_the_beat():
    watchdog._beat = 0.0

    async def run():
        task = asyncio.create_task(watchdog.heartbeat(interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert watchdog._beat > 0.0


def test_watchdog_dumps_when_loop_blocks(tmp_path):
    logfile = tmp_path / "freeze.log"
    watchdog.enable_faulthandler(str(logfile))
    watchdog.start_watchdog(stall_seconds=1)

    async def run():
        watchdog._beat = time.monotonic()
        beat = asyncio.create_task(watchdog.heartbeat(interval=0.1))
        await asyncio.sleep(0.2)          # let the heartbeat tick a few times
        time.sleep(2.5)                   # BLOCK the loop synchronously > threshold
        await asyncio.sleep(0.1)
        beat.cancel()

    asyncio.run(run())
    time.sleep(0.5)                       # let the watchdog thread flush its dump

    content = logfile.read_text(encoding="utf-8")
    assert "EVENT LOOP STALLED" in content, content[:500]
    # the dump captured the frozen main thread parked in the blocking sleep
    assert "time.sleep" in content or "Thread" in content
