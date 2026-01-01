import os
import re
import smtplib
import ssl
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
TEMPLATE_VERSION = "v-newspaper-16"
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
    "https://www.theguardian.com/politics/rss",
    "https://www.reuters.com/rssFeed/politicsNews",  # may fail sometimes; kept as an optional source
]

RUGBY_FEEDS = [
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",   # BBC Sport Rugby Union
    "https://www.rugbypass.com/feed/",                      # RugbyPass
    "https://www.planetrugby.com/feed/",                    # Planet Rugby
    "https://www.rugbyworld.com/feed",                      # Rugby World (often works)
]

PUNK_ROCK_FEEDS = [
    "https://www.punknews.org/rss",
    "https://www.kerrang.com/feed",  # may not always be available; optional
]

# ----------------------------
# HELPERS
# ----------------------------
def reader_link(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return f"{READER_BASE_URL}/read?url={urllib.parse.quote(url, safe='')}"


def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


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


def fetch_url(url: str, timeout: int = 10, headers: dict | None = None) -> bytes:
    headers = headers or {}
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (The 2k Times bot)",
            "Accept": "*/*",
            **headers,
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url: str, timeout: int = 10) -> dict:
    raw = fetch_url(url, timeout=timeout, headers={"Accept": "application/json"})
    return json.loads(raw.decode("utf-8", errors="replace"))


# ----------------------------
# DATA COLLECTION
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)
uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=5)
rugby_items = collect_articles(RUGBY_FEEDS, limit=7)
punk_items = collect_articles(PUNK_ROCK_FEEDS, limit=5)

# ----------------------------
# CARDIFF WEATHER + SUN (Open-Meteo)
# ----------------------------
def get_cardiff_weather_and_sun():
    """
    Uses Open-Meteo without an API key.
    Returns dict with:
      temp_c, feels_c, hi_c, lo_c, sunrise, sunset
    """
    # Cardiff approx
    lat, lon = 51.4816, -3.1791

    # We request current temp + apparent, and today's hi/lo + sunrise/sunset.
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        "&timezone=Europe%2FLondon"
    )

    try:
        data = fetch_json(url, timeout=12)

        cur = (data.get("current") or {})
        daily = (data.get("daily") or {})

        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")

        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]

        sunrise_raw = (daily.get("sunrise") or [None])[0]
        sunset_raw = (daily.get("sunset") or [None])[0]

        def hhmm(dt_str: str | None):
            if not dt_str:
                return "--:--"
            # dt_str like "2026-01-01T08:18"
            m = re.search(r"T(\d{2}:\d{2})", dt_str)
            return m.group(1) if m else "--:--"

        return {
            "ok": True,
            "temp_c": temp,
            "feels_c": feels,
            "hi_c": hi,
            "lo_c": lo,
            "sunrise": hhmm(sunrise_raw),
            "sunset": hhmm(sunset_raw),
        }
    except Exception:
        return {"ok": False}


wx = get_cardiff_weather_and_sun()

# ----------------------------
# WHO'S IN SPACE (whoisinspace.com)
# ----------------------------
def get_people_in_space():
    """
    Prefer whoisinspace.com (HTML parse).
    Returns list of dicts: {name, craft}
    """
    url = "https://whoisinspace.com/"
    people = []

    try:
        html = fetch_url(url, timeout=12).decode("utf-8", errors="replace")

        # The site commonly contains headings for craft/station and lists of people.
        # We'll parse in a robust way:
        # - find craft blocks (ISS, Tiangong, etc.)
        # - within each block, find person names (often in <h3> or links)
        #
        # Strategy:
        # Split by craft headings (look for ISS / Tiangong / etc in text).
        # Then extract likely names from that section.
        text = re.sub(r"\s+", " ", html)

        # Find craft markers (very forgiving)
        craft_markers = []
        for craft in ["ISS", "Tiangong", "Shenzhou", "Space Station", "Crew", "Axiom"]:
            for m in re.finditer(rf">{craft}[^<]*<", html, flags=re.IGNORECASE):
                craft_markers.append((m.start(), craft))
        craft_markers.sort(key=lambda x: x[0])

        # If we can't find craft markers, fallback to a simple name list
        if not craft_markers:
            # Find visible-name patterns (this is a last resort)
            candidates = re.findall(r'aria-label="([^"]+)"', html)
            for c in candidates:
                c = c.strip()
                if 3 <= len(c) <= 60 and "who is in space" not in c.lower():
                    people.append({"name": c, "craft": "In space"})
            # de-dupe
            seen = set()
            out = []
            for p in people:
                k = (p["name"].lower(), p["craft"].lower())
                if k in seen:
                    continue
                seen.add(k)
                out.append(p)
            return out

        # Build segments between markers
        for idx, (pos, craft_guess) in enumerate(craft_markers):
            end = craft_markers[idx + 1][0] if idx + 1 < len(craft_markers) else len(html)
            segment = html[pos:end]

            # Normalize craft label from nearest heading text around the marker
            craft_label = craft_guess
            # Try to grab a bit of heading text
            head = re.search(r">(ISS[^<]{0,60}|Tiangong[^<]{0,60}|Shenzhou[^<]{0,60})<", segment, flags=re.IGNORECASE)
            if head:
                craft_label = strip_html(head.group(1)).strip()

            # Find likely person names (often in <h3> / <h2> / strong tags / links)
            # We'll accept Title Case name-like strings.
            name_candidates = re.findall(r">(?!ISS|Tiangong|Shenzhou)([A-Z][a-z]+(?:\s+[A-Z][a-z'\-]+){1,3})<", segment)

            for nm in name_candidates:
                nm = nm.strip()
                # Filter obvious non-names
                if any(bad in nm.lower() for bad in ["daily", "edition", "read in", "space", "station", "crew"]):
                    continue
                people.append({"name": nm, "craft": craft_label})

        # Clean + de-dupe; also keep order
        seen = set()
        out = []
        for p in people:
            name = p["name"].strip()
            craft = p["craft"].strip()

            if not name:
                continue

            # normalize craft display
            if craft.lower().startswith("iss"):
                craft_disp = "ISS"
            elif "tiangong" in craft.lower():
                craft_disp = "Tiangong"
            else:
                craft_disp = craft

            key = (name.lower(), craft_disp.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "craft": craft_disp})

        return out

    except Exception:
        return []


space_people = get_people_in_space()

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#1b1b1b"  # dark paper
    ink = "#f2f2f2"
    muted = "#c7c7c7"
    rule_light = "#2e2e2e"
    link = "#7aa7ff"

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

    # --- block renderer shared across sections (matches World Headlines style) ---
    def story_block(i, it, lead=False, show_kicker=False):
        headline_size = "18px"  # keep top story same size as others (per your request earlier)
        headline_weight = "800" if lead else "700"
        summary_size = "13.5px"
        summary_weight = "400"
        pad_top = "18px" if lead else "16px"

        left_bar = f"border-left:4px solid {ink};padding-left:12px;" if lead else ""

        kicker_row = ""
        if show_kicker and lead:
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
          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    def section_header(label: str, emoji: str):
        return f"""
        <tr>
          <td style="padding:18px 20px 10px 20px;">
            <span style="font-family:{font};
                         font-size:12px !important;
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

    # Build World HTML
    if world_items:
        world_html = ""
        for i, it in enumerate(world_items, start=1):
            world_html += story_block(i, it, lead=(i == 1), show_kicker=True)
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

    # Inside Today counts
    inside_counts = [
        f"UK Politics ({len(uk_politics_items)} stories)",
        f"Rugby Union ({len(rugby_items)} stories)",
        f"Punk Rock ({len(punk_items)} stories)",
    ]

    # Weather line
    if wx.get("ok"):
        def fmt_c(v):
            if v is None:
                return "--"
            try:
                return f"{float(v):.1f}".rstrip("0").rstrip(".")
            except Exception:
                return "--"

        wx_line = f"{fmt_c(wx.get('temp_c'))}°C (feels {fmt_c(wx.get('feels_c'))}°C) · H {fmt_c(wx.get('hi_c'))}°C / L {fmt_c(wx.get('lo_c'))}°C"
        sunrise_line = f"Sunrise: <span style='font-weight:800;color:{ink};'>{esc(wx.get('sunrise'))}</span> &nbsp;·&nbsp; Sunset: <span style='font-weight:800;color:{ink};'>{esc(wx.get('sunset'))}</span>"
    else:
        wx_line = "Weather unavailable."
        sunrise_line = "Sunrise: --:-- · Sunset: --:--"

    # Who's in space list (ALL)
    if space_people:
        space_lines = "<br/>".join([f"{esc(p['name'])} ({esc(p['craft'])})" for p in space_people])
    else:
        space_lines = "Space list unavailable."

    # Bottom sections stacked, formatted like World
    def build_section_items(items, label, emoji):
        if items:
            blocks = ""
            for idx, it in enumerate(items, start=1):
                blocks += story_block(idx, it, lead=(idx == 1), show_kicker=False)
            return f"""
            {section_header(label, emoji)}
            <tr>
              <td style="padding:0 20px 6px 20px;">
                {blocks}
              </td>
            </tr>
            """
        return f"""
        {section_header(label, emoji)}
        <tr>
          <td style="padding:14px 20px 18px 20px;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
            No stories in the last 24 hours.
          </td>
        </tr>
        """

    uk_section_html = build_section_items(uk_politics_items, "UK Politics", "🏛️")
    rugby_section_html = build_section_items(rugby_items, "Rugby Union", "🏉")
    punk_section_html = build_section_items(punk_items, "Punk Rock", "🎸")

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
                                                font-size:54px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.02;
                                                {size_fix_inline}">
                        <span style="font-size:54px !important;font-weight:900 !important;">
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

              <!-- Single thin rule (same weight everywhere) -->
              <tr>
                <td style="padding:0 20px 6px 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <!-- WORLD + INSIDE (two columns) -->
              {section_header("World Headlines", "🌍")}

              <tr>
                <td style="padding:10px 20px 18px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="56%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule_light};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="44%" valign="top" style="padding-left:12px;">

                        <!-- Align INSIDE TODAY with TOP STORY by adding same top spacer -->
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:12px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🗞️ Inside today
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
                              • {esc(inside_counts[0])}<br/>
                              • {esc(inside_counts[1])}<br/>
                              • {esc(inside_counts[2])}
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
                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Weather -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:12px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              ⛅ Weather · Cardiff
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       color:{ink};
                                       line-height:1.5;
                                       {size_fix_inline}">
                              {esc(wx_line)}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Sunrise/Sunset -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:12px !important;
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
                                       color:{muted};
                                       line-height:1.6;
                                       {size_fix_inline}">
                              {sunrise_line}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Who's in space -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:12px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🚀 Who&#39;s in space
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:500 !important;
                                       color:{muted};
                                       line-height:1.55;
                                       {size_fix_inline}">
                              {space_lines}
                            </td>
                          </tr>

                          <tr><td style="height:6px;font-size:0;line-height:0;">&nbsp;</td></tr>
                        </table>

                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Bottom stacked sections -->
              {uk_section_html}
              {rugby_section_html}
              {punk_section_html}

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

plain_lines += ["", "INSIDE TODAY", ""]
plain_lines += [f"- UK Politics: {len(uk_politics_items)}", f"- Rugby Union: {len(rugby_items)}", f"- Punk Rock: {len(punk_items)}", ""]

# Weather/sun for text
plain_lines += ["WEATHER — CARDIFF"]
if wx.get("ok"):
    plain_lines.append(wx_line)
    plain_lines.append(f"Sunrise: {wx.get('sunrise')}  Sunset: {wx.get('sunset')}")
else:
    plain_lines.append("Weather unavailable.")
plain_lines.append("")

# Space list for text
plain_lines += ["WHO'S IN SPACE"]
if space_people:
    for p in space_people:
        plain_lines.append(f"- {p['name']} ({p['craft']})")
else:
    plain_lines.append("Space list unavailable.")
plain_lines.append("")

# Bottom sections
def add_plain_section(title, items):
    plain_lines.append(title.upper())
    if not items:
        plain_lines.append("No stories in the last 24 hours.")
        plain_lines.append("")
        return
    for i, it in enumerate(items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

add_plain_section("UK Politics", uk_politics_items)
add_plain_section("Rugby Union", rugby_items)
add_plain_section("Punk Rock", punk_items)

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
print("UK politics:", len(uk_politics_items))
print("Rugby:", len(rugby_items))
print("Punk:", len(punk_items))
print("Weather OK:", bool(wx.get("ok")))
print("People in space:", len(space_people))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

context = ssl.create_default_context()
with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls(context=context)
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
