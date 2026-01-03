import os
import re
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests


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
# SOURCES
# ----------------------------
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

UK_POLITICS_FEEDS = [
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://www.theguardian.com/uk/politics/rss",
    "https://feeds.reuters.com/reuters/UKDomesticNews",
]

# Rugby sources (as requested: include BBC Sport, RugbyPass, Planet Rugby)
RUGBY_FEEDS = [
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "https://www.rugbypass.com/feed/",
    "https://www.planet-rugby.com/feed",
    # Optional extras from your list:
    "https://www.world.rugby/rss/news",
    "https://www.rugbyworld.com/feed",
    "https://www.ruck.co.uk/feed/",
    "https://www.rugby365.com/feed/",
    "https://www.skysports.com/rss/12040",
]

PUNK_FEEDS = [
    "https://www.punknews.org/rss.php",
    "https://www.kerrang.com/feed",
    "https://www.loudersound.com/feeds/all",
    "https://www.nme.com/feed",
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


def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ----------------------------
# WEATHER (Open-Meteo)
# ----------------------------
CARDIFF_LAT = 51.4816
CARDIFF_LON = -3.1791

def get_cardiff_weather():
    """
    Returns dict with:
    temp_c, feels_c, hi_c, lo_c, sunrise, sunset
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": CARDIFF_LAT,
        "longitude": CARDIFF_LON,
        "current": "temperature_2m,apparent_temperature",
        "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
        "timezone": "Europe/London",
    }
    headers = {
        "User-Agent": "The2kTimes/1.0 (+https://the-2k-times.onrender.com)"
    }
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()

    cur = data.get("current") or {}
    daily = data.get("daily") or {}

    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")

    tmax = None
    tmin = None
    sunrise = None
    sunset = None
    try:
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        sunrise = (daily.get("sunrise") or [None])[0]
        sunset = (daily.get("sunset") or [None])[0]
    except Exception:
        pass

    def _fmt_time(iso_str):
        # iso_str example: "2026-01-03T08:17"
        if not iso_str:
            return "--:--"
        try:
            dt = datetime.fromisoformat(iso_str).replace(tzinfo=TZ)
            return dt.strftime("%H:%M")
        except Exception:
            try:
                dt = datetime.fromisoformat(iso_str)
                return dt.strftime("%H:%M")
            except Exception:
                return "--:--"

    return {
        "temp_c": temp,
        "feels_c": feels,
        "hi_c": tmax,
        "lo_c": tmin,
        "sunrise": _fmt_time(sunrise),
        "sunset": _fmt_time(sunset),
    }


# ----------------------------
# WHO'S IN SPACE (whoisinspace.com)
# ----------------------------
def get_space_roster():
    """
    whoisinspace.com is a Next.js site. We parse __NEXT_DATA__ and pull people + their craft/station.
    Returns list of tuples: (name, craft)
    """
    url = "https://whoisinspace.com/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "text/html",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    html = r.text

    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find __NEXT_DATA__ on whoisinspace.com")

    blob = m.group(1)
    data = json.loads(blob)

    # We try a few paths because Next apps vary.
    # Common: props.pageProps.people or props.pageProps.initialData.people, etc.
    def deep_get(d, path):
        cur = d
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return None
        return cur

    candidates = [
        ["props", "pageProps", "people"],
        ["props", "pageProps", "data", "people"],
        ["props", "pageProps", "initialData", "people"],
        ["props", "pageProps", "props", "people"],
    ]

    people = None
    for path in candidates:
        people = deep_get(data, path)
        if isinstance(people, list) and people:
            break

    if not isinstance(people, list):
        # Last resort: search dict for list items shaped like {name, craft}
        people = []
        def walk(x):
            if isinstance(x, dict):
                if "name" in x and ("craft" in x or "station" in x):
                    people.append(x)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data)

    roster = []
    for p in people:
        name = (p.get("name") or "").strip()
        craft = (p.get("craft") or p.get("station") or p.get("vehicle") or "").strip()
        if name:
            roster.append((name, craft if craft else "Unknown"))

    # De-dupe while preserving order
    seen = set()
    out = []
    for name, craft in roster:
        key = (name.lower(), craft.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((name, craft))
    return out


# ----------------------------
# COLLECT ITEMS
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)
uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=5)
rugby_items = collect_articles(RUGBY_FEEDS, limit=7)
punk_items = collect_articles(PUNK_FEEDS, limit=5)

# Sidebar data (safe fallbacks)
wx = None
sunrise = "--:--"
sunset = "--:--"
try:
    wx = get_cardiff_weather()
    sunrise = wx.get("sunrise") or "--:--"
    sunset = wx.get("sunset") or "--:--"
except Exception:
    wx = None

space_roster = []
try:
    space_roster = get_space_roster()
except Exception:
    space_roster = []


# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#f7f5ef"
    ink = "#111111"
    muted = "#4a4a4a"
    rule_light = "#2e2e2e"
    link = "#7aa7ff"

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

    def hr():
        return f'<tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>'

    def section_title(label, emoji):
        return f"""
        <tr>
          <td style="padding:18px 20px 10px 20px;">
            <span style="font-family:{font};
                         font-size:12px !important;
                         font-weight:900 !important;
                         letter-spacing:2px;
                         text-transform:uppercase;
                         color:#ffffff;
                         {size_fix_inline}">
              {emoji} {esc(label)}
            </span>
          </td>
        </tr>
        {hr()}
        """

    def story_block(i, it, lead=False):
        # Lead story headline is forced down to match other headlines (your earlier request)
        headline_size = "18px"
        headline_weight = "900" if lead else "700"
        summary_size = "15px" if lead else "13.5px"
        summary_weight = "500" if lead else "400"
        pad_top = "18px" if lead else "16px"

        left_bar = "border-left:4px solid #ffffff;padding-left:12px;" if lead else ""

        kicker_row = ""
        if lead:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:11px;font-weight:900;letter-spacing:2px;
                         text-transform:uppercase;color:#cfcfcf;padding:0 0 8px 0;{size_fix_inline}">
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
                             line-height:1.15;
                             color:#ffffff;
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
                             color:#d7d7d7;
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
          {hr()}
        </table>
        """

    def section_stories(items, max_items):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="padding:14px 0 10px 0;font-family:{font};color:#cfcfcf;font-size:14px;line-height:1.7;{size_fix_inline}">
                  No stories in the last 24 hours.
                </td>
              </tr>
              {hr()}
            </table>
            """
        out = ""
        for idx, it in enumerate(items[:max_items], start=1):
            out += story_block(idx, it, lead=(idx == 1))
        return out

    world_html = section_stories(world_items, 3)

    # Inside Today counts + sidebar blocks
    uk_count = len(uk_politics_items)
    rugby_count = len(rugby_items)
    punk_count = len(punk_items)

    # Weather text
    wx_line = "Weather data unavailable."
    sun_line = f"Sunrise: {sunrise} · Sunset: {sunset}"
    if wx:
        def _fmt(x):
            try:
                return f"{float(x):.1f}°C"
            except Exception:
                return "--.-°C"
        wx_line = (
            f"{_fmt(wx.get('temp_c'))} (feels {_fmt(wx.get('feels_c'))}) · "
            f"H {_fmt(wx.get('hi_c'))} / L {_fmt(wx.get('lo_c'))}"
        )

    # Space roster lines (ALL people)
    if space_roster:
        space_lines = ""
        for name, craft in space_roster:
            craft_label = craft.strip() if craft else "Unknown"
            space_lines += f"{esc(name)} <span style='color:#cfcfcf'>({esc(craft_label)})</span><br/>"
    else:
        space_lines = "<span style='color:#cfcfcf'>Unable to load space roster.</span>"

    # Bottom sections (stacked, same formatting as World Headlines)
    uk_html = section_stories(uk_politics_items, 5)
    rugby_html = section_st
