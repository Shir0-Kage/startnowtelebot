"""Task 1 — the Human Bingo tunables must exist in config with the exact
values and types the rest of the feature imports by name."""

from datetime import timedelta

import config


def test_announce_chat_id_default_and_type():
    # int, and the StartNOW! 2026 channel id from decision #5 of the spec
    assert isinstance(config.ANNOUNCE_CHAT_ID, int)
    assert config.ANNOUNCE_CHAT_ID == -1004292606016


def test_prize_limit():
    assert config.BINGO_PRIZE_LIMIT == 10
    assert isinstance(config.BINGO_PRIZE_LIMIT, int)


def test_confirm_timeout():
    assert config.BINGO_CONFIRM_TIMEOUT == timedelta(hours=12)
    assert isinstance(config.BINGO_CONFIRM_TIMEOUT, timedelta)


def test_match_threshold():
    # rapidfuzz 0-100 score cutoff
    assert config.BINGO_MATCH_THRESHOLD == 85
    assert isinstance(config.BINGO_MATCH_THRESHOLD, int)


def test_match_margin():
    # best must beat second-best by this many points
    assert config.BINGO_MATCH_MARGIN == 8
    assert isinstance(config.BINGO_MATCH_MARGIN, int)


def test_retry_cooldown():
    assert config.BINGO_RETRY_COOLDOWN == timedelta(seconds=60)
    assert isinstance(config.BINGO_RETRY_COOLDOWN, timedelta)


def test_bool_is_not_accepted_as_int():
    # guard against someone typing `True`/`False`; bool is a subclass of int
    assert not isinstance(config.BINGO_PRIZE_LIMIT, bool)
    assert not isinstance(config.BINGO_MATCH_THRESHOLD, bool)
    assert not isinstance(config.BINGO_MATCH_MARGIN, bool)
