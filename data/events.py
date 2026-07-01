"""StartNOW! 2026 schedule.

Three kinds of entries:
  - "engagement": optional sessions, single timing, same for everyone.
  - "meetup": the three official meet-ups. Each has an AM and a PM slot, and
    a group chat gets reminders for whichever slot it's assigned.
  - "platform": Gather Town open/close markers (shown in the schedule, no
    attendance or reminders).

Meet-up slots start at 1000 SGT (AM) and 1900 SGT (PM). Each meet-up runs
roughly 1.5-2 hours, but the exact length varies per meet-up. Everything is
Singapore time.
"""

from datetime import date, datetime, time

from config import TIMEZONE


def _dt(d, t):
    """Combine a date + time into a timezone-aware SGT datetime."""
    return datetime.combine(d, t, tzinfo=TIMEZONE)


# --- Optional engagement sessions -----------------------------------------
ENGAGEMENTS = [
    {
        "key": "fireside",
        "emoji": "🔥",
        "short": "Fireside Q&A",
        "name": "Fireside Q&A",
        "date": date(2026, 6, 12),
        "time": time(10, 0),
        "where": "Online",
        "what": "Ask-me-anything style chat with seniors",
    },
    {
        "key": "movie_night",
        "emoji": "🎬",
        "short": "Movie Night",
        "name": "Movie Night",
        "date": date(2026, 6, 18),
        "time": time(19, 0),
        "where": "Online",
        "what": "Casual movie night together",
    },
    {
        "key": "dry_run",
        "emoji": "🧪",
        "short": "Meet-Up Dry Run",
        "name": "StartNOW! Meet-Up Dry Run",
        "date": date(2026, 6, 24),
        "time": time(10, 0),
        "where": "Gather Town",
        "what": "Trial run so everyone's comfy with the meet-up format",
    },
    {
        "key": "game_night",
        "emoji": "🎲",
        "short": "Game Night",
        "name": "Game Night",
        "date": date(2026, 6, 30),
        "time": time(19, 0),
        "where": "Online",
        "what": "Games and hanging out",
    },
    {
        "key": "bingo",
        "emoji": "🎟️",
        "short": "Social Bingo Mixer",
        "name": "Social Bingo Mixer",
        "date": date(2026, 7, 6),
        "time": time(10, 0),
        "where": "Online",
        "what": "Bingo mixer to meet people outside your group",
    },
]


# --- Official meet-ups (AM + PM slots) -------------------------------------
# am_time / pm_time are PLACEHOLDERS. Swap in the confirmed timings here.
MEETUPS = [
    {
        "key": "meetup1",
        "emoji": "🎮",
        "short": "Meet Up 1",
        "name": "Meet Up 1 — Icebreaker Games",
        "date": date(2026, 7, 12),
        "am_time": time(10, 0),   # AM slot — 1000 SGT
        "pm_time": time(19, 0),   # PM slot — 1900 SGT
        "where": "Gather Town",
        "what": "Icebreaker games with your orientation group",
    },
    {
        "key": "meetup2",
        "emoji": "🤝",
        "short": "Meet Up 2",
        "name": "Meet Up 2 — Cross-Group Bonding Games",
        "date": date(2026, 7, 18),
        "am_time": time(10, 0),   # AM slot — 1000 SGT
        "pm_time": time(19, 0),   # PM slot — 1900 SGT
        "where": "Gather Town",
        "what": "Bonding games across different orientation groups",
    },
    {
        "key": "meetup3",
        "emoji": "🏆",
        "short": "Meet Up 3",
        "name": "Meet Up 3 — Finale Event",
        "date": date(2026, 7, 25),
        "am_time": time(10, 0),   # AM slot — 1000 SGT
        "pm_time": time(19, 0),   # PM slot — 1900 SGT
        "where": "Gather Town",
        "what": "The big finale to wrap up StartNOW!",
    },
]


# --- Platform period -------------------------------------------------------
PLATFORM = [
    {
        "key": "gather_open",
        "emoji": "🟢",
        "short": "Gather Town opens",
        "name": "Gather Town opens",
        "date": date(2026, 7, 3),
        "time": time(0, 0),
    },
    {
        "key": "gather_close",
        "emoji": "🔴",
        "short": "Gather Town closes",
        "name": "Gather Town closes",
        "date": date(2026, 8, 2),
        "time": time(23, 59),
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def engagement_dt(ev):
    return _dt(ev["date"], ev["time"])


def meetup_slot_dt(ev, slot):
    """Datetime for a meet-up's AM or PM slot."""
    t = ev["am_time"] if slot == "AM" else ev["pm_time"]
    return _dt(ev["date"], t)


def slot_time_str(ev, slot):
    """e.g. '1000H' for a meet-up slot — handy for reminder text."""
    t = ev["am_time"] if slot == "AM" else ev["pm_time"]
    return t.strftime("%H%M") + "H"


# events that support attendance collection (everything except platform markers)
def attendance_events():
    return MEETUPS + ENGAGEMENTS


def all_by_key():
    out = {}
    for ev in MEETUPS + ENGAGEMENTS + PLATFORM:
        out[ev["key"]] = ev
    return out


EVENTS_BY_KEY = all_by_key()


def is_meetup(ev):
    return ev in MEETUPS or ev.get("key", "").startswith("meetup")


def find_event(text):
    """Match user input to an event by key, short name, or full name."""
    if not text:
        return None
    needle = text.strip().lower()
    pool = MEETUPS + ENGAGEMENTS + PLATFORM

    if needle in EVENTS_BY_KEY:
        return EVENTS_BY_KEY[needle]

    # allow "meet up 1", "meetup 1", etc.
    squashed = needle.replace(" ", "").replace("-", "")
    for ev in pool:
        if squashed == ev["key"]:
            return ev

    for ev in pool:
        if needle in ev["name"].lower() or needle in ev["short"].lower():
            return ev

    for ev in pool:
        if squashed in ev["short"].lower().replace(" ", ""):
            return ev

    return None


def representative_dt(ev):
    """A single datetime used for ordering/'next event'. Meet-ups use their AM
    slot as the marker since it comes first in the day."""
    if is_meetup(ev):
        return meetup_slot_dt(ev, "AM")
    return _dt(ev["date"], ev["time"])


def next_event(now):
    """The soonest upcoming engagement or meet-up after `now` (aware datetime).
    Returns (event, datetime) or (None, None)."""
    upcoming = []
    for ev in MEETUPS + ENGAGEMENTS:
        when = representative_dt(ev)
        if when >= now:
            upcoming.append((ev, when))
    if not upcoming:
        return None, None
    upcoming.sort(key=lambda pair: pair[1])
    return upcoming[0]
