import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import feedparser

# ----------------------------
# Settings (Render env vars)
# ----------------------------
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times")
SMTP_USER = os.environ.get("MAILGUN_SMTP_USER")
SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS")

if not all([MAILGUN_DOMAIN, EMAIL_TO, SMTP_USER, SMTP_PASS]):
    raise SystemExit("Missing one or more required environment variables in Render")

TZ = ZoneInfo("Europe/London")

# ----------------------------
# Time window: previous 24 hours (UK time)
# Locked rule: 05:30 yesterday → 05:29 today, relative to send time
# Implementation: always take the last 24 hours at run time (UK)
# ----------------------------
now_uk = datetime.now(TZ)
window_end = now_uk
window_start = now_uk - timedelta(hours=24)

# Subject: The 2k Times, dd.mm.yyyy
subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"

# ----------------------------
# World Headlines sources (RSS)
# ----------------------------
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",          # BBC World
    "https://feeds.reuters.com/Reuters/worldNews",          # Reuters World
]

# ----------------------------
# Helpers
# ----------------------------
def clean_text_link(url: str) -> str:
    """
    Clean-text fallback link: converts any URL into a very readable text-first version.
    This avoids ads/popups and works well on mobile.
    """
    url = url.strip()
    if url.startswith("http://"):
        return "https://r.jina.ai/http://" + url[len("http://"):]
    if url.startswith("https://"):
        return "https://r.jina.ai/https://" + url[len("https://"):]
    return "https://r.jina.ai/https://" + url

def strip_html(s: str) -> str:
    if not s:
        return ""
    # Remove HTML tags
    s = re.sub(r"<[^>]+>", "", s)
    # Decode common HTML entities crudely
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

def medium_summary_from_rss(rss_summary: str) -> str:
    """
    Creates a tidy 4–5 sentence summary from RSS summary text.
    Much cleaner than summarising from full article pages (which include nav/menus/etc).
    """
    s = strip_html(rss_summary)
    if not s:
        return "Summary unavailable. Use the clean-text link to read the full story."

    # Split into sentences (simple, works well enough for news briefs)
    sentences = re.split(r"(?<=[.!?])\s+", s)
    sentences = [x.strip() for x in sentences if len(x.strip()) > 25]

    # Take up to 5 sentences for "Medium"
    picked = sentences[:5]
    return " ".join(picked) if picked else s

def parse_entry_time(entry) -> datetime | None:
    """
    Converts RSS publish time to timezone-aware UK datetime.
    """
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        dt = datetime(*entry.published_parsed[:6]).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        return dt
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        dt = datetime(*entry.updated_parsed[:6]).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        return dt
    return None

def looks_like_low_value(title: str) -> bool:
    t = title.lower()
    bad_words = ["live", "minute-by-minute", "as it happened"]
    return any(w in t for w in bad_words)

def collect_world_candidates():
    items = []
    for feed_url in WORLD_FEEDS:
        feed = feedparser.parse(feed_url)

        for e in feed.entries:
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "").strip()
            if not title or not link:
                continue
            if looks_like_low_value(title):
                continue

            published_dt = parse_entry_time(e)
            if not published_dt:
                continue

            # Filter to previous 24 hours (UK time)
            if not (window_start <= published_dt <= window_end):
                continue

            rss_summary = ""
            # RSS feeds differ in which field is present
            if hasattr(e, "summary") and e.summary:
                rss_summary = e.summary
            elif hasattr(e, "description") and e.description:
                rss_summary = e.description

            items.append({
                "title": title,
                "link": link,
                "published": published_dt,
                "rss_summary": rss_summary,
                "source_feed": feed_url,
            })

    # Sort newest first
    items.sort(key=lambda x: x["published"], reverse=True)

    # De-dupe by title (simple)
    seen = set()
    deduped = []
    for it in items:
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    return deduped

# ----------------------------
# Build World Headlines section
# ----------------------------
world_candidates = collect_world_candidates()
world_top3 = world_candidates[:3]

world_lines = ["WORLD HEADLINES"]
if not world_top3:
    world_lines.append("(No qualifying world headlines in the last 24 hours.)")
else:
    for idx, it in enumerate(world_top3, start=1):
        clean_url = clean_text_link(it["link"])
        summary = medium_summary_from_rss(it["rss_summary"])

        world_lines.append(f"{idx}) {it['title']}")
        world_lines.append(f"   {summary}")
        world_lines.append(f"   Read full article (clean text) → {clean_url}")
        world_lines.append("")

# Keep other sections as placeholders for now
body_text = (
    "\n".join(world_lines).strip()
    + "\n\n\nUK POLITICS\n(Placeholder — next step)\n\n\n"
    + "RUGBY UNION\n(Placeholder — next step)\n\n\n"
    + "PUNK ROCK\n(Placeholder — next step)\n"
)

# ----------------------------
# Send email via Mailgun SMTP
# ----------------------------
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO
msg.set_content(body_text)

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Sent edition:", subject)
print("World headlines included:", len(world_top3))
print("Window (UK):", window_start.isoformat(), "→", window_end.isoformat())

