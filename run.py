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
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

# ----------------------------
# Helpers
# ----------------------------
def clean_text_link(url: str) -> str:
    """Creates a clean-text reader-friendly URL."""
    url = url.strip()
    if url.startswith("http://"):
        return "https://r.jina.ai/http://" + url[len("http://"):]
    if url.startswith("https://"):
        return "https://r.jina.ai/https://" + url[len("https://"):]
    return "https://r.jina.ai/https://" + url

def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def remove_rss_noise(s: str) -> str:
    """
    BBC sometimes includes a lot of metadata in RSS summaries (Title/URL/Published/Markdown Content etc).
    This removes common noisy labels and trims anything that looks like site nav / image refs.
    """
    s = strip_html(s)

    # Remove common noisy labels
    noise_patterns = [
        r"\bTitle:\s*.*?(?=URL\s*Source:|Published\s*Time:|Markdown\s*Content:|$)",
        r"\bURL\s*Source:\s*https?://\S+",
        r"\bPublished\s*Time:\s*\S+",
        r"\bMarkdown\s*Content:\s*",
        r"\[Skip to content\]",
    ]
    for pat in noise_patterns:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)

    # Remove image markdown like ![Image ...](url) or [Image ...]
    s = re.sub(r"!\[.*?\]\(.*?\)", " ", s)
    s = re.sub(r"\[Image\s*\d+.*?\]", " ", s, flags=re.IGNORECASE)

    # Remove leftover URLs inside the summary (we provide links separately)
    s = re.sub(r"https?://\S+", " ", s)

    # Final whitespace normalisation
    s = re.sub(r"\s+", " ", s).strip()
    return s

def two_sentence_summary(rss_summary: str) -> str:
    """
    Returns a clean 2-sentence summary.
    Falls back to first ~240 chars if sentence splitting fails.
    """
    s = remove_rss_noise(rss_summary)
    if not s:
        return "Summary unavailable."

    # Sentence split
    sentences = re.split(r"(?<=[.!?])\s+", s)
    sentences = [x.strip() for x in sentences if len(x.strip()) > 20]

    if len(sentences) >= 2:
        return f"{sentences[0]} {sentences[1]}"
    if len(sentences) == 1:
        return sentences[0]

    # Fallback: hard trim
    return (s[:240].rstrip() + "…") if len(s) > 240 else s

def parse_entry_time(entry) -> datetime | None:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6]).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6]).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None

def looks_like_low_value(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in ["live", "minute-by-minute", "as it happened"])

def collect_candidates(feed_urls):
    items = []
    for feed_url in feed_urls:
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

            if not (window_start <= published_dt <= window_end):
                continue

            rss_summary = ""
            if hasattr(e, "summary") and e.summary:
                rss_summary = e.summary
            elif hasattr(e, "description") and e.description:
                rss_summary = e.description

            items.append({
                "title": title,
                "link": link,
                "published": published_dt,
                "rss_summary": rss_summary,
            })

    # newest first
    items.sort(key=lambda x: x["published"], reverse=True)

    # dedupe by title
    seen = set()
    out = []
    for it in items:
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

# ----------------------------
# Build World Headlines section (top 3)
# ----------------------------
world = collect_candidates(WORLD_FEEDS)[:3]

lines = ["WORLD HEADLINES"]
if not world:
    lines.append("(No qualifying world headlines in the last 24 hours.)")
else:
    for idx, it in enumerate(world, start=1):
        summary = two_sentence_summary(it["rss_summary"])
        article_url = it["link"].strip()
        clean_url = clean_text_link(article_url)

        # Format exactly as requested
        lines.append(f"{idx}) {it['title']}")
        lines.append(f"{summary}")
        lines.append(f"Article Link: {article_url}")
        lines.append(f"Clean Text Link: {clean_url}")
        lines.append("")  # spacing between stories

# Placeholders for other sections (next steps)
body = (
    "\n".join(lines).strip()
    + "\n\nUK POLITICS\n(Placeholder — next step)\n\n"
    + "RUGBY UNION\n(Placeholder — next step)\n\n"
    + "PUNK ROCK\n(Placeholder — next step)\n"
)

# ----------------------------
# Send email via Mailgun SMTP
# ----------------------------
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO
msg.set_content(body)

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Sent edition:", subject)
print("World headlines included:", len(world))
print("Window (UK):", window_start.isoformat(), "→", window_end.isoformat())
