# Forward-Based Batch Prize Round Design

**Date:** 2026-07-09
**Status:** Approved (in-chat)
**Builds on:** the submission queue + round-open + import features (all on `main`).

## Motivation

The old bot discarded every bingo submission that wasn't a recognised winning line, so there's no record of who submitted (`bingo_submissions` is empty; 34 people got cards). To recover a fair prize round without that data, players **forward** their original card photos back to the bot — a forwarded message carries `forward_origin.date` (the original send time), so we can order by true submission time. This is a self-contained "forward round" run once by a facil.

## Approved decisions

1. **Confirm as they forward** — the confirmation template is sent the moment someone forwards a card (they confirm during the collection window).
2. **Results are batched** — winner announcements are held and released all at once when the 10 winners are settled; @zzehao gets one message with all winners' handles. The per-win DMs from the winner-notify feature are suppressed during this round.
3. **Keep the tagged-people check** — a line must still pass the "is this you?" verification to win. Verification runs strictly in **earliest-original-time order** (it starts when collection closes, not as each person confirms).

## DESIGN REVISION (2026-07-09) — no tagged-people verification

Decision 3 above is **superseded**: the "is this you?" tagged-people verification is
**removed entirely**. New rule:

- **A win = a confirmed 5-in-a-row where all 5 handles are roster handles** (i.e.
  `bingo_lines.winning_lines` returns a line — it only ever does so when every cell is
  a confident roster match). **No reachability requirement, no verification DMs.**
- The self-confirmation step (short "here's your line — ✅ / fix it" or full fill-in
  template) is **kept** so an OCR misread doesn't cost a real win — but the short vs
  full choice now keys on **"a winning line exists"**, not "fully recognised/reachable".
- A confirmed line → status `ready` (a valid winning entry).
- **Winners = the earliest 10 `ready` entries by original forward-time.** At collection
  close, they are selected, `claim_bingo_prize`d + set `verified`, and the batch release
  fires immediately. There is **no `verifying` phase, no rolling replacement, and no
  `_award` change** (the forward round claims prizes directly and never uses the
  tagged-people `_finalize`/`_award` path).
- Phases collapse to **`collecting → released`**.

Everything below that references verification/`_award`/rolling is replaced by the above.

## Phases

The round is a single game-wide state machine (stored in `bingo_flags`):

- **`collecting`** — set by the facil start command. The bot DMs all 34 card-holders, accepts forwarded cards, sends confirmations, and queues entries. Ends when **20 of 34 have forwarded OR 2 days pass** (whichever first).
- **`verifying`** — collection closed. The earliest 10 *ready* entries (by original time) enter tagged-people verification; rolling replacement fills a slot when one fails. Results held.
- **`released`** — the 10 winners are settled (or everything resolved with fewer). All winners are congratulated at once and @zzehao gets the summary. Terminal.

## Submission state machine (forward round)

Forward-round rows live in `bingo_submissions` with dedicated statuses so they never collide with the live queue's `queued`/`confirming`:

- **`fwd_confirming`** — forwarded, OCR'd, confirmation sent, awaiting the submitter's confirm/fix.
- **`ready`** — the submitter confirmed a fully-matching line; held until verification opens.
- **`pending`** — (reused) in tagged-people verification.
- **`verified`** — (reused) passed = a winner (announcement held until release).
- **`failed`** — (reused) didn't pass / couldn't confirm in time.

`ready` and `fwd_confirming` are terminal-for-the-live-queue: excluded from `active_slot_count`, `queued_in_order`, `confirming_submissions`, `pending_submissions` (so a forward round never disturbs the live queue, and vice-versa).

## Flow

### Collection (`collecting`)
1. Facil runs `/start_forward_round` → set phase `collecting`; DM every `bingo_allocation` user: *"Forward me the earliest bingo card you sent — I'll check it for the prize round."*
2. A **forwarded photo/image** in a private chat, while `collecting`:
   - Read the original time: `message.forward_origin.date` (fallback to `message.date` if not actually forwarded, flagged in the reply).
   - OCR it (existing isolated `_run_ocr`).
   - Insert a row: status `fwd_confirming`, `submitted_at` = the original time, submitter = the forwarder.
   - Send the confirmation immediately (reuse `evaluate` + `bingo_text` builders): **short** ✅ line if fully recognised (all handles match the roster and are reachable), else the **full** fill-in template with `/start` flags.
   - **One live forward entry per person** — a re-forward replaces their `fwd_confirming`/`ready` row.
3. The submitter confirms (a dedicated `bingofwd:confirm` button) or fixes-and-resends (text). On a fully-recognised line → status `ready`. Not-fully → re-send the full template (unlimited).
4. Collection closes when `count(fwd_confirming+ready+…) forward rows` reaches 20 **or** the 2-day timer fires.

### Verification (`verifying`)
5. On close → phase `verifying`. Promote the **earliest 10 `ready`** rows (by `submitted_at`) into tagged-people verification via the existing pipeline (record members, DM subjects, arm the 12h timeout, evaluate). As a `pending` row fails, promote the next-earliest `ready` (rolling), until 10 are `verified` or no `ready` remain.
6. A submitter in verification is told: *"You're in — results will be released together soon."*

### Release (`released`)
7. Winner announcements are held throughout. When the 10th prize is claimed (or all forward entries resolve with fewer), fire **once**: DM every winner a congratulations, and DM @zzehao a single message listing all winners' `@handles`. Set phase `released`.
8. During `verifying`/`collecting`, `_award` claims the prize (respecting the 10-cap and `bingo_prizes`) but **suppresses** the per-win winner DM, the per-win admin DM, and the channel post — those are replaced by the batch release.

## Data / storage

- `bingo_flags`: `forward_round` (its `set_at` is the collection start, for the 2-day timer) and `forward_phase` values via a small helper set (`collecting`/`verifying`/`released`). Simplest: three flag names `forward_collecting`, `forward_verifying`, `forward_released`; `forward_phase()` returns the furthest set.
- New statuses `fwd_confirming`, `ready` (free-text; no schema change).
- Helpers: `queue_forwarded_submission(user_id, handle, sheet_no, submitted_at)` (explicit time, dedup the user's `fwd_confirming`/`ready`); `set_forward_ready(id)`; `ready_in_order()`; `forward_entry_count()`; `forward_submissions()` (all forward-round rows); phase get/set; `forward_batch_active()` (True while `collecting`/`verifying`).
- Config: `FORWARD_ROUND_TARGET = 20`, `FORWARD_ROUND_WINDOW = timedelta(days=2)`.

## Module structure

- `handlers/bingo_forward.py` (new) — owns the whole round: `start_forward_round` (broadcast), `on_forwarded_card`, forward-specific `_send_confirmation`/`confirm_button`/`on_resend` (reusing `bingo_queue.evaluate` + `bingo_text` builders), `close_collection` + the 2-day timer job, `kickoff_verification` (promote earliest-10 `ready` → verify, rolling), `_release_results`, phase/timer re-arm on startup.
- `storage.py` — the helpers above.
- `handlers/bingo.py` — `_award` gains a `forward_batch_active()` branch that holds announcements and triggers `_release_results` at the cap; register the command + forwarded-image handler; re-arm the forward timer at startup.
- `config.py` — the two constants.
- Reused unchanged: `_run_ocr`, `bingo_queue.evaluate`, `bingo_text.*`, the tagged-people verification tail (`_dm_subjects`, `bingoconf` `confirm_button`, `_finalize`), `claim_bingo_prize`.

## Reuse vs. isolation

The forward round uses its **own** confirm callbacks (`bingofwd:*`) and statuses so it can't perturb the live `/submit_bingo` queue. It **reuses** the pure recognition/text helpers and the tagged-people verification + prize-claim machinery. `_award`'s only change is a hold-and-batch branch guarded by `forward_batch_active()`.

## Edge cases

- A card that isn't actually forwarded (`forward_origin is None`): accept it, order by `message.date`, and note in the reply that the original time couldn't be read.
- Late forwards after `verifying` begins: queue as `ready` behind; used only if an earlier `pending` fails (rolling still consults `ready_in_order`).
- Fewer than 10 valid winners: release whoever won once every forward entry has resolved (no more `ready`/`pending`).
- Restart mid-round: phase + collection start persist in `bingo_flags`; the 2-day timer and confirm/verify timeouts re-arm on startup; `_PENDING_READ` for forward rows rebuilds from `winning_members` (same fallback as the queue).

## Testing

- Storage: forward statuses + helpers; forward rows excluded from live-queue queries.
- Forward handler: reads `forward_origin.date`; OCR → `fwd_confirming` + confirmation; re-forward replaces.
- Confirm/resend → `ready`.
- Close at 20 forwarded and at the 2-day timer; kickoff promotes earliest-10 `ready`, rolling on fail.
- Batch release: `_award` holds announcements while active; release fires once at 10 winners (and on final resolve with fewer), DMing all winners + @zzehao the list.
- Broadcast DMs all card-holders.
