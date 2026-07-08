# Human Bingo — Submission Queue & Submitter-Confirmation Design

**Date:** 2026-07-08
**Status:** Draft for review

## Motivation

Today a winning `/submit_bingo` is processed **immediately**: the bot DMs the
tagged people ("is this you?") and the first 10 lines to pass those checks claim
a prize. We want a fairer, more controlled prize round:

- Rank submissions by submission time (earliest first).
- The **10 earliest** submitters are the prize candidates.
- Before the tagged-people check, the **submitter** first confirms/completes
  their own card (a self-review step).
- If a candidate drops out, the next person in the queue takes their slot.

## Definitions

- **Reachable handle** — a matched roster handle whose person has `/start`ed the
  bot, i.e. `storage.user_id_for_handle(handle)` is not `None`. The bot can only
  DM (and therefore verify) reachable people.
- **Fully recognised submission** — the card yields a **complete 5-in-a-row**
  winning line in which **every** cell's handle is both matched to a roster
  handle **and reachable**. (The FREE centre counts as filled, so a line through
  the centre has 4 fillable cells.)
- **Not fully recognised** — anything else: a square's text didn't match a roster
  handle, or there's no complete matched line, or a matched handle is
  **unreachable** (that person hasn't `/start`ed).

## The flow

### 1. On submit (replaces today's immediate processing)

A submission arrives via either mode (photo → OCR → "looks right?" confirm, or
text). We parse it to the `{cells}` shape (unchanged), then:

- Record it into the **queue**: state `queued`, with `submitted_at` and the
  recognised cells / chosen winning line.
- **One live submission per person** — a re-submit replaces the person's existing
  queued/confirming submission (never two rows for one user).
- Reply: **"You're in the queue (#N). I'll message you if I need you to confirm
  your squares."**
- **No** tagged-people DM and **no** prize at this point.

### 2. Kickoff — automatically when the queue reaches 10

The moment 10 submissions are `queued`, the bot sends each of the **10 earliest**
its confirmation message and moves them to `confirming`:

- **Fully recognised** → **short** message: just the **4–5 winning-line cells**,
  with a **✅ Confirm** button (tap to confirm the line is correct).
- **Not fully recognised** → **full** message: the fill-in list with our attempted
  matches **pre-filled** and blanks for the rest, **plus** a flag for any matched
  but **unreachable** handle: *"@X hasn't started the bot — ask them to send it
  `/start` so I can verify them."* The submitter edits and **resends** (see 3).

**Fallback for a small game:** if fewer than 10 ever submit, a facil can close the
round (facil command / game close) to fire the batch over whoever's queued.

### 3. Submitter responds (state `confirming`)

- **Short → ✅** → confirmed; proceed to step 4.
- **Full → edit & resend** → re-parsed. **Unlimited resends** allowed. If it now
  meets *fully recognised* → confirmed, proceed to step 4. If not (still blanks /
  unreachable people) → we re-send the updated full message + flags and keep
  waiting.
- **Timeout** — a submitter **fails only** by not reaching a confirmed state
  within `BINGO_CONFIRM_TIMEOUT` (12h). A bad resend never fails them; going
  silent does.

### 4. Tagged-people verification (unchanged, state `verifying`)

Once the submitter confirms a fully-recognised line, the existing
`_dm_subjects` → `bingoconf` → `_finalize` pipeline runs: the tagged people get
"is this you?", their answers decide the line, and a pass claims the prize
(`won`). Because *fully recognised* guarantees every tagged person is reachable,
there are no silent "unreachable = miss" gaps here.

### 5. Rolling replacement

There are up to **10 active slots** (submissions in `confirming` / `verifying` /
`won`). When a `confirming` submission **times out** (→ `failed`), its slot frees
and the **next `queued`** submission (by `submitted_at`) is pulled into
`confirming` and messaged. Continues until 10 have `won` or the queue is empty.

> **To confirm in review:** if a submission reaches `verifying` and the
> tagged-people check *fails* (a tagged person says "no"), does that also free the
> slot for the next queued person? Recommended: **yes** — they didn't win, so the
> slot rolls on. (The submitter-confirm *timeout* is still the only failure the
> submitter controls.)

## Data / storage

Extend the existing `bingo_submissions` row (no new table):

- `status`: `queued → confirming → verifying → won | failed`.
- Reuse `submitted_at` for ordering, `winning_members` for the chosen line,
  `bingo_confirmations` / `bingo_prizes` unchanged.
- A submitter's typed/edited cells are re-parsed on each resend; we persist the
  current chosen line via `record_winning_members`.
- New helpers (names illustrative): `queue_submission`, `queued_in_order`,
  `active_slot_count`, `promote_next_queued`, `set_status`.

## Templates (the first ask: pre-generate)

- Pre-build the **15 blank fill-in templates** once at startup and cache them
  (`bingo_text.build_template_text` is currently rebuilt per call). The short
  (winning-line) and prefilled (per-submission) messages are still built on
  demand since they depend on the submission.

## What stays the same

- Card allocation (`/get_bingo`), the 15 templates, both submission input modes
  (photo/OCR-confirm and text), the winning-line detection (`bingo_lines`), the
  tagged-people confirmation mechanics, and the prize/close logic (10 limit).
- The OCR subprocess isolation and all freeze protections.

## Testing

- Queue ordering + one-live-submission-per-person.
- Kickoff at 10; short vs full message selection by *fully recognised*.
- Unreachable-handle flagging in the full message.
- Resend loop (unlimited) + timeout-only failure.
- Rolling replacement promotes the next queued on failure.
- Fully-recognised → tagged-people verify → prize, in queue order.
- Fallback close fires the batch for < 10 submissions.
