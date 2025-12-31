import os
import re
import smtplib
import json
import urllib.parse
import urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-12"
DEBUG_SUBJECT = True  # set False when you're happy

# ----------------------------
# ENV
# ----------------------------
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")
SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

READER_BASE_URL = (os.environ.get("READER_BASE_URL", "https://the-2k-times.onrender.com") or "").rstrip("/")

SMTP_HOST = os.environ.get("MAILGUN_SMTP_HOST", "smtp.mailgun.org")
SMTP_PORT = int(os.environ.get("MAILGUN_SMTP_PORT", "587"))

if not all([MAILGUN_DOMAIN, EMAIL_TO, SMTP_USER, SMTP_PASS]):
    raise SystemExit(
        "Missing required env vars: MAILGUN_DOMAIN, EMAIL_TO, MAILGUN_SMTP_USER, MAILGUN_SMTP_PASS"
    )

# ----------------------------
# TIME WINDOW
# ----------------------------
TZ = ZoneInfo("Europe/London")
now_uk = datetime.now(TZ)
window_start = now_uk - timedelta(hours=24)

base_subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"
subject = (
    base_subject
    if not DEBUG_SUBJECT
    else f"{base_subject} · {now_uk.strftime('%H:%M:%S')} · {TEMPLATE_VERSION}"
)

# ----------------------------
# SOURCES (World Headlines)
# ----------------------------
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

# ----------------------------
# HELPERS
# ----------------------------
def esc(s: str) -> str:
    s = s or ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def reader_link(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return f"{READER_BASE_URL}/read?url={urllib.parse.quote(url, safe='')}"


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def two_sentence_summary(text: str) -> str:
    text = strip_html(text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
    if not sentences:
        return "Summary unavailable."
    return " ".join(sentences[:2])


def parse_time(entry):
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    if getattr(entry, "updated_parsed", None):
        return datetime(*entry.updated_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None


def looks_like_low_value(title: str) -> bool:
    t = (title or "").lower()
    return any(w in t for w in ["live", "minute-by-minute", "as it happened"])


def collect_articles(feed_urls, limit):
    articles = []
    for feed_url in feed_urls:
        feed = feedparser.parse(feed_url)
        for e in feed.entries:
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "").strip()
            if not title or not link:
                continue
            if looks_like_low_value(title):
                continue

            published = parse_time(e)
            if not published or not (window_start <= published <= now_uk):
                continue

            summary_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            articles.append(
                {
                    "title": title,
                    "summary": two_sentence_summary(summary_raw),
                    "url": link,
                    "reader": reader_link(link),
                    "published": published,
                }
            )

    articles.sort(key=lambda x: x["published"], reverse=True)

    seen = set()
    unique = []
    for a in articles:
        k = a["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        unique.append(a)

    return unique[:limit]


def fetch_json(url: str, timeout: int = 12):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "The-2k-Times/1.0 (+https://the-2k-times.onrender.com)"
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def get_cardiff_daily_weather():
    """
    Open-Meteo (no key) — returns:
    - current temp + feels-like
    - today's high/low
    - sunrise/sunset (Europe/London)
    """
    # Cardiff coords
    lat, lon = 51.4816, -3.1791
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        "&timezone=Europe%2FLondon"
    )
    try:
        data = fetch_json(url)
        cur = data.get("current", {}) or {}
        daily = data.get("daily", {}) or {}

        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")

        tmax = None
        tmin = None
        sunrise = None
        sunset = None

        if isinstance(daily.get("temperature_2m_max"), list) and daily["temperature_2m_max"]:
            tmax = daily["temperature_2m_max"][0]
        if isinstance(daily.get("temperature_2m_min"), list) and daily["temperature_2m_min"]:
            tmin = daily["temperature_2m_min"][0]
        if isinstance(daily.get("sunrise"), list) and daily["sunrise"]:
            sunrise = daily["sunrise"][0]
        if isinstance(daily.get("sunset"), list) and daily["sunset"]:
            sunset = daily["sunset"][0]

        return {
            "ok": True,
            "temp": temp,
            "feels": feels,
            "high": tmax,
            "low": tmin,
            "sunrise": sunrise,  # ISO-ish string in local tz (e.g. 2025-12-31T08:12)
            "sunset": sunset,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_people_in_space():
    """
    Open Notify (simple public endpoint). If it fails, return empty list.
    """
    url = "http://api.open-notify.org/astros.json"
    try:
        data = fetch_json(url, timeout=12)
        people = data.get("people", []) or []
        # Normalize: [{name, craft}, ...]
        out = []
        for p in people:
            name = (p.get("name") or "").strip()
            craft = (p.get("craft") or "").strip()
            if name:
                out.append({"name": name, "craft": craft})
        return {"ok": True, "people": out, "number": data.get("number")}
    except Exception as e:
        return {"ok": False, "error": str(e), "people": []}


def fmt_c(v):
    if v is None or v == "":
        return "—"
    try:
        # Avoid trailing .0
        iv = int(round(float(v)))
        return f"{iv}°C"
    except Exception:
        return f"{v}°C"


def fmt_time_local_iso(iso_str):
    """
    iso_str like '2025-12-31T08:12' in Europe/London timezone
    -> '08:12'
    """
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except Exception:
        return iso_str


# ----------------------------
# DATA
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)
wx = get_cardiff_daily_weather()
space = get_people_in_space()

# Counts for “Inside Today”
counts = {
    "UK Politics": 2,
    "Rugby Union": 5,
    "Punk Rock": 0,
}

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#f7f5ef"
    ink = "#111111"
    muted = "#4a4a4a"
    rule = "#ddd8cc"      # unify to thinnest rule
    link = "#0b57d0"

    font = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
    date_line = now_uk.strftime("%d.%m.%Y")

    # Prevent client “font boosting”
    size_fix_inline = "-webkit-text-size-adjust:100%;text-size-adjust:100%;-ms-text-size-adjust:100%;"

    style_block = """
    <style>
      @media screen and (max-width:640px){
        .container{width:100%!important}
        .stack{display:block!important;width:100%!important}
        .divider{display:none!important}
        .colpadL{padding-left:0!important}
        .colpadR{padding-right:0!important}
      }
    </style>
    """

    def section_title(text, emoji):
        return f"""
          <span style="font-family:{font};
                       font-size:14px !important;
                       font-weight:900 !important;
                       letter-spacing:3px;
                       text-transform:uppercase;
                       color:{ink};
                       {size_fix_inline}">
            {emoji} {esc(text)}
          </span>
        """

    def story_block(i, it, lead=False):
        # Top story headline now same size as others (per your request)
        headline_size = "20px"
        headline_weight = "900" if lead else "800"  # still a touch heavier for lead, but same size
        summary_size = "14px"
        summary_weight = "500" if lead else "400"

        # Keep the left bar and "TOP STORY" kicker for #1, but without giant headline
        left_bar = f"border-left:4px solid {ink};padding-left:12px;" if lead else ""
        kicker_row = ""
        if lead:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:11px !important;font-weight:900 !important;
                         letter-spacing:2px;text-transform:uppercase;color:{muted};
                         padding:0 0 8px 0;{size_fix_inline}">
                TOP STORY
              </td>
            </tr>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:16px 0 0 0;{size_fix_inline}">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="{left_bar}{size_fix_inline}">
                    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                      {kicker_row}

                      <tr>
                        <td style="font-family:{font};
                                   font-size:{headline_size} !important;
                                   font-weight:{headline_weight} !important;
                                   line-height:1.2;
                                   color:{ink};
                                   padding:0;
                                   {size_fix_inline}">
                          <span style="font-size:{headline_size} !important;font-weight:{headline_weight} !important;">
                            {i}. {esc(it['title'])}
                          </span>
                        </td>
                      </tr>

                      <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>

                      <tr>
                        <td style="font-family:{font};
                                   font-size:{summary_size} !important;
                                   font-weight:{summary_weight} !important;
                                   line-height:1.7;
                                   color:{muted};
                                   padding:0;
                                   {size_fix_inline}">
                          <span style="font-size:{summary_size} !important;font-weight:{summary_weight} !important;">
                            {esc(it['summary'])}
                          </span>
                        </td>
                      </tr>

                      <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                      <tr>
                        <td style="font-family:{font};
                                   font-size:12px !important;
                                   font-weight:900 !important;
                                   letter-spacing:1px;
                                   text-transform:uppercase;
                                   padding:0;
                                   {size_fix_inline}">
                          <a href="{esc(it['reader'])}" style="color:{link};text-decoration:none;">
                            Read in Reader →
                          </a>
                        </td>
                      </tr>

                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    world_html = ""
    if world_items:
        for i, it in enumerate(world_items, start=1):
            world_html += story_block(i, it, lead=(i == 1))
    else:
        world_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:18px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
              No qualifying world headlines in the last 24 hours.
            </td>
          </tr>
          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    # Build the “Inside Today” info blocks
    # Weather
    if wx.get("ok"):
        wx_line = f"{fmt_c(wx.get('temp'))} (feels {fmt_c(wx.get('feels'))}) · H {fmt_c(wx.get('high'))} / L {fmt_c(wx.get('low'))}"
        sunrise = fmt_time_local_iso(wx.get("sunrise"))
        sunset = fmt_time_local_iso(wx.get("sunset"))
    else:
        wx_line = "Weather unavailable."
        sunrise = "—"
        sunset = "—"

    # People in space
    people = (space.get("people") or []) if space.get("ok") else []
    # prefer grouping ISS first (if present)
    iss_people = [p for p in people if (p.get("craft") or "").upper() == "ISS"]
    other_people = [p for p in people if p not in iss_people]
    ordered_people = iss_people + other_people

    max_names = 8
    shown = ordered_people[:max_names]
    remaining = max(0, len(ordered_people) - len(shown))

    if shown:
        # show as bullet-like lines with craft tag
        space_lines = ""
        for p in shown:
            nm = esc(p.get("name") or "—")
            craft = esc(p.get("craft") or "")
            craft_tag = f" <span style='color:{muted};font-weight:600'>( {craft} )</span>" if craft else ""
            space_lines += f"{nm}{craft_tag}<br/>"
        if remaining:
            space_lines += f"<span style='color:{muted};'>+ {remaining} more</span>"
    else:
        space_lines = "<span style='color:#4a4a4a;'>No data right now.</span>"

    inside_today_html = f"""
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        <!-- Spacer so INSIDE TODAY aligns with TOP STORY block -->
        <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

        <tr>
          <td style="padding:0;{size_fix_inline}">
            {section_title("Inside today", "🗞️")}
          </td>
        </tr>
        <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

        <tr>
          <td style="font-family:{font};
                     font-size:15px !important;
                     font-weight:600 !important;
                     line-height:1.9;
                     color:{muted};
                     {size_fix_inline}">
            • UK Politics ({counts['UK Politics']} stories)<br/>
            • Rugby Union ({counts['Rugby Union']} stories)<br/>
            • Punk Rock ({counts['Punk Rock']} stories)
          </td>
        </tr>

        <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

        <tr>
          <td style="font-family:{font};
                     font-size:12px !important;
                     font-weight:500;
                     line-height:1.7;
                     color:{muted};
                     {size_fix_inline}">
            Curated from the last 24 hours.<br/>
            Reader links included.
          </td>
        </tr>

        <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

        <!-- Weather -->
        <tr>
          <td style="padding:0;{size_fix_inline}">
            {section_title("Weather · Cardiff", "🌦️")}
          </td>
        </tr>
        <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr>
          <td style="font-family:{font};
                     font-size:14px !important;
                     font-weight:600 !important;
                     line-height:1.7;
                     color:{ink};
                     {size_fix_inline}">
            <span style="font-weight:800;">{esc(wx_line)}</span>
          </td>
        </tr>

        <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

        <!-- Sunrise / Sunset -->
        <tr>
          <td style="padding:0;{size_fix_inline}">
            {section_title("Sunrise / Sunset", "🌅")}
          </td>
        </tr>
        <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr>
          <td style="font-family:{font};
                     font-size:14px !important;
                     font-weight:600 !important;
                     line-height:1.7;
                     color:{ink};
                     {size_fix_inline}">
            Sunrise: <span style="font-weight:900;">{esc(sunrise)}</span>
            &nbsp;&nbsp;•&nbsp;&nbsp;
            Sunset: <span style="font-weight:900;">{esc(sunset)}</span>
          </td>
        </tr>

        <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

        <!-- Who's in space -->
        <tr>
          <td style="padding:0;{size_fix_inline}">
            {section_title("Who's in space", "🚀")}
          </td>
        </tr>
        <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr>
          <td style="font-family:{font};
                     font-size:13.5px !important;
                     font-weight:600 !important;
                     line-height:1.8;
                     color:{ink};
                     {size_fix_inline}">
            {space_lines}
          </td>
        </tr>

      </table>
    """

    return f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      {style_block}
    </head>

    <body style="margin:0;background:{outer_bg};{size_fix_inline}">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;background:{outer_bg};{size_fix_inline}">
        <tr>
          <td align="center" style="padding:18px;{size_fix_inline}">
            <table class="container" width="720" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse;background:{paper};border-radius:14px;overflow:hidden;{size_fix_inline}">

              <!-- Masthead -->
              <tr>
                <td align="center" style="padding:28px 20px 14px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:46px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.05;
                                                {size_fix_inline}">
                        <span style="font-size:46px !important;font-weight:900 !important;">
                          The 2k Times
                        </span>
                      </td>
                    </tr>
                    <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:12px !important;
                                                font-weight:700 !important;
                                                letter-spacing:2px;
                                                text-transform:uppercase;
                                                color:{muted};
                                                {size_fix_inline}">
                        <span style="font-size:12px !important;font-weight:700 !important;">
                          {date_line} · Daily Edition · {TEMPLATE_VERSION}
                        </span>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Single thin rule under masthead (removed thick black line) -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- Section header -->
              <tr>
                <td style="padding:16px 20px 10px 20px;">
                  {section_title("World Headlines", "🌍")}
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- Content columns -->
              <tr>
                <td style="padding:12px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="50%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider (thin, same as article separators) -->
                      <td class="divider" width="1" style="background:{rule};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="50%" valign="top" style="padding-left:12px;">
                        {inside_today_html}
                      </td>

                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="padding:16px;text-align:center;font-family:{font};
                           font-size:11px !important;color:{muted};{size_fix_inline}">
                  © The 2k Times · Delivered daily at 05:30
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """


# ----------------------------
# Plain text fallback
# ----------------------------
plain_lines = [
    f"THE 2K TIMES — {now_uk.strftime('%d.%m.%Y')}",
    "",
    f"(Plain-text fallback) {TEMPLATE_VERSION}",
    "",
    "🌍 WORLD HEADLINES",
    "",
]

if not world_items:
    plain_lines.append("No qualifying world headlines in the last 24 hours.")
else:
    for i, it in enumerate(world_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

plain_lines += [
    "",
    "🗞️ INSIDE TODAY",
    f"- UK Politics ({counts['UK Politics']} stories)",
    f"- Rugby Union ({counts['Rugby Union']} stories)",
    f"- Punk Rock ({counts['Punk Rock']} stories)",
    "",
    "🌦️ WEATHER · CARDIFF",
]

if wx.get("ok"):
    plain_lines.append(f"{fmt_c(wx.get('temp'))} (feels {fmt_c(wx.get('feels'))}) · H {fmt_c(wx.get('high'))} / L {fmt_c(wx.get('low'))}")
else:
    plain_lines.append("Weather unavailable.")

plain_lines += [
    "",
    "🌅 SUNRISE / SUNSET",
    f"Sunrise: {fmt_time_local_iso(wx.get('sunrise'))} · Sunset: {fmt_time_local_iso(wx.get('sunset'))}",
    "",
    "🚀 WHO'S IN SPACE",
]

if space.get("ok") and (space.get("people") or []):
    people = space["people"]
    iss_people = [p for p in people if (p.get("craft") or "").upper() == "ISS"]
    other_people = [p for p in people if p not in iss_people]
    ordered_people = iss_people + other_people
    shown = ordered_people[:10]
    for p in shown:
        craft = p.get("craft") or ""
        craft_tag = f" ({craft})" if craft else ""
        plain_lines.append(f"- {p.get('name','—')}{craft_tag}")
    if len(ordered_people) > len(shown):
        plain_lines.append(f"... +{len(ordered_people) - len(shown)} more")
else:
    plain_lines.append("No data right now.")

plain_body = "\n".join(plain_lines).strip() + "\n"

# ----------------------------
# Send email (multipart/alternative)
# ----------------------------
html_body = build_html()

msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO

msg.set_content(plain_body)
msg.add_alternative(html_body, subtype="html")

print("Sending:", subject)
print("TEMPLATE_VERSION:", TEMPLATE_VERSION)
print("Window (UK):", window_start.isoformat(), "→", now_uk.isoformat())
print("World headlines:", len(world_items))
print("Weather ok:", wx.get("ok"))
print("Space ok:", space.get("ok"), "People:", len(space.get("people") or []))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
