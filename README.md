# StartNOW! 2026 Telegram Bot

A Telegram bot that helps facilitators run their StartNOW! 2026 orientation
groups — quest info, the event schedule, automatic reminders, attendance
collection, and tidy announcements.

Built with Python and [python-telegram-bot](https://docs.python-telegram-bot.org/).

---

## What it does

- **Quest guide** — `/quests` and `/quest <name>` for Gather Town locations.
- **Schedule** — `/schedule`, `/next`, `/meetups`, `/engagements` (all in SGT).
- **Slot-aware reminders** — each meet-up has an AM and PM slot; the bot sends
  the right timing to each group based on how it's set (`/setslot am|pm`).
  Reminders go out 1 day, 1 hour and 10 minutes before an event.
- **Attendance** — facils open a check, participants tap a button, and the bot
  records who actually showed up (once each). Export to CSV any time.
- **Announcements** — `/announce`, `/remind`, `/pinannounce`.

---

## Command reference

**Everyone**

| Command | What it does |
| --- | --- |
| `/start` | Intro to the bot |
| `/help` | List every command |
| `/quests` | All quests and their locations |
| `/quest <name>` | Details for one quest (e.g. `/quest acads`) |
| `/schedule` | The full schedule |
| `/next` | The next upcoming event |
| `/meetups` | The three official meet-ups |
| `/engagements` | Optional engagement sessions |
| `/slot` | Whether this group is AM or PM |
| `/attendance` | Pick an event to collect attendance for |
| `/attendance_summary <event>` | See who's marked present |

**Facilitators only** (group admins, or IDs in `FACILITATOR_IDS`)

| Command | What it does |
| --- | --- |
| `/setslot am` / `/setslot pm` | Assign this group's meet-up slot |
| `/reminders on` / `/reminders off` | Toggle reminders for this group |
| `/attendance <event>` | Open an attendance check |
| `/close_attendance <event>` | Stop collecting for an event |
| `/clear_attendance <event>` | Reset an event's records |
| `/export_attendance` | Download this group's records as CSV |
| `/announce <message>` | Post a formatted announcement |
| `/remind <message>` | Post a short reminder |
| `/pinannounce <message>` | Announce and pin it |

Event names accept the short key or the readable name, e.g. `meetup1`,
`meet up 1`, `movie_night`, `dry run`.

---

## Setup

### 1. Create your bot and get a token

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (pick a name and a username ending in
   `bot`).
3. BotFather replies with a **token** that looks like
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Keep it private.

### 2. Get the code

```bash
git clone https://github.com/Shir0-Kage/startnowtelebot.git
cd startnowtelebot
```

### 3. Install dependencies

You need Python 3.10 or newer. A virtual environment keeps things clean:

```bash
# create and activate a venv
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

# install
pip install -r requirements.txt
```

### 4. Add your token

Copy the example env file and fill it in:

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

Open `.env` and set:

```
BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FACILITATOR_IDS=11111111,22222222
```

To find a Telegram user ID, message [@userinfobot](https://t.me/userinfobot).
`FACILITATOR_IDS` is optional — group admins are treated as facilitators
automatically. It's a good backup so facils can run commands even if they
aren't made group admins.

> Prefer not to use a `.env` file? Just set `BOT_TOKEN` as a normal environment
> variable instead:
> ```bash
> # Windows (PowerShell)
> $env:BOT_TOKEN = "123456789:AAE..."
> # macOS / Linux
> export BOT_TOKEN="123456789:AAE..."
> ```

### 5. Run it

```bash
python main.py
```

You should see `bot is up and running`. Leave it running — the bot only works
while this process is alive.

---

## Adding the bot to a group

1. Open your orientation group → add members → search your bot's username.
2. Make the bot an **admin** if you want it to pin messages
   (`/pinannounce`) — it needs the "pin messages" permission for that.
3. Set the group's slot: a facil sends `/setslot am` or `/setslot pm`.
4. That's it. Try `/quests` or `/schedule` to check it's alive.

Group privacy is fine to leave on — the bot only needs to see the `/commands`
people send it.

---

## Updating the schedule

All the event data lives in [`data/events.py`](data/events.py). The meet-up AM
and PM timings there are **placeholders** — search for `placeholder` and swap in
the real times, then restart the bot:

```python
"am_time": time(10, 0),   # placeholder
"pm_time": time(15, 0),   # placeholder
```

Quest text lives in [`data/quests.py`](data/quests.py).

Reminders are queued when the bot starts, so **restart the bot after changing
any timings** for the new times to take effect.

---

## Keeping it running (deployment)

The bot uses long-polling, so it doesn't need a public URL or open ports — it
just needs to stay running somewhere.

### Option A — a small Linux server (systemd)

Create `/etc/systemd/system/startnow-bot.service`:

```ini
[Unit]
Description=StartNOW! Telegram bot
After=network.target

[Service]
WorkingDirectory=/home/youruser/startnowtelebot
ExecStart=/home/youruser/startnowtelebot/.venv/bin/python main.py
EnvironmentFile=/home/youruser/startnowtelebot/.env
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now startnow-bot
sudo systemctl status startnow-bot     # check it's running
journalctl -u startnow-bot -f          # follow the logs
```

### Option B — a hosting platform (Railway, Render, Fly.io, etc.)

1. Push this repo to GitHub (already done if you're reading this there).
2. Create a new service from the repo.
3. Set the start command to `python main.py`.
4. Add `BOT_TOKEN` (and `FACILITATOR_IDS`) as environment variables in the
   dashboard — don't commit them.
5. Note: the SQLite file lives on disk, so use a persistent volume if the host
   wipes storage between deploys, otherwise attendance records reset.

### Option C — just leave `python main.py` running

Fine for testing. For anything real, use Option A or B so it restarts if the
machine reboots or the process dies.

---

## Where data is stored

Everything persists in a single SQLite file (`bot.db` by default):

- **groups** — chat ID, group name, slot (AM/PM/unset), reminders on/off
- **attendance** — one row per person per event, with slot and timestamp
- **attendance_state** — whether a check is open or closed

Back it up by copying `bot.db`. Change its location with `DB_PATH` in `.env`.

---

## Project layout

```
main.py                 # entry point, wiring, polling
config.py               # env + settings (token, facils, timezone, reminders)
storage.py              # SQLite: groups + attendance
data/
  quests.py             # quest guide content
  events.py             # schedule + meet-up slot timings
handlers/
  common.py             # /start, /help
  quests.py             # /quests, /quest
  schedule.py           # /schedule, /next, /meetups, /engagements
  settings.py           # /setslot, /slot, /reminders
  attendance.py         # attendance commands + button
  announcements.py      # /announce, /remind, /pinannounce
  reminders.py          # scheduled reminder jobs
utils/
  auth.py               # facilitator/admin checks
  text.py               # long-message splitting, name helpers
```

---

## Troubleshooting

- **`BOT_TOKEN is not set`** — your `.env` is missing or the variable is empty.
- **Reminders never fire** — make sure `python-telegram-bot[job-queue]` is
  installed (it's in `requirements.txt`), and remember reminders are only queued
  for events still in the future when the bot starts.
- **`/pinannounce` doesn't pin** — the bot needs to be a group admin with the
  "pin messages" permission.
- **Bot ignores commands in a group** — make sure it was actually added to the
  group and isn't blocked; try `/help` directed at it.
