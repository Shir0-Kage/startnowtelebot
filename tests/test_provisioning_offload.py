"""Regression: roster loads are blocking Google-Sheets fetches (urllib + DNS).
They MUST run off the asyncio event loop, and a lookup must never trigger a
fetch on the calling thread — otherwise a slow/stalled fetch freezes the whole
bot (Ctrl+C included)."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

from handlers import provisioning as prov


def _reset():
    prov._facil_by_handle = None
    prov._year1_by_handle = None
    prov._year1_by_email = None


def test_start_loads_rosters_off_the_event_loop(monkeypatch):
    _reset()
    main = threading.main_thread()
    seen = {}

    def fake_facil():
        seen["facil"] = threading.current_thread()
        return {}

    def fake_year1():
        seen["year1"] = threading.current_thread()
        return {}

    monkeypatch.setattr(prov.sheets, "load_facil_members", fake_facil)
    monkeypatch.setattr(prov.sheets, "load_year1_members", fake_year1)

    upd = MagicMock()
    upd.effective_chat.type = "private"
    upd.effective_user.username = "nobody_at_all"
    upd.effective_user.id = 999
    upd.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = []
    ctx.user_data = {}

    assert asyncio.run(prov.try_send_group_link(upd, ctx)) is True
    # the blocking sheet fetches ran in worker threads, not on the event loop
    assert seen.get("facil") is not None and seen["facil"] is not main
    assert seen.get("year1") is not None and seen["year1"] is not main


def test_accessor_does_not_fetch_when_cold(monkeypatch):
    # a lookup against a cold cache must NOT hit the network on the calling
    # thread — it just misses. Loading is the async ensure step's job.
    _reset()
    calls = {"n": 0}

    def fake_year1():
        calls["n"] += 1
        return {}

    monkeypatch.setattr(prov.sheets, "load_year1_members", fake_year1)
    assert prov._og_by_handle("whoever") is None
    assert prov._og_by_email("who@x") is None
    assert calls["n"] == 0     # never fetched on the calling thread


def test_ensure_rosters_loaded_caches_and_reads(monkeypatch):
    _reset()
    calls = {"n": 0}

    def fake_year1():
        calls["n"] += 1
        return {"AM1": [{"name": "A", "handle": "alice", "email": "a@x"}]}

    monkeypatch.setattr(prov.sheets, "load_year1_members", fake_year1)
    monkeypatch.setattr(prov.sheets, "load_facil_members", lambda: {})

    asyncio.run(prov.ensure_rosters_loaded())
    assert prov._og_by_handle("alice") == "AM1"
    asyncio.run(prov.ensure_rosters_loaded())   # second call: already cached
    assert calls["n"] == 1
