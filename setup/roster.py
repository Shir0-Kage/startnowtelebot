"""Who and what the one-time provisioning scripts create.

Edit here if the admin team or the group list changes.
"""

# Telegram's hard cap on a custom admin title (the "rank").
MAX_TITLE_LEN = 16

BOT_USERNAME = "startnow2026_bot"

# The five people who run every group. The owner is the account that logs in and
# creates the groups, so they're in from the start; the rest are added once
# they've messaged the bot /start.
ADMINS = [
    {"username": "zzehao",    "title": "StartNOW! PD",   "owner": True},
    {"username": "dxnellek",  "title": "NOW! Director"},
    {"username": "Aashnag13", "title": "NOW! Admin VPD"},
    {"username": "ngocmyism", "title": "NOW! Ops VPD"},
    {"username": "jvsoh",     "title": "NOW! Progs VPD"},
]


def _build_groups():
    groups = []
    for slot in ("AM", "PM"):
        for n in range(1, 11):
            groups.append({"title": f"StartNOW! {slot}{n}", "slot": slot})
    return groups


GROUPS = _build_groups()


def owner():
    for a in ADMINS:
        if a.get("owner"):
            return a
    return ADMINS[0]


def added_admins():
    """Everyone who gets added after /start (i.e. not the owner account)."""
    return [a for a in ADMINS if not a.get("owner")]


def validate():
    """Catch roster mistakes before we touch Telegram. Raises ValueError."""
    problems = []
    for a in ADMINS:
        if not a.get("username"):
            problems.append(f"missing username in {a}")
        if len(a["title"]) > MAX_TITLE_LEN:
            problems.append(
                f"title over {MAX_TITLE_LEN} chars ({len(a['title'])}): {a['title']}"
            )
    titles = [g["title"] for g in GROUPS]
    if len(set(titles)) != len(titles):
        problems.append("duplicate group titles")
    if len(GROUPS) != 20:
        problems.append(f"expected 20 groups, got {len(GROUPS)}")
    if problems:
        raise ValueError("roster problems:\n  - " + "\n  - ".join(problems))
    return True
