# Anonymous Whistleblowing Design

**Date:** 2026-07-09
**Status:** Approved (in-chat)

## Motivation

Let Year 1s report concerns anonymously. An admin opens a "whistleblowing" thread
(a base post in a private channel); anyone DMs the bot a report, and the bot posts
it — **by the bot, never revealing the sender** — as a **comment** under that base
post. Channel members see only the anonymous comment.

## How Telegram "comments" work (the mechanic this relies on)

Comments exist only on a **channel** post when the channel has a **linked
discussion group**. A channel post is auto-copied into the discussion group; a
reply to that copy appears under the post's "Comments". So the bot posts a whistle
by sending a message **into the discussion group as a reply** to the base post's
auto-forwarded copy.

Requirements (confirmed): the target (`t.me/+g_EHFWxQtqAwOTY1`) is a **channel with a
linked discussion group**, and the bot is an **admin in both**. As a discussion-group
admin the bot receives every message there, including the auto-forwarded channel
posts — which is how it learns the IDs.

## Behaviour

### Auto-learn (no hardcoded IDs, no secrets)
A handler on discussion-group messages that are **auto-forwards** (`is_automatic_forward`)
captures and stores the **channel id** (`forward_from_chat`/`sender_chat`) and the
**discussion-group id** (`chat`). This links the bot to the channel the first time
any post is auto-forwarded there. Also: if the auto-forward's `forward_from_message_id`
matches a **pending** base post (see `/start_whistle`), it records that copy's
`message_id` as the **active anchor** (what whistles reply to).

### `/start_whistle` — admin only (@zzehao / `is_admin`)
- If the bot hasn't learned the channel yet → reply *"Post anything in the channel
  once so I can link up, then run this again."*
- Else post the base message to the channel:
  *"🔔 Anonymous whistleblowing is open. DM me `/whistle <your message>` and it'll
  appear here anonymously."* Store its channel `message_id` as the **pending anchor**.
- The auto-forward handler resolves the pending anchor to the discussion-group copy
  when it arrives (usually within a second).
- Reply to the admin: *"Whistle thread posted. Reports will appear under it."*
- Each run posts a **new** base post; whistles target the **latest** anchor.

### `/whistle <message>` — anyone, DM-only, anonymous
- **Private chat only** — in a group it refuses: *"DM me privately so no one sees
  you reporting."*
- If no active anchor → *"No whistle thread is open right now — ask an admin to run
  /start_whistle."*
- Post into the discussion group as a reply to the anchor:
  `bot.send_message(chat_id=group_id, text="🔔 Anonymous report:\n\n"+msg,
  reply_to_message_id=anchor_msg_id)` → surfaces under the base post's Comments.
- Reply privately: *"Sent anonymously ✅."*
- **Anonymity: the handler never logs, stores, or echoes the sender's id/username/name.**
  The report text is passed straight through to the channel and not persisted.

## Data / storage

New single-row `whistle` table (no whistleblower data ever stored):

```
whistle(id=1, channel_id, group_id, anchor_msg_id, pending_channel_msg_id, updated_at)
```

Helpers: `set_whistle_link(channel_id, group_id)`, `get_whistle_link() -> (channel_id, group_id)`,
`set_whistle_pending(channel_msg_id)`, `resolve_whistle_anchor(channel_msg_id, anchor_msg_id) -> bool`
(sets anchor + clears pending iff the pending channel id matches),
`get_whistle_anchor() -> (group_id, anchor_msg_id) | (None, None)`.

## Module structure

- `handlers/whistle.py` (new) — `start_whistle` (`@` admin-gated via `utils.auth.is_admin`),
  `whistle` (DM-only), `on_channel_autoforward` (capture), `register(app)`.
- `storage.py` — the `whistle` table + helpers.
- `main.py` — `whistle.register(app)` alongside the others.
- `handlers/common.py` — a `/whistle` line in HELP_TEXT (public), `/start_whistle` under the admin section.

## Anonymity guarantees & limits

- No sender identity is logged or stored anywhere for `/whistle`.
- The comment is posted by the bot, so channel members can't see the reporter.
- Honest limit: the bot's process momentarily receives the sender's Telegram id (to
  reply "sent ✅"); it is never recorded. Anonymity is toward the channel + the logs,
  not toward whoever runs the server process.

## Testing

- Unit (mocked bot + updates): auto-forward capture stores link + resolves a pending
  anchor; `/start_whistle` admin-gate + not-linked path + posts + sets pending;
  `/whistle` DM-only refusal, no-thread path, posts as a reply to the anchor with the
  anonymous prefix, replies "sent ✅", and does NOT reference the sender in any
  stored/logged value.
- **Live check (post-deploy, by the user):** the bot must be admin in the channel +
  discussion group with the link active; then `/start_whistle` then a DM `/whistle test`
  should show an anonymous comment under the base post. Not testable from the dev host.
