import bingo_lines
from handlers import bingo_queue


def _cells(matched):
    """Build a read_submission-shaped cells list from {(r,c): handle}."""
    cells = []
    for r in range(5):
        for c in range(5):
            if (r, c) == (2, 2):
                continue
            h = matched.get((r, c))
            cells.append({"row": r, "col": c, "handle": h,
                          "score": 100.0 if h else 0.0})
    return cells


_TOP_ROW = {(0, 0): "alice", (0, 1): "bob", (0, 2): "carol", (0, 3): "dan",
            (0, 4): "eve"}


def test_evaluate_fully_recognised_when_line_all_reachable(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)
    res = bingo_queue.evaluate({"cells": _cells(_TOP_ROW)}, "submitter", 1)
    assert res["fully_recognised"] is True
    assert res["line"] is not None and res["unreachable"] == []


def test_evaluate_flags_unreachable(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle",
                        lambda h: None if h == "dan" else 1)
    res = bingo_queue.evaluate({"cells": _cells(_TOP_ROW)}, "submitter", 1)
    assert res["fully_recognised"] is False
    assert res["unreachable"] == ["dan"]
    assert res["line"] is not None            # a line exists, just not all reachable


def test_evaluate_no_line(monkeypatch):
    monkeypatch.setattr(bingo_queue.storage, "user_id_for_handle", lambda h: 1)
    res = bingo_queue.evaluate({"cells": _cells({(0, 0): "alice"})}, "submitter", 1)
    assert res["line"] is None
    assert res["fully_recognised"] is False and res["unreachable"] == []
