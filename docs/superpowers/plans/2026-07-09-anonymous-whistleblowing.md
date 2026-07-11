# Anonymous Whistleblowing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `/start_whistle` (admin only) opens a base post in the linked channel; anyone DMs `/whistle <text>` and the bot posts it anonymously as a comment under that post (a reply in the channel's discussion group), never revealing or logging the sender.

**Architecture:** A new `handlers/whistle.py` owns two commands + one capture handler. The bot **auto-learns** the channel + discussion-group ids from the first auto-forwarded channel post (it's a group admin). A single-row `whistle` storage table holds the ids + the active base-post anchor. No whistleblower data is ever stored or logged.

**Tech Stack:** python-telegram-bot v22 (`Message.is_automatic_forward`, `reply_to_message_id`), sqlite3.

## Global Constraints

- Python 3.12; tests `.venv/Scripts/python.exe -m pytest`; Git Bash. Human-style commits, NO `Co-Authored-By`/AI trailer. Before committing, `git status --short`; restore any deleted `__init__.py`.
- Storage idiom: `with _lock:`, `_now_iso()`, `_conn.commit()`, `dict(row)`.
- **ANONYMITY (hard requirement):** the `/whistle` path must never log, store, echo, or persist the sender's id/username/name. The report text is passed straight to the channel and not saved. Any `log.*` on the whistle path must NOT include sender-identifying data.
- Admin gate for `/start_whistle` uses `utils.auth.is_admin(update.effective_user)` (@zzehao is always an admin).
- Cannot be end-to-end tested from the dev host (needs the live channel + discussion group with the bot admin in both); tests are unit-level with mocked bot/updates. The user does the live check post-deploy.

## File Structure

- `storage.py` (modify) — `whistle` table + helpers.
- `handlers/whistle.py` (create) — `start_whistle`, `whistle`, `on_channel_autoforward`, `register`.
- `main.py` (modify) — `whistle.register(app)`.
- `handlers/common.py` (modify) — HELP_TEXT entries.
- Tests: `tests/test_bingo_storage.py` (or a new `tests/test_whistle.py`), `tests/test_whistle.py` (create).

---

### Task 1: Storage — `whistle` table + helpers

**Files:** Modify `storage.py`; Test `tests/test_bingo_storage.py` (append, reuse `store`).

**Interfaces (Produces):**
- `set_whistle_link(channel_id, group_id)` — upsert the learned ids on the single row.
- `get_whistle_link() -> (channel_id, group_id)` — `(None, None)` if unset.
- `set_whistle_pending(channel_msg_id)` — record the base post's channel message id, awaiting its auto-forward.
- `resolve_whistle_anchor(channel_msg_id, anchor_msg_id) -> bool` — iff the pending channel id matches, set the active anchor (the discussion-group copy's id) and clear pending; return whether it matched.
- `get_whistle_anchor() -> (group_id, anchor_msg_id)` — `(None, None)` if no active thread.

- [ ] **Step 1: Failing tests** (append to `tests/test_bingo_storage.py`, reuse `store`)

```python
def test_whistle_link_and_anchor_lifecycle(store):
    assert store.get_whistle_link() == (None, None)
    assert store.get_whistle_anchor() == (None, None)
    store.set_whistle_link(-100123, -100456)          # channel, group
    assert store.get_whistle_link() == (-100123, -100456)
    store.set_whistle_pending(77)                      # base post's channel msg id
    assert store.resolve_whistle_anchor(88, 999) is False   # wrong channel id -> no match
    assert store.get_whistle_anchor() == (None, None)
    assert store.resolve_whistle_anchor(77, 500) is True    # matches pending -> sets anchor
    assert store.get_whistle_anchor() == (-100456, 500)     # (group_id, anchor msg id)
    assert store.resolve_whistle_anchor(77, 501) is False    # pending cleared -> no re-resolve
```

- [ ] **Step 2: Run → fail.** `.venv/Scripts/python.exe -m pytest tests/test_bingo_storage.py -q`

- [ ] **Step 3: Implement.** In `storage.py` `SCHEMA`, add:

```sql
CREATE TABLE IF NOT EXISTS whistle (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    channel_id             INTEGER,
    group_id               INTEGER,
    anchor_msg_id          INTEGER,
    pending_channel_msg_id INTEGER,
    updated_at             TEXT
);
```

Add the helpers:

```python
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
```

- [ ] **Step 4: Run → pass** (existing + new). **Step 5: Commit** `git add storage.py tests/test_bingo_storage.py` / `git commit -m "Add whistle storage: channel/group link + base-post anchor"`.

---

### Task 2: `handlers/whistle.py` — commands + capture

**Files:** Create `handlers/whistle.py`; Test `tests/test_whistle.py` (create).

**Interfaces (Produces):** `on_channel_autoforward(update, context)`, `start_whistle(update, context)`, `whistle(update, context)`, `register(app)`.

- [ ] **Step 1: Failing tests** (`tests/test_whistle.py`) — mocked bot + updates + the temp-DB `store` fixture (copy the `store` fixture from `tests/test_bingo_storage.py`). Cover: capture stores link + resolves a matching pending anchor; `start_whistle` blocks a non-admin, handles not-linked, and on the linked path posts to the channel + sets pending; `whistle` refuses non-private, refuses when no anchor, and on the happy path calls `bot.send_message(chat_id=group_id, reply_to_message_id=anchor, text=<contains the report + "Anonymous">)` then replies "Sent anonymously". Assert the report post does NOT include the sender's id/username anywhere. Monkeypatch `whistle.is_admin` for the admin test and `whistle.storage` to the temp store.

- [ ] **Step 2: Run → fail** (module missing).

- [ ] **Step 3: Implement `handlers/whistle.py`:**

```python
"""Anonymous whistleblowing. An admin opens a thread (a base post in the linked
channel); anyone DMs /whistle <text> and the bot posts it as a comment under that
post (a reply in the channel's discussion group) WITHOUT ever revealing or logging
the sender. The bot auto-learns the channel + discussion-group ids from the first
auto-forwarded channel post (it is an admin in the discussion group)."""

import logging

from telegram.ext import CommandHandler, MessageHandler, filters

import storage
from utils.auth import is_admin

log = logging.getLogger(__name__)

_BASE_TEXT = ("🔔 Anonymous whistleblowing is open.\n\n"
             "DM me  /whistle <your message>  and it'll appear here anonymously — "
             "your name is never shown or logged.")


async def on_channel_autoforward(update, context):
    """A channel post auto-copied into the linked discussion group: learn the
    channel + group ids, and resolve a pending base-post anchor if this is it."""
    msg = update.effective_message
    if msg is None or not getattr(msg, "is_automatic_forward", False):
        return
    origin = msg.forward_from_chat or msg.sender_chat
    if origin is None:
        return
    storage.set_whistle_link(origin.id, msg.chat.id)
    if msg.forward_from_message_id is not None:
        storage.resolve_whistle_anchor(msg.forward_from_message_id, msg.message_id)


async def start_whistle(update, context):
    if not is_admin(update.effective_user):
        await update.effective_message.reply_text(
            "Only an admin can open a whistle thread.")
        return
    channel_id, _group = storage.get_whistle_link()
    if channel_id is None:
        await update.effective_message.reply_text(
            "I'm not linked to the whistle channel yet — post anything in the "
            "channel once so I can find it, then run /start_whistle again.")
        return
    try:
        post = await context.bot.send_message(chat_id=channel_id, text=_BASE_TEXT)
    except Exception as exc:
        log.warning("couldn't post whistle base message: %s", exc)
        await update.effective_message.reply_text(
            "Couldn't post to the channel — make sure I'm still an admin there.")
        return
    storage.set_whistle_pending(post.message_id)
    await update.effective_message.reply_text(
        "Whistle thread posted 🔔 — anonymous reports will appear as comments under it.")


async def whistle(update, context):
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await update.effective_message.reply_text(
            "DM me privately so no one sees you reporting 🙏")
        return
    # everything after the command word; split(maxsplit=1) keeps newlines in the body
    parts = (update.effective_message.text or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.effective_message.reply_text(
            "Send it like:  /whistle <your message>")
        return
    group_id, anchor = storage.get_whistle_anchor()
    if group_id is None or anchor is None:
        await update.effective_message.reply_text(
            "No whistle thread is open right now — ask an admin to run /start_whistle.")
        return
    try:
        await context.bot.send_message(
            chat_id=group_id,
            text="🔔 Anonymous report:\n\n" + text,
            reply_to_message_id=anchor)
    except Exception as exc:
        # NOTE: never log the sender — anonymity. Only the failure reason.
        log.warning("couldn't post anonymous whistle: %s", exc)
        await update.effective_message.reply_text(
            "Something went wrong sending that — please try again in a moment.")
        return
    await update.effective_message.reply_text("Sent anonymously ✅")


def register(app):
    app.add_handler(CommandHandler("start_whistle", start_whistle))
    app.add_handler(CommandHandler("whistle", whistle))
    # capture auto-forwarded channel posts in the discussion group (group=1 so it
    # never shadows the other group handlers).
    app.add_handler(
        MessageHandler(filters.IS_AUTOMATIC_FORWARD, on_channel_autoforward),
        group=1)
```

> If `filters.IS_AUTOMATIC_FORWARD` isn't available in this PTB version, use a broad
> group filter and gate inside on `msg.is_automatic_forward` (already checked in
> `on_channel_autoforward`). Verify which exists.

- [ ] **Step 4: Run → pass** (`tests/test_whistle.py` + full suite). **Step 5: Commit** `git add handlers/whistle.py tests/test_whistle.py` / `git commit -m "Add whistleblowing handlers: /start_whistle, /whistle, auto-forward capture"`.

---

### Task 3: Wire it up + help text

**Files:** Modify `main.py`, `handlers/common.py`; Test `tests/test_whistle.py`.

**Interfaces (Produces):** `whistle.register(app)` called at startup; `/whistle` (public) + `/start_whistle` (admin) lines in HELP_TEXT; a `register` test.

- [ ] **Step 1: Failing test** — a `test_register_adds_whistle_handlers` mirroring the bingo register test: `whistle.register(MagicMock())` adds the two `CommandHandler`s (assert their callbacks are `start_whistle`/`whistle`) and the auto-forward `MessageHandler`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** In `main.py`, add `from handlers import ... whistle` and `whistle.register(app)` alongside the other `*.register(app)` calls. In `handlers/common.py` HELP_TEXT, add a public line `/whistle <message> — report something anonymously (DM me)` and, under the facil/admin section, `/start_whistle — open an anonymous whistle thread (admin)`.
- [ ] **Step 4: Run full suite** → green; `.venv/Scripts/python.exe -c "import main"` OK. **Step 5: Commit** `git add main.py handlers/common.py tests/test_whistle.py` / `git commit -m "Register whistleblowing commands + help"`.

---

## Self-Review

- **Coverage:** `/start_whistle` admin-only via `is_admin` (T2) ✓; `/whistle` anyone + DM-only + anonymous (T2) ✓; auto-learn ids + anchor from the auto-forward (T2) ✓; storage single-row, no sender data (T1) ✓; wiring + help (T3) ✓.
- **Anonymity:** the `/whistle` handler reads `text`, posts it, and replies — it never passes the sender's id/username/name to `storage`, `log`, or the channel message. The `whistle` table has no sender column. (T2 test asserts no sender leak in the posted text.)
- **Isolation:** the auto-forward `MessageHandler` is in `group=1` and filters on `IS_AUTOMATIC_FORWARD`, so it can't shadow the bingo/provisioning group handlers.
- **Type consistency:** `get_whistle_link -> (channel_id, group_id)`, `get_whistle_anchor -> (group_id, anchor_msg_id)`, `resolve_whistle_anchor -> bool` used identically in `handlers/whistle.py`.
- **Untestable-from-here note:** the actual "comment appears under the base post" behavior depends on the live channel/discussion-group link + bot admin rights; unit tests mock the bot, and the user verifies live post-deploy.
