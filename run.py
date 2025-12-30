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

# NEW: Reader base URL (your onrender domain)
READER_BASE_URL = (os.environ.get("READER_BASE_URL", "https://the-2k-times.onrender.com") or "").rstrip("/")

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
    """
    Clean Text Link now points to your reader service:
    https://the-2k-times.onrender.com/read?url=<article>
    """
    url = (url or "").strip()
    if not url:
        return ""
    # Reader service
    return f"{READER_BASE_URL}/read?url={url}"

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def two_sentence_summary(text: str) -> str:
    text = strip_html(text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
    return " ".join(sentences[:2]) if sentences else "Summary unavailable."

def parse_time(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None

def looks_like_low_value(title: str) -> bool:
    t = (title or "").lower()
    return any(w in t for w in ["live", "minute-by-minute", "as it happened"])

# ----------------------------
# Collect articles
# ----------------------------
def collect_articles():
    articles = []
    for feed_url in WORLD_FEEDS:
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
                "article_url": link,
                "clean_url": clean_text_link(link),
                "published": published,
            })

    articles.sort(key=lambda x: x["published"], reverse=True)

    # de-dupe by title
    seen = set()
    unique = []
    for a in articles:
        key = a["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)

    return unique[:3]  # top 3 world headlines

world_items = collect_articles()

# ----------------------------
# HTML Newspaper Layout
# ----------------------------
def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

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

    if not world_items:
        rows.append("""
        <tr>
          <td style="padding:18px;border-bottom:1px solid #333;color:#ddd;">
            No qualifying world headlines in the last 24 hours.
          </td>
        </tr>
        """)
    else:
        for i, it in enumerate(world_items, start=1):
            rows.append(f"""
            <tr>
              <td style="padding:18px;border-bottom:1px solid #333;">
                <div style="font-size:18px;font-weight:700;line-height:1.4;">
                  {i}) {esc(it['title'])}
                </div>

                <div style="margin-top:8px;font-size:14px;line-height:1.6;color:#ddd;">
                  {esc(it['summary'])}
                </div>

                <div style="margin-top:12px;font-size:13px;">
                  <a href="{esc(it['article_url'])}" style="color:#6ea8ff;text-decoration:none;">
                    📰 Article Link
                  </a>
                  &nbsp;|&nbsp;
                  <a href="{esc(it['clean_url'])}" style="color:#6ea8ff;text-decoration:none;">
                    📄 Clean Text Link
                  </a>
                </div>
              </td>
            </tr>
            """)

    # Placeholders for next sections
    for section in ["UK POLITICS", "RUGBY UNION", "PUNK ROCK"]:
        rows.append(f"""
        <tr>
          <td style="padding:16px;border-bottom:1px solid #333;">
            <div style="font-size:14px;font-weight:800;letter-spacing:1px;">{section}</div>
            <div style="margin-top:8px;color:#bbb;font-size:13px;">(Placeholder — next step)</div>
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
# Plain-text fallback
# ----------------------------
plain_lines = [f"THE 2K TIMES — {now_uk.strftime('%d.%m.%Y')}\n", "WORLD HEADLINES\n"]
if not world_items:
    plain_lines.append("No qualifying world headlines in the last 24 hours.\n")
else:
    for i, it in enumerate(world_items, start=1):
        plain_lines.append(f"{i}) {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Article: {it['article_url']}")
        plain_lines.append(f"Clean:  {it['clean_url']}\n")

plain_body = "\n".join(plain_lines)

# ----------------------------
# Send email (HTML first for Spark)
# ----------------------------
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO

msg.set_content(html_body, subtype="html")
msg.add_alternative(plain_body, subtype="plain")

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent:", subject)
print("World headlines included:", len(world_items))
print("Window (UK):", window_start.isoformat(), "→", now_uk.isoformat())
print("Reader base URL:", READER_BASE_URL)
