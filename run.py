import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

# ----------------------------
# Environment variables (Render)
# ----------------------------
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")
SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

if not all([MAILGUN_DOMAIN, EMAIL_TO, SMTP_USER, SMTP_PASS]):
    raise SystemExit("Missing required environment variables")

TZ = ZoneInfo("Europe/London")

# ----------------------------
# Time window: last 24 hours
# ----------------------------
now_uk = datetime.now(TZ)
window_start = now_uk - timedelta(hours=24)
subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"

WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

# ----------------------------
# Helpers
# ----------------------------
def clean_text_link(url: str) -> str:
    return "https://r.jina.ai/" + url

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def two_sentence_summary(text: str) -> str:
    text = strip_html(text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s for s in sentences if len(s) > 20]
    return " ".join(sentences[:2]) if sentences else "Summary unavailable."

def parse_time(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None

# ----------------------------
# Collect articles
# ----------------------------
def collect_articles():
    articles = []
    for feed_url in WORLD_FEEDS:
        feed = feedparser.parse(feed_url)
        for e in feed.entries:
            published = parse_time(e)
            if not published or not (window_start <= published <= now_uk):
                continue

            articles.append({
                "title": e.title,
                "summary": two_sentence_summary(getattr(e, "summary", "")),
                "article_url": e.link,
                "clean_url": clean_text_link(e.link),
                "published": published,
            })

    articles.sort(key=lambda x: x["published"], reverse=True)

    seen = set()
    unique = []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    return unique[:3]

world_items = collect_articles()

# ----------------------------
# HTML Newspaper Layout
# ----------------------------
def build_html():
    rows = []

    rows.append(f"""
    <tr>
      <td style="padding:20px;border-bottom:2px solid #111;">
        <div style="font-size:32px;font-weight:800;">The 2k Times</div>
        <div style="font-size:13px;color:#aaa;margin-top:6px;">
          {now_uk.strftime('%d.%m.%Y')} · Daily Edition
        </div>
      </td>
    </tr>
    """)

    rows.append("""
    <tr>
      <td style="padding:16px;border-bottom:1px solid #333;">
        <div style="font-size:14px;font-weight:800;letter-spacing:1px;">
          WORLD HEADLINES
        </div>
      </td>
    </tr>
    """)

    for i, it in enumerate(world_items, start=1):
        rows.append(f"""
        <tr>
          <td style="padding:18px;border-bottom:1px solid #333;">
            <div style="font-size:18px;font-weight:700;line-height:1.4;">
              {i}) {it['title']}
            </div>

            <div style="margin-top:8px;font-size:14px;line-height:1.6;color:#ddd;">
              {it['summary']}
            </div>

            <div style="margin-top:12px;font-size:13px;">
              <a href="{it['article_url']}" style="color:#6ea8ff;text-decoration:none;">
                📰 Article Link
              </a>
              &nbsp;|&nbsp;
              <a href="{it['clean_url']}" style="color:#6ea8ff;text-decoration:none;">
                📄 Clean Text Link
              </a>
            </div>
          </td>
        </tr>
        """)

    return f"""
    <html>
    <body style="margin:0;background:#111;color:#fff;font-family:Georgia,serif;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center">
            <table width="720" cellpadding="0" cellspacing="0"
              style="background:#1b1b1b;border-radius:16px;overflow:hidden;">
              {''.join(rows)}
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

html_body = build_html()

# ----------------------------
