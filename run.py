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
# Time window: previous 24 hours
# (locked: run at 05:30 UK time)
# ----------------------------
now_uk = datetime.now(TZ)
window_end = now_uk
window_start = now_uk - timedelta(hours=24)

# Subject: The 2k Times, dd.mm.yyyy
subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"

# ----------------------------
# World Headlines sources
# ----------------------------
WORLD_FEEDS = [
    # BBC World RSS
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    # Reuters World News RSS
    "https://feeds.reuters.com/Reuters/worldNews",
]

# Clean-text link helper (reader-friendly view)
def clean_text_link(url: str) -> str:
    """
    Creates a clean-text version of a URL using a text-first proxy.
    This avoids popups/ads and is very readable on mobile.
    """
    url = url.strip()
    if url.startswith("http://"):
        return "https://r.jina.ai/http://" + url[len("http://"):]
    if url.startswith("https://"):
        return "https://r.jina.ai/https://" + url[len("https://"):]
    return "https://r.jina.ai/https://" + url

def fetch_clean_text_preview(url: str, max_sentences: int = 5) -> str:
    """
    Pulls the clean-text page and returns ~4–5 sentences.
    If the clean-text fetch fails, falls back to a short generic summary.
    """
    ct_url = clean_text_link(url)
    try:
        r = requests.get(ct_url, timeout=15)
        r.raise_for_status()
        text = r.text

        # Basic cleanup
        text = re.sub(r"\s+", " ", text).strip()

        # Split into sentences (simple and good enough for a newspaper brief)
        # We’ll take the first 4–5 sentences with reasonable length.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        picked = []
        for s in sentences:
            s = s.strip()
            if len(s) < 40:
                continue
            picked.append(s)
            if len(picked) >= max_sentences:
                break

        if picked:
            return " ".join(picked)

    except Exception:
        pass

    return "Summary unavailable (source format restricted). Use the clean-text link to read the full story."

def parse_entry_time(entry) -> datetime | None:
    """
    Converts RSS entry publish time to a timezone-aware UK datetime.
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
    # Simple filters to reduce “live” pages and low-signal items
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

            # Use RSS summary if present (fallback only)
            rss_summary = ""
            if hasattr(e, "summary") and e.summary:
                rss_summary = re.sub(r"<[^>]+>", "", e.summary)  # strip HTML tags
                rss_summary = re.sub(r"\s+", " ", rss_summary).strip()

            items.append({
                "title": title,
                "link": link,
                "published": published_dt,
                "rss_summary": rss_summary,
                "source_feed": feed_url
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

world_candidates = collect_world_candidates()
world_top3 = world_candidates[:3]

# Build World Headlines section text
world_text_lines = ["WORLD HEADLINES"]
if not world_top3:
    world_text_lines.append("(No qualifying world headlines in the last 24 hours.)")
else:
    for idx, it in enumerate(world_top3, start=1):
        clean_url = clean_text_link(it["link"])
        summary = fetch_clean_text_preview(it["link"], max_sentences=5)

        world_text_lines.append(f"{idx}) {it['title']}")
        world_text_lines.append(f"   {summary}")
        world_text_lines.append(f"   Read full article (clean text) → {clean_url}")
        world_text_lines.append("")  # blank line

# Keep other sections as placeholders for now
text = (
    "\n".join(world_text_lines).strip()
    + "\n\n\nUK POLITICS\n(Placeholder — next step)\n\n\n"
    + "RUGBY UNION\n(Placeholder — next step)\n\n\n"
    + "PUNK ROCK\n(Placeholder — next step)\n"
)

# Send email via Mailgun SMTP
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO
msg.set_content(text)

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Sent edition:", subject)
print("World headlines included:", len(world_top3))
print("Window (UK):", window_start.isoformat(), "→", window_end.isoformat())
