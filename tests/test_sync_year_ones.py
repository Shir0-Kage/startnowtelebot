"""/sync_year_ones: onboard already-/started Year 1s after roster changes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from handlers import provisioning as prov


def _roster_env(monkeypatch):
    monkeypatch.setattr(prov.sheets, "load_year1_members", lambda: {"PM1": [
        {"name": "Ansel", "handle": "camembertcheese", "raw_handle": "camembertcheese", "email": "a@x"},
        {"name": "Benaiah", "handle": "beani_boi", "raw_handle": "@beani_boi", "email": "b@x"},
        {"name": "Kairos", "handle": "caerustay", "raw_handle": "@caerustay", "email": "KAIROS@u.nus.edu"},
        {"name": "Broken", "handle": None, "raw_handle": "@no name", "email": "brk@u.nus.edu"},
    ]})
    monkeypatch.setattr(prov.storage, "get_started", lambda: [
        {"user_id": 1, "username": "camembertcheese"},   # started + placed
        {"user_id": 2, "username": "beani_boi"},          # started, waiting
    ])
    monkeypatch.setattr(prov.storage, "link_sent_to", lambda uid: uid == 1)
    monkeypatch.setattr("utils.auth.is_facilitator", AsyncMock(return_value=True))  # pass facil_only
    monkeypatch.setattr(prov, "ensure_rosters_loaded", AsyncMock())                 # skip real loads


def _run_roster(og):
    upd = MagicMock()
    upd.effective_message.reply_text = AsyncMock()
    upd.effective_chat.type = "private"
    ctx = MagicMock()
    ctx.args = [og]
    ctx.bot = AsyncMock()
    asyncio.run(prov.roster_status(upd, ctx))
    return upd.effective_message.reply_text.await_args.args[0]


def test_roster_status_admin_sees_breakdown_with_emails(monkeypatch):
    _roster_env(monkeypatch)
    monkeypatch.setattr(prov, "is_admin", lambda u: True)   # admin -> any OG
    r = _run_roster("pm1")
    assert "1 in group" in r and "1 waiting" in r and "1 not reachable" in r and "1 bad handle" in r
    assert "✅ Ansel" in r and "⏳ Benaiah" in r
    assert "❌ Kairos" in r and "kairos@u.nus.edu" in r     # email shown for the unreachable one
    assert "⚠️ Broken" in r and "brk@u.nus.edu" in r        # and the bad-handle one


def test_roster_status_facil_scoped_to_own_group(monkeypatch):
    _roster_env(monkeypatch)
    monkeypatch.setattr(prov, "is_admin", lambda u: False)              # a facil, not an admin
    monkeypatch.setattr(prov, "_og_by_facil_handle", lambda username: "PM1")  # their OG is PM1
    assert "✅ Ansel" in _run_roster("pm1")                 # own group -> allowed
    denied = _run_roster("pm5")                            # someone else's group -> blocked
    assert "only check" in denied.lower() and "PM1" in denied


def test_roster_status_refused_in_group_for_privacy(monkeypatch):
    _roster_env(monkeypatch)
    monkeypatch.setattr(prov, "is_admin", lambda u: True)   # even an admin can't in-group
    upd = MagicMock()
    upd.effective_message.reply_text = AsyncMock()
    upd.effective_chat.type = "supergroup"                 # a group, not a DM
    ctx = MagicMock()
    ctx.args = ["pm1"]
    ctx.bot = AsyncMock()
    asyncio.run(prov.roster_status(upd, ctx))
    r = upd.effective_message.reply_text.await_args.args[0]
    assert "private" in r.lower()
    assert "Ansel" not in r and "email" not in r.lower()   # no roster leaked to the group


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
