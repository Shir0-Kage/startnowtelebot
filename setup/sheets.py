"""Reading the StartNOW! Google Sheets and turning them into clean records.

All the sheets used here are shared "anyone with the link can view", so we can
pull them with a plain HTTP GET — no Google login needed, which matters because
the worker runs headless on the server.
"""

import csv
import io
import re
import urllib.request

# --- Which sheets/tabs to read -------------------------------------------

# Year 1 groupings (detailed sheet). Two tabs, same layout: AM and PM.
YEAR1_SHEET_ID = "17WxHixZ41a0m7LkdHPSrLzqyeyuh09fbI5Z8CG5YyXw"
YEAR1_TABS = ["46132107", "1604732138"]  # AM1-10, PM1-10

# Facil group assignments — the tab with the AM1..PM10 "Groups" column.
FACIL_GROUP_SHEET_ID = "15UHyd0jaK5JnU1YRH1lfDN7CBUMzK1R_C-Jq9p-Pjik"
FACIL_GROUP_TAB = "1236016020"

# Facil Telegram handles — the assessment workbook, one tab per house.
FACIL_HANDLE_SHEET_ID = "1GRwRDVcS-WaK32JKSJszkcgVjOeV1SxRyOCV-bH5Tlk"
FACIL_HANDLE_TABS = [
    "1076372068", "1110163284", "1115875278", "1320530118", "1362263984",
    "1490125461", "1742810157", "2053346216", "359727576",
]


# --- Fetching -------------------------------------------------------------

def fetch_csv(sheet_id, gid):
    """Download one tab as CSV text."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
        f"?format=csv&gid={gid}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    if "Sign in to your Google" in text:
        raise RuntimeError(
            f"sheet {sheet_id} tab {gid} isn't shared — set it to "
            "'anyone with the link can view'."
        )
    return text


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


# --- Handle / name cleanup ------------------------------------------------

# Telegram usernames: 5-32 chars, start with a letter, letters/digits/underscore.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_OG_RE = re.compile(r"^(AM|PM)\d+")


def normalize_handle(raw):
    """Return a clean username (no @, lowercased) or None if it isn't a valid
    Telegram handle (e.g. blank, or has a space like '@Duong Le')."""
    if not raw:
        return None
    h = raw.strip().lstrip("@").strip()
    return h.lower() if _USERNAME_RE.match(h) else None


def name_tokens(name):
    """Lowercased word set for fuzzy name matching, ignoring punctuation."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    return {t for t in cleaned.split() if t}


# --- Year 1 groupings -----------------------------------------------------

def parse_year1(text):
    """Turn one Year 1 tab into {OG: [member, ...]}.

    A member is {name, handle, raw_handle, addable}. 'addable' is False when the
    handle is missing or unusable (those fall to the invite-link/DM path)."""
    groups = {}
    current = None
    for row in _rows(text):
        first = (row[0] if row else "").strip()
        if _OG_RE.match(first):
            current = _OG_RE.match(first).group(0)
            groups.setdefault(current, [])
            continue
        if not current or not first or first == "." or first.lower() == "name":
            continue
        raw = row[1].strip() if len(row) > 1 else ""
        handle = normalize_handle(raw)
        groups[current].append(
            {
                "name": first,
                "handle": handle,
                "raw_handle": raw,
                "addable": handle is not None,
            }
        )
    return groups


def load_year1_members(fetch=fetch_csv):
    """Fetch both Year 1 tabs and merge into one {OG: [member, ...]}."""
    merged = {}
    for gid in YEAR1_TABS:
        for og, members in parse_year1(fetch(YEAR1_SHEET_ID, gid)).items():
            merged.setdefault(og, []).extend(members)
    return merged


# --- Facil group assignments ---------------------------------------------

def parse_facil_groups(text):
    """[{name, group, house}] from the facil grouping tab."""
    out = []
    for row in _rows(text):
        first = (row[0] if row else "").strip()
        if not _OG_RE.match(first):
            continue  # skips headers and the 'AM'/'PM' slot separators
        name = row[2].strip() if len(row) > 2 else ""
        if not name:
            continue
        out.append({"name": name, "group": _OG_RE.match(first).group(0),
                    "house": row[1].strip() if len(row) > 1 else ""})
    return out


# --- Facil handles (per-house assessment tabs) ---------------------------

def parse_facil_handles(text, house_hint=""):
    """[{name, handle, house}] from one house tab. Finds the Name and
    'Tele Handle' columns by header so column shuffles don't break it."""
    rows = _rows(text)
    name_col = handle_col = None
    data_start = 0
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        if "tele handle" in lowered:
            name_col = lowered.index("name") if "name" in lowered else 0
            handle_col = lowered.index("tele handle")
            data_start = i + 1
            break
    if handle_col is None:
        return []

    out = []
    for row in rows[data_start:]:
        if len(row) <= handle_col:
            continue
        name = row[name_col].strip()
        raw = row[handle_col].strip()
        if not name or name.lower() == "name":
            continue
        out.append({"name": name, "handle": normalize_handle(raw),
                    "raw_handle": raw, "house": house_hint})
    return out


def load_facil_handles(fetch=fetch_csv):
    out = []
    for gid in FACIL_HANDLE_TABS:
        out.extend(parse_facil_handles(fetch(FACIL_HANDLE_SHEET_ID, gid)))
    return out
