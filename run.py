import os
import sys
import requests
import feedparser
import datetime as dt
from urllib.parse import quote_plus
from email.utils import formatdate

# --------------------------------------------------
# Config
# --------------------------------------------------

ISSUE_TAG = "v-newspaper-14"
READER_BASE_URL = os.environ.get("READER_BASE_URL", "").rstrip("/")

SEND_EMAIL = os.environ.get("SEND_EMAIL", "false").lower() == "true"
DEBUG_EMAIL = os.environ.get("DEBUG_EMAIL", "false").lower() == "true"

EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")

SEND_TIMEZONE = os.environ.get("SEND_TIMEZONE", "UTC")

# --------------------------------------------------
# RSS Sources (World Headlines)
# --------------------------------------------------

WORLD_SOURCES = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Reuters", "https://feeds.reuters.com/Reuters/worldNews"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
]

# --------------------------------------------------
# Helpers
# --------------------------------------------------

def now_utc():
    return dt.datetime.utcnow()

def reader_url(original_url):
    return f"{READER_BASE_URL}/read?url={quote_plus(original_url)}"

def fetch_world_stories(limit=3):
    stories = []
    for name, url in WORLD_SOURCES:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if len(stories) >= limit:
                return stories
            stories.append({
                "source": name,
                "title": entry.title,
                "summary": entry.summary if hasattr(entry, "summary") else "",
                "link": entry.link
            })
    return stories

# --------------------------------------------------
# Sidebar Data
# --------------------------------------------------

def get_weather_cardiff():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 51.4816,
                "longitude": -3.1791,
                "current_weather": True,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "Europe/London",
            },
            timeout=10,
        )
        data = r.json()
        cw = data["current_weather"]
        hi = data["daily"]["temperature_2m_max"][0]
        lo = data["daily"]["temperature_2m_min"][0]
        return f"{cw['temperature']}°C · H {hi}°C / L {lo}°C"
    except Exception:
        return "Weather unavailable"

def get_sun_times():
    try:
        r = requests.get(
            "https://api.sunrise-sunset.org/json",
            params={
                "lat": 51.4816,
                "lng": -3.1791,
                "formatted": 0,
            },
            timeout=10,
        )
        res = r.json()["results"]
        sunrise = res["sunrise"][11:16]
        sunset = res["sunset"][11:16]
        return sunrise, sunset
    except Exception:
        return "—", "—"

def get_whos_in_space():
    try:
        r = requests.get("http://api.open-notify.org/astros.json", timeout=10)
        people = r.json()["people"]
        return people
    except Exception:
        return []

# --------------------------------------------------
# HTML Rendering
# --------------------------------------------------

def render_email(world_stories):
    date_str = now_utc().strftime("%d.%m.%Y")

    weather = get_weather_cardiff()
    sunrise, sunset = get_sun_times()
    space = get_whos_in_space()

    html = f"""
    <html>
    <body style="background:#111;color:#fff;font-family:Arial,Helvetica,sans-serif;max-width:720px;margin:auto;">
        <h1 style="font-size:42px;">The 2k Times</h1>
        <p>{date_str} · Daily Edition · {ISSUE_TAG}</p>

        <hr>

        <h2>🌍 World Headlines</h2>
    """

    for i, s in enumerate(world_stories, start=1):
        html += f"""
        <div style="margin-bottom:28px;">
            <h3>{i}. {s['title']}</h3>
            <p>{s['summary']}</p>
            <a href="{reader_url(s['link'])}" style="color:#6fa8ff;">Read in Reader →</a>
        </div>
        """

    html += f"""
        <hr>

        <h3>☁️ Weather · Cardiff</h3>
        <p>{weather}</p>

        <h3>🌅 Sunrise / Sunset</h3>
        <p>Sunrise: {sunrise} · Sunset: {sunset}</p>

        <h3>🚀 Who’s in Space</h3>
    """

    if space:
        for p in space:
            html += f"<p>{p['name']} ({p['craft']})</p>"
    else:
        html += "<p>Unable to load space roster.</p>"

    html += """
        <hr>
        <p style="font-size:12px;opacity:0.7;">© The 2k Times · Delivered daily at 05:30</p>
    </body>
    </html>
    """

    return html

# --------------------------------------------------
# Mailgun
# --------------------------------------------------

import os
import re
import requests

def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _clean_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "https://api.mailgun.net"
    return url.rstrip("/")

def _format_from_header(from_name: str, from_email: str) -> str:
    """
    Returns a valid RFC-ish From header.
    If name is blank, returns the email only.
    """
    from_name = (from_name or "").strip()
    from_email = (from_email or "").strip()
    if not from_name:
        return from_email
    # Quote if contains special chars
    if re.search(r'[",<>@]', from_name):
        from_name = from_name.replace('"', '\\"')
        return f"\"{from_name}\" <{from_email}>"
    return f"{from_name} <{from_email}>"

def send_mailgun(subject: str, html: str, text: str | None = None) -> bool:
    """
    Known-good Mailgun API sender using env vars:
      EMAIL_TO
      EMAIL_FROM_NAME
      EMAIL_FROM
      MAILGUN_API_BASE_URL
      MAILGUN_API_KEY
      MAILGUN_DOMAIN
      DEBUG_EMAIL
      SEND_EMAIL

    Returns True on success, False on failure.
    Never raises unless requests itself explodes unexpectedly.
    """
    send_enabled = _bool_env("SEND_EMAIL", default=False)
    debug_enabled = _bool_env("DEBUG_EMAIL", default=False)

    email_to = (os.getenv("EMAIL_TO") or "").strip()
    from_name = (os.getenv("EMAIL_FROM_NAME") or "").strip()
    from_email = (os.getenv("EMAIL_FROM") or "").strip()

    api_key = (os.getenv("MAILGUN_API_KEY") or "").strip()
    mg_domain = (os.getenv("MAILGUN_DOMAIN") or "").strip()
    base_url = _clean_base_url(os.getenv("MAILGUN_API_BASE_URL"))

    if not send_enabled:
        print("SEND_EMAIL=false - skipping send")
        return True  # not an error, just skipped

    # Basic env validation
    missing = []
    if not email_to: missing.append("EMAIL_TO")
    if not from_email: missing.append("EMAIL_FROM")
    if not api_key: missing.append("MAILGUN_API_KEY")
    if not mg_domain: missing.append("MAILGUN_DOMAIN")
    if missing:
        print(f"Mailgun: missing env vars: {', '.join(missing)}")
        return False

    # Sandbox domains are strict about From; this keeps you safe.
    # If EMAIL_FROM isn't on the same domain, force postmaster@<domain>.
    # (This avoids Mailgun rejecting with forbidden / unauthorized sender.)
    if "sandbox" in mg_domain and not from_email.lower().endswith("@" + mg_domain.lower()):
        print(f"Mailgun: sandbox domain detected; forcing EMAIL_FROM to postmaster@{mg_domain}")
        from_email = f"postmaster@{mg_domain}"

    from_header = _format_from_header(from_name, from_email)

    endpoint = f"{base_url}/v3/{mg_domain}/messages"

    data = {
        "from": from_header,
        "to": [email_to],
        "subject": subject,
        "html": html,
    }
    if text:
        data["text"] = text

    if debug_enabled:
        print("Mailgun DEBUG:")
        print(f"  endpoint: {endpoint}")
        print(f"  from: {from_header}")
        print(f"  to: {email_to}")
        print(f"  subject: {subject}")

    try:
        resp = requests.post(
            endpoint,
            auth=("api", api_key),
            data=data,
            timeout=20,
        )
    except Exception as e:
        print(f"Mailgun send exception: {type(e).__name__}: {e}")
        return False

    if 200 <= resp.status_code < 300:
        if debug_enabled:
            print(f"Mailgun OK: {resp.status_code} {resp.text[:300]}")
        return True

    # Helpful error output without killing cron
    body = (resp.text or "").strip()
    print(f"Mailgun send failed: {resp.status_code} {body[:2000]}")
    return False

# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    world = fetch_world_stories(limit=3)

    print("World stories:")
    for s in world:
        print(f"- [{s['source']}] {s['title']}")

    email_html = render_email(world)
    subject = f"The 2k Times · {now_utc().strftime('%d.%m.%Y')}"

    if str(os.getenv("SEND_EMAIL", "false")).lower() == "true":
        ok = send_mailgun(subject, email_html)
        if not ok:
            print("Email failed (continuing — cron safe).")
    else:
        print("SEND_EMAIL=false — skipping send")
