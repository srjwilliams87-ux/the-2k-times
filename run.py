import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import unquote

import feedparser
import requests

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
# SOURCES
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

RUGBY_FEEDS = [
    "https://www.world.rugby/rss",
    "https://www.planetrugby.com/feed",
    "https://www.rugbypass.com/feed/",
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "https://www.rugbyworld.com/feed",
    "https://www.ruck.co.uk/feed/",
]

PUNK_FEEDS = [
    "https://www.nme.com/music/feed",
    "https://pitchfork.com/rss/news/",
    "https://www.kerrang.com/rss",
]

# ----------------------------
# HELPERS
# ----------------------------
UA = "The2kTimesBot/1.0 (+https://the-2k-times.onrender.com)"


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
    return any(w in t for w in ["live", "minute-by-minute", "as it happened", "watch live"])


def collect_articles(feed_urls, limit, keyword_filter=None):
    articles = []
    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue

        if not getattr(feed, "entries", None):
            continue

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
            summary = two_sentence_summary(summary_raw)

            if keyword_filter:
                hay = (title + " " + summary).lower()
                if not any(k.lower() in hay for k in keyword_filter):
                    continue

            articles.append(
                {
                    "title": title,
                    "summary": summary,
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
# WEATHER (Cardiff) + Sunrise/Sunset
# ----------------------------
def get_cardiff_weather():
    lat, lon = 51.4816, -3.1791
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        "&timezone=Europe%2FLondon"
    )
    out = {"temp": None, "feels": None, "hi": None, "lo": None, "sunrise": None, "sunset": None}

    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": UA})
        r.raise_for_status()
        data = r.json()

        cur = data.get("current", {}) or {}
        daily = data.get("daily", {}) or {}

        out["temp"] = cur.get("temperature_2m")
        out["feels"] = cur.get("apparent_temperature")

        out["hi"] = (daily.get("temperature_2m_max") or [None])[0]
        out["lo"] = (daily.get("temperature_2m_min") or [None])[0]

        sunrise = (daily.get("sunrise") or [None])[0]
        sunset = (daily.get("sunset") or [None])[0]

        def _hhmm(x):
            if not x:
                return None
            m = re.search(r"T(\d{2}:\d{2})", str(x))
            return m.group(1) if m else None

        out["sunrise"] = _hhmm(sunrise)
        out["sunset"] = _hhmm(sunset)

    except Exception:
        pass

    return out


# ----------------------------
# WHO'S IN SPACE (whoisinspace.com)
# ----------------------------
def get_people_in_space():
    people = []
    json_urls = [
        "https://whoisinspace.com/astronauts.json",
        "https://whoisinspace.com/people.json",
    ]
    for u in json_urls:
        try:
            r = requests.get(u, timeout=12, headers={"User-Agent": UA})
            if r.status_code != 200:
                continue
            data = r.json()

            cand = None
            if isinstance(data, dict):
                cand = data.get("people") or data.get("astronauts") or data.get("crew")
            elif isinstance(data, list):
                cand = data

            if not cand:
                continue

            for p in cand:
                if not isinstance(p, dict):
                    continue
                name = p.get("name") or p.get("person") or p.get("title")
                craft = p.get("craft") or p.get("station") or p.get("location") or p.get("spacecraft")
                if name:
                    people.append({"name": str(name).strip(), "craft": str(craft or "Space").strip()})
            if people:
                return people
        except Exception:
            continue

    return people


# ----------------------------
# COLLECT CONTENT
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)
uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=5)
rugby_items = collect_articles(
    RUGBY_FEEDS,
    limit=7,
    keyword_filter=[
        "rugby",
        "six nations",
        "premiership",
        "urc",
        "champions cup",
        "wales",
        "scarlets",
        "ospreys",
        "cardiff",
        "dragons",
    ],
)
punk_items = collect_articles(
    PUNK_FEEDS,
    limit=5,
    keyword_filter=["punk", "hardcore", "post-punk", "pop-punk", "ska-punk", "tour", "album", "single"],
)

wx = get_cardiff_weather()
people_in_space = get_people_in_space()

# ----------------------------
# Derived display lines
# ----------------------------
def _fmt_temp(x):
    if x is None:
        return "--"
    try:
        return f"{float(x):.1f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)

wx_line = f"{_fmt_temp(wx.get('temp'))}°C (feels {_fmt_temp(wx.get('feels'))}°C) · H {_fmt_temp(wx.get('hi'))}°C / L {_fmt_temp(wx.get('lo'))}°C"
sr = wx.get("sunrise") or "--:--"
ss = wx.get("sunset") or "--:--"

# ----------------------------
# HTML
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#1a1a1a"
    ink = "#ffffff"
    muted = "#cfcfcf"
    rule = "#3a3a3a"
    rule_light = "#2e2e2e"
    link = "#86a8ff"

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

    def section_label(text, emoji=""):
        return f"""
        <span style="font-family:{font};
                     font-size:14px !important;
                     font-weight:900 !important;
                     letter-spacing:2px;
                     text-transform:uppercase;
                     color:{ink};
                     {size_fix_inline}">
          {esc((emoji + " " if emoji else "") + text)}
        </span>
        """

    def story_block(i, it, lead=False):
        headline_size = "22px"
        headline_weight = "900" if lead else "800"
        summary_size = "15px"
        summary_weight = "500"
        pad_top = "18px"
        left_bar = f"border-left:4px solid {ink};padding-left:12px;" if lead else ""

        kicker_row = ""
        if lead:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:12px;font-weight:900;letter-spacing:2px;
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
                             line-height:1.18;
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
                             font-size:13px !important;
                             font-weight:900 !important;
                             letter-spacing:0.5px;
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

    def stack_section(title, emoji, items, limit=3):
        if not items:
            body = f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>
              <tr>
                <td style="font-family:{font};font-size:15px;font-weight:600;color:{muted};line-height:1.7;{size_fix_inline}">
                  No stories in the last 24 hours.
                </td>
              </tr>
              <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>
              <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
            </table>
            """
        else:
            body = ""
            for idx, it in enumerate(items[:limit], start=1):
                body += story_block(idx, it, lead=False)

        return f"""
        <tr>
          <td style="padding:18px 20px 0 20px;">
            {section_label(title, emoji)}
          </td>
        </tr>
        <tr>
          <td style="padding:10px 20px 0 20px;">
            <div style="height:1px;background:{rule};"></div>
          </td>
        </tr>
        <tr>
          <td style="padding:0 20px 18px 20px;">
            {body}
          </td>
        </tr>
        """

    world_html = ""
    if world_items:
        for i, it in enumerate(world_items, start=1):
            world_html += story_block(i, it, lead=(i == 1))
    else:
        world_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:18px 0;font-family:{font};color:{muted};font-size:15px;line-height:1.7;{size_fix_inline}">
              No qualifying world headlines in the last 24 hours.
            </td>
          </tr>
        </table>
        """

    inside_today_counts = f"""
      • UK Politics ({len(uk_politics_items)} stories)<br/>
      • Rugby Union ({len(rugby_items)} stories)<br/>
      • Punk Rock ({len(punk_items)} stories)
    """

    if people_in_space:
        space_lines = "<br/>".join([f"{esc(p['name'])} ({esc(p['craft'])})" for p in people_in_space])
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

              <tr>
                <td align="center" style="padding:30px 20px 16px 20px;{size_fix_inline}">
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
                    <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:12px !important;
                                                font-weight:800 !important;
                                                letter-spacing:2px;
                                                text-transform:uppercase;
                                                color:{muted};
                                                {size_fix_inline}">
                        <span style="font-size:12px !important;font-weight:800 !important;">
                          {date_line} · Daily Edition · {TEMPLATE_VERSION}
                        </span>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px 14px 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <tr>
                <td style="padding:18px 20px 10px 20px;">
                  {section_label("World Headlines", "🌍")}
                </td>
              </tr>
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <tr>
                <td style="padding:6px 20px 10px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td class="stack colpadR" width="58%" valign="top" style="padding-right:14px;">
                        {world_html}
                      </td>

                      <td class="divider" width="1" style="background:{rule};"></td>

                      <td class="stack colpadL" width="42%" valign="top" style="padding-left:14px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr><td>{section_label("Inside Today", "🗞️")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       line-height:1.9;
                                       color:{muted};
                                       {size_fix_inline}">
                              {inside_today_counts}
                            </td>
                          </tr>

                          <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:13px !important;
                                       font-weight:600;
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

                          <tr><td>{section_label("Weather · Cardiff", "⛅")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};font-size:16px;font-weight:800;color:{ink};line-height:1.5;{size_fix_inline}">
                              {esc(wx_line)}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td>{section_label("Sunrise / Sunset", "🌅")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};font-size:16px;font-weight:800;color:{ink};line-height:1.5;{size_fix_inline}">
                              Sunrise: <span style="color:{ink};">{esc(sr)}</span>
                              &nbsp;·&nbsp;
                              Sunset: <span style="color:{ink};">{esc(ss)}</span>
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td>{section_label("Who's in Space", "🚀")}</td></tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:650;
                                       line-height:1.55;
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

              {stack_section("UK Politics", "🏛️", uk_politics_items, limit=3)}
              {stack_section("Rugby Union", "🏉", rugby_items, limit=5)}
              {stack_section("Punk Rock", "🎸", punk_items, limit=3)}

              <tr>
                <td style="padding:18px;text-align:center;font-family:{font};
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
plain_lines.append(f"UK Politics: {len(uk_politics_items)}")
plain_lines.append(f"Rugby Union: {len(rugby_items)}")
plain_lines.append(f"Punk Rock: {len(punk_items)}")

plain_lines += ["", "WEATHER (Cardiff)", ""]
plain_lines.append(wx_line)

plain_lines += ["", "SUNRISE / SUNSET", ""]
plain_lines.append(f"Sunrise: {sr} · Sunset: {ss}")

plain_lines += ["", "WHO'S IN SPACE", ""]
if people_in_space:
    for p in people_in_space:
        plain_lines.append(f"- {p['name']} ({p['craft']})")
else:
    plain_lines.append("Unable to load space roster.")


def _plain_section(lines, title, items, limit):
    # NOTE: we pass `lines` in so we never fight Python scope rules
    lines += ["", title.upper(), ""]
    if not items:
        lines.append("No stories in the last 24 hours.")
        return lines
    for i, it in enumerate(items[:limit], start=1):
        lines.append(f"{i}. {it['title']}")
        lines.append(it["summary"])
        lines.append(f"Read in Reader: {it['reader']}")
        lines.append("")
    return lines


plain_lines = _plain_section(plain_lines, "UK Politics", uk_politics_items, 3)
plain_lines = _plain_section(plain_lines, "Rugby Union", rugby_items, 5)
plain_lines = _plain_section(plain_lines, "Punk Rock", punk_items, 3)

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
print("Weather line:", wx_line)
print("Sunrise/Sunset:", sr, ss)
print("People in space:", len(people_in_space))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
