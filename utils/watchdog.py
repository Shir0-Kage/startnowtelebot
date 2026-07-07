"""Freeze diagnostics: catch a blocked event loop and dump every thread's stack.

When the asyncio loop is blocked by a synchronous call, Ctrl+C is dead and the
bot logs nothing (the thing that writes logs is the frozen loop). This module
gives two escape hatches that both work from OUTSIDE the loop:

  * `kill -USR1 <pid>`  -> dump all thread stacks on demand (Unix only).
  * a WATCHDOG thread   -> auto-dump all thread stacks whenever the loop stops
                           ticking a heartbeat for `stall_seconds`.

`faulthandler.dump_traceback(all_threads=True)` walks every thread's Python
frames without needing the GIL, so it captures the frozen main thread's stack
even while it's stuck in a blocking C call (urllib, sqlite, onnxruntime, ...) —
pointing straight at the offending line.
"""

import asyncio
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime

log = logging.getLogger("watchdog")

_beat = time.monotonic()   # bumped ~every second by heartbeat() while the loop is alive
_freeze_file = None        # persistent append-only log for stack dumps


def enable_faulthandler(logfile="freeze_dumps.log"):
    """Enable faulthandler + (on Unix) a SIGUSR1 handler that dumps all thread
    stacks. Call as early as possible so even a startup hang is catchable."""
    global _freeze_file
    try:
        _freeze_file = open(logfile, "a", buffering=1, encoding="utf-8")
    except Exception as exc:
        log.warning("couldn't open freeze log %s: %s", logfile, exc)
        _freeze_file = None
    faulthandler.enable()  # dump to stderr on a fatal error (segfault, etc.)
    if hasattr(signal, "SIGUSR1"):
        target = _freeze_file or sys.stderr
        faulthandler.register(signal.SIGUSR1, file=target, all_threads=True, chain=False)
        log.info("faulthandler armed: `kill -USR1 %d` dumps all stacks to %s",
                 os.getpid(), logfile)


def dump_now(reason):
    """Dump all thread stacks to the freeze log and stderr. Safe to call from any
    thread; used by the watchdog and callable manually."""
    header = f"\n===== {datetime.now().isoformat(timespec='seconds')} {reason} " \
             f"(pid {os.getpid()}) — all thread stacks =====\n"
    for target in (_freeze_file, sys.stderr):
        if target is None:
            continue
        try:
            target.write(header)
            target.flush()
            faulthandler.dump_traceback(file=target, all_threads=True)
            target.flush()
        except Exception:
            pass  # never let diagnostics crash the process


async def heartbeat(interval=1.0):
    """Bump the loop heartbeat on every tick. Schedule once on the event loop
    (e.g. app.create_task(watchdog.heartbeat())). If the loop blocks, this stops
    running and the watchdog notices."""
    global _beat
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(interval)


def start_watchdog(stall_seconds=20):
    """Start a daemon thread that dumps all stacks when the loop stalls (the
    heartbeat goes stale) for >= stall_seconds. Dumps once per stall, then re-arms
    when the loop recovers, so a long freeze doesn't spam the log."""
    poll = max(0.25, stall_seconds / 4.0)

    def _run():
        armed = True
        while True:
            time.sleep(poll)
            stalled = time.monotonic() - _beat
            if stalled >= stall_seconds:
                if armed:
                    log.critical("EVENT LOOP STALLED %.0fs — dumping thread stacks "
                                 "(the loop is blocked by a synchronous call)", stalled)
                    dump_now(f"EVENT LOOP STALLED {stalled:.0f}s")
                    armed = False
            else:
                armed = True

    threading.Thread(target=_run, name="loop-watchdog", daemon=True).start()
    log.info("loop watchdog started (stall threshold %ds)", stall_seconds)
