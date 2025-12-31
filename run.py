import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

# ----------------------------
# ENV
# ----------------------------
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")
SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

READER_BASE_URL = (os.environ.get("READER_BASE_URL", "https://the-2k-times.onrender.com") or "").rstrip("/")

if not all([MAILGUN_DOMAIN, EMAIL_TO, SMTP_USER, SMTP_PASS]):
    raise SystemExit("Missing required environment variables (MAILGUN_DOMAIN, EMAIL_TO, MAILGUN_SMTP_USER, MAILGUN_SMTP_PASS)")

TZ = ZoneInfo("Europe/London")

# ----------------------------
# Time window: last 24 hours
# ----------------------------
now_uk = datetime.now(TZ)
window_start = now_uk - timedelta(hours=24)
subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"

# ----------------------------
# Sources (World Headlines)
# ----------------------------
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

# ----------------------------
# Helpers
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
    # split sentences
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
    if not sentences:
        return "Summary unavailable."
    return " ".join(sentences[:2])

def parse_time(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
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
            articles.append({
                "title": title,
                "summary": two_sentence_summary(summary_raw),
                "url": link,
                "reader": reader_link(link),
                "published": published,
            })

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

world_items = collect_articles(WORLD_FEEDS, limit=3)

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
# Newspaper HTML (mobile-stacked)
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

    def story_row(i, it, lead=False):
        headline_size = "22px" if lead else "18px"
        headline_weight = "800" if lead else "700"
        summary_size = "15px" if lead else "14px"

        return f"""
        <tr>
          <td style="padding:18px 0 16px 0;">
            <div style="
              font-family:{font};
              font-size:{headline_size};
              font-weight:{headline_weight};
              line-height:1.3;
              color:{ink};
            ">
              {i}. {esc(it['title'])}
            </div>

            <div style="
              margin-top:8px;
              font-family:{font};
              font-size:{summary_size};
              font-weight:400;
              line-height:1.7;
              color:{muted};
            ">
              {esc(it['summary'])}
            </div>

            <div style="
              margin-top:10px;
              font-family:{font};
              font-size:13px;
              font-weight:600;
            ">
              <a href="{esc(it['reader'])}" style="color:{link};text-decoration:none;">
                Read in Reader →
              </a>
            </div>
          </td>
        </tr>
        <tr>
          <td>
            <div style="height:1px;background:{rule_light};"></div>
          </td>
        </tr>
        """

    world_html = ""
    if world_items:
        for i, it in enumerate(world_items, start=1):
            world_html += story_row(i, it, lead=(i == 1))
    else:
        world_html = f"""
        <tr>
          <td style="padding:18px 0;font-family:{font};color:{muted};font-size:15px;">
            No qualifying world headlines in the last 24 hours.
          </td>
        </tr>
        """

    style_block = """
    <style>
      @media screen and (max-width:640px){
        .container{width:100%!important}
        .stack{display:block!important;width:100%!important}
        .divider{display:none!important}
      }
    </style>
    """

    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      {style_block}
    </head>
    <body style="margin:0;background:{outer_bg};">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center" style="padding:18px;">
            <table class="container" width="720" style="background:{paper};border-radius:14px;overflow:hidden;">

              <!-- Masthead -->
              <tr>
                <td style="padding:24px 20px 14px 20px;text-align:center;">
                  <div style="
                    font-family:{font};
                    font-size:42px;
                    font-weight:900;
                    color:{ink};
                  ">
                    The 2k Times
                  </div>
                  <div style="
                    margin-top:6px;
                    font-family:{font};
                    font-size:13px;
                    letter-spacing:1.5px;
                    text-transform:uppercase;
                    color:{muted};
                  ">
                    {date_line} · Daily Edition
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px;">
                  <div style="height:3px;background:{ink};"></div>
                  <div style="height:1px;background:{rule};margin-top:6px;"></div>
                </td>
              </tr>

              <!-- Section Header -->
              <tr>
                <td style="padding:18px 20px 10px 20px;">
                  <div style="
                    font-family:{font};
                    font-size:12px;
                    font-weight:800;
                    letter-spacing:2px;
                    text-transform:uppercase;
                    color:{ink};
                  ">
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
                <td style="padding:10px 20px 22px 20px;">
                  <table width="100%">
                    <tr>
                      <td class="stack" width="50%" valign="top" style="padding-right:12px;">
                        <table width="100%">
                          {world_html}
                        </table>
                      </td>

                      <td class="divider" width="1" style="background:{rule};"></td>

                      <td class="stack" width="50%" valign="top" style="padding-left:12px;">
                        <div style="
                          font-family:{font};
                          font-size:13px;
                          font-weight:800;
                          letter-spacing:1.5px;
                          text-transform:uppercase;
                          color:{ink};
                        ">
                          Inside today
                        </div>

                        <div style="height:1px;background:{rule};margin:10px 0;"></div>

                        <div style="
                          font-family:{font};
                          font-size:14px;
                          line-height:1.8;
                          color:{muted};
                        ">
                          • UK Politics (2 stories)<br/>
                          • Rugby Union (top 5)<br/>
                          • Punk Rock (UK gigs + releases)
                        </div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="
                  padding:16px;
                  text-align:center;
                  font-family:{font};
                  font-size:11px;
                  color:{muted};
                ">
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


html_body = build_html()

# ----------------------------
# Plain text fallback
# ----------------------------
plain_lines = [
    f"THE 2K TIMES — {now_uk.strftime('%d.%m.%Y')}",
    "",
    "WORLD HEADLINES",
    ""
]
if not world_items:
    plain_lines.append("No qualifying world headlines in the last 24 hours.")
else:
    for i, it in enumerate(world_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read: {it['reader']}")
        plain_lines.append("")

plain_body = "\n".join(plain_lines).strip() + "\n"

# ----------------------------
# Send email (plain first, html alternative)
# ----------------------------
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO

# Correct multipart/alternative order
msg.set_content(plain_body)
msg.add_alternative(html_body, subtype="html")

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent:", subject)
print("World headlines included:", len(world_items))
print("Window (UK):", window_start.isoformat(), "→", now_uk.isoformat())
print("Reader base URL:", READER_BASE_URL)
