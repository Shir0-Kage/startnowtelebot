"""Tracks what's already been created so re-runs are safe and resumable.

The manifest is a small JSON file keyed by group title. Path can be overridden
with SETUP_MANIFEST (handy for tests).
"""

import json
import os

MANIFEST_PATH = os.environ.get(
    "SETUP_MANIFEST",
    os.path.join(os.path.dirname(__file__), "created_groups.json"),
)


def load():
    if not os.path.exists(MANIFEST_PATH):
        return {}
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data):
    # write to a temp file then swap, so a crash mid-write can't corrupt it
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MANIFEST_PATH)


def group_entry(data, title):
    """Fetch (or create) the record for one group."""
    return data.setdefault(
        title,
        {
            "channel_id": None,
            "chat_id": None,
            "slot": None,
            "bot_added": False,
            "owner_title_set": False,
            "members_added": [],
        },
    )
