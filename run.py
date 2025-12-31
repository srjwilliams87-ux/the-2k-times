import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-11"
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
# FEEDS (by section)
# ----------------------------
SECTIONS = [
    {
        "key": "world",
        "name": "WORLD HEADLINES",
        "limit": 3,
        "feeds": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.reuters.com/Reuters/worldNews",
        ],
    },
    {
        "key": "uk_politics",
        "name": "UK POLITICS",
        "limit": 2,
        "feeds": [
            "https://feeds.bbci.co.uk/news/politics/rss.xml",
            "https://feeds.reuters.com/reuters/UKdomesticNews",
        ],
    },
    {
        "key": "rugby_union",
        "name": "RUGBY UNION",
        "limit": 5,
        "feeds": [
            "https://www.bbc.co.uk/sport/rugby-union/rss.xml",
        ],
    },
    {
        "key": "punk_rock",
        "name": "PUNK ROCK",
        "limit": 5,
        "feeds": [
            # You can swap/add better sources later. This is a safe starter set.
            "https://www.punknews.org/rss",
        ],
    },
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


# ----------------------------
# COLLECT ALL SECTIONS
# ----------------------------
section_items = {}
for s in SECTIONS:
    section_items[s["key"]] = collect_articles(s["feeds"], s["limit"])

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#2b2a26"
    ink = "#f2f2f2"
    muted = "#c8c8c8"
    link = "#8ab4ff"
    rule = "#4a4a4a"  # single thin rule everywhere

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
        .sectionPad{padding-top:8px!important}
      }
    </style>
    """

    def section_title_row(title: str):
        return f"""
        <tr>
          <td style="padding:16px 20px 10px 20px;{size_fix_inline}">
            <span style="font-family:{font};
                         font-size:15px !important;
                         font-weight:900 !important;
                         letter-spacing:2.2px;
                         text-transform:uppercase;
                         color:{ink};
                         {size_fix_inline}">
              {esc(title)}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:0 20px;{size_fix_inline}">
            <div style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</div>
          </td>
        </tr>
        """

    def story_block(i, it, lead=False):
        headline_size = "18px"
        headline_weight = "800"
        summary_size = "13.5px"
        summary_weight = "400"
        pad_top = "18px" if i == 1 else "16px"

        left_bar = "border-left:4px solid %s;padding-left:12px;" % ink if lead else ""

        kicker_row = ""
        if lead:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};
                         font-size:11px !important;
                         font-weight:900 !important;
                         letter-spacing:2px;
                         text-transform:uppercase;
                         color:{muted};
                         padding:0 0 8px 0;
                         {size_fix_inline}">
                TOP STORY
              </td>
            </tr>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;{size_fix_inline}">
          <tr><td style="height:{pad_top};font-size:0;line-height:0;">&nbsp;</td></tr>

          <tr>
            <td style="{left_bar}{size_fix_inline}">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;{size_fix_inline}">
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

    def render_section_items(items, lead_first=False):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;{size_fix_inline}">
              <tr>
                <td style="padding:18px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
                  No qualifying stories in the last 24 hours.
                </td>
              </tr>
              <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
            </table>
            """
        out = ""
        for i, it in enumerate(items, start=1):
            out += story_block(i, it, lead=(lead_first and i == 1))
        return out

    # Inside today bullets (auto)
    inside_lines = []
    for s in SECTIONS[1:]:  # exclude world in sidebar list if you want
        count = len(section_items.get(s["key"], []))
        inside_lines.append(f"• {s['name'].title()} ({count} stories)" if s["key"] != "rugby_union" else f"• Rugby Union ({count} stories)")

    inside_html = "<br/>".join(esc(x) for x in inside_lines) if inside_lines else "• No other sections configured."

    # Build sections HTML: world uses 2-column layout; other sections full-width
    world = section_items.get("world", [])
    world_left = render_section_items(world, lead_first=True)

    extra_sections_html = ""
    for s in SECTIONS[1:]:
        items = section_items.get(s["key"], [])
        extra_sections_html += f"""
        {section_title_row(s["name"])}
        <tr>
          <td class="sectionPad" style="padding:12px 20px 22px 20px;{size_fix_inline}">
            {render_section_items(items, lead_first=False)}
          </td>
        </tr>
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
                   style="border-collapse:collapse;background:{paper};border-radius:18px;overflow:hidden;{size_fix_inline}">

              <!-- Masthead -->
              <tr>
                <td align="center" style="padding:28px 20px 18px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;{size_fix_inline}">
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

              <!-- Thin rule under masthead -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</div>
                </td>
              </tr>

              <!-- WORLD HEADLINES -->
              {section_title_row("WORLD HEADLINES")}

              <tr>
                <td style="padding:12px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;{size_fix_inline}">
                    <tr>
                      <td class="stack colpadR" width="50%" valign="top" style="padding-right:12px;">
                        {world_left}
                      </td>

                      <td class="divider" width="1" style="background:{rule};"></td>

                      <td class="stack colpadL" width="50%" valign="top" style="padding-left:12px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;{size_fix_inline}">
                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2.2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              INSIDE TODAY
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:14px !important;
                                       font-weight:600 !important;
                                       line-height:1.9;
                                       color:{muted};
                                       {size_fix_inline}">
                              {inside_html}
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
                        </table>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- OTHER SECTIONS -->
              {extra_sections_html}

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
]

for s in SECTIONS:
    plain_lines.append(s["name"])
    plain_lines.append("")
    items = section_items.get(s["key"], [])
    if not items:
        plain_lines.append("No qualifying stories in the last 24 hours.")
        plain_lines.append("")
        continue

    for i, it in enumerate(items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
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
for s in SECTIONS:
    print(f"{s['name']}: {len(section_items.get(s['key'], []))}")
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
