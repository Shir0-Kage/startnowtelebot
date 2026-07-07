"""Liveness watchdog + freeze diagnostics for the asyncio bot.

When the event loop is blocked by a synchronous call, Ctrl+C is dead and the bot
logs nothing (the thing that writes logs is the frozen loop). A separate daemon
thread watches a heartbeat the loop bumps every second and, when the loop stops
ticking, it:

  1. at `stall_seconds`  — dumps EVERY thread's stack to `freeze_dumps.log` (so
     we can see exactly which line is blocked), and
  2. at `restart_seconds`— force-exits the process so the supervisor (the bash
     restart loop / systemd) relaunches a fresh one, clearing whatever blocked
     the loop.

You can also dump on demand with `kill -USR1 <pid>` (Unix). Everything here runs
off the event loop and writes straight to file descriptors (not through the
`logging` module), so it works even if the main thread froze mid-log or mid-C
call, and in an unprivileged container where py-spy can't attach.
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
        faulthandler.register(signal.SIGUSR1, file=_freeze_file or sys.stderr,
                              all_threads=True, chain=False)
        log.info("faulthandler armed: `kill -USR1 %d` dumps all stacks to %s",
                 os.getpid(), logfile)


def dump_now(reason):
    """Dump all thread stacks to the freeze log AND stderr. Safe from any thread
    and free of the logging module, so it can't deadlock on a log handler lock."""
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


def _hard_exit(reason):
    """Flush diagnostics and terminate NOW. os._exit bypasses the wedged main
    thread and atexit — a graceful shutdown is impossible when the loop is stuck.
    The supervisor (restart loop / systemd) then relaunches a fresh process."""
    for target in (_freeze_file, sys.stderr, sys.stdout):
        try:
            target.write(f"[watchdog] {reason}: forcing os._exit(1) so the "
                         "supervisor restarts the bot\n")
            target.flush()
        except Exception:
            pass
    os._exit(1)


async def heartbeat(interval=1.0):
    """Bump the loop heartbeat on every tick. Schedule once on the event loop
    (e.g. app.create_task(watchdog.heartbeat())). If the loop blocks, this stops
    running and the watchdog notices."""
    global _beat
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(interval)


def start_watchdog(stall_seconds=20, restart_seconds=60):
    """Start a daemon thread that watches the loop heartbeat. Dumps all stacks
    once the loop has stalled >= stall_seconds, and (if restart_seconds > 0)
    force-restarts the process once the stall reaches restart_seconds. Returns a
    callable that stops the watchdog (used by tests; main() ignores it)."""
    stop = threading.Event()
    poll = max(0.25, stall_seconds / 4.0)

    def _run():
        dumped = False
        while not stop.wait(poll):
            stalled = time.monotonic() - _beat
            if stalled < stall_seconds:
                dumped = False
                continue
            if not dumped:
                dump_now(f"EVENT LOOP STALLED {stalled:.0f}s "
                         "(blocked by a synchronous call)")
                dumped = True
            if restart_seconds and stalled >= restart_seconds:
                _hard_exit(f"EVENT LOOP STALLED {stalled:.0f}s >= {restart_seconds}s")

    threading.Thread(target=_run, name="loop-watchdog", daemon=True).start()
    log.info("loop watchdog started (stack dump at %ds; auto-restart at %s)",
             stall_seconds, f"{restart_seconds}s" if restart_seconds else "off")
    return stop.set
