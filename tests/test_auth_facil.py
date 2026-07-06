"""Facilitator recognition: by roster @handle, by id, and the admin fallback."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import utils.auth as auth


def _update(username, uid=5, chat_type="private"):
    upd = MagicMock()
    upd.effective_user.id = uid
    upd.effective_user.username = username
    upd.effective_chat.type = chat_type
    return upd


def _reset(monkeypatch, *, handles=(), fac_ids=(), roster=None):
    """Clear caches and stub the sheet/config so only the path under test fires."""
    auth._facil_handles = None
    auth._managed_admins.update(ids=set(), handles=set(), at=0.0)
    monkeypatch.setattr(auth.config, "FACILITATORS", set(fac_ids))
    monkeypatch.setattr(auth.config, "FACILITATOR_HANDLES", set(handles))
    monkeypatch.setattr(auth.sheets, "load_facil_members", lambda: roster or {})
    # by default no managed chats, so the wide admin scan is a no-op
    monkeypatch.setattr(auth.manifest, "load", lambda: {})


def test_facil_recognised_by_handle(monkeypatch):
    _reset(monkeypatch, roster={"AM1": [{"name": "Tan Wei Xuan", "handle": "theoverlord27"}]})
    # case-insensitive and @-tolerant
    assert asyncio.run(auth.is_facilitator(_update("TheOverlord27"), MagicMock())) is True
    assert asyncio.run(auth.is_facilitator(_update("@theoverlord27"), MagicMock())) is True


def test_non_facil_rejected(monkeypatch):
    _reset(monkeypatch, roster={"AM1": [{"name": "X", "handle": "theoverlord27"}]})
    assert asyncio.run(auth.is_facilitator(_update("some_year_one"), MagicMock())) is False


def test_facilitator_id_still_works(monkeypatch):
    _reset(monkeypatch, fac_ids={42})
    assert asyncio.run(auth.is_facilitator(_update("nobody", uid=42), MagicMock())) is True


def test_roster_load_failure_falls_back_without_crashing(monkeypatch):
    _reset(monkeypatch)

    def _boom():
        raise RuntimeError("sheet unreachable")

    monkeypatch.setattr(auth.sheets, "load_facil_members", _boom)
    assert asyncio.run(auth.is_facilitator(_update("whoever"), MagicMock())) is False


def test_config_handle_is_facilitator_everywhere(monkeypatch):
    # @zzehao (and anyone in FACILITATOR_HANDLES) counts even in a DM
    _reset(monkeypatch, handles={"zzehao"})
    assert asyncio.run(auth.is_facilitator(_update("ZzeHao"), MagicMock())) is True
    assert asyncio.run(auth.is_facilitator(_update("@zzehao"), MagicMock())) is True
    assert asyncio.run(auth.is_facilitator(_update("someoneelse"), MagicMock())) is False


def _bot_with_admins(admins):
    """A context whose bot reports `admins` as the admins of every managed chat."""
    members = []
    for uid, uname in admins:
        m = MagicMock()
        m.user.id = uid
        m.user.username = uname
        members.append(m)
    ctx = MagicMock()
    ctx.bot.get_chat_administrators = AsyncMock(return_value=members)
    return ctx


def test_managed_group_admin_recognised_in_dm(monkeypatch):
    # a group admin who is NOT in the roster still counts as a facil in a DM
    _reset(monkeypatch)
    monkeypatch.setattr(auth.manifest, "load", lambda: {"StartNOW! AM1": {"chat_id": -100}})
    ctx = _bot_with_admins([(999, "groupadmin")])
    # matched by id
    assert asyncio.run(auth.is_facilitator(_update("whoever", uid=999), ctx)) is True
    # matched by @username (different id)
    auth._managed_admins.update(at=0.0)  # bypass the cache for a clean second read
    assert asyncio.run(auth.is_facilitator(_update("GroupAdmin", uid=7), ctx)) is True


def test_non_admin_still_rejected_with_managed_scan(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(auth.manifest, "load", lambda: {"StartNOW! AM1": {"chat_id": -100}})
    ctx = _bot_with_admins([(999, "groupadmin")])
    assert asyncio.run(auth.is_facilitator(_update("random_student", uid=8), ctx)) is False
