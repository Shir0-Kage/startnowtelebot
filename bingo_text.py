"""Text-mode Human Bingo submission.

An alternative to photo/OCR submission: the player types their filled-in card
back as plain text instead of sending a photo. build_template_text() gives
them a fill-in-the-blank list; parse_submission() reads their reply back into
the same {"corner", "cells"} shape bingo_ocr.read_submission() produces, so
handlers/bingo.py's downstream line-detection/confirmation pipeline can
consume either source unchanged.

Unlike OCR, there's no image noise to correct for, so matching here is exact
(normalized case/@) against the known roster handles -- no rapidfuzz, no
OCR-confusion variants. An unrecognised handle just leaves that cell
unmatched (same "never guess the wrong person" safety property as OCR's
confidence threshold), it does not reject the whole submission.
"""

import re

from data import bingo_templates as templates
from setup import sheets

_LINE_RE = re.compile(r"^\s*R(\d+)\s*C(\d+)\s*:\s*(.*)$", re.IGNORECASE)


def _build_template_text(sheet_no):
    lines = []
    for row in range(templates.GRID):
        for col in range(templates.GRID):
            if templates.is_free(row, col):
                continue
            prompt = templates.prompt_for(sheet_no, row, col)
            lines.append(f"R{row + 1}C{col + 1}: {prompt} - ")
    return "\n".join(lines)


# Pre-build the 15 blank fill-in templates once at import (they never change).
_TEMPLATE_CACHE = {n: _build_template_text(n) for n in templates.SHEETS}


def build_template_text(sheet_no):
    """The cached fill-in-the-blank list a player replies to, one line per
    non-FREE cell (R1C1..R5C5, FREE centre omitted). Pre-generated at import."""
    return _TEMPLATE_CACHE[sheet_no]


def build_line_confirm_text(sheet_no, line):
    """Render just the winning line's cells for the short confirm message.
    `line`: list of (row, col, handle) 0-indexed, as bingo_lines returns."""
    out = []
    for row, col, handle in line:
        prompt = templates.prompt_for(sheet_no, row, col)
        out.append(f"R{row + 1}C{col + 1}: {prompt} - @{handle}")
    return "\n".join(out)


def build_prefilled_text(sheet_no, cells):
    """Same list as build_template_text, but with each cell's already-matched
    @handle filled in (blank for a cell with no confident match).

    `cells`: the read_submission()/parse_submission() "cells" list -- dicts
    with "row", "col", "handle" (handle may be None). Lets a player review or
    correct an OCR read as plain text instead of retyping the whole card from
    scratch: confidently-read squares show their handle, everything else is
    left blank for the player to fill in.
    """
    handle_by_cell = {(c["row"], c["col"]): c.get("handle") for c in (cells or [])}
    lines = []
    for row in range(templates.GRID):
        for col in range(templates.GRID):
            if templates.is_free(row, col):
                continue
            prompt = templates.prompt_for(sheet_no, row, col)
            handle = handle_by_cell.get((row, col))
            suffix = f"@{handle}" if handle else ""
            lines.append(f"R{row + 1}C{col + 1}: {prompt} - {suffix}")
    return "\n".join(lines)


def parse_submission(sheet_no, text, index):
    """Mirrors bingo_ocr.read_submission()'s return shape:
    {"corner": None, "cells": [{"row", "col", "handle", "score"}, ...24]}.

    Only the leading 'R{row}C{col}:' id and the text after the LAST '-' on
    each line are trusted; a garbled or omitted restated prompt in between
    never breaks parsing. A duplicate line for an already-MATCHED cell is
    ignored (first successful match wins) -- a malformed or unresolved first
    attempt at a cell does not block a later, valid correction for it.
    """
    handles = {}
    for raw_line in (text or "").splitlines():
        m = _LINE_RE.match(raw_line)
        if not m:
            continue
        row, col = int(m.group(1)) - 1, int(m.group(2)) - 1
        if not (0 <= row < templates.GRID and 0 <= col < templates.GRID):
            continue
        if templates.is_free(row, col) or (row, col) in handles:
            continue

        rest = m.group(3)
        if "-" not in rest:
            continue
        candidate = rest.rsplit("-", 1)[1].strip()
        handle = sheets.normalize_handle(candidate)
        if handle and handle in index.get("handles", ()):
            handles[(row, col)] = handle

    cells = []
    for row in range(templates.GRID):
        for col in range(templates.GRID):
            if templates.is_free(row, col):
                continue
            handle = handles.get((row, col))
            cells.append({
                "row": row, "col": col,
                "handle": handle,
                "score": 100.0 if handle else 0.0,
            })
    return {"corner": None, "cells": cells}
