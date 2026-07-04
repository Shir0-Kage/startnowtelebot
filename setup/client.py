"""Shared Telethon client for the provisioning scripts.

Credentials come from the environment (same .env the bot uses):
  TELEGRAM_API_ID, TELEGRAM_API_HASH   -> from https://my.telegram.org
  TELEGRAM_PHONE                       -> the owner account's number

The login session is stored in setup/<name>.session (gitignored). The first run
prompts for the login code (and 2FA password if set); after that it's silent.
"""

import os
import sys

from telethon import TelegramClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
PHONE = os.environ.get("TELEGRAM_PHONE")

# session file lives next to these scripts
SESSION_NAME = os.path.join(os.path.dirname(__file__), "zzehao")


def build_client():
    if not (API_ID and API_HASH):
        sys.exit(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set. Add them to .env "
            "(get them from https://my.telegram.org)."
        )
    return TelegramClient(SESSION_NAME, int(API_ID), API_HASH)


# Two Telethon clients can't share one session file — the second gets
# "database is locked". This holds an OS-level lock so only one setup script
# uses the session at a time; the lock frees automatically if the process dies.
_lock_handle = None


def _acquire_session_lock():
    global _lock_handle
    try:
        import fcntl  # Linux/macOS (the server); absent on Windows dev boxes
    except ImportError:
        return
    _lock_handle = open(SESSION_NAME + ".lock", "w")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(
            "Another setup script is already using the Telegram session — only "
            "one can at a time.\nStop it first, or run the combined worker "
            "instead of separate --watch scripts:\n    python -m setup.worker"
        )
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()


async def start_client():
    """Grab the single-session lock, then build + connect the client.
    Prompts for the login code on first run."""
    _acquire_session_lock()
    client = build_client()
    await client.start(**({"phone": PHONE} if PHONE else {}))
    return client
