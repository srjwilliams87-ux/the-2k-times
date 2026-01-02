import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import feedparser
import requests

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-16"
DEBUG_SUBJECT = True  # set False when you're happy

# ----------------------------
# ENV (Mailgun / Sending)
# ----------------------------
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")
SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

SMTP_HOST = os.environ.get("MAILGUN_SMTP_HOST", "smtp.mailgun.org")
SMTP_PORT = int(os.environ.get("MAILGUN_SMTP_PORT", "587"))

# Reader service base
READER_BASE_URL = (os.environ.get("READER_BASE_URL", "https://the-2k-times.onrender.com") or "").rstrip("/")

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
# FEEDS
# ----------------------------
# Allow overriding via env vars so you can tweak without code changes.
def env_csv(name: str, fallback: list[str]) -> list[str]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    return [x.strip() for x in raw.split(",") if x.strip()]

WORLD_FEEDS = env_csv(
    "WORLD_FEEDS",
    [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.reuters.com/Reuters/worldNews",
    ],
)

UK_POLITICS_FEEDS = env_csv(
    "UK_POLITICS_FEEDS",
    [
        "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "https://www.theguardian.com/politics/rss",
        "https://feeds.reuters.com/reuters/UKdomesticNews",
    ],
)

RUGBY_FEEDS = env_csv(
    "RUGBY_FEEDS",
    [
        # Requested + reliable-ish defaults
        "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
        "https://www.rugbypass.com/feed/",
        "https://www.planetrugby.com/feed",
        # World Rugby RSS varies; include, harmless if it returns empty
        "https://www.world.rugby/rss",
    ],
)

PUNK_FEEDS = env_csv(
    "PUNK_FEEDS",
    [
        # You can swap these later if you have “original sources” you prefer
        "https://www.punknews.org/rss",
        "https://www.kerrang.com/feed",
        "https://loudwire.com/feed/",
    ],
)

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

    # newest first
    articles.sort(key=lambda x: x["published"], reverse=True)

    # de-dupe by title
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
# WEATHER + SUNRISE/SUNSET (Cardiff)
# Uses Open-Meteo (no key)
# ----------------------------
def get_cardiff_weather():
    # Cardiff approx
    lat = 51.4816
    lon = -3.1791

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&timezone=Europe%2FLondon"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
    )

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        cur = data.get("current", {}) or {}
        daily = (data.get("daily", {}) or {})

        t = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")

        tmax_list = daily.get("temperature_2m_max") or []
        tmin_list = daily.get("temperature_2m_min") or []
        sunrise_list = daily.get("sunrise") or []
        sunset_list = daily.get("sunset") or []

        tmax = tmax_list[0] if tmax_list else None
        tmin = tmin_list[0] if tmin_list else None

        sunrise_iso = sunrise_list[0] if sunrise_list else None
        sunset_iso = sunset_list[0] if sunset_list else None

        def hhmm(iso_str):
            # open-meteo gives "YYYY-MM-DDTHH:MM"
            if not iso_str:
                return None
            m = re.search(r"T(\d{2}:\d{2})", iso_str)
            return m.group(1) if m else None

        sunrise = hhmm(sunrise_iso)
        sunset = hhmm(sunset_iso)

        return {
            "temp": t,
            "feels": feels,
            "hi": tmax,
            "lo": tmin,
            "sunrise": sunrise,
            "sunset": sunset,
        }
    except Exception:
        return {
            "temp": None,
            "feels": None,
            "hi": None,
            "lo": None,
            "sunrise": None,
            "sunset": None,
        }

# ----------------------------
# WHO'S IN SPACE (from whoisinspace.com + fallback)
# ----------------------------
def get_people_in_space():
    """
    Returns list of dicts: [{"name": "...", "craft": "ISS"}, ...]
    Primary: whoisinspace.com
    Fallback: open-notify (craft only; less accurate sometimes)
    """
    people = []

    # Try a few likely JSON endpoints first (if they exist, great; if not, harmless)
    candidate_json = [
        "https://whoisinspace.com/api/people",
        "https://whoisinspace.com/people.json",
        "https://whoisinspace.com/data.json",
    ]

    for jurl in candidate_json:
        try:
            r = requests.get(jurl, timeout=10, headers={"User-Agent": "The2kTimes/1.0"})
            if r.status_code != 200:
                continue
            data = r.json()
            # Attempt to normalize common shapes
            # Shape A: {"people":[{"name":"X","craft":"ISS"}, ...]}
            if isinstance(data, dict) and isinstance(data.get("people"), list):
                for p in data["people"]:
                    name = (p.get("name") or "").strip()
                    craft = (p.get("craft") or p.get("station") or "").strip()
                    if name and craft:
                        people.append({"name": name, "craft": craft})
                if people:
                    return people
            # Shape B: [{"name":"X","craft":"ISS"}, ...]
            if isinstance(data, list):
                for p in data:
                    if isinstance(p, dict):
                        name = (p.get("name") or "").strip()
                        craft = (p.get("craft") or p.get("station") or "").strip()
                        if name and craft:
                            people.append({"name": name, "craft": craft})
                if people:
                    return people
        except Exception:
            pass

    # HTML parse fallback
    try:
        html = requests.get(
            "https://whoisinspace.com/",
            timeout=10,
            headers={"User-Agent": "The2kTimes/1.0"},
        ).text

        # Pull lines with likely "ISS" / "Tiangong" / etc from visible text
        # We’ll keep it intentionally forgiving.
        text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text)

        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        # Heuristic: look for "ISS" or "Tiangong" near a name
        craft_words = ["ISS", "Tiangong", "Shenzhou", "Crew Dragon", "Soyuz"]

        for ln in lines:
            if not any(c.lower() in ln.lower() for c in craft_words):
                continue

            # Examples we try to catch:
            # "Jane Doe — ISS"
            # "Jane Doe (ISS)"
            # "ISS: Jane Doe, John Smith"
            m = re.search(r"^(.*?)(?:\(|—|–|-)\s*(ISS|Tiangong|Shenzhou|Soyuz|Crew Dragon)\s*\)?$", ln, re.I)
            if m:
                name = m.group(1).strip(" :–—-")
                craft = m.group(2).strip()
                if len(name) >= 3:
                    people.append({"name": name, "craft": craft})
                continue

            m2 = re.search(r"^(ISS|Tiangong)\s*:\s*(.+)$", ln, re.I)
            if m2:
                craft = m2.group(1).strip()
                names = [x.strip() for x in re.split(r",|;|•", m2.group(2)) if x.strip()]
                for name in names:
                    if len(name) >= 3:
                        people.append({"name": name, "craft": craft})
                continue

        # Deduplicate
        seen = set()
        uniq = []
        for p in people:
            k = (p["name"].lower(), p["craft"].lower())
            if k in seen:
                continue
            seen.add(k)
            uniq.append(p)
        if uniq:
            return uniq
    except Exception:
        pass

    # Last resort fallback (not whoisinspace.com, but better than nothing)
    try:
        r = requests.get("http://api.open-notify.org/astros.json", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and isinstance(data.get("people"), list):
                return [{"name": p.get("name", "").strip(), "craft": p.get("craft", "").strip()}
                        for p in data["people"]
                        if (p.get("name") and p.get("craft"))]
    except Exception:
        pass

    return []

# ----------------------------
# COLLECT CONTENT
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)

uk_pol_items = collect_articles(UK_POLITICS_FEEDS, limit=3)
rugby_items = collect_articles(RUGBY_FEEDS, limit=5)
punk_items = collect_articles(PUNK_FEEDS, limit=3)

wx = get_cardiff_weather()
people_in_space = get_people_in_space()

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#f7f5ef"
    ink = "#111111"
    muted = "#4a4a4a"
    rule = "#ddd8cc"  # unify to thinnest line
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

    def section_heading(label: str, emoji: str):
        # Bold, larger, all caps, and emoji
        return f"""
        <tr>
          <td style="padding:18px 20px 10px 20px;">
            <span style="font-family:{font};
                         font-size:14px !important;
                         font-weight:900 !important;
                         letter-spacing:2px;
                         text-transform:uppercase;
                         color:{ink};
                         {size_fix_inline}">
              {emoji} {esc(label)}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:0 20px;">
            <div style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</div>
          </td>
        </tr>
        """

    def story_block(i, it, lead=False):
        # Top story headline now same size as other headlines
        headline_size = "18px"
        headline_weight = "900" if lead else "700"
        summary_size = "13.5px"
        summary_weight = "500" if lead else "400"
        pad_top = "16px"
        left_bar = "border-left:4px solid %s;padding-left:12px;" % ink if lead else ""

        kicker_row = ""
        if lead:
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
          <tr><td style="height:{pad_top};font-size:0;line-height:0;">&nbsp;</td></tr>

          <tr>
            <td style="{left_bar}{size_fix_inline}">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                {kicker_row}

                <tr>
                  <td style="font-family:{font};
                             font-size:{headline_size} !important;
                             font-weight:{headline_weight} !important;
                             line-height:1.25;
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
          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    def build_story_list(items, limit, lead_first=True):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="padding:16px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
                  No stories in the last 24 hours.
                </td>
              </tr>
              <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
            </table>
            """
        html = ""
        for idx, it in enumerate(items[:limit], start=1):
            html += story_block(idx, it, lead=(lead_first and idx == 1))
        return html

    # Left column: World headlines
    world_html = build_story_list(world_items, limit=3, lead_first=True)

    # Right column: Inside Today + weather + sunrise/sunset + space
    uk_count = len(uk_pol_items)
    rugby_count = len(rugby_items)
    punk_count = len(punk_items)

    # Weather lines
    def fmt_temp(x):
        if x is None:
            return "--"
        # show 1dp if needed
        try:
            return f"{float(x):.1f}".rstrip("0").rstrip(".")
        except Exception:
            return str(x)

    wx_line = f"{fmt_temp(wx['temp'])}°C (feels {fmt_temp(wx['feels'])}°C) · H {fmt_temp(wx['hi'])}°C / L {fmt_temp(wx['lo'])}°C"
    sr = wx.get("sunrise") or "--:--"
    ss = wx.get("sunset") or "--:--"

    # Space list (ALL)
    if people_in_space:
        space_lines = "<br/>".join([f"{esc(p['name'])} ({esc(p['craft'])})" for p in people_in_space])
    else:
        space_lines = "No data available."

    inside_today_block = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

      <!-- align top of inside today with top story: remove extra top spacing -->
      <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:14px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          🗞️ Inside Today
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
          • UK Politics ({uk_count} stories)<br/>
          • Rugby Union ({rugby_count} stories)<br/>
          • Punk Rock ({punk_count} stories)
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
      <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:14px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          🌦️ Weather · Cardiff
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

      <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:14px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
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
          Sunrise: <span style="font-weight:900;color:{ink};">{esc(sr)}</span>
          &nbsp;&nbsp;·&nbsp;&nbsp;
          Sunset: <span style="font-weight:900;color:{ink};">{esc(ss)}</span>
        </td>
      </tr>

      <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:14px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          🚀 Who&#39;s in Space
        </td>
      </tr>
      <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr>
        <td style="font-family:{font};
                   font-size:14px !important;
                   font-weight:500 !important;
                   line-height:1.6;
                   color:{muted};
                   {size_fix_inline}">
          {space_lines}
        </td>
      </tr>

    </table>
    """

    # Bottom stacked sections (match World Headlines formatting)
    uk_pol_html = build_story_list(uk_pol_items, limit=3, lead_first=False)
    rugby_html = build_story_list(rugby_items, limit=5, lead_first=False)
    punk_html = build_story_list(punk_items, limit=3, lead_first=False)

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

              <!-- Remove thick/extra rules; use the unified thin rule only -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</div>
                </td>
              </tr>

              <!-- WORLD -->
              {section_heading("World Headlines", "🌍")}

              <!-- Content columns -->
              <tr>
                <td style="padding:0 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="50%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="50%" valign="top" style="padding-left:12px;">
                        {inside_today_block}
                      </td>

                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Bottom stacked sections -->
              {section_heading("UK Politics", "🏛️")}
              <tr><td style="padding:0 20px 10px 20px;">{uk_pol_html}</td></tr>

              {section_heading("Rugby Union", "🏉")}
              <tr><td style="padding:0 20px 10px 20px;">{rugby_html}</td></tr>

              {section_heading("Punk Rock", "🎸")}
              <tr><td style="padding:0 20px 10px 20px;">{punk_html}</td></tr>

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
    "WORLD HEADLINES",
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

plain_lines += ["", "UK POLITICS", ""]
if not uk_pol_items:
    plain_lines.append("No stories in the last 24 hours.")
else:
    for i, it in enumerate(uk_pol_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

plain_lines += ["", "RUGBY UNION", ""]
if not rugby_items:
    plain_lines.append("No stories in the last 24 hours.")
else:
    for i, it in enumerate(rugby_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

plain_lines += ["", "PUNK ROCK", ""]
if not punk_items:
    plain_lines.append("No stories in the last 24 hours.")
else:
    for i, it in enumerate(punk_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

# Weather & space in plain text too
plain_lines += ["", "WEATHER (Cardiff)", ""]
plain_lines.append(wx_line)
plain_lines += ["", "SUNRISE / SUNSET", ""]
plain_lines.append(f"Sunrise: {sr} · Sunset: {ss}")

plain_lines += ["", "WHO'S IN SPACE", ""]
if people_in_space:
    for p in people_in_space:
        plain_lines.append(f"- {p['name']} ({p['craft']})")
else:
    plain_lines.append("No data available.")

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
print("UK politics:", len(uk_pol_items))
print("Rugby:", len(rugby_items))
print("Punk:", len(punk_items))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
