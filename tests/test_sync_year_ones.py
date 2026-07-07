"""/sync_year_ones: onboard already-/started Year 1s after roster changes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from handlers import provisioning as prov


def test_roster_status_categorizes_each_year_one(monkeypatch):
    monkeypatch.setattr(prov.sheets, "load_year1_members", lambda: {"PM1": [
        {"name": "Ansel", "handle": "camembertcheese", "raw_handle": "camembertcheese", "email": "a@x"},
        {"name": "Benaiah", "handle": "beani_boi", "raw_handle": "@beani_boi", "email": "b@x"},
        {"name": "Kairos", "handle": "caerustay", "raw_handle": "@caerustay", "email": "c@x"},
        {"name": "Broken", "handle": None, "raw_handle": "@no name", "email": "d@x"},
    ]})
    # Ansel started + placed; Benaiah started but not placed; Kairos never started
    monkeypatch.setattr(prov.storage, "get_started", lambda: [
        {"user_id": 1, "username": "camembertcheese"},
        {"user_id": 2, "username": "beani_boi"},
    ])
    monkeypatch.setattr(prov.storage, "link_sent_to", lambda uid: uid == 1)
    monkeypatch.setattr("utils.auth.is_facilitator", AsyncMock(return_value=True))

    upd = MagicMock()
    upd.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = ["pm1"]
    ctx.bot = AsyncMock()

    asyncio.run(prov.roster_status(upd, ctx))
    r = upd.effective_message.reply_text.await_args.args[0]
    assert "1 in group" in r and "1 waiting" in r and "1 not reachable" in r and "1 bad handle" in r
    assert "✅ Ansel" in r          # started + placed
    assert "⏳ Benaiah" in r        # started, waiting
    assert "❌ Kairos" in r         # not reachable
    assert "⚠️ Broken" in r         # bad handle


def test_sync_flags_unusable_and_cleaned_handles(monkeypatch):
    monkeypatch.setattr(prov.sheets, "load_year1_members", lambda: {
        "AM4": [
            {"name": "Fine", "handle": "finehandle", "raw_handle": "@finehandle", "email": "f@x"},
            {"name": "Spacey", "handle": "duongle", "raw_handle": "@Duong Le", "email": "d@x"},
            {"name": "Broken", "handle": None, "raw_handle": "", "email": "b@x"},
        ],
    })
    prov._year1_by_handle = None
    prov._year1_by_email = None
    monkeypatch.setattr(prov.storage, "get_started", lambda: [])   # nobody to DM
    monkeypatch.setattr("utils.auth.is_facilitator", AsyncMock(return_value=True))

    upd = MagicMock()
    upd.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.bot = AsyncMock()

    asyncio.run(prov.sync_year_ones(upd, ctx))
    reply = upd.effective_message.reply_text.await_args.args[0]
    assert "Broken" in reply                      # blank handle -> flagged unusable
    assert "Spacey" in reply and "duongle" in reply  # cleaned -> flagged to verify
    assert "Fine" not in reply                     # a clean handle is not flagged


def test_sync_dms_opened_holds_unopened_skips_others(monkeypatch):
    monkeypatch.setattr(prov.sheets, "load_year1_members", lambda: {
        "AM1": [{"name": "Alice", "handle": "alice", "email": "a@x"}],   # opened
        "AM2": [{"name": "Bob", "handle": "bob", "email": "b@x"}],       # not opened
        "AM3": [{"name": "Carol", "handle": "carol", "email": "c@x"}],   # opened, already linked
    })
    prov._year1_by_handle = None
    prov._year1_by_email = None

    started = [
        {"user_id": 1, "username": "alice"},
        {"user_id": 2, "username": "bob"},
        {"user_id": 3, "username": "carol"},
        {"user_id": 4, "username": "stranger"},   # not a Year 1
    ]
    monkeypatch.setattr(prov.storage, "get_started", lambda: started)
    monkeypatch.setattr(prov.storage, "link_sent_to", lambda uid: uid == 3)
    monkeypatch.setattr(prov.storage, "is_og_opened", lambda og: og in ("AM1", "AM3"))
    monkeypatch.setattr(prov.storage, "mark_link_sent", lambda uid, og: None)
    monkeypatch.setattr(prov.storage, "remove_waiting", lambda uid: None)
    held = []
    monkeypatch.setattr(prov.storage, "add_waiting", lambda uid, og: held.append((uid, og)))
    monkeypatch.setattr(prov, "_group_link", AsyncMock(return_value="http://join/x"))
    # bypass the @facil_only gate
    monkeypatch.setattr("utils.auth.is_facilitator", AsyncMock(return_value=True))

    upd = MagicMock()
    upd.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.bot = AsyncMock()

    asyncio.run(prov.sync_year_ones(upd, ctx))

    # Alice (AM1 opened, no link) is DM'd; Carol already linked, stranger not a Year 1.
    ctx.bot.send_message.assert_awaited_once()
    assert ctx.bot.send_message.await_args.args[0] == 1
    # Bob (AM2 not opened) is held for the facil.
    assert held == [(2, "AM2")]
    upd.effective_message.reply_text.assert_awaited()
