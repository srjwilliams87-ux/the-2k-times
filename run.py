import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from urllib.request import Request, urlopen

import feedparser

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


def fetch_url(url: str, timeout: int = 12) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "The-2k-Times/1.0 (+newsletter bot)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


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
    """
    Returns:
      people: list[{"name": str, "station": str}]
      err: optional error string
    """
    try:
        html = fetch_url("https://whoisinspace.com/")
        parser = _H2Extractor()
        parser.feed(html)

        # Page structure is basically:
        # H2: "ISS - Soyuz MS-28" (group header)
        # H2: "Person Name"
        # H2: "Person Name"
        # H2: "ISS - SpaceX Crew-11" (next group header)
        # H2: "Person Name" ...
        # H2: "Tiangong space station - Shenzhou 21" (group header)
        # ...

        people = []
        current_station = None

        def normalize_station(group_header: str) -> str:
            left = group_header.split(" - ")[0].strip()
            # Keep station wording, but tighten common cases
            low = left.lower()
            if "iss" in low:
                return "ISS"
            if "tiangong" in low:
                return "Tiangong"
            return left  # fallback (e.g., "Lunar Gateway", etc.)

        for t in parser.h2:
            clean = re.sub(r"\s+", " ", t).strip()
            if not clean:
                continue

            # Group header: contains " - " and looks like a mission grouping
            if " - " in clean and any(k in clean.lower() for k in ["iss", "tiangong", "space station", "soyuz", "crew", "shenzhou"]):
                current_station = normalize_station(clean)
                continue

            # Person name entries come after a group header.
            if current_station:
                # Guard against any weird non-names
                if len(clean) < 3:
                    continue
                if clean.lower().startswith("launched"):
                    continue
                people.append({"name": clean, "station": current_station})

        # Dedupe by name (some pages can re-render)
        seen = set()
        uniq = []
        for p in people:
            k = p["name"].lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(p)

        return uniq, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ----------------------------
# DATA
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)

# You already have Weather/Sunrise handled in your current file; leaving placeholders here
# so you can drop in your existing implementations without breaking anything.
# (If you already have these values, just overwrite them.)
weather_line = os.environ.get("CARDIFF_WEATHER_LINE", "").strip()  # e.g. "1°C (feels -2°C) · H 5°C / L 0°C"
sunrise_line = os.environ.get("CARDIFF_SUNRISE", "").strip()       # e.g. "08:18"
sunset_line = os.environ.get("CARDIFF_SUNSET", "").strip()         # e.g. "16:13"

people_in_space, who_err = fetch_who_in_space()

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#1b1b1b"         # your dark "paper" look
    ink = "#ffffff"
    muted = "#cfcfcf"
    rule_light = "#2b2b2b"
    link = "#6ea1ff"

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

    def story_block(i, it, lead=False):
        # lead headline uses same size as others now (per your earlier tweak request)
        headline_size = "18px"
        headline_weight = "800"
        summary_size = "13.5px"
        summary_weight = "400"
        pad_top = "14px" if lead else "16px"

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
          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    # LEFT COLUMN: World
    if world_items:
        world_html = ""
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

    # RIGHT COLUMN: Inside Today + Weather + Sunrise/Sunset + Who's in Space
    # Build the Who's in Space list
    if who_err:
        space_lines = [f"Unavailable ({esc(who_err)})"]
    elif not people_in_space:
        space_lines = ["No data returned."]
    else:
        max_show = 6
        shown = people_in_space[:max_show]
        remaining = max(0, len(people_in_space) - len(shown))
        space_lines = [f"{esc(p['name'])} <span style='color:{muted};'>({esc(p['station'])})</span>" for p in shown]
        if remaining:
            space_lines.append(f"<span style='color:{muted};'>+ {remaining} more</span>")

    space_html = "<br/>".join(space_lines)

    wx_html = esc(weather_line) if weather_line else "<span style='color:#777;'>Add weather data</span>"
    sunrise_html = esc(sunrise_line) if sunrise_line else "--:--"
    sunset_html = esc(sunset_line) if sunset_line else "--:--"

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

              <!-- Thin rule only (no thick black line) -->
              <tr>
                <td style="padding:0 20px 14px 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <!-- Section title -->
              <tr>
                <td style="padding:16px 20px 10px 20px;">
                  <span style="font-family:{font};
                               font-size:14px !important;
                               font-weight:900 !important;
                               letter-spacing:2px;
                               text-transform:uppercase;
                               color:{ink};">
                    🌍 WORLD HEADLINES
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
                <td style="padding:12px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="50%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule_light};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="50%" valign="top" style="padding-left:12px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

                          <!-- Align this block to the same top baseline as TOP STORY by adding top padding -->
                          <tr>
                            <td style="padding-top:14px;font-family:{font};
                                       font-size:14px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              📰 INSIDE TODAY
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <!-- Your section counts (keep your real values where you already compute them) -->
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.9;
                                       color:{muted};
                                       {size_fix_inline}">
                              • UK Politics (2 stories)<br/>
                              • Rugby Union (5 stories)<br/>
                              • Punk Rock (0 stories)
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
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🌦️ WEATHER · CARDIFF
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              {wx_html}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🌅 SUNRISE / SUNSET
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:700 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              Sunrise: <span style="color:{ink};">{sunrise_html}</span>
                              <span style="color:{muted};"> · </span>
                              Sunset: <span style="color:{ink};">{sunset_html}</span>
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🚀 WHO&#39;S IN SPACE
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.6;
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
    "WHO'S IN SPACE",
]

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
print("Who's in space:", len(people_in_space), ("ERR: " + who_err if who_err else ""))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
