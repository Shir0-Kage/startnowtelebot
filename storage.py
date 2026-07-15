"""SQLite persistence for group settings and attendance.

One small database file (bot.db by default). The connection is shared across
the app with a lock, which is plenty for a single-process bot.
"""

import sqlite3
import threading
from datetime import datetime

from config import DB_PATH, TIMEZONE, REMINDERS_DEFAULT_ON, BINGO_PRIZE_LIMIT

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

-- Who the bot has DM'd their group invite link, so we don't message them twice.
CREATE TABLE IF NOT EXISTS link_sent (
    user_id INTEGER PRIMARY KEY,
    og      TEXT,
    sent_at TEXT
);

-- OGs a facil has opened via /add_year_ones (Year 1s can be let in).
CREATE TABLE IF NOT EXISTS opened_ogs (
    og        TEXT PRIMARY KEY,
    opened_at TEXT
);

-- Year 1s who /started before their OG was opened — held until the facil's
-- command. OG comes from their deep link, so this survives a wrong handle.
CREATE TABLE IF NOT EXISTS year1_waiting (
    user_id INTEGER PRIMARY KEY,
    og      TEXT,
    since   TEXT
);

-- ===================================================================
-- Human Bingo
-- ===================================================================

-- Frozen, even round-robin allocation of players to card templates (1..15).
-- Keyed on user_id so a later @username change never loses their sheet.
CREATE TABLE IF NOT EXISTS bingo_allocation (
    user_id     INTEGER PRIMARY KEY,
    handle      TEXT,              -- lowercased, no @ (best-effort, for audit)
    sheet_no    INTEGER,
    assigned_at TEXT
);

-- One row per submitted card. At most one 'pending' per submitter.
CREATE TABLE IF NOT EXISTS bingo_submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter_user_id INTEGER,
    submitter_handle  TEXT,
    sheet_no          INTEGER,
    corner_read       INTEGER,     -- OCR'd top-left number, may be NULL
    status            TEXT,        -- 'pending' | 'verified' | 'failed' | 'rejected'
    submitted_at      TEXT,
    verified_at       TEXT         -- set when it crosses the pass threshold
);

-- The chosen winning line's real (non-free) cells. prompt is frozen so the
-- confirmation button text stays stable even if templates change.
CREATE TABLE IF NOT EXISTS bingo_winning_members (
    submission_id  INTEGER,
    row            INTEGER,
    col            INTEGER,
    handle         TEXT,
    prompt         TEXT,
    target_user_id INTEGER,        -- resolved player, may be NULL (unreachable)
    PRIMARY KEY (submission_id, row, col)
);

-- Game-wide Yes/No cache keyed on (subject, prompt): a popular person is DMed
-- at most once per distinct prompt.
CREATE TABLE IF NOT EXISTS bingo_confirmations (
    subject_user_id INTEGER,
    prompt          TEXT,
    answer          TEXT,          -- 'yes' | 'no'
    responded_at    TEXT,
    PRIMARY KEY (subject_user_id, prompt)
);

-- Prize ledger + counter. UNIQUE winner is the last-line race guard.
CREATE TABLE IF NOT EXISTS bingo_prizes (
    winner_user_id INTEGER PRIMARY KEY,
    handle         TEXT,
    submission_id  INTEGER,
    claim_no       INTEGER,
    claimed_at     TEXT,
    posted_at      TEXT,           -- set once the channel post succeeds
    admin_notified_at TEXT         -- set once the admin(s) were DM'd about this winner
);

-- Single-row flags (e.g. 'closed' once the 10th prize is claimed).
CREATE TABLE IF NOT EXISTS bingo_flags (
    name    TEXT PRIMARY KEY,
    set_at  TEXT
);

-- ===================================================================
-- Anonymous whistleblowing
-- ===================================================================

-- Single-row: the learned channel/group link plus the active base-post
-- anchor. Never stores any whistleblower identity.
CREATE TABLE IF NOT EXISTS whistle (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    channel_id             INTEGER,
    group_id               INTEGER,
    anchor_msg_id          INTEGER,
    pending_channel_msg_id INTEGER,
    updated_at             TEXT
);

-- Recent channel-post -> discussion-group-copy mapping, learned from
-- auto-forwards. Lets an admin adopt an existing post as the base via its link
-- (/set_whistle_base). Holds only message ids — no whistleblower identity.
CREATE TABLE IF NOT EXISTS whistle_forward (
    channel_msg_id INTEGER PRIMARY KEY,
    group_id       INTEGER,
    group_msg_id   INTEGER,
    seen_at        TEXT
);
"""


def init_db():
    global _conn
    # timeout: how long a write waits for a busy lock before erroring. Kept short
    # so a lock conflict fails fast instead of parking the event-loop thread.
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    # synchronous=NORMAL: every storage write runs synchronously on the asyncio
    # event loop, and the default (FULL) fsyncs on EVERY commit. bot.db sits on a
    # busy Docker overlay filesystem where an fsync can stall for seconds — which
    # blocks the loop thread and freezes the whole bot (Ctrl+C dead). In WAL mode
    # NORMAL is crash-safe (at worst the last transaction is lost on an OS/power
    # crash — never corruption) and turns a commit into a fast buffered write, so
    # a storage call no longer stalls the loop. This is the fix for the hang.
    _conn.execute("PRAGMA synchronous=NORMAL")
    # cap any writer-lock wait at 5s instead of 30s (defence if a 2nd process
    # ever opens bot.db) so a collision degrades to a blip, not a long freeze.
    _conn.execute("PRAGMA busy_timeout=5000")
    with _lock:
        _conn.executescript(SCHEMA)
        # migrate older DBs created before the 'answer' column existed
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(attendance)")]
        if "answer" not in cols:
            _conn.execute("ALTER TABLE attendance ADD COLUMN answer TEXT")
        # migrate older DBs created before the 'admin_notified_at' column existed
        pcols = [r[1] for r in _conn.execute("PRAGMA table_info(bingo_prizes)")]
        if "admin_notified_at" not in pcols:
            _conn.execute("ALTER TABLE bingo_prizes ADD COLUMN admin_notified_at TEXT")
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


def mark_link_sent(user_id, og):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO link_sent (user_id, og, sent_at) VALUES (?, ?, ?)",
            (user_id, og, _now_iso()),
        )
        _conn.commit()


def link_sent_to(user_id):
    """The OG we've already DM'd this user (or None)."""
    with _lock:
        row = _conn.execute(
            "SELECT og FROM link_sent WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["og"] if row else None


def open_og(og):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO opened_ogs (og, opened_at) VALUES (?, ?)",
            (og, _now_iso()),
        )
        _conn.commit()


def is_og_opened(og):
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM opened_ogs WHERE og = ?", (og,)
        ).fetchone()
    return row is not None


def add_waiting(user_id, og):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO year1_waiting (user_id, og, since) VALUES (?, ?, ?)",
            (user_id, og, _now_iso()),
        )
        _conn.commit()


def waiting_for_og(og):
    with _lock:
        rows = _conn.execute(
            "SELECT user_id FROM year1_waiting WHERE og = ?", (og,)
        ).fetchall()
    return [r["user_id"] for r in rows]


def remove_waiting(user_id):
    with _lock:
        _conn.execute("DELETE FROM year1_waiting WHERE user_id = ?", (user_id,))
        _conn.commit()


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


# ---------------------------------------------------------------------------
# Human Bingo
# ---------------------------------------------------------------------------

def allocate_bingo_sheet(user_id, handle):
    """Return this user's frozen card number, assigning one on first call.

    Existing rows are never moved (frozen). A genuinely new user is dealt into
    the currently-smallest sheet so counts stay even (differ by at most 1),
    breaking ties toward the lowest sheet number for determinism."""
    with _lock:
        row = _conn.execute(
            "SELECT sheet_no FROM bingo_allocation WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is not None:
            return row["sheet_no"]
        counts = {s: 0 for s in range(1, 16)}
        for r in _conn.execute("SELECT sheet_no FROM bingo_allocation"):
            if r["sheet_no"] in counts:
                counts[r["sheet_no"]] += 1
        # smallest count, then smallest sheet number
        sheet_no = min(range(1, 16), key=lambda s: (counts[s], s))
        _conn.execute(
            "INSERT INTO bingo_allocation (user_id, handle, sheet_no, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, (handle or "").lower(), sheet_no, _now_iso()),
        )
        _conn.commit()
        return sheet_no


def get_bingo_sheet(user_id):
    """The user's frozen sheet number, or None if never allocated."""
    with _lock:
        row = _conn.execute(
            "SELECT sheet_no FROM bingo_allocation WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["sheet_no"] if row else None


def all_bingo_allocations():
    """Every user who was dealt a card (for the forward-round broadcast)."""
    with _lock:
        rows = _conn.execute("SELECT * FROM bingo_allocation").fetchall()
    return [dict(r) for r in rows]


def fwd_confirming_for(user_id):
    """This user's in-progress forwarded submission, or None."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE submitter_user_id = ? "
            "AND status = 'fwd_confirming' ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
    return dict(row) if row else None


def user_id_for_handle(handle):
    """Resolve a @handle to a user_id via started_users (they must have /started
    for us to reach them). Returns None if no such user is known."""
    h = (handle or "").lstrip("@").lower()
    with _lock:
        row = _conn.execute(
            "SELECT user_id FROM started_users WHERE username = ?", (h,)
        ).fetchone()
    return row["user_id"] if row else None


def bingo_is_closed():
    """True once the game has been closed (10th prize claimed)."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM bingo_flags WHERE name = 'closed'"
        ).fetchone()
    return row is not None


def set_bingo_closed():
    """Persist the closed flag. Idempotent."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES ('closed', ?)",
            (_now_iso(),),
        )
        _conn.commit()


def set_queue_open():
    """Open the confirmation round (persisted). Idempotent."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES ('queue_open', ?)",
            (_now_iso(),),
        )
        _conn.commit()


def is_queue_open():
    """True once the round has been opened (10 queued, or a facil command)."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM bingo_flags WHERE name = 'queue_open'"
        ).fetchone()
    return row is not None


def all_bingo_submissions():
    """Every submission row, earliest first (for the past-submission import)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def requeue_submission(submission_id):
    """Re-queue an existing submission in place: status -> 'queued', clear
    verified_at, keep submitted_at and id (so its winning_members stay linked)."""
    with _lock:
        _conn.execute(
            "UPDATE bingo_submissions SET status = 'queued', verified_at = NULL "
            "WHERE id = ?",
            (submission_id,),
        )
        _conn.commit()


def active_submission(user_id):
    """The submitter's current pending submission as a dict, or None."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bingo_submissions "
            "WHERE submitter_user_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def submission_by_id(submission_id):
    """Any submission by its id, regardless of status, as a dict (or None).

    Read-side pair to start_bingo_submission: the confirmation callback and the
    12h timeout job use this to map a submission_id back to its submitter
    (id, submitter_user_id, submitter_handle, status, sheet_no, ...)."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return dict(row) if row else None


def last_bingo_activity(user_id):
    """ISO timestamp of the user's most recent submission (for the retry
    cooldown), or None if they've never submitted."""
    with _lock:
        row = _conn.execute(
            "SELECT submitted_at FROM bingo_submissions "
            "WHERE submitter_user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["submitted_at"] if row else None


def start_bingo_submission(user_id, handle, sheet_no, corner_read=None):
    """Open a new pending submission and return its id.

    corner_read is retained for backwards compatibility / audit but is always
    NULL now — the wrong-sheet check was dropped (the printed sheet number is an
    OCR-unreadable pixel font), so verification leans on per-person confirmation.
    """
    with _lock:
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, corner_read, "
            " status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, corner_read, _now_iso()),
        )
        _conn.commit()
        return cur.lastrowid


def set_submission_status(submission_id, status, verified_at=None):
    """Move a submission to a terminal (or pending) state. verified_at is set
    only when provided (the moment it crossed the pass threshold)."""
    with _lock:
        if verified_at is None:
            _conn.execute(
                "UPDATE bingo_submissions SET status = ? WHERE id = ?",
                (status, submission_id),
            )
        else:
            _conn.execute(
                "UPDATE bingo_submissions SET status = ?, verified_at = ? "
                "WHERE id = ?",
                (status, verified_at, submission_id),
            )
        _conn.commit()


def record_winning_members(submission_id, members):
    """Store the chosen line's real cells. members: list of dicts with keys
    row, col, handle, prompt, target_user_id. Replaces any prior rows for
    this submission."""
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_winning_members WHERE submission_id = ?",
            (submission_id,),
        )
        _conn.executemany(
            "INSERT INTO bingo_winning_members "
            "(submission_id, row, col, handle, prompt, target_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (submission_id, m["row"], m["col"], m["handle"],
                 m["prompt"], m.get("target_user_id"))
                for m in members
            ],
        )
        _conn.commit()


def winning_members(submission_id):
    """The recorded line cells for a submission, ordered by (row, col)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_winning_members WHERE submission_id = ? "
            "ORDER BY row, col",
            (submission_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_bingo_confirmation(subject_user_id, prompt, answer):
    """Upsert a person's Yes/No for a prompt; the latest answer wins."""
    with _lock:
        _conn.execute(
            "INSERT INTO bingo_confirmations "
            "(subject_user_id, prompt, answer, responded_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subject_user_id, prompt) DO UPDATE SET "
            "    answer = excluded.answer, "
            "    responded_at = excluded.responded_at",
            (subject_user_id, prompt, answer, _now_iso()),
        )
        _conn.commit()


def get_cached_confirmation(subject_user_id, prompt):
    """The cached 'yes'/'no' for (subject, prompt), or None if unanswered."""
    with _lock:
        row = _conn.execute(
            "SELECT answer FROM bingo_confirmations "
            "WHERE subject_user_id = ? AND prompt = ?",
            (subject_user_id, prompt),
        ).fetchone()
    return row["answer"] if row else None


def has_bingo_prize(user_id):
    """True if this user has already won a prize."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM bingo_prizes WHERE winner_user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def claim_bingo_prize(user_id, handle, submission_id):
    """Atomically claim a prize slot. In ONE locked transaction: count existing
    prizes, refuse if the game is full (>= BINGO_PRIZE_LIMIT) or this winner
    already has one, else INSERT with claim_no = count + 1. Returns the slot
    number (1..limit) or None. UNIQUE(winner_user_id) is the last-line guard."""
    with _lock:
        count = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_prizes"
        ).fetchone()["c"]
        if count >= BINGO_PRIZE_LIMIT:
            return None
        already = _conn.execute(
            "SELECT 1 FROM bingo_prizes WHERE winner_user_id = ?", (user_id,)
        ).fetchone()
        if already is not None:
            return None
        claim_no = count + 1
        try:
            _conn.execute(
                "INSERT INTO bingo_prizes "
                "(winner_user_id, handle, submission_id, claim_no, "
                " claimed_at, posted_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (user_id, (handle or "").lower(), submission_id, claim_no,
                 _now_iso()),
            )
        except sqlite3.IntegrityError:
            # concurrent duplicate winner slipped past the check — UNIQUE wins
            _conn.rollback()
            return None
        _conn.commit()
        return claim_no


def bingo_prizes_claimed():
    """How many prizes have been awarded (derived from the DB, never memory)."""
    with _lock:
        row = _conn.execute("SELECT COUNT(*) AS c FROM bingo_prizes").fetchone()
    return row["c"]


def mark_prize_posted(user_id):
    """Record that this winner's channel announcement went out (once)."""
    with _lock:
        _conn.execute(
            "UPDATE bingo_prizes SET posted_at = ? "
            "WHERE winner_user_id = ? AND posted_at IS NULL",
            (_now_iso(), user_id),
        )
        _conn.commit()


def winners_pending_admin_notice():
    """Winners not yet DM'd to the facil admin(s), earliest prize first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_prizes WHERE admin_notified_at IS NULL "
            "ORDER BY claim_no"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_admin_notified(winner_user_id):
    """Record that the admin(s) were told about this winner (once)."""
    with _lock:
        _conn.execute(
            "UPDATE bingo_prizes SET admin_notified_at = ? "
            "WHERE winner_user_id = ? AND admin_notified_at IS NULL",
            (_now_iso(), winner_user_id),
        )
        _conn.commit()


def all_bingo_prizes():
    """Every claimed prize, earliest claim first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_prizes ORDER BY claim_no").fetchall()
    return [dict(r) for r in rows]


def pending_submissions():
    """All still-pending submissions (for re-arming 12h timeout jobs on
    startup), ordered by submission time."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'pending' "
            "ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def queue_submission(user_id, handle, sheet_no):
    """Enqueue a fresh submission, replacing this user's existing queued/confirming
    one (never a row already in tagged-people verification). Returns its id."""
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_submissions "
            "WHERE submitter_user_id = ? AND status IN ('queued','confirming')",
            (user_id,),
        )
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, corner_read, "
            " status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, NULL, 'queued', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, _now_iso()),
        )
        _conn.commit()
        return cur.lastrowid


def queued_in_order():
    """All 'queued' submissions, earliest first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'queued' "
            "ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def confirming_submissions():
    """All 'confirming' submissions, earliest first."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'confirming' "
            "ORDER BY submitted_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def active_slot_count():
    """In-flight prize slots: confirming + verifying(pending) + won(verified)."""
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('confirming','pending','verified')"
        ).fetchone()
    return row["c"]


def submission_status(submission_id):
    """A submission's current status string, or None if it doesn't exist."""
    with _lock:
        row = _conn.execute(
            "SELECT status FROM bingo_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return row["status"] if row else None


# --- Forward round: phase flags + forward-submission helpers ---------------

def set_forward_phase(phase):
    """Record the forward round phase ('collecting'|'verifying'|'released')."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO bingo_flags (name, set_at) VALUES (?, ?)",
            (f"forward_{phase}", _now_iso()),
        )
        _conn.commit()


def reset_forward_round():
    """Clear every forward-round phase flag so a fresh round can be opened
    (phases are cumulative flags, so a leftover 'released' otherwise sticks)."""
    with _lock:
        _conn.execute("DELETE FROM bingo_flags WHERE name LIKE 'forward_%'")
        _conn.commit()


def forward_phase():
    with _lock:
        rows = {r["name"] for r in _conn.execute(
            "SELECT name FROM bingo_flags WHERE name LIKE 'forward_%'")}
    for phase in ("released", "verifying", "collecting"):
        if f"forward_{phase}" in rows:
            return phase
    return None


def forward_started_at():
    with _lock:
        row = _conn.execute(
            "SELECT set_at FROM bingo_flags WHERE name = 'forward_collecting'").fetchone()
    return row["set_at"] if row else None


def forward_batch_active():
    return forward_phase() in ("collecting", "verifying")


def queue_forwarded_submission(user_id, handle, sheet_no, submitted_at):
    with _lock:
        _conn.execute(
            "DELETE FROM bingo_submissions WHERE submitter_user_id = ? "
            "AND status IN ('fwd_confirming','ready')", (user_id,))
        cur = _conn.execute(
            "INSERT INTO bingo_submissions "
            "(submitter_user_id, submitter_handle, sheet_no, corner_read, "
            " status, submitted_at, verified_at) "
            "VALUES (?, ?, ?, NULL, 'fwd_confirming', ?, NULL)",
            (user_id, (handle or "").lower(), sheet_no, submitted_at))
        _conn.commit()
        return cur.lastrowid


def set_forward_ready(submission_id):
    with _lock:
        _conn.execute(
            "UPDATE bingo_submissions SET status = 'ready' WHERE id = ?",
            (submission_id,))
        _conn.commit()


def ready_in_order():
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bingo_submissions WHERE status = 'ready' "
            "ORDER BY submitted_at, id").fetchall()
    return [dict(r) for r in rows]


def forward_entry_count():
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('fwd_confirming','ready')").fetchone()
    return row["c"]


def active_forward_verifying_count():
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS c FROM bingo_submissions "
            "WHERE status IN ('pending','verified')").fetchone()
    return row["c"]


# ---------------------------------------------------------------------------
# Anonymous whistleblowing
# ---------------------------------------------------------------------------

def _whistle_row():
    with _lock:
        row = _conn.execute("SELECT * FROM whistle WHERE id = 1").fetchone()
    return dict(row) if row else None


def set_whistle_link(channel_id, group_id):
    with _lock:
        _conn.execute(
            "INSERT INTO whistle (id, channel_id, group_id, updated_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET channel_id=excluded.channel_id, "
            "group_id=excluded.group_id, updated_at=excluded.updated_at",
            (channel_id, group_id, _now_iso()))
        _conn.commit()


def set_whistle_channel(channel_id):
    """Record just the channel id — learned from a direct channel post the bot
    sees as a channel admin — leaving any already-known group_id untouched."""
    with _lock:
        _conn.execute(
            "INSERT INTO whistle (id, channel_id, updated_at) "
            "VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET channel_id=excluded.channel_id, "
            "updated_at=excluded.updated_at",
            (channel_id, _now_iso()))
        _conn.commit()


def get_whistle_link():
    row = _whistle_row()
    return (row["channel_id"], row["group_id"]) if row else (None, None)


def set_whistle_pending(channel_msg_id):
    with _lock:
        _conn.execute(
            "INSERT INTO whistle (id, pending_channel_msg_id, updated_at) "
            "VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "pending_channel_msg_id=excluded.pending_channel_msg_id, "
            "updated_at=excluded.updated_at",
            (channel_msg_id, _now_iso()))
        _conn.commit()


def resolve_whistle_anchor(channel_msg_id, anchor_msg_id):
    with _lock:
        row = _conn.execute(
            "SELECT pending_channel_msg_id FROM whistle WHERE id = 1").fetchone()
        if not row or row["pending_channel_msg_id"] != channel_msg_id:
            return False
        _conn.execute(
            "UPDATE whistle SET anchor_msg_id = ?, pending_channel_msg_id = NULL, "
            "updated_at = ? WHERE id = 1", (anchor_msg_id, _now_iso()))
        _conn.commit()
        return True


def get_whistle_anchor():
    row = _whistle_row()
    if not row or row.get("anchor_msg_id") is None:
        return (None, None)
    return (row["group_id"], row["anchor_msg_id"])


def set_whistle_anchor(group_id, anchor_msg_id):
    """Point the active anchor at a specific discussion-group message (used when
    an admin adopts an existing post as the base). Clears any pending post."""
    with _lock:
        _conn.execute(
            "INSERT INTO whistle (id, group_id, anchor_msg_id, "
            "pending_channel_msg_id, updated_at) VALUES (1, ?, ?, NULL, ?) "
            "ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, "
            "anchor_msg_id=excluded.anchor_msg_id, pending_channel_msg_id=NULL, "
            "updated_at=excluded.updated_at",
            (group_id, anchor_msg_id, _now_iso()))
        _conn.commit()


def remember_forward(channel_msg_id, group_id, group_msg_id):
    """Record a channel post's discussion-group copy, seen via its auto-forward,
    so an admin can later adopt it as the base by its link. Kept to the most
    recent 200 posts."""
    with _lock:
        _conn.execute(
            "INSERT INTO whistle_forward "
            "(channel_msg_id, group_id, group_msg_id, seen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel_msg_id) DO UPDATE SET group_id=excluded.group_id, "
            "group_msg_id=excluded.group_msg_id, seen_at=excluded.seen_at",
            (channel_msg_id, group_id, group_msg_id, _now_iso()))
        _conn.execute(
            "DELETE FROM whistle_forward WHERE channel_msg_id NOT IN "
            "(SELECT channel_msg_id FROM whistle_forward "
            "ORDER BY seen_at DESC LIMIT 200)")
        _conn.commit()


def lookup_forward(channel_msg_id):
    """The (group_id, group_msg_id) copy of a channel post, or (None, None)."""
    with _lock:
        row = _conn.execute(
            "SELECT group_id, group_msg_id FROM whistle_forward "
            "WHERE channel_msg_id = ?", (channel_msg_id,)).fetchone()
    return (row["group_id"], row["group_msg_id"]) if row else (None, None)
