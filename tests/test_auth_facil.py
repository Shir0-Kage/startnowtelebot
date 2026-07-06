"""Facilitator recognition: by roster @handle, by id, and the admin fallback."""

import asyncio
from unittest.mock import MagicMock

import utils.auth as auth


def _update(username, uid=5, chat_type="private"):
    upd = MagicMock()
    upd.effective_user.id = uid
    upd.effective_user.username = username
    upd.effective_chat.type = chat_type
    return upd


def test_facil_recognised_by_handle(monkeypatch):
    auth._facil_handles = None
    monkeypatch.setattr(auth.config, "FACILITATORS", set())
    monkeypatch.setattr(
        auth.sheets, "load_facil_members",
        lambda: {"AM1": [{"name": "Tan Wei Xuan", "handle": "theoverlord27"}]},
    )
    # case-insensitive and @-tolerant
    assert asyncio.run(auth.is_facilitator(_update("TheOverlord27"), MagicMock())) is True
    assert asyncio.run(auth.is_facilitator(_update("@theoverlord27"), MagicMock())) is True


def test_non_facil_rejected(monkeypatch):
    auth._facil_handles = None
    monkeypatch.setattr(auth.config, "FACILITATORS", set())
    monkeypatch.setattr(
        auth.sheets, "load_facil_members",
        lambda: {"AM1": [{"name": "X", "handle": "theoverlord27"}]},
    )
    assert asyncio.run(auth.is_facilitator(_update("some_year_one"), MagicMock())) is False


def test_facilitator_id_still_works(monkeypatch):
    auth._facil_handles = None
    monkeypatch.setattr(auth.config, "FACILITATORS", {42})
    monkeypatch.setattr(auth.sheets, "load_facil_members", lambda: {})
    assert asyncio.run(auth.is_facilitator(_update("nobody", uid=42), MagicMock())) is True


def test_roster_load_failure_falls_back_without_crashing(monkeypatch):
    auth._facil_handles = None
    monkeypatch.setattr(auth.config, "FACILITATORS", set())

    def _boom():
        raise RuntimeError("sheet unreachable")

    monkeypatch.setattr(auth.sheets, "load_facil_members", _boom)
    assert asyncio.run(auth.is_facilitator(_update("whoever"), MagicMock())) is False
