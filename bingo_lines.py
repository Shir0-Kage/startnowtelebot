"""Pure winning-line logic for Human Bingo.

No Telegram, OCR, or storage here — just the 5x5 geometry and the pass-rule
math, so it's trivially unit-testable. The only import is the grid geometry
(GRID, FREE) from the templates module.

A "line" is the list of REAL (non-free) cells it contains, each a
(row, col, handle) tuple. The centre FREE cell auto-counts as filled and is
never part of a returned line.
"""

from data.bingo_templates import GRID, FREE

# A line is its real (non-free) cells: (row, col, handle).
Line = list  # type alias for readability; elements are (int, int, str)


def _candidate_lines():
    """The 12 candidate lines as lists of (row, col), excluding the FREE cell.

    5 rows, 5 columns, 2 diagonals. The centre is dropped wherever it appears
    (middle row, middle column, both diagonals) so it never needs a match.
    """
    lines = []
    # rows
    for r in range(GRID):
        lines.append([(r, c) for c in range(GRID)])
    # columns
    for c in range(GRID):
        lines.append([(r, c) for r in range(GRID)])
    # main diagonal (top-left -> bottom-right)
    lines.append([(i, i) for i in range(GRID)])
    # anti-diagonal (top-right -> bottom-left)
    lines.append([(i, GRID - 1 - i) for i in range(GRID)])

    # strip the free centre from every candidate
    return [[cell for cell in cells if cell != FREE] for cells in lines]


def winning_lines(matched, submitter_handle):
    """Every complete, valid line in `matched`.

    `matched`: {(row, col): handle} for CONFIDENT cells only (handle lowercased,
    no '@'). A candidate qualifies iff every one of its real (non-free) cells is
    in `matched`, all those handles are DISTINCT, and none equals
    `submitter_handle`. Returns each qualifying line as its list of real
    (row, col, handle) cells.
    """
    out = []
    for cells in _candidate_lines():
        # every real cell must be a confident match
        if not all(cell in matched for cell in cells):
            continue
        handles = [matched[cell] for cell in cells]
        # no self-cheese: a cell matching the submitter breaks the line
        if submitter_handle in handles:
            continue
        # icebreaker rule: everyone in the line must be a different person
        if len(set(handles)) != len(handles):
            continue
        out.append([(r, c, matched[(r, c)]) for (r, c) in cells])
    return out


def required_yes(line):
    """How many YES answers a line needs: at most one miss allowed."""
    return len(line) - 1


def line_passes(line, answers):
    """True iff the line clears the pass threshold.

    `answers`: {handle: "yes" | "no"}; a missing handle is unanswered. A "no"
    and an unanswered handle each count as a miss. Passes iff the number of
    "yes" answers among the line's handles is >= len(line) - 1.
    """
    yes = sum(1 for (_r, _c, handle) in line if answers.get(handle) == "yes")
    return yes >= required_yes(line)


def pick_best_line(lines):
    """The easiest line to verify: fewest real cells, deterministic tie-break.

    Ties (equal length) resolve on the line's sorted cell tuples, so the same
    set of lines always yields the same choice regardless of input order.
    """
    return min(lines, key=lambda line: (len(line), sorted(line)))
