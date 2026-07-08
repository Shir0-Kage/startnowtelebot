"""Bingo submission queue: enqueue, kickoff-at-10, submitter self-confirm, and
rolling replacement, ahead of the existing tagged-people verification.

State (see the plan's STATUS MAPPING): queued -> confirming -> pending(verify)
-> verified(won) | failed. Only 'queued'/'confirming' are owned here; once a
submitter confirms a fully-recognised line, _start_verification flips the row to
'pending' and the existing handlers/bingo.py pipeline takes over unchanged.
"""

import logging

import bingo_lines as lines
import bingo_text
import config
import storage

log = logging.getLogger(__name__)

# submission_id -> {"read": <cells dict>, "handle": str, "sheet_no": int}
# The submitter's latest parsed read, needed by kickoff/confirm/resend. An
# in-memory miss after a restart just means the confirm message can't be
# re-derived until the user resends, which is acceptable.
_PENDING_READ = {}


def evaluate(read, submitter_handle, sheet_no):
    """Classify a parsed read.
    Returns {"line", "fully_recognised", "unreachable"}."""
    from handlers.bingo import _matched_and_prompts   # lazy: avoid import cycle
    matched, _prompts = _matched_and_prompts(
        read.get("cells", []), submitter_handle, sheet_no)
    candidates = lines.winning_lines(matched, submitter_handle)
    if not candidates:
        return {"line": None, "fully_recognised": False, "unreachable": []}
    line = lines.pick_best_line(candidates)
    unreachable = [h for (_r, _c, h) in line
                   if storage.user_id_for_handle(h) is None]
    return {"line": line, "fully_recognised": not unreachable,
            "unreachable": unreachable}
