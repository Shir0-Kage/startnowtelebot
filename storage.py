"""SQLite persistence for group settings and attendance.

One small database file (bot.db by default). The connection is shared across
the app with a lock, which is plenty for a single-process bot.
"""

import sqlite3
import threading
from datetime import datetime

from config import DB_PATH, TIMEZONE, REMINDERS_DEFAULT_ON

_lock = threading.Lock()
_conn = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    chat_id           INTEGER PRIMARY KEY,
    title             TEXT,
    slot              TEXT    DEFAULT 'unset',   -- 'AM' | 'PM' | 'unset'
    reminders_enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS attendance (
    chat_id      INTEGER,
    event_key    TEXT,
    user_id      INTEGER,
    display_name TEXT,
    username     TEXT,
    slot         TEXT,          -- the group's slot at time of marking
    marked_at    TEXT,          -- ISO timestamp, SGT
    PRIMARY KEY (chat_id, event_key, user_id)
);

CREATE TABLE IF NOT EXISTS attendance_state (
    chat_id   INTEGER,
    event_key TEXT,
    is_open   INTEGER DEFAULT 1,
    PRIMARY KEY (chat_id, event_key)
);
"""


def init_db():
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()


def _now_iso():
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Group settings
# ---------------------------------------------------------------------------

def ensure_group(chat_id, title):
    """Make sure a row exists for this chat and keep the title fresh. Called
    whenever we handle a command so settings are always available."""
    default = 1 if REMINDERS_DEFAULT_ON else 0
    with _lock:
        _conn.execute(
            """INSERT INTO groups (chat_id, title, reminders_enabled)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title""",
            (chat_id, title, default),
        )
        _conn.commit()


def get_group(chat_id):
    with _lock:
        row = _conn.execute(
            "SELECT * FROM groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return dict(row) if row else None


def set_slot(chat_id, slot):
    with _lock:
        _conn.execute(
            "UPDATE groups SET slot = ? WHERE chat_id = ?", (slot, chat_id)
        )
        _conn.commit()


def get_slot(chat_id):
    g = get_group(chat_id)
    return g["slot"] if g else "unset"


def set_reminders(chat_id, enabled):
    with _lock:
        _conn.execute(
            "UPDATE groups SET reminders_enabled = ? WHERE chat_id = ?",
            (1 if enabled else 0, chat_id),
        )
        _conn.commit()


def groups_with_reminders():
    """All chats that currently want reminders."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM groups WHERE reminders_enabled = 1"
        ).fetchall()
    return [dict(r) for r in rows]


def groups_by_slot(slot):
    """Chats assigned to a given slot that also want reminders."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM groups WHERE reminders_enabled = 1 AND slot = ?",
            (slot,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

def open_attendance(chat_id, event_key):
    with _lock:
        _conn.execute(
            """INSERT INTO attendance_state (chat_id, event_key, is_open)
               VALUES (?, ?, 1)
               ON CONFLICT(chat_id, event_key) DO UPDATE SET is_open = 1""",
            (chat_id, event_key),
        )
        _conn.commit()


def close_attendance(chat_id, event_key):
    with _lock:
        _conn.execute(
            """INSERT INTO attendance_state (chat_id, event_key, is_open)
               VALUES (?, ?, 0)
               ON CONFLICT(chat_id, event_key) DO UPDATE SET is_open = 0""",
            (chat_id, event_key),
        )
        _conn.commit()


def is_open(chat_id, event_key):
    with _lock:
        row = _conn.execute(
            "SELECT is_open FROM attendance_state WHERE chat_id = ? AND event_key = ?",
            (chat_id, event_key),
        ).fetchone()
    # if we've never posted a check, treat it as not open
    return bool(row["is_open"]) if row else False


def mark_attendance(chat_id, event_key, user_id, display_name, username, slot):
    """Record one attendance tap. Returns True if this is a new mark, False if
    the user was already counted (so we don't double-count)."""
    with _lock:
        cur = _conn.execute(
            """INSERT OR IGNORE INTO attendance
               (chat_id, event_key, user_id, display_name, username, slot, marked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, event_key, user_id, display_name, username, slot, _now_iso()),
        )
        _conn.commit()
        return cur.rowcount > 0


def get_attendance(chat_id, event_key):
    with _lock:
        rows = _conn.execute(
            """SELECT * FROM attendance
               WHERE chat_id = ? AND event_key = ?
               ORDER BY marked_at""",
            (chat_id, event_key),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_attendance(chat_id, event_key):
    """Wipe attendance for one event in one chat. Returns rows removed."""
    with _lock:
        cur = _conn.execute(
            "DELETE FROM attendance WHERE chat_id = ? AND event_key = ?",
            (chat_id, event_key),
        )
        _conn.execute(
            "DELETE FROM attendance_state WHERE chat_id = ? AND event_key = ?",
            (chat_id, event_key),
        )
        _conn.commit()
        return cur.rowcount


def attendance_counts(chat_id):
    """Per-event mark counts for a chat: list of (event_key, count)."""
    with _lock:
        rows = _conn.execute(
            """SELECT event_key, COUNT(*) AS n FROM attendance
               WHERE chat_id = ?
               GROUP BY event_key""",
            (chat_id,),
        ).fetchall()
    return [(r["event_key"], r["n"]) for r in rows]


def all_attendance(chat_id):
    """Every attendance row for a chat — used for CSV export."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM attendance WHERE chat_id = ? ORDER BY event_key, marked_at",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]
