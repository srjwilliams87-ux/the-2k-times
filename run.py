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
TEMPLATE_VERSION = "v-newspaper-08"
DEBUG_SUBJECT = True  # set False once confirmed

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
# SOURCES (World Headlines)
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


world_items = collect_articles(WORLD_FEEDS, limit=3)

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

    # TEXT_SIZE_FIX: stop Spark/Gmail font boosting (especially on mobile / dark mode)
    # Put it inline because some clients strip <style> blocks.
    size_fix_inline = "-webkit-text-size-adjust:100%;text-size-adjust:100%;-ms-text-size-adjust:100%;"

    style_block = """
    <style>
      @media screen and (max-width:640px){
        .container{width:100%!important}
        .stack{display:block!important;width:100%!important}
        .divider{display:none!important}
        .colpad{padding-left:0!important;padding-right:0!important}
      }
    </style>
    """

    def story_row(i, it, lead=False):
        # TEXT_SIZE_FIX: use !important to reduce “flattening”
        headline_size = "40px" if lead else "18px"
        headline_weight = "900" if lead else "700"
        summary_size = "16px" if lead else "13.5px"
        outer_pad = "26px 0 20px 0" if lead else "16px 0 14px 0"

        kicker = ""
        if lead:
            kicker = f"""
            <div style="font-family:{font};font-size:11px !important;font-weight:900 !important;letter-spacing:2px;
                        text-transform:uppercase;color:{muted};margin:0 0 8px 0;">
              Top Story
            </div>
            """

        left_wrap_open = f'<div style="border-left:4px solid {ink};padding-left:12px;">' if lead else "<div>"
        left_wrap_close = "</div>"

        return f"""
        <tr>
          <td style="padding:{outer_pad};">
            {left_wrap_open}
              {kicker}

              <div style="font-family:{font};
                          font-size:{headline_size} !important;
                          font-weight:{headline_weight} !important;
                          line-height:1.15;
                          color:{ink};">
                {i}. {esc(it['title'])}
              </div>

              <div style="margin-top:10px;
                          font-family:{font};
                          font-size:{summary_size} !important;
                          font-weight:400;
                          line-height:1.7;
                          color:{muted};">
                {esc(it['summary'])}
              </div>

              <div style="margin-top:12px;
                          font-family:{font};
                          font-size:12px !important;
                          font-weight:800 !important;
                          letter-spacing:1px;
                          text-transform:uppercase;">
                <a href="{esc(it['reader'])}" style="color:{link};text-decoration:none;">
                  Read in Reader →
                </a>
              </div>
            {left_wrap_close}
          </td>
        </tr>
        <tr><td><div style="height:1px;background:{rule_light};"></div></td></tr>
        """

    if world_items:
        world_html = ""
        for i, it in enumerate(world_items, start=1):
            world_html += story_row(i, it, lead=(i == 1))
    else:
        world_html = f"""
        <tr>
          <td style="padding:18px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;">
            No qualifying world headlines in the last 24 hours.
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
                   style="border-collapse:collapse;background:{paper};border-radius:14px;overflow:hidden;{size_fix_inline}">

              <!-- Masthead -->
              <tr>
                <td style="padding:28px 20px 14px 20px;text-align:center;{size_fix_inline}">
                  <div style="font-family:{font};
                              font-size:56px !important;
                              font-weight:900 !important;
                              color:{ink};
                              line-height:1.0;">
                    The 2k Times
                  </div>

                  <div style="margin-top:10px;font-family:{font};
                              font-size:12px !important;
                              font-weight:700 !important;
                              letter-spacing:2px;
                              text-transform:uppercase;
                              color:{muted};">
                    {date_line} · Daily Edition · {TEMPLATE_VERSION}
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:3px;background:{ink};"></div>
                  <div style="height:1px;background:{rule};margin-top:7px;"></div>
                </td>
              </tr>

              <!-- Section header -->
              <tr>
                <td style="padding:16px 20px 10px 20px;">
                  <div style="font-family:{font};
                              font-size:12px !important;
                              font-weight:900 !important;
                              letter-spacing:2px;
                              text-transform:uppercase;
                              color:{ink};">
                    World Headlines
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px;">
                  <div style="height:2px;background:{rule};"></div>
                </td>
              </tr>

              <!-- Content -->
              <tr>
                <td style="padding:12px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <!-- Left -->
                      <td class="stack colpad" width="50%" valign="top" style="padding-right:12px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                          {world_html}
                        </table>
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule};"></td>

                      <!-- Right -->
                      <td class="stack colpad" width="50%" valign="top" style="padding-left:12px;">
                        <div style="font-family:{font};
                                    font-size:12px !important;
                                    font-weight:900 !important;
                                    letter-spacing:2px;
                                    text-transform:uppercase;
                                    color:{ink};">
                          Inside today
                        </div>

                        <div style="height:1px;background:{rule};margin:10px 0 12px 0;"></div>

                        <div style="font-family:{font};
                                    font-size:15px !important;
                                    font-weight:500;
                                    line-height:1.9;
                                    color:{muted};">
                          • UK Politics (2 stories)<br/>
                          • Rugby Union (top 5)<br/>
                          • Punk Rock (UK gigs + releases)
                        </div>

                        <div style="margin-top:14px;font-family:{font};
                                    font-size:12px !important;
                                    line-height:1.7;
                                    color:{muted};">
                          Curated from the last 24 hours.<br/>
                          Reader links included.
                        </div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="padding:16px;text-align:center;font-family:{font};
                           font-size:11px !important;color:{muted};">
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
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
