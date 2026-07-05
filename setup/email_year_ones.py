"""Email every Year 1 their group's Telegram invite link.

A reliable fallback for anyone whose Telegram handle was mistyped — email has no
Telegram limits. Reads the Year 1 sheet (names + emails + OG) and emails each
person a deep link to the bot (t.me/<bot>?start=<OG>); tapping it makes the bot
DM them their group's join link. The group comes from the link, so it works
even if their handle in the sheet is wrong.

    python -m setup.email_year_ones                # dry run: summary + one sample
    python -m setup.email_year_ones --csv out.csv   # write a mail-merge CSV instead
    python -m setup.email_year_ones --send          # actually send (needs SMTP creds)

Sending reads these env vars:
    EMAIL_USER   login for the sending mailbox (e.g. e1595887@u.nus.edu)
    EMAIL_PASS   its password / app password
    EMAIL_FROM   from-address (default: EMAIL_USER)
    EMAIL_SMTP_HOST / EMAIL_SMTP_PORT   default smtp.office365.com : 587

If your provider blocks SMTP AUTH, skip --send and use --csv, then mail-merge
from Outlook or a Google Sheets add-on.
"""

import argparse
import csv
import os
import time

from setup import roster, sheets

SUBJECT = "Join your StartNOW! 2026 orientation group"
BODY = (
    "Hi {name},\n\n"
    "Welcome to StartNOW! 2026 — your NUSC orientation programme!\n\n"
    "Tap here to get your orientation group ({og}) chat — our Telegram bot will "
    "send you the join link:\n{link}\n\n"
    "(If nothing happens, open Telegram, search @" + roster.BOT_USERNAME +
    ", press Start, and it'll send your link.)\n\n"
    "If you have any trouble, just reply to this email.\n\n"
    "See you there!\n"
    "The StartNOW! 2026 Team"
)


def _recipients():
    """[(email, name, og, link)] for every Year 1 with an email. The link is a
    deep link to the bot carrying their OG. Returns (recipients, skipped)."""
    year1s = sheets.load_year1_members()
    out, skipped = [], []
    for og, members in year1s.items():
        link = f"https://t.me/{roster.BOT_USERNAME}?start={og}"
        for m in members:
            if m.get("email"):
                out.append((m["email"].strip(), m["name"], og, link))
            else:
                skipped.append(f"{m['name']} ({og}) — no email in sheet")
    return out, skipped


def _write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["email", "name", "group", "invite_link", "subject", "body"])
        for email, name, og, link in rows:
            w.writerow([email, name, og, link, SUBJECT,
                        BODY.format(name=name, og=og, link=link)])
    print(f"wrote {len(rows)} row(s) to {path}")


def _send(rows):
    import smtplib
    import ssl
    from email.message import EmailMessage

    user, pw = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS")
    if not (user and pw):
        raise SystemExit("EMAIL_USER / EMAIL_PASS not set — see the module docstring.")
    sender = os.environ.get("EMAIL_FROM", user)
    host = os.environ.get("EMAIL_SMTP_HOST", "smtp.office365.com")
    port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))

    server = smtplib.SMTP(host, port, timeout=30)
    try:
        server.starttls(context=ssl.create_default_context())
        server.login(user, pw)
        sent = 0
        for email, name, og, link in rows:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, email, SUBJECT
            msg.set_content(BODY.format(name=name, og=og, link=link))
            try:
                server.send_message(msg)
                sent += 1
                print(f"  sent to {email} ({og})")
            except Exception as exc:
                print(f"  FAILED {email} ({exc})")
            time.sleep(0.5)  # be gentle with the mail server
        print(f"\nsent {sent}/{len(rows)} email(s).")
    finally:
        server.quit()


def main():
    p = argparse.ArgumentParser(description="Email Year 1s their group invite link.")
    p.add_argument("--csv", metavar="PATH", help="write a mail-merge CSV instead of sending")
    p.add_argument("--send", action="store_true", help="actually send (needs SMTP creds)")
    args = p.parse_args()

    rows, skipped = _recipients()
    print(f"{len(rows)} Year 1(s) ready to email; {len(skipped)} skipped.")
    for s in skipped[:25]:
        print("  -", s)

    if args.csv:
        _write_csv(rows, args.csv)
    elif args.send:
        _send(rows)
    else:
        if rows:
            e, n, og, link = rows[0]
            print(f"\n--- sample ---\nTo: {e}\nSubject: {SUBJECT}\n\n"
                  + BODY.format(name=n, og=og, link=link))
        print("\nDry run. Use --csv out.csv to mail-merge, or --send to send.")


if __name__ == "__main__":
    main()
