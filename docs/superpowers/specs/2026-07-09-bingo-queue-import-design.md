# Bingo Queue — Round-Open Kickoff + Past-Submission Import Design

**Date:** 2026-07-09
**Status:** Approved (in-chat)
**Builds on:** `2026-07-08-bingo-submission-queue-design.md` (the submission queue, now on `main`)

## Motivation

Two gaps surfaced after the submission queue shipped:

1. **Kickoff timing is "eager", not batched.** `maybe_kickoff` runs after every
   submission and fills the 10 slots immediately, so the first 10 submitters each
   get their confirmation the instant they submit — contradicting the "You're in
   the queue — hang tight" message and the approved "automatically once 10 have
   submitted" design, and making the confirm race unfair (early submitters get a
   head start).

2. **Past submitters aren't in the queue.** Submissions made before the queue
   deployed (all of which already had a winning line, since the old flow only
   recorded a submission when a line was found) never enter the new confirmation
   flow. Facils want to fold everyone who has already submitted into the queue.

## Part 1 — Round-open kickoff (replaces eager filling)

A game-wide **`queue_open`** flag (persisted in `bingo_flags`, like `closed`)
gates promotion:

- Submissions accumulate as `queued` ("You're in the queue (#N) — hang tight").
  **Nothing is promoted while the round is closed.**
- The round **opens** when **either**:
  - the number of `queued` submissions reaches `BINGO_PRIZE_LIMIT` (10) — checked
    in `enqueue`, automatic; **or**
  - a facil runs `/close_bingo_round` or `/import_bingo_queue` — manual, for a
    small game or to start early.
- Once open it stays open (the flag persists across restarts). `maybe_kickoff`
  promotes only when `queue_open` is set: it then fills up to 10 active slots
  (earliest `submitted_at` first) and **rolls** (promotes the next queued as each
  slot frees), exactly as today.

Net effect: the earliest 10 are messaged **together** when the round opens; the
rest roll in as slots free. Matches the "hang tight" UX and makes the race fair.

## Part 2 — `/import_bingo_queue` (facil command)

Folds past submitters into the queue. Rules (as specified):

- **Who:** every player who has submitted a valid line and has **not already won**
  (a winner — has a row in `bingo_prizes` — is excluded and keeps their prize).
  ("Failed people" = all non-winners; under the old flow every recorded
  submission had a line, so this is simply "everyone who submitted, minus
  winners.")
- **One check per person — their FIRST submission only.** For each eligible
  player, their **earliest** submission (min `submitted_at`, tie-break min `id`)
  is the one re-queued. All their **other** submissions are **superseded**
  (a new terminal status `superseded`, ignored by the queue and by
  `active_slot_count`), so nobody is checked twice — including a submission
  currently mid-verification.
- **Order:** the re-queued rows keep their **original `submitted_at`**, so
  `queued_in_order()` ranks them earliest → latest, stable against new
  submissions.
- **Start immediately:** after importing, the command **opens the round** (sets
  `queue_open`) and runs `maybe_kickoff`, so the earliest 10 get their checks
  right away and the rest roll in.
- **Idempotent:** re-running only imports players whose current state is terminal
  (not already `queued`/`confirming`/`pending`/`verified`, and not a winner), so a
  second run won't double-queue anyone mid-flight. (A player who has since failed
  again is eligible for re-import — a fresh shot — which is acceptable.)

### Reconstructing the confirmation

Old rows store the **winning line** (`bingo_winning_members`: row, col, handle per
cell) but not the full 24-square card. The import rebuilds a minimal read from
those members — `{"cells": [{row, col, handle, score:100} …]}` — and seeds
`_PENDING_READ[submission_id]`. `evaluate()` then reproduces the line:

- everyone in the line has `/started` → **short** ✅ confirm message;
- someone is now unreachable → **full** fill-in template flagging who must `/start`
  (only the line cells are pre-filled; the rest are blank, which is all the
  tagged-people check needs).

Because `bingo_winning_members` is persisted, the same reconstruction is used as a
**restart fallback**: if `_PENDING_READ` is missing but the submission has recorded
members, rebuild from them (instead of the "please resend" prompt). New-flow
queued submissions have no members yet, so they still fall back to the resend
prompt.

## Data / storage

Reuse existing tables. Additions:

- `bingo_flags` row `queue_open` — `set_queue_open()`, `is_queue_open()`.
- New terminal `status` value `superseded` (no schema change; `status` is free
  text). Not counted by `active_slot_count`, not returned by `queued_in_order` /
  `confirming_submissions` / `pending_submissions`.
- Read helpers for the import: all submissions grouped/scannable by user; the
  eligible earliest-per-user set (excluding winners and players with a live
  submission).
- A helper to re-queue an existing row in place: set `status='queued'`,
  `verified_at=NULL`, keep `submitted_at` and `id` (so its `winning_members` stay
  linked for reconstruction).

## What stays the same

- The whole confirmation → tagged-people verify → prize pipeline, the 10-prize
  cap and close-at-10, `/get_bingo`, both submit input modes, OCR isolation, and
  all freeze protections.
- `queue_submission`'s per-user dedup of `queued`/`confirming` (new submissions).

## Testing

- `queue_open` gates `maybe_kickoff`; `enqueue` opens at 10; `close_round` /
  import open early; flag persists (re-arm path).
- Import: excludes winners; dedups to earliest submission per user; supersedes the
  rest; preserves `submitted_at` order; opens the round; earliest-10 messaged;
  idempotent on re-run.
- Reconstruction from `winning_members` yields the right short/full message and
  serves as the restart fallback.
