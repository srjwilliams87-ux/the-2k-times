import os
import requests

print("The 2k Times — sending test email")

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME")

headers = {
    "Authorization": f"Bearer {MAILGUN_API_KEY}"
}

response = requests.post(
    f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
    headers=headers,
    data={
        "from": f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>",
        "to": EMAIL_TO,
        "subject": "The 2k Times — Test Email",
        "text": "If you’re reading this, the daily newspaper robot works."
    }
)

print("Mailgun response:", response.status_code)
print(response.text)
