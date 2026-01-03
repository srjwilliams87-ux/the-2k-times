import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup


# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-CORE-01"
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
# TIME
# ----------------------------
TZ = ZoneInfo("Europe/London")
now_uk = datetime.now(TZ)
window_start_24h = now_uk - timedelta(hours=24)
window_start_72h = now_uk - timedelta(hours=72)  # fallback window to ensure we always get 3 distinct sources


base_subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"
subject = (
    base_subject
    if not DEBUG_SUBJECT
    else f"{base_subject} · {now_uk.strftime('%H:%M:%S')} · {TEMPLATE_VERSION}"
)


# ----------------------------
# WORLD SOURCES (required)
# ----------------------------
WORLD_SOURCE_FEEDS = {
    "BBC": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
    ],
    "Reuters": [
        "https://feeds.reuters.com/reuters/worldNews",
    ],
    "Guardian": [
        "https://www.theguardian.com/world/rss",
    ],
    "Independent": [
        # Independent RSS can be flaky; this is the standard feed endpoint they publish.
        "https://www.independent.co.uk/news/world/rss",
    ],
}

SOURCE_DOMAIN_HINTS = {
    "BBC": ["bbc.co.uk", "bbc.com"],
    "Reuters": ["reuters.com"],
    "Guardian": ["theguardian.com"],
    "Independent": ["independent.co.uk"],
}


# ----------------------------
# HELPERS
# ----------------------------
def reader_link(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return f"{READER_BASE_URL}/read?url={url}"


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


def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def host_for(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def guess_source_from_url(url: str) -> str:
    h = host_for(url)
    for source, hints in SOURCE_DOMAIN_HINTS.items():
        if any(h.endswith(x) or x in h for x in hints):
            return source
    return "Unknown"


def collect_recent_entries(feed_urls, window_start):
    """Collect entries within a time window."""
    out = []
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
            if not published:
                continue

            if published < window_start or published > now_uk:
                continue

            summary_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            out.append(
                {
                    "title": title,
                    "summary": two_sentence_summary(summary_raw),
                    "url": link,
                    "reader": reader_link(link),
                    "published": published,
                    "source": guess_source_from_url(link),
                }
            )
    out.sort(key=lambda x: x["published"], reverse=True)

    # de-dupe by title
    seen = set()
    unique = []
    for a in out:
        k = a["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        unique.append(a)
    return unique


def pick_world_three_distinct():
    """
    Essential requirement:
    - 3 stories
    - from 3 different sources
    - sources must be among BBC/Reuters/Guardian/Independent
    """

    # Step 1: try strict 24h
    selected = {}
    for source, feeds in WORLD_SOURCE_FEEDS.items():
        items = collect_recent_entries(feeds, window_start_24h)
        if items:
            selected[source] = items[0]

    # Step 2: if not enough distinct sources, expand to 72h *per missing source*
    if len(selected) < 3:
        for source, feeds in WORLD_SOURCE_FEEDS.items():
            if source in selected:
                continue
            items = collect_recent_entries(feeds, window_start_72h)
            if items:
                selected[source] = items[0]
            if len(selected) >= 3:
                break

    # Step 3: if still not enough (rare), take next-best from sources we do have,
    # but ensure "3 different sources" by prioritising distinct sources first.
    # If we literally cannot get 3 distinct, we'll pad with newest available items (still useful).
    chosen = list(selected.values())
    chosen.sort(key=lambda x: x["published"], reverse=True)

    if len(chosen) >= 3:
        return chosen[:3]

    # Fallback: pull a combined pool from all feeds (72h), then fill missing slots
    pool = []
    for source, feeds in WORLD_SOURCE_FEEDS.items():
        pool.extend(collect_recent_entries(feeds, window_start_72h))
    pool.sort(key=lambda x: x["published"], reverse=True)

    used_sources = {c["source"] for c in chosen}
    for it in pool:
        if it["source"] in used_sources:
            continue
        chosen.append(it)
        used_sources.add(it["source"])
        if len(chosen) == 3:
            break

    # final padding (worst case)
    if len(chosen) < 3:
        for it in pool:
            if it not in chosen:
                chosen.append(it)
            if len(chosen) == 3:
                break

    return chosen[:3]


# ----------------------------
# WEATHER (Open-Meteo)
# ----------------------------
def get_cardiff_weather():
    try:
        lat, lon = 51.4816, -3.1791
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,apparent_temperature"
            "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
            "&timezone=Europe%2FLondon"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        cur = data.get("current", {}) or {}
        daily = data.get("daily", {}) or {}

        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]
        sunrise_iso = (daily.get("sunrise") or [""])[0]
        sunset_iso = (daily.get("sunset") or [""])[0]

        def fmt_time(iso_str):
            if not iso_str:
                return "--:--"
            try:
                t = datetime.fromisoformat(iso_str)
                return t.strftime("%H:%M")
            except Exception:
                return "--:--"

        return {
            "temp_c": temp,
            "feels_c": feels,
            "hi_c": hi,
            "lo_c": lo,
            "sunrise": fmt_time(sunrise_iso),
            "sunset": fmt_time(sunset_iso),
        }
    except Exception:
        return None


# ----------------------------
# WHO'S IN SPACE (whoisinspace.com scrape)
# ----------------------------
def get_whos_in_space():
    try:
        url = "https://whoisinspace.com/"
        headers = {"User-Agent": "Mozilla/5.0 (The 2k Times)"}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n", strip=True)

        people = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Expect patterns like "Name (ISS)" or "Name (Tiangong)"
            m = re.match(r"^(.+?)\s*\((ISS|Tiangong|CSS)\)\s*$", line, re.I)
            if m:
                people.append({"name": m.group(1).strip(), "station": m.group(2).strip()})

        # Deduplicate preserve order
        seen = set()
        out = []
        for p in people:
            key = (p["name"].lower(), p["station"].lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(p)

        return out
    except Exception:
        return []


# ----------------------------
# COLLECT CORE CONTENT
# ----------------------------
world_items = pick_world_three_distinct()  # must be 3 from 3 different sources
wx = get_cardiff_weather()
space_people = get_whos_in_space()


# ----------------------------
# HTML EMAIL (Newspaper)
# ----------------------------
def build_html():
    # Keep the same overall look/feel as your current version.
    outer_bg = "#111111"
    paper = "#1b1b1b"   # dark paper (matches your current screenshots)
    ink = "#ffffff"
    muted = "#cfcfcf"
    rule_light = "#2a2a2a"
    link = "#8ab4ff"

    font = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
    date_line = now_uk.strftime("%d.%m.%Y")

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

    def story_block(i, it, lead=False):
        headline_size = "18px"
        headline_weight = "900" if lead else "800"
        summary_size = "14px"
        summary_weight = "500"
        pad_top = "18px" if lead else "16px"

        left_bar = "border-left:4px solid %s;padding-left:14px;" % ink if lead else ""

        kicker_row = ""
        if lead:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:11px;font-weight:900;letter-spacing:2px;
                         text-transform:uppercase;color:{muted};padding:0 0 10px 0;{size_fix_inline}">
                TOP STORY
              </td>
            </tr>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr><td style="height:{pad_top};font-size:0;line-height:0;">&nbsp;</td></tr>

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

                <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

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

                <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

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

          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    # World HTML (must be 3)
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
        </table>
        """

    # Right column values (KEEP SAME SECTIONS)
    inside_today_counts = {
        "UK Politics": 0,
        "Rugby Union": 0,
        "Punk Rock": 0,
    }

    # Weather line
    if wx and (wx.get("temp_c") is not None):
        wx_line = f"{wx['temp_c']:.1f}°C (feels {wx['feels_c']:.1f}°C) · H {wx['hi_c']:.1f}°C / L {wx['lo_c']:.1f}°C"
        sunrise_line = f"Sunrise: {wx['sunrise']}  ·  Sunset: {wx['sunset']}"
    else:
        wx_line = "Weather data unavailable."
        sunrise_line = "Sunrise: --:--  ·  Sunset: --:--"

    # Who's in Space (ALL people)
    if space_people:
        space_lines = "<br/>".join([f"{esc(p['name'])} ({esc(p['station'])})" for p in space_people])
    else:
        space_lines = "Unable to load space roster."

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
                <td align="center" style="padding:30px 20px 14px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:60px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.03;
                                                {size_fix_inline}">
                        <span style="font-size:60px !important;font-weight:900 !important;">
                          The 2k Times
                        </span>
                      </td>
                    </tr>
                    <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:13px !important;
                                                font-weight:700 !important;
                                                letter-spacing:2px;
                                                text-transform:uppercase;
                                                color:{muted};
                                                {size_fix_inline}">
                        <span style="font-size:13px !important;font-weight:700 !important;">
                          {date_line} · Daily Edition · {TEMPLATE_VERSION}
                        </span>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Rules -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <!-- Section header -->
              <tr>
                <td style="padding:18px 20px 10px 20px;">
                  <span style="font-family:{font};
                               font-size:18px !important;
                               font-weight:900 !important;
                               letter-spacing:0.2px;
                               color:{ink};">
                    🌍 World Headlines
                  </span>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <!-- Content columns -->
              <tr>
                <td style="padding:14px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="60%" valign="top" style="padding-right:18px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule_light};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="40%" valign="top" style="padding-left:18px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

                          <!-- Inside Today (keep same) -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:16px !important;
                                       font-weight:900 !important;
                                       letter-spacing:0.2px;
                                       color:{ink};
                                       {size_fix_inline}">
                              🧾 Inside Today
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.9;
                                       color:{muted};
                                       {size_fix_inline}">
                              • UK Politics ({inside_today_counts['UK Politics']} stories)<br/>
                              • Rugby Union ({inside_today_counts['Rugby Union']} stories)<br/>
                              • Punk Rock ({inside_today_counts['Punk Rock']} stories)
                            </td>
                          </tr>

                          <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

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
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Weather -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:16px !important;
                                       font-weight:900 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              🌤️ Weather · Cardiff
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.7;
                                       color:{muted};
                                       {size_fix_inline}">
                              {esc(wx_line)}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Sunrise/Sunset -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:16px !important;
                                       font-weight:900 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              🌅 Sunrise / Sunset
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.7;
                                       color:{muted};
                                       {size_fix_inline}">
                              {esc(sunrise_line)}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Who's in Space -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:16px !important;
                                       font-weight:900 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              🚀 Who&#39;s in Space
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.7;
                                       color:{muted};
                                       {size_fix_inline}">
                              {space_lines}
                            </td>
                          </tr>

                        </table>
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
# Plain text fallback (core only)
# ----------------------------
plain_lines = [
    f"THE 2K TIMES — {now_uk.strftime('%d.%m.%Y')}",
    "",
    f"(Plain-text fallback) {TEMPLATE_VERSION}",
    "",
    "WORLD HEADLINES",
    "",
]

if not world_items:
    plain_lines.append("No qualifying world headlines found.")
else:
    for i, it in enumerate(world_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

plain_lines += [
    "",
    "INSIDE TODAY",
    "• UK Politics (0 stories)",
    "• Rugby Union (0 stories)",
    "• Punk Rock (0 stories)",
    "",
]

if wx and (wx.get("temp_c") is not None):
    plain_lines.append(f"WEATHER · CARDIFF: {wx['temp_c']:.1f}C (feels {wx['feels_c']:.1f}C) H {wx['hi_c']:.1f}C / L {wx['lo_c']:.1f}C")
    plain_lines.append(f"SUNRISE/SUNSET: Sunrise {wx['sunrise']} · Sunset {wx['sunset']}")
else:
    plain_lines.append("WEATHER · CARDIFF: Weather data unavailable.")
    plain_lines.append("SUNRISE/SUNSET: Sunrise --:-- · Sunset --:--")

plain_lines.append("")
plain_lines.append("WHO'S IN SPACE")
if space_people:
    for p in space_people:
        plain_lines.append(f"- {p['name']} ({p['station']})")
else:
    plain_lines.append("Unable to load space roster.")

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
print("Window (UK):", window_start_24h.isoformat(), "→", now_uk.isoformat())
print("World headlines:", len(world_items), [it.get("source") for it in world_items])
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
