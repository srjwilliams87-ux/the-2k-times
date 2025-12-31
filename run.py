import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import feedparser
import requests

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-13"
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
    "https://feeds.bbci.co.uk/news/uk/rss.xml",
]

RUGBY_UNION_FEEDS = [
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "https://feeds.bbci.co.uk/sport/rss.xml",
]

PUNK_ROCK_FEEDS = [
    "https://www.punknews.org/rss.php",
]

# ----------------------------
# HELPERS
# ----------------------------
def reader_link(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    # URL-encode so reader works reliably
    return f"{READER_BASE_URL}/read?url={quote_plus(url)}"


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
# DATA
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)
uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=2)
rugby_items = collect_articles(RUGBY_UNION_FEEDS, limit=5)
punk_items = collect_articles(PUNK_ROCK_FEEDS, limit=3)

# ----------------------------
# CARDIFF WEATHER + SUNRISE/SUNSET
# ----------------------------
CARDIFF_LAT = 51.4816
CARDIFF_LON = -3.1791

def get_cardiff_weather():
    """
    Uses Open-Meteo (no API key).
    Returns dict with: temp, feels, hi, lo (°C)
    """
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={CARDIFF_LAT}&longitude={CARDIFF_LON}"
            "&current=temperature_2m,apparent_temperature"
            "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
            "&timezone=Europe%2FLondon"
        )
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        cur = data.get("current", {}) or {}
        daily = data.get("daily", {}) or {}

        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]
        sunrise = (daily.get("sunrise") or [""])[0]
        sunset = (daily.get("sunset") or [""])[0]

        # Format sunrise/sunset to HH:MM
        def hhmm(dt_str: str) -> str:
            if not dt_str:
                return "—"
            try:
                dt = datetime.fromisoformat(dt_str)
                return dt.strftime("%H:%M")
            except Exception:
                return "—"

        return {
            "temp": temp,
            "feels": feels,
            "hi": hi,
            "lo": lo,
            "sunrise": hhmm(sunrise),
            "sunset": hhmm(sunset),
        }
    except Exception:
        return {
            "temp": None, "feels": None, "hi": None, "lo": None,
            "sunrise": "—", "sunset": "—",
        }

wx = get_cardiff_weather()

# ----------------------------
# WHO'S IN SPACE
# ----------------------------
def get_whos_in_space():
    """
    Best-effort: Open Notify (sometimes flaky) + fallback.
    """
    # Primary
    try:
        r = requests.get("http://api.open-notify.org/astros.json", timeout=10)
        r.raise_for_status()
        data = r.json()
        people = data.get("people", []) or []
        # Normalize to "Name (Craft)"
        out = []
        for p in people:
            name = (p.get("name") or "").strip()
            craft = (p.get("craft") or "").strip()
            if name:
                out.append(f"{name} ({craft or 'Space'})")
        if out:
            return out
    except Exception:
        pass

    # Fallback (very simple): show unavailable
    return []

astronauts = get_whos_in_space()

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#f7f5ef"
    ink = "#111111"
    muted = "#4a4a4a"
    rule = "#c9c4b8"
    rule_light = "#ddd8cc"
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
        .mtMobile{padding-top:18px!important}
      }
    </style>
    """

    def thin_rule():
        return f'<tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>'

    def story_block(i, it, lead=False, show_kicker=False):
        # Top Story headline now same as other headlines (per your request)
        headline_size = "20px" if lead else "18px"
        headline_weight = "800" if lead else "700"
        summary_size = "14.5px" if lead else "13.5px"
        summary_weight = "500" if lead else "400"
        pad_top = "18px" if lead else "14px"

        left_bar = f"border-left:4px solid {ink};padding-left:12px;" if lead else ""
        kicker_row = ""
        if show_kicker:
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

          <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>
          {thin_rule()}
        </table>
        """

    def section_header(label, emoji):
        return f"""
        <tr>
          <td style="padding:18px 20px 10px 20px;">
            <span style="font-family:{font};
                         font-size:13px !important;
                         font-weight:900 !important;
                         letter-spacing:2px;
                         text-transform:uppercase;
                         color:{ink};{size_fix_inline}">
              {emoji} {esc(label)}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:0 20px;">
            <div style="height:1px;background:{rule_light};"></div>
          </td>
        </tr>
        """

    # Build story blocks for each section
    def build_story_list(items, lead_first=False):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="padding:14px 0 2px 0;font-family:{font};color:{muted};
                           font-size:14px;line-height:1.7;{size_fix_inline}">
                  No qualifying stories in the last 24 hours.
                </td>
              </tr>
              {thin_rule()}
            </table>
            """
        out = ""
        for idx, it in enumerate(items, start=1):
            is_lead = lead_first and idx == 1
            out += story_block(idx, it, lead=is_lead, show_kicker=is_lead)
        return out

    world_html = build_story_list(world_items, lead_first=True)

    # Full-width sections after the first block (not in sidebar)
    uk_pol_html = build_story_list(uk_politics_items, lead_first=False)
    rugby_html = build_story_list(rugby_items, lead_first=False)
    punk_html = build_story_list(punk_items, lead_first=False)

    # Sidebar content strings
    def fmt_c(v):
        if v is None:
            return "—"
        try:
            return f"{int(round(float(v)))}°C"
        except Exception:
            return "—"

    wx_line = f"{fmt_c(wx.get('temp'))} (feels {fmt_c(wx.get('feels'))}) · H {fmt_c(wx.get('hi'))} / L {fmt_c(wx.get('lo'))}"
    sunrise_line = f"Sunrise: <b>{esc(wx.get('sunrise','—'))}</b> &nbsp; • &nbsp; Sunset: <b>{esc(wx.get('sunset','—'))}</b>"

    if astronauts:
        # show up to 8, then "+ N more"
        shown = astronauts[:8]
        extra = len(astronauts) - len(shown)
        who_lines = "<br/>".join(esc(x) for x in shown)
        if extra > 0:
            who_lines += f"<br/><span style='color:{muted};'>+ {extra} more</span>"
    else:
        who_lines = f"<span style='color:{muted};'>Unavailable right now.</span>"

    # INSIDE TODAY: align with Top Story by adding same top padding as story lead
    inside_pad_top = "18px"  # matches lead story pad_top in story_block

    html = f"""
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
                                                font-size:48px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.05;
                                                {size_fix_inline}">
                        <span style="font-size:48px !important;font-weight:900 !important;">
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

              <!-- Single thin rule under masthead (remove heavy/black feel) -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <!-- WORLD HEADLINES -->
              {section_header("World Headlines", "🌍")}

              <!-- Two-column block -->
              <tr>
                <td style="padding:0 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="58%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule_light};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL mtMobile" width="42%" valign="top" style="padding-left:12px;padding-top:{inside_pad_top};">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

                          <!-- Inside today -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:13px !important;
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
                              • UK Politics ({len(uk_politics_items)} stories)<br/>
                              • Rugby Union ({len(rugby_items)} stories)<br/>
                              • Punk Rock ({len(punk_items)} stories)
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
                                       font-size:13px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              ☁️ Weather · Cardiff
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       line-height:1.5;
                                       color:{ink};
                                       {size_fix_inline}">
                              {esc(wx_line)}
                            </td>
                          </tr>

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Sunrise / Sunset -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:13px !important;
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
                                       font-size:14px !important;
                                       font-weight:500;
                                       line-height:1.7;
                                       color:{muted};
                                       {size_fix_inline}">
                              {sunrise_line}
                            </td>
                          </tr>

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Who's in space -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:13px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🚀 Who&apos;s in space
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:500;
                                       line-height:1.6;
                                       color:{muted};
                                       {size_fix_inline}">
                              {who_lines}
                            </td>
                          </tr>

                        </table>
                      </td>

                    </tr>
                  </table>
                </td>
              </tr>

              <!-- OTHER SECTIONS (full-width) -->
              {section_header("UK Politics", "🏛️")}
              <tr>
                <td style="padding:0 20px 18px 20px;">
                  {uk_pol_html}
                </td>
              </tr>

              {section_header("Rugby Union", "🏉")}
              <tr>
                <td style="padding:0 20px 18px 20px;">
                  {rugby_html}
                </td>
              </tr>

              {section_header("Punk Rock", "🎸")}
              <tr>
                <td style="padding:0 20px 18px 20px;">
                  {punk_html}
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

def append_plain_section(title, items):
    plain_lines.append(title.upper())
    plain_lines.append("")
    if not items:
        plain_lines.append("No qualifying stories in the last 24 hours.")
        plain_lines.append("")
        return
    for i, it in enumerate(items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

append_plain_section("World Headlines", world_items)
append_plain_section("UK Politics", uk_politics_items)
append_plain_section("Rugby Union", rugby_items)
append_plain_section("Punk Rock", punk_items)

# Weather/space quick footer for plain text
plain_lines.append("CARDIFF WEATHER")
plain_lines.append(wx_line)
plain_lines.append(f"Sunrise: {wx.get('sunrise','—')} · Sunset: {wx.get('sunset','—')}")
plain_lines.append("")
plain_lines.append("WHO'S IN SPACE")
if astronauts:
    plain_lines.extend(astronauts)
else:
    plain_lines.append("Unavailable right now.")
plain_lines.append("")

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
print("Rugby union:", len(rugby_items))
print("Punk rock:", len(punk_items))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
