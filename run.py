import os
import re
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.parse import quote

import feedparser

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-17"
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
# FEEDS (defaults)
# Override via env CSV:
#   UK_POLITICS_FEEDS="url1,url2"
#   RUGBY_UNION_FEEDS="url1,url2"
#   PUNK_ROCK_FEEDS="url1,url2"
# ----------------------------
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

UK_POLITICS_FEEDS = [
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://www.theguardian.com/politics/rss",
    "https://feeds.reuters.com/reuters/UKdomesticNews",
]

RUGBY_UNION_FEEDS = [
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "https://www.rugbypass.com/feed/",
    "https://www.planetrugby.com/feed",
]

PUNK_ROCK_FEEDS = [
    "https://www.punknews.org/backend.xml",
    "https://www.loudersound.com/feeds/tag/punk",
]


def env_csv(name: str):
    v = (os.environ.get(name) or "").strip()
    if not v:
        return None
    return [x.strip() for x in v.split(",") if x.strip()]


UK_POLITICS_FEEDS = env_csv("UK_POLITICS_FEEDS") or UK_POLITICS_FEEDS
RUGBY_UNION_FEEDS = env_csv("RUGBY_UNION_FEEDS") or RUGBY_UNION_FEEDS
PUNK_ROCK_FEEDS = env_csv("PUNK_ROCK_FEEDS") or PUNK_ROCK_FEEDS

# ----------------------------
# HELPERS
# ----------------------------
def reader_link(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return f"{READER_BASE_URL}/read?url={quote(url, safe='')}"


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
                    "source": feed_url,
                }
            )

    articles.sort(key=lambda x: x["published"], reverse=True)

    seen = set()
    unique = []
    for a in articles:
        k = a["title"].lower().strip()
        if k in seen:
            continue
        seen.add(k)
        unique.append(a)

    return unique[:limit]


def collect_count(feed_urls):
    count = 0
    seen = set()
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
            k = title.lower().strip()
            if k in seen:
                continue
            seen.add(k)
            count += 1
    return count


def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def fetch_url(url: str, timeout: int = 14) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "The-2k-Times/1.0 (+newsletter bot)",
            "Accept": "text/html,application/xhtml+xml,application/json",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ----------------------------
# WEATHER + SUNRISE/SUNSET (Cardiff) via Open-Meteo (no key)
# ----------------------------
def fetch_cardiff_weather():
    lat, lon = 51.4816, -3.1791
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        "&timezone=Europe%2FLondon"
    )
    try:
        raw = fetch_url(url)
        data = json.loads(raw)

        cur = data.get("current", {}) or {}
        daily = data.get("daily", {}) or {}

        current_c = cur.get("temperature_2m")
        feels_c = cur.get("apparent_temperature")

        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]

        sunrise_iso = (daily.get("sunrise") or [""])[0]
        sunset_iso = (daily.get("sunset") or [""])[0]

        def hhmm(iso):
            if not iso or "T" not in iso:
                return "--:--"
            return iso.split("T", 1)[1][:5]

        return {
            "ok": True,
            "current_c": current_c,
            "feels_c": feels_c,
            "hi_c": tmax,
            "lo_c": tmin,
            "sunrise": hhmm(sunrise_iso),
            "sunset": hhmm(sunset_iso),
        }
    except Exception:
        return {"ok": False}


def fmt_temp(x):
    if x is None:
        return "--"
    try:
        if float(x).is_integer():
            return str(int(float(x)))
        return f"{float(x):.1f}"
    except Exception:
        return "--"


# ----------------------------
# WHO IS IN SPACE (whoisinspace.com)
# ----------------------------
class _H2Extractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_h2 = False
        self.h2 = []
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "h2":
            self.in_h2 = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag.lower() == "h2" and self.in_h2:
            text = "".join(self._buf)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                self.h2.append(text)
            self.in_h2 = False
            self._buf = []

    def handle_data(self, data):
        if self.in_h2 and data:
            self._buf.append(data)


def fetch_who_in_space():
    try:
        html = fetch_url("https://whoisinspace.com/")
        parser = _H2Extractor()
        parser.feed(html)

        people = []
        current_station = None

        def normalize_station(group_header: str) -> str:
            left = group_header.split(" - ")[0].strip()
            low = left.lower()
            if "iss" in low:
                return "ISS"
            if "tiangong" in low:
                return "Tiangong"
            return left

        for t in parser.h2:
            clean = re.sub(r"\s+", " ", t).strip()
            if not clean:
                continue

            if " - " in clean and any(k in clean.lower() for k in ["iss", "tiangong", "space", "soyuz", "crew", "shenzhou"]):
                current_station = normalize_station(clean)
                continue

            if current_station:
                if len(clean) < 3:
                    continue
                if clean.lower().startswith("launched"):
                    continue
                people.append({"name": clean, "station": current_station})

        seen = set()
        uniq = []
        for p in people:
            k = p["name"].lower().strip()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(p)

        return uniq, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ----------------------------
# COLLECT CONTENT
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)

uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=3)
rugby_union_items = collect_articles(RUGBY_UNION_FEEDS, limit=5)
punk_rock_items = collect_articles(PUNK_ROCK_FEEDS, limit=3)

uk_politics_count = collect_count(UK_POLITICS_FEEDS)
rugby_union_count = collect_count(RUGBY_UNION_FEEDS)
punk_rock_count = collect_count(PUNK_ROCK_FEEDS)

wx = fetch_cardiff_weather()
people_in_space, who_err = fetch_who_in_space()

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#1b1b1b"
    ink = "#ffffff"
    muted = "#cfcfcf"
    rule = "#2b2b2b"
    link = "#6ea1ff"

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
        .colpadX{padding-left:0!important;padding-right:0!important}
      }
    </style>
    """

    def section_heading(text, emoji):
        return f"""
          <span style="font-family:{font};
                       font-size:14px !important;
                       font-weight:900 !important;
                       letter-spacing:2px;
                       text-transform:uppercase;
                       color:{ink};
                       {size_fix_inline}">
            {emoji} {text}
          </span>
        """

    # ✅ Single renderer used everywhere (World + stacked sections)
    def story_block(i, it, lead=False):
        headline_size = "18px"
        headline_weight = "800"
        summary_size = "13.5px"
        summary_weight = "400"
        pad_top = "14px" if lead else "16px"

        # for world top story only
        left_bar = "border-left:4px solid #0f0f0f;padding-left:12px;" if lead else ""

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

    # helper to render a full section using the SAME story renderer
    def render_section(items, lead_first=False):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="padding:12px 0 12px 0;font-family:{font};font-size:13px;line-height:1.6;color:{muted};{size_fix_inline}">
                  No stories in the last 24 hours.
                </td>
              </tr>
              <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
            </table>
            """
        out = ""
        for idx, it in enumerate(items, start=1):
            out += story_block(idx, it, lead=(lead_first and idx == 1))
        return out

    # WORLD
    world_html = render_section(world_items, lead_first=True) if world_items else f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:18px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
              No qualifying world headlines in the last 24 hours.
            </td>
          </tr>
        </table>
    """

    # WEATHER / SUN
    if wx.get("ok"):
        weather_line = f"{fmt_temp(wx.get('current_c'))}°C (feels {fmt_temp(wx.get('feels_c'))}°C) · H {fmt_temp(wx.get('hi_c'))}°C / L {fmt_temp(wx.get('lo_c'))}°C"
        sunrise = wx.get("sunrise", "--:--")
        sunset = wx.get("sunset", "--:--")
    else:
        weather_line = "Weather unavailable."
        sunrise = "--:--"
        sunset = "--:--"

    # SPACE (ALL)
    if who_err:
        space_html = f"<span style='color:{muted};'>Unavailable ({esc(who_err)})</span>"
    elif not people_in_space:
        space_html = f"<span style='color:{muted};'>No data returned.</span>"
    else:
        rows = []
        for p in people_in_space:
            rows.append(f"{esc(p['name'])} <span style='color:{muted};'>({esc(p['station'])})</span>")
        space_html = "<br/>".join(rows)

    # ✅ Bottom sections now rendered with the SAME story format
    uk_html = render_section(uk_politics_items, lead_first=False)
    rugby_html = render_section(rugby_union_items, lead_first=False)
    punk_html = render_section(punk_rock_items, lead_first=False)

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
                   style="border-collapse:collapse;background:{paper};border-radius:16px;overflow:hidden;{size_fix_inline}">

              <!-- Masthead -->
              <tr>
                <td align="center" style="padding:28px 20px 18px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:56px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.0;
                                                {size_fix_inline}">
                        <span style="font-size:56px !important;font-weight:900 !important;">
                          The 2k Times
                        </span>
                      </td>
                    </tr>
                    <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>
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

              <!-- Thin rule -->
              <tr>
                <td style="padding:0 20px 14px 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- World section title -->
              <tr>
                <td style="padding:16px 20px 10px 20px;">
                  {section_heading("World Headlines", "🌍")}
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- World + Sidebar columns -->
              <tr>
                <td style="padding:12px 20px 22px 20px;">
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
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

                          <!-- INSIDE TODAY -->
                          <tr>
                            <td style="padding-top:14px;">
                              {section_heading("Inside Today", "📰")}
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
                              • UK Politics ({uk_politics_count} stories)<br/>
                              • Rugby Union ({rugby_union_count} stories)<br/>
                              • Punk Rock ({punk_rock_count} stories)
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

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Weather -->
                          <tr><td>{section_heading("Weather · Cardiff", "🌦️")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              {esc(weather_line)}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Sunrise/Sunset -->
                          <tr><td>{section_heading("Sunrise / Sunset", "🌅")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              Sunrise: <span style="color:{ink};">{esc(sunrise)}</span>
                              <span style="color:{muted};"> · </span>
                              Sunset: <span style="color:{ink};">{esc(sunset)}</span>
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Who's in space -->
                          <tr><td>{section_heading("Who's in Space", "🚀")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:600 !important;
                                       line-height:1.55;
                                       color:{ink};
                                       {size_fix_inline}">
                              {space_html}
                            </td>
                          </tr>

                        </table>
                      </td>

                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Divider -->
              <tr>
                <td style="padding:0 20px 18px 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- UK Politics -->
              <tr>
                <td style="padding:0 20px 10px 20px;">
                  {section_heading("UK Politics", "🏛️")}
                </td>
              </tr>
              <tr><td style="padding:0 20px;"><div style="height:1px;background:{rule};"></div></td></tr>
              <tr><td style="padding:0 20px 22px 20px;">{uk_html}</td></tr>

              <!-- Rugby -->
              <tr>
                <td style="padding:0 20px 10px 20px;">
                  {section_heading("Rugby Union", "🏉")}
                </td>
              </tr>
              <tr><td style="padding:0 20px;"><div style="height:1px;background:{rule};"></div></td></tr>
              <tr><td style="padding:0 20px 22px 20px;">{rugby_html}</td></tr>

              <!-- Punk -->
              <tr>
                <td style="padding:0 20px 10px 20px;">
                  {section_heading("Punk Rock", "🎸")}
                </td>
              </tr>
              <tr><td style="padding:0 20px;"><div style="height:1px;background:{rule};"></div></td></tr>
              <tr><td style="padding:0 20px 22px 20px;">{punk_html}</td></tr>

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

plain_lines += [
    "",
    "INSIDE TODAY",
    f"- UK Politics ({uk_politics_count})",
    f"- Rugby Union ({rugby_union_count})",
    f"- Punk Rock ({punk_rock_count})",
]

def add_plain_section(label, items):
    plain_lines.append("")
    plain_lines.append(label.upper())
    if items:
        for it in items:
            plain_lines.append(it["title"])
            plain_lines.append(it["summary"])
            plain_lines.append(f"Read in Reader: {it['reader']}")
            plain_lines.append("")
    else:
        plain_lines.append("No stories in the last 24 hours.")

add_plain_section("UK Politics", uk_politics_items)
add_plain_section("Rugby Union", rugby_union_items)
add_plain_section("Punk Rock", punk_rock_items)

plain_lines += ["", "WEATHER (CARDIFF)"]
if wx.get("ok"):
    plain_lines.append(
        f"{fmt_temp(wx.get('current_c'))}°C (feels {fmt_temp(wx.get('feels_c'))}°C) · "
        f"H {fmt_temp(wx.get('hi_c'))}°C / L {fmt_temp(wx.get('lo_c'))}°C"
    )
    plain_lines.append(f"Sunrise: {wx.get('sunrise','--:--')} · Sunset: {wx.get('sunset','--:--')}")
else:
    plain_lines.append("Weather unavailable.")

plain_lines += ["", "WHO'S IN SPACE"]
if who_err:
    plain_lines.append(f"Unavailable ({who_err})")
elif not people_in_space:
    plain_lines.append("No data returned.")
else:
    for p in people_in_space:
        plain_lines.append(f"- {p['name']} ({p['station']})")

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
print("Inside today counts:", uk_politics_count, rugby_union_count, punk_rock_count)
print("UK items:", len(uk_politics_items), "Rugby items:", len(rugby_union_items), "Punk items:", len(punk_rock_items))
print("Weather OK:", wx.get("ok", False))
print("Who's in space:", len(people_in_space), ("ERR: " + who_err if who_err else ""))
print("Feeds UK:", UK_POLITICS_FEEDS)
print("Feeds Rugby:", RUGBY_UNION_FEEDS)
print("Feeds Punk:", PUNK_ROCK_FEEDS)
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
