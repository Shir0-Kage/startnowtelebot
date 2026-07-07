"""The watchdog must (a) capture a synchronously-blocked event loop and (b) force
a restart if the freeze persists — that's the self-healing the bot relies on when
the loop can't log or honour Ctrl+C.

Each test stops its watchdog thread in a finally block so no leftover daemon can
fire os._exit after the test (which would kill the whole pytest run)."""

import asyncio
import time

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
    stop = watchdog.start_watchdog(stall_seconds=1, restart_seconds=0)  # no restart
    try:
        async def run():
            watchdog._beat = time.monotonic()
            beat = asyncio.create_task(watchdog.heartbeat(interval=0.1))
            await asyncio.sleep(0.2)          # let the heartbeat tick a few times
            time.sleep(2.5)                   # BLOCK the loop synchronously > threshold
            await asyncio.sleep(0.1)
            beat.cancel()

        asyncio.run(run())
        time.sleep(0.5)                       # let the watchdog flush its dump
    finally:
        stop()

    content = logfile.read_text(encoding="utf-8")
    assert "EVENT LOOP STALLED" in content, content[:500]
    assert "time.sleep" in content or "Thread" in content


def test_watchdog_forces_restart_on_persistent_stall(tmp_path, monkeypatch):
    exits = []
    monkeypatch.setattr(watchdog, "_hard_exit", lambda reason: exits.append(reason))
    watchdog.enable_faulthandler(str(tmp_path / "f.log"))
    stop = watchdog.start_watchdog(stall_seconds=1, restart_seconds=2)
    try:
        async def run():
            watchdog._beat = time.monotonic()
            beat = asyncio.create_task(watchdog.heartbeat(interval=0.1))
            await asyncio.sleep(0.2)
            time.sleep(3.0)                   # stall past the restart threshold
            await asyncio.sleep(0.1)
            beat.cancel()

        asyncio.run(run())
        time.sleep(0.3)
    finally:
        stop()

    assert exits, "watchdog should have forced a restart on a persistent stall"


def test_watchdog_does_not_restart_when_disabled(tmp_path, monkeypatch):
    exits = []
    monkeypatch.setattr(watchdog, "_hard_exit", lambda reason: exits.append(reason))
    watchdog.enable_faulthandler(str(tmp_path / "f.log"))
    stop = watchdog.start_watchdog(stall_seconds=1, restart_seconds=0)
    try:
        async def run():
            watchdog._beat = time.monotonic()
            beat = asyncio.create_task(watchdog.heartbeat(interval=0.1))
            await asyncio.sleep(0.2)
            time.sleep(2.5)
            await asyncio.sleep(0.1)
            beat.cancel()

        asyncio.run(run())
        time.sleep(0.3)
    finally:
        stop()

    assert not exits, "restart_seconds=0 must disable auto-restart"
