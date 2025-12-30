import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo

# --- Settings from Render env vars ---
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")
SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

if not all([MAILGUN_DOMAIN, EMAIL_TO, SMTP_USER, SMTP_PASS]):
    raise SystemExit("Missing one or more required environment variables")

# --- Date formatting for subject: dd.mm.yyyy (UK time) ---
uk_now = datetime.now(ZoneInfo("Europe/London"))
subject_date = uk_now.strftime("%d.%m.%Y")
subject = f"The 2k Times, {subject_date}"

# --- Skeleton edition content (placeholders for now) ---
text = f"""WORLD HEADLINES
1) Placeholder headline — this will become real stories
   Read full article (clean text) →

2) Placeholder headline — this will become real stories
   Read full article (clean text) →

3) Placeholder headline — this will become real stories
   Read full article (clean text) →


UK POLITICS
1) Placeholder headline — this will become real stories
   Read full article (clean text) →

2) Placeholder headline — this will become real stories
   Read full article (clean text) →


RUGBY UNION
1) Placeholder headline — this will become real stories
   Read full article (clean text) →

2) Placeholder headline — this will become real stories
   Read full article (clean text) →

3) Placeholder headline — this will become real stories
   Read full article (clean text) →

4) Placeholder headline — this will become real stories
   Read full article (clean text) →

5) Placeholder headline — this will become real stories
   Read full article (clean text) →


PUNK ROCK
(Section omitted automatically when there are no qualifying items — we’ll implement this next.)
"""

# --- Send email via Mailgun SMTP ---
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO
msg.set_content(text)

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Sent skeleton edition:", subject)
