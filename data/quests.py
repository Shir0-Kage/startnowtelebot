"""StartNOW! quest guide data. Locations are Gather Town spots on the virtual
NUSC map. Edit the text here if a quest moves or the blurb changes."""

# Each quest has a short key (used in /quest <name> and button callbacks),
# an emoji, a display name, its Gather Town location, and a longer blurb.
QUESTS = [
    {
        "key": "tour",
        "emoji": "🚶",
        "name": "NUSC Tour Quest",
        "location": "Across the virtual campus",
        "blurb": (
            "Take a wander across the virtual NUSC campus. Just follow the "
            "signboards scattered around the map and they'll point you the way."
        ),
    },
    {
        "key": "houses",
        "emoji": "🏠",
        "name": "NUSC Houses Quest",
        "location": "Elm Courtyard",
        "blurb": "Get to know the NUSC houses and what house life is all about.",
    },
    {
        "key": "now",
        "emoji": "✨",
        "name": "NUSC NOW! Quest",
        "location": "Elm Courtyard",
        "blurb": "An intro to O'Week NOW!, Community NOW! and Explore NOW!.",
    },
    {
        "key": "acads",
        "emoji": "📚",
        "name": "NUSC Acads Quest",
        "location": "Saga Courtyard",
        "blurb": (
            "Walks you through the NUSC academic roadmap, your Year 1–2 courses, "
            "and video intros from seniors and professors."
        ),
    },
    {
        "key": "interest",
        "emoji": "🎪",
        "name": "NUSC Interest Groups Quest",
        "location": "Multi-Purpose Hall (MPH)",
        "blurb": (
            "Meet the NUSC Interest Groups — casual NUSC-wide CCAs that are open "
            "only to NUSC students."
        ),
    },
    {
        "key": "happenings",
        "emoji": "🎉",
        "name": "NUSC Happenings Quest",
        "location": "Cendana Courtyard",
        "blurb": (
            "Sports, arts, music, welfare events and everything else going on "
            "around NUSC through the year."
        ),
    },
]

# quick lookups by key
QUESTS_BY_KEY = {q["key"]: q for q in QUESTS}


def find_quest(text):
    """Best-effort match of user input to a quest. Accepts the key, the full
    name, or any distinctive word from the name. Returns the quest dict or None."""
    if not text:
        return None
    needle = text.strip().lower()

    # exact key first
    if needle in QUESTS_BY_KEY:
        return QUESTS_BY_KEY[needle]

    # then substring against the full name
    for q in QUESTS:
        if needle in q["name"].lower():
            return q

    # finally, match on any word (so "acads" or "interest" works)
    for q in QUESTS:
        words = q["name"].lower().replace("!", "").split()
        if needle in words:
            return q

    return None
