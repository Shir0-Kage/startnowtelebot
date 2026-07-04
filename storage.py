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
    slot         TEXT,          -- the group's slot when they voted
    answer       TEXT,          -- 'Going' | 'Not going' | 'Maybe'
    marked_at    TEXT,          -- ISO timestamp, SGT
    PRIMARY KEY (chat_id, event_key, user_id)
);

-- one row per attendance poll the bot posts, so a PollAnswer update (which only
-- carries the poll id) can be mapped back to its chat + event.
CREATE TABLE IF NOT EXISTS polls (
    poll_id    TEXT PRIMARY KEY,
    chat_id    INTEGER,
    event_key  TEXT,
    message_id INTEGER,
    slot       TEXT,
    is_open    INTEGER DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS started_users (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,          -- lowercased, no @
    display_name TEXT,
    marked_at    TEXT
);

-- Facil-triggered jobs for the setup worker (e.g. /add_year_ones).
CREATE TABLE IF NOT EXISTS group_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER,
    og           TEXT,          -- 'AM3', 'PM7', ...
    kind         TEXT,          -- 'year_ones'
    status       TEXT,          -- 'pending' | 'done'
    requested_by INTEGER,
    requested_at TEXT
);

-- Who the worker has already added, so re-runs don't double-add or re-DM.
CREATE TABLE IF NOT EXISTS year_one_added (
    chat_id  INTEGER,
    handle   TEXT,              -- lowercased, no @
    added_at TEXT,
    PRIMARY KEY (chat_id, handle)
);
"""


def init_db():
    global _conn
    # timeout lets a write wait for the lock instead of erroring — bot.db is
    # shared by the bot and the setup worker (separate processes).
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=30000")
    with _lock:
        _conn.executescript(SCHEMA)
        # migrate older DBs created before the 'answer' column existed
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(attendance)")]
        if "answer" not in cols:
            _conn.execute("ALTER TABLE attendance ADD COLUMN answer TEXT")
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

def record_poll(poll_id, chat_id, event_key, message_id, slot):
    """Remember a poll we posted, so its votes can be tied to chat + event."""
    with _lock:
        _conn.execute(
            """INSERT OR REPLACE INTO polls
               (poll_id, chat_id, event_key, message_id, slot, is_open, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (poll_id, chat_id, event_key, message_id, slot, _now_iso()),
        )
        _conn.commit()


def get_poll(poll_id):
    with _lock:
        row = _conn.execute(
            "SELECT * FROM polls WHERE poll_id = ?", (poll_id,)
        ).fetchone()
    return dict(row) if row else None


def open_poll_messages(chat_id, event_key):
    """message_ids of still-open polls for an event — used to stop them."""
    with _lock:
        rows = _conn.execute(
            "SELECT message_id FROM polls "
            "WHERE chat_id = ? AND event_key = ? AND is_open = 1",
            (chat_id, event_key),
        ).fetchall()
    return [r["message_id"] for r in rows]


def close_event_polls(chat_id, event_key):
    with _lock:
        _conn.execute(
            "UPDATE polls SET is_open = 0 WHERE chat_id = ? AND event_key = ?",
            (chat_id, event_key),
        )
        _conn.commit()


def record_vote(chat_id, event_key, user_id, display_name, username, slot, answer):
    """Store (or update) one person's poll answer for an event."""
    with _lock:
        _conn.execute(
            """INSERT INTO attendance
               (chat_id, event_key, user_id, display_name, username, slot, answer, marked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, event_key, user_id) DO UPDATE SET
                   display_name = excluded.display_name,
                   username     = excluded.username,
                   slot         = excluded.slot,
                   answer       = excluded.answer,
                   marked_at    = excluded.marked_at""",
            (chat_id, event_key, user_id, display_name, username, slot, answer, _now_iso()),
        )
        _conn.commit()


def remove_vote(chat_id, event_key, user_id):
    """Drop someone's answer (they retracted their vote)."""
    with _lock:
        _conn.execute(
            "DELETE FROM attendance WHERE chat_id = ? AND event_key = ? AND user_id = ?",
            (chat_id, event_key, user_id),
        )
        _conn.commit()


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
    """Wipe answers (and poll records) for one event in one chat. Returns rows removed."""
    with _lock:
        cur = _conn.execute(
            "DELETE FROM attendance WHERE chat_id = ? AND event_key = ?",
            (chat_id, event_key),
        )
        _conn.execute(
            "DELETE FROM polls WHERE chat_id = ? AND event_key = ?",
            (chat_id, event_key),
        )
        _conn.commit()
        return cur.rowcount


def all_attendance(chat_id):
    """Every attendance row for a chat — used for CSV export."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM attendance WHERE chat_id = ? ORDER BY event_key, marked_at",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Who has messaged the bot /start (used by the group-provisioning scripts)
# ---------------------------------------------------------------------------

def mark_started(user_id, username, display_name):
    with _lock:
        _conn.execute(
            """INSERT INTO started_users (user_id, username, display_name, marked_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username = excluded.username,
                   display_name = excluded.display_name""",
            (user_id, (username or "").lower(), display_name, _now_iso()),
        )
        _conn.commit()


def get_started():
    with _lock:
        rows = _conn.execute("SELECT * FROM started_users").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Setup job queue (bot enqueues, the @zzehao worker fulfils)
# ---------------------------------------------------------------------------

def enqueue_request(chat_id, og, kind, requested_by):
    with _lock:
        _conn.execute(
            """INSERT INTO group_requests
               (chat_id, og, kind, status, requested_by, requested_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (chat_id, og, kind, requested_by, _now_iso()),
        )
        _conn.commit()


def pending_requests(kind):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM group_requests WHERE kind = ? AND status = 'pending' "
            "ORDER BY id",
            (kind,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_request_done(request_id):
    with _lock:
        _conn.execute(
            "UPDATE group_requests SET status = 'done' WHERE id = ?",
            (request_id,),
        )
        _conn.commit()


def already_added(chat_id, handle):
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM year_one_added WHERE chat_id = ? AND handle = ?",
            (chat_id, handle.lower()),
        ).fetchone()
    return row is not None


def record_added(chat_id, handle):
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO year_one_added (chat_id, handle, added_at) "
            "VALUES (?, ?, ?)",
            (chat_id, handle.lower(), _now_iso()),
        )
        _conn.commit()
