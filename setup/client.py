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


async def connect(client):
    """Connect + authenticate. Prompts for the code on first run."""
    kwargs = {"phone": PHONE} if PHONE else {}
    await client.start(**kwargs)
    return client
