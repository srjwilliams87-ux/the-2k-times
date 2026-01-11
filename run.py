print(">>> run.py starting")

import os
import sys
import requests
import feedparser
import datetime as dt
from urllib.parse import quote_plus
from email.utils import formatdate
from datetime import datetime

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
    return dt.datetime.now(dt.timezone.utc)

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

def edition_line():
    edition_tag = "v-newspaper-14"
    now = dt.datetime.now(dt.timezone.utc)
    return f"{now.strftime('%d.%m.%Y')} · Daily Edition · {edition_tag}"

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

from html import escape

def render_email(world, edition="", weather=None, sunrise_sunset=None, space_people=None):
    """
    Newspaper-style HTML email (Gmail-safe).
    - world: list[dict] with title/summary/source and optionally reader_url/url
    - weather: dict or string (optional)
    - sunrise_sunset: dict or string (optional)
    - space_people: list[str] or string (optional)
    """

    def e(x):
        return escape(str(x)) if x is not None else ""

    def story_link(s):
    # your feed data uses 'link'
        link = (s.get("reader_url") or s.get("url") or s.get("link") or "").strip()

    # Ensure scheme exists (Gmail needs absolute URLs)
    if link.startswith(("http://", "https://")):
        return link
    if link.startswith("www."):
        return "https://" + link

    # If you ever pass relative reader paths in future, this will fix them
    base = (os.getenv("READER_BASE_URL", "") or "").rstrip("/")
    if base and link.startswith("/"):
        return base + link

    return link

def render_story(s, idx):
    title = e(s.get("title", ""))
    source = e(s.get("source", ""))
    summary = e(s.get("summary", ""))

    raw_link = story_link(s)          # <- unescaped
    link = e(raw_link) if raw_link else ""

    link_html = (
        f'<a href="{link}" style="font-size:14px; font-weight:600; text-decoration:none;">'
        'Read in Reader &rarr;</a>'
        if link else ""
    )

    return f"""
    <tr>
      <td style="padding: 0 0 18px 0;">
        <div style="font-size:12px; letter-spacing:0.08em; text-transform:uppercase; opacity:0.8;">
          {idx}. {source}
        </div>
        <div style="font-size:18px; line-height:1.25; font-weight:700; margin: 4px 0 6px 0;">
          {title}
        </div>
        <div style="font-size:14px; line-height:1.55; margin: 0 0 10px 0;">
          {summary}
        </div>
        {link_html}
      </td>
    </tr>
    """
    

    def render_box(title, body_html):
        return f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
               style="border:1px solid rgba(0,0,0,0.12); border-radius:10px;">
          <tr>
            <td style="padding:12px 12px 10px 12px;">
              <div style="font-size:13px; letter-spacing:0.08em; text-transform:uppercase; font-weight:700;">
                {e(title)}
              </div>
              <div style="height:8px;"></div>
              <div style="font-size:14px; line-height:1.5;">
                {body_html}
              </div>
            </td>
          </tr>
        </table>
        """

    # Right column content (optional modules)
    right_col_blocks = []

    if weather is not None:
        if isinstance(weather, dict):
            # Example: {"location":"Cardiff","temp":"1°C","hi":"6.8°C","lo":"0.9°C"}
            w = weather
            body = f"<strong>{e(w.get('location','Weather'))}</strong><br>{e(w.get('temp',''))} · H {e(w.get('hi',''))} / L {e(w.get('lo',''))}"
        else:
            body = e(weather)
        right_col_blocks.append(render_box("Weather", body))

    if sunrise_sunset is not None:
        if isinstance(sunrise_sunset, dict):
            ss = sunrise_sunset
            body = f"Sunrise: <strong>{e(ss.get('sunrise',''))}</strong><br>Sunset: <strong>{e(ss.get('sunset',''))}</strong>"
        else:
            body = e(sunrise_sunset)
        right_col_blocks.append(render_box("Sunrise / Sunset", body))

    if space_people is not None:
        if isinstance(space_people, (list, tuple)):
            ppl = "<br>".join(e(x) for x in space_people)
        else:
            ppl = e(space_people)
        right_col_blocks.append(render_box("Who's in Space", ppl))

    right_col_html = ""
    if right_col_blocks:
        # Stack blocks with spacing
        right_col_html = "<div style='height:12px;'></div>".join(right_col_blocks)
    else:
        # If no modules provided, show a small placeholder note (or leave empty)
        right_col_html = render_box("Today", "No extra modules enabled.")

    # Edition line (you can change this to whatever you already build)
    # Keep it plain text so it survives client quirks.
    # If you already have now_utc(), you can pass in a preformatted string instead.
    edition_line = "Daily Edition"

    # Build main stories
    stories_rows = "".join(render_story(s, i + 1) for i, s in enumerate(world or []))

    html = f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    /* Gmail supports <style> but still use inline for key layout */
    @media screen and (max-width: 640px) {{
      .container {{ width: 100% !important; }}
      .col {{ display: block !important; width: 100% !important; }}
      .col-pad-left {{ padding-left: 0 !important; }}
      .rule-vert {{ display: none !important; }}
      .masthead-title {{ font-size: 34px !important; }}
      .headline {{ font-size: 20px !important; }}
    }}
  </style>
</head>
<body style="margin:0; padding:0; background:#f6f3ea;">
  <!-- Outer background -->
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f6f3ea;">
    <tr>
      <td align="center" style="padding: 24px 12px;">
        <!-- Container -->
        <table role="presentation" class="container" width="680" cellspacing="0" cellpadding="0" border="0"
               style="width:680px; max-width:680px; background:#ffffff; border:1px solid rgba(0,0,0,0.12);">
          <!-- Masthead -->
          <tr>
            <td style="padding: 22px 22px 14px 22px;">
              <div class="masthead-title" style="font-family: Georgia, 'Times New Roman', Times, serif; font-size:42px; line-height:1.05; font-weight:700; margin:0;">
                The 2k Times
              </div>
              <div style="height:10px;"></div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="font-family: Arial, Helvetica, sans-serif; font-size:13px; letter-spacing:0.06em; text-transform:uppercase;">
                    {e(edition_line)}
                  </td>
                  <td align="right" style="font-family: Arial, Helvetica, sans-serif; font-size:13px; letter-spacing:0.06em;">
                    <!-- Put your date string here if you want -->
                  </td>
                </tr>
              </table>
              <div style="height:12px;"></div>
              <div style="border-top:2px solid #111; height:0;"></div>
              <div style="height:10px;"></div>
              <div style="border-top:1px solid rgba(0,0,0,0.25); height:0;"></div>
            </td>
          </tr>

          <!-- Two-column body -->
          <tr>
            <td style="padding: 18px 22px 24px 22px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <!-- Left column -->
                  <td class="col" width="64%" valign="top" style="width:64%; padding-right:16px;">
                    <div style="font-family: Arial, Helvetica, sans-serif; font-size:13px; letter-spacing:0.08em; text-transform:uppercase; font-weight:800;">
                      🌍 World Headlines
                    </div>
                    <div style="height:10px;"></div>
                    <div style="border-top:1px solid rgba(0,0,0,0.18); height:0;"></div>
                    <div style="height:14px;"></div>

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                           style="font-family: Arial, Helvetica, sans-serif; color:#111;">
                      {stories_rows}
                    </table>
                  </td>

                  <!-- Vertical rule -->
                  <td class="rule-vert" width="1" valign="top" style="width:1px; background: rgba(0,0,0,0.12);"></td>

                  <!-- Right column -->
                  <td class="col col-pad-left" width="36%" valign="top" style="width:36%; padding-left:16px; font-family: Arial, Helvetica, sans-serif; color:#111;">
                    {right_col_html}
                  </td>
                </tr>
              </table>

              <div style="height:18px;"></div>
              <div style="border-top:1px solid rgba(0,0,0,0.18); height:0;"></div>
              <div style="height:14px;"></div>

              <div style="font-family: Arial, Helvetica, sans-serif; font-size:12px; line-height:1.4; opacity:0.75;">
                You’re receiving this because you subscribed to The 2k Times.
              </div>
            </td>
          </tr>

        </table>
        <!-- /Container -->
      </td>
    </tr>
  </table>
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

def send_mailgun(subject: str, html: str) -> bool:
    """
    Drop-in Mailgun sender (API).
    Env vars expected (per your Render screenshot):
      - MAILGUN_API_KEY
      - MAILGUN_DOMAIN
      - MAILGUN_API_BASE_URL   (e.g. https://api.mailgun.net)  [optional; defaults to api.mailgun.net]
      - EMAIL_FROM             (e.g. postmaster@<your-domain>)
      - EMAIL_FROM_NAME        (e.g. The 2k Times)             [optional]
      - EMAIL_TO
      - DEBUG_EMAIL            ("true"/"false")                [optional]
    Returns True on success, False on failure (cron-safe).
    """
    import os
    import requests

    api_key = (os.getenv("MAILGUN_API_KEY") or "").strip()
    domain = (os.getenv("MAILGUN_DOMAIN") or "").strip()
    base = (os.getenv("MAILGUN_API_BASE_URL") or "https://api.mailgun.net").strip().rstrip("/")

    email_from = (os.getenv("EMAIL_FROM") or "").strip()
    from_name = (os.getenv("EMAIL_FROM_NAME") or "The 2k Times").strip()
    email_to = (os.getenv("EMAIL_TO") or "").strip()

    debug = (os.getenv("DEBUG_EMAIL") or "").strip().lower() in ("1", "true", "yes", "y", "on")

    missing = [k for k, v in {
        "MAILGUN_API_KEY": api_key,
        "MAILGUN_DOMAIN": domain,
        "EMAIL_FROM": email_from,
        "EMAIL_TO": email_to,
    }.items() if not v]

    if missing:
        print(f"Mailgun: missing env vars: {', '.join(missing)}")
        return False

    # IMPORTANT: Use the same base URL you proved via curl.
    endpoint = f"{base}/v3/{domain}/messages"

    # Match your working curl test: from="The 2k Times <EMAIL_FROM>"
    from_header = f"{from_name} <{email_from}>"

    data = {
        "from": from_header,
        "to": email_to,
        "subject": subject,
        "html": html,
    }

    if debug:
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
        print(f"Mailgun send exception: {e}")
        return False

    if 200 <= resp.status_code < 300:
        if debug:
            print(f"Mailgun OK: {resp.status_code} {resp.text}")
        return True

    # Helpful debugging for the exact problem you had
    print(f"Mailgun send failed: {resp.status_code} {resp.text}")
    if resp.status_code in (401, 403) and "Invalid private key" in (resp.text or ""):
        print("Hint: Your API key is being rejected. Double-check MAILGUN_API_KEY and MAILGUN_API_BASE_URL.")
        print("Your working curl used: https://api.mailgun.net (NOT api.eu.mailgun.net).")

    return False

# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    world = fetch_world_stories(limit=3)

    print("World stories:")
    for s in world:
        print(f"- [{s['source']}] {s['title']}")

    line = edition_line()
    weather = get_weather_cardiff()
    sunrise_sunset = get_sun_times()
    space_people = get_whos_in_space()

    email_html = render_email(
    world,
    edition=line,
    weather=weather,
    sunrise_sunset=sunrise_sunset,
    space_people=space_people,
)


    # STEP 4: write a local preview file (only when DEBUG_EMAIL=true)
    if str(os.getenv("DEBUG_EMAIL", "false")).lower() == "true":
        with open("email_preview.html", "w", encoding="utf-8") as f:
            f.write(email_html)
        print("Wrote email_preview.html")

    subject = f"The 2k Times · {now_utc().strftime('%d.%m.%Y')}"

    if str(os.getenv("SEND_EMAIL", "false")).lower() == "true":
        ok = send_mailgun(subject, email_html)
        if not ok:
            print("Email failed (continuing — cron safe).")
    else:
        print("SEND_EMAIL=false — skipping send")


if __name__ == "__main__":
    print(">>> run.py starting")
    try:
        main()
    finally:
        print(">>> run.py finished")
