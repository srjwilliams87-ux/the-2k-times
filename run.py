import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-18"
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
# FEEDS
# ----------------------------

WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

UK_POLITICS_FEEDS = [
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://www.theguardian.com/uk-news/rss",
    "https://feeds.reuters.com/reuters/UKdomesticNews",
]

# Rugby Union sources requested (these may occasionally fail; we handle gracefully)
RUGBY_UNION_FEEDS = [
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "https://www.rugbypass.com/feed/",
    "https://www.planetrugby.com/feed/",
    "https://www.rugbyworld.com/feed/",
    "https://www.world.rugby/rss",  # may not exist; safe to keep
]

PUNK_ROCK_FEEDS = [
    "https://www.kerrang.com/feed",
    "https://www.punknews.org/rss.xml",
]

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


def collect_articles(feed_urls, limit):
    articles = []
    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue

        for e in getattr(feed, "entries", []) or []:
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


# ----------------------------
# WEATHER + SUNRISE/SUNSET (Cardiff)
# ----------------------------
def get_cardiff_weather():
    """
    Open-Meteo (no key):
    - current temp
    - apparent temp ("feels like")
    - daily high/low
    - sunrise/sunset
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
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {
            "ok": False,
            "temp_c": None,
            "feels_c": None,
            "hi_c": None,
            "lo_c": None,
            "sunrise": None,
            "sunset": None,
        }

    try:
        temp = data["current"]["temperature_2m"]
        feels = data["current"]["apparent_temperature"]
        hi = data["daily"]["temperature_2m_max"][0]
        lo = data["daily"]["temperature_2m_min"][0]
        sunrise = data["daily"]["sunrise"][0][-5:]  # "HH:MM"
        sunset = data["daily"]["sunset"][0][-5:]
        return {
            "ok": True,
            "temp_c": float(temp),
            "feels_c": float(feels),
            "hi_c": float(hi),
            "lo_c": float(lo),
            "sunrise": sunrise,
            "sunset": sunset,
        }
    except Exception:
        return {
            "ok": False,
            "temp_c": None,
            "feels_c": None,
            "hi_c": None,
            "lo_c": None,
            "sunrise": None,
            "sunset": None,
        }


# ----------------------------
# WHO'S IN SPACE (whoisinspace.com)
# ----------------------------
def get_people_in_space():
    """
    Scrape whoisinspace.com and return list of:
      [{"name": "...", "station": "ISS"}, ...]
    Keeps station label (ISS/Tiangong/Other) based on section headers.
    """
    url = "https://whoisinspace.com/"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 said-hi"})
        r.raise_for_status()
    except Exception:
        return {"ok": False, "people": []}

    soup = BeautifulSoup(r.text, "html.parser")

    # The site typically uses <h2> as repeating headings.
    h2s = soup.find_all("h2")
    if not h2s:
        return {"ok": False, "people": []}

    people = []
    current_station = None

    def station_from_header(txt: str):
        t = (txt or "").strip()
        tl = t.lower()
        # common patterns
        if "tiangong" in tl:
            return "Tiangong"
        if "iss" in tl or "international space station" in tl:
            return "ISS"
        return None

    def is_group_header(txt: str):
        # group headers usually mention ISS/Tiangong and often contain a dash
        st = station_from_header(txt)
        if not st:
            return False
        return ("-" in txt) or ("space station" in txt.lower()) or ("iss" in txt.lower()) or ("tiangong" in txt.lower())

    for h in h2s:
        txt = " ".join(h.get_text(" ", strip=True).split())
        if not txt:
            continue

        if is_group_header(txt):
            current_station = station_from_header(txt) or current_station
            continue

        # Otherwise treat it as a person name if it looks like a name (two words min, not too long)
        if len(txt) > 2 and len(txt) <= 60 and any(ch.isalpha() for ch in txt) and " " in txt:
            people.append({"name": txt, "station": current_station or "Unknown"})

    # Dedup while preserving order
    seen = set()
    deduped = []
    for p in people:
        key = (p["name"].lower(), p["station"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)

    return {"ok": True, "people": deduped}


# ----------------------------
# COLLECT CONTENT
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)
uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=3)
rugby_items = collect_articles(RUGBY_UNION_FEEDS, limit=7)
punk_items = collect_articles(PUNK_ROCK_FEEDS, limit=3)

wx = get_cardiff_weather()
space = get_people_in_space()

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#1b1b1b"
    ink = "#ffffff"
    muted = "#cfcfcf"
    rule_light = "#2b2b2b"
    link = "#78a6ff"

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

    def section_header(label: str, emoji: str):
        return f"""
        <tr>
          <td style="padding:18px 20px 12px 20px;">
            <span style="font-family:{font};
                         font-size:13px !important;
                         font-weight:900 !important;
                         letter-spacing:2px;
                         text-transform:uppercase;
                         color:{ink};
                         {size_fix_inline}">
              {esc(emoji)} {esc(label)}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:0 20px;">
            <div style="height:1px;background:{rule_light};"></div>
          </td>
        </tr>
        """

    def story_block(i, it, lead=False, show_kicker=True):
        headline_size = "22px" if lead else "18px"
        headline_weight = "900" if lead else "800"
        summary_size = "14px" if lead else "13.5px"
        summary_weight = "500" if lead else "400"

        left_bar = "border-left:4px solid %s;padding-left:12px;" % ink if lead else ""

        kicker_row = ""
        if lead and show_kicker:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:11px;font-weight:900;letter-spacing:2px;
                         text-transform:uppercase;color:{muted};padding:0 0 8px 0;{size_fix_inline}">
                TOP STORY
              </td>
            </tr>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
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

          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    def story_list(items, limit, lead_first=True):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="padding:16px 0;font-family:{font};color:{muted};font-size:13.5px;line-height:1.7;{size_fix_inline}">
                  No stories in the last 24 hours.
                </td>
              </tr>
              <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
            </table>
            """

        out = ""
        for idx, it in enumerate(items[:limit], start=1):
            out += story_block(idx, it, lead=(lead_first and idx == 1), show_kicker=(lead_first and idx == 1))
        return out

    # Inside today counts
    inside_counts = [
        f"• UK Politics ({len(uk_politics_items)} stories)",
        f"• Rugby Union ({len(rugby_items)} stories)",
        f"• Punk Rock ({len(punk_items)} stories)",
    ]

    # Weather lines
    if wx.get("ok"):
        wx_line = f"{wx['temp_c']:.1f}°C (feels {wx['feels_c']:.1f}°C) · H {wx['hi_c']:.1f}°C / L {wx['lo_c']:.1f}°C"
        sun_line = f"Sunrise: {wx['sunrise']}  ·  Sunset: {wx['sunset']}"
    else:
        wx_line = "Unable to load weather."
        sun_line = "Sunrise: --:--  ·  Sunset: --:--"

    # Space roster
    if space.get("ok") and space.get("people"):
        space_lines = "<br/>".join([f"{esc(p['name'])} ({esc(p['station'])})" for p in space["people"]])
    else:
        space_lines = "Unable to load space roster."

    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      {style_block}
    </head>

    <body style="margin:0;background:{outer_bg};{size_fix_inline}">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:{outer_bg};{size_fix_inline}">
        <tr>
          <td align="center" style="padding:18px;{size_fix_inline}">
            <table class="container" width="720" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse;background:{paper};border-radius:14px;overflow:hidden;{size_fix_inline}">

              <!-- Masthead -->
              <tr>
                <td align="center" style="padding:30px 20px 18px 20px;{size_fix_inline}">
                  <div style="font-family:{font};font-size:60px !important;font-weight:900 !important;line-height:1.05;color:{ink};{size_fix_inline}">
                    The 2k Times
                  </div>
                  <div style="margin-top:12px;font-family:{font};font-size:13px !important;font-weight:700 !important;letter-spacing:2px;text-transform:uppercase;color:{muted};{size_fix_inline}">
                    {date_line} · Daily Edition · {TEMPLATE_VERSION}
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px 0 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              {section_header("World Headlines", "🌍")}

              <!-- Two columns (World + Inside) -->
              <tr>
                <td style="padding:14px 20px 6px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="55%" valign="top" style="padding-right:14px;">
                        {story_list(world_items, 3, lead_first=True)}
                      </td>

                      <td class="divider" width="1" style="background:{rule_light};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="45%" valign="top" style="padding-left:14px;">

                        <!-- Inside Today -->
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                          <tr>
                            <td style="padding-top:16px;font-family:{font};font-size:13px !important;font-weight:900 !important;letter-spacing:2px;text-transform:uppercase;color:{ink};{size_fix_inline}">
                              🗞️ Inside Today
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};font-size:15px !important;font-weight:600 !important;line-height:1.8;color:{muted};{size_fix_inline}">
                              {"<br/>".join([esc(x) for x in inside_counts])}
                            </td>
                          </tr>

                          <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};font-size:12px !important;font-weight:500;line-height:1.7;color:{muted};{size_fix_inline}">
                              Curated from the last 24 hours.<br/>Reader links included.
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Weather -->
                          <tr>
                            <td style="padding-top:18px;font-family:{font};font-size:13px !important;font-weight:900 !important;letter-spacing:2px;text-transform:uppercase;color:{ink};{size_fix_inline}">
                              ⛅ Weather · Cardiff
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};font-size:15px !important;font-weight:600 !important;line-height:1.8;color:{muted};{size_fix_inline}">
                              {esc(wx_line)}
                            </td>
                          </tr>

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Sunrise/Sunset -->
                          <tr>
                            <td style="font-family:{font};font-size:13px !important;font-weight:900 !important;letter-spacing:2px;text-transform:uppercase;color:{ink};{size_fix_inline}">
                              🌅 Sunrise / Sunset
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};font-size:15px !important;font-weight:600 !important;line-height:1.8;color:{muted};{size_fix_inline}">
                              {esc(sun_line)}
                            </td>
                          </tr>

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Who's in Space -->
                          <tr>
                            <td style="font-family:{font};font-size:13px !important;font-weight:900 !important;letter-spacing:2px;text-transform:uppercase;color:{ink};{size_fix_inline}">
                              🚀 Who&apos;s in Space
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};font-size:14px !important;font-weight:500 !important;line-height:1.6;color:{muted};{size_fix_inline}">
                              {space_lines}
                            </td>
                          </tr>

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
                        </table>

                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- STACKED SECTIONS BELOW -->
              {section_header("UK Politics", "🏛️")}
              <tr><td style="padding:0 20px 8px 20px;">{story_list(uk_politics_items, 3, lead_first=False)}</td></tr>

              {section_header("Rugby Union", "🏉")}
              <tr><td style="padding:0 20px 8px 20px;">{story_list(rugby_items, 7, lead_first=False)}</td></tr>

              {section_header("Punk Rock", "🎸")}
              <tr><td style="padding:0 20px 10px 20px;">{story_list(punk_items, 3, lead_first=False)}</td></tr>

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
    return html


# ----------------------------
# Plain text fallback
# ----------------------------
plain_lines = [
    f"THE 2K TIMES — {now_uk.strftime('%d.%m.%Y')}",
    "",
    f"(Plain-text fallback) {TEMPLATE_VERSION}",
    "",
]

if wx.get("ok"):
    plain_lines += [
        "WEATHER (CARDIFF)",
        f"{wx['temp_c']:.1f}C (feels {wx['feels_c']:.1f}C) | H {wx['hi_c']:.1f}C / L {wx['lo_c']:.1f}C",
        f"Sunrise {wx['sunrise']} | Sunset {wx['sunset']}",
        "",
    ]
else:
    plain_lines += ["WEATHER (CARDIFF)", "Unable to load weather.", ""]

if space.get("ok") and space.get("people"):
    plain_lines += ["WHO'S IN SPACE"]
    for p in space["people"]:
        plain_lines.append(f"- {p['name']} ({p['station']})")
    plain_lines.append("")
else:
    plain_lines += ["WHO'S IN SPACE", "Unable to load space roster.", ""]


def add_plain_section(title: str, items: list, limit: int):
    plain_lines.append(title.upper())
    plain_lines.append("")
    if not items:
        plain_lines.append("No stories in the last 24 hours.")
        plain_lines.append("")
        return
    for i, it in enumerate(items[:limit], start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")


add_plain_section("World Headlines", world_items, 3)
add_plain_section("UK Politics", uk_politics_items, 3)
add_plain_section("Rugby Union", rugby_items, 7)
add_plain_section("Punk Rock", punk_items, 3)

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
print("UK Politics:", len(uk_politics_items))
print("Rugby Union:", len(rugby_items))
print("Punk Rock:", len(punk_items))
print("Weather ok:", wx.get("ok"))
print("Space ok:", space.get("ok"), "count:", len(space.get("people") or []))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
