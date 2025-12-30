import os
import smtplib
from email.message import EmailMessage

print("The 2k Times — sending test email (SMTP)")

MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")

SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

if not MAILGUN_DOMAIN:
    raise SystemExit("Missing MAILGUN_DOMAIN")
if not EMAIL_TO:
    raise SystemExit("Missing EMAIL_TO")
if not SMTP_USER:
    raise SystemExit("Missing MAILGUN_SMTP_USER")
if not SMTP_PASS:
    raise SystemExit("Missing MAILGUN_SMTP_PASS")

msg = EmailMessage()
msg["Subject"] = "The 2k Times — Test Email"
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO
msg.set_content("If you’re reading this, the daily newspaper robot works.")

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("SMTP send: OK")
