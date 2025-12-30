import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
# Helpers (text cleanup + summary)
# ----------------------------
def clean_text_link(url: str) -> str:
    """Creates a clean-text reader-friendly URL (no popups/ads)."""
    url = (url or "").strip()
    if url.startswith("http://"):
        return "https://r.jina.ai/http://" + url[len("http://") :]
    if url.startswith("https://"):
        return "https://r.jina.ai/https://" + url[len("https://") :]
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
    BBC sometimes includes metadata/noise in RSS summaries.
    Remove common noisy labels, image refs, embedded URLs, etc.
    """
    s = strip_html(s)

    noise_patterns = [
        r"\bTitle:\s*.*?(?=URL\s*Source:|Published\s*Time:|Markdown\s*Content:|$)",
        r"\bURL\s*Source:\s*https?://\S+",
        r"\bPublished\s*Time:\s*\S+",
        r"\bMarkdown\s*Content:\s*",
        r"\[Skip to content\]",
    ]
    for pat in noise_patterns:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)

    # Remove image markdown and [Image ...] tokens
    s = re.sub(r"!\[.*?\]\(.*?\)", " ", s)
    s = re.sub(r"\[Image\s*\d+.*?\]", " ", s, flags=re.IGNORECASE)

    # Remove any URLs inside summary (we provide links separately)
    s = re.sub(r"https?://\S+", " ", s)

    s = re.sub(r"\s+", " ", s).strip()
    return s

def two_sentence_summary(rss_summary: str) -> str:
    """Return a clean 2-sentence summary; fallback to a short trim."""
    s = remove_rss_noise(rss_summary)
    if not s:
        return "Summary unavailable."

    sentences = re.split(r"(?<=[.!?])\s+", s)
    sentences = [x.strip() for x in sentences if len(x.strip()) > 20]

    if len(sentences) >= 2:
        return f"{sentences[0]} {sentences[1]}"
    if len(sentences) == 1:
        return sentences[0]

    return (s[:240].rstrip() + "…") if len(s) > 240 else s

def parse_entry_time(entry):
    """Convert RSS publish time to timezone-aware UK datetime."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6]).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6]).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None

def looks_like_low_value(title: str) -> bool:
    t = (title or "").lower()
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

            items.append(
                {
                    "title": title,
                    "link": link,
                    "published": published_dt,
                    "rss_summary": rss_summary,
                }
            )

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
# Helpers (HTML newspaper)
# ----------------------------
def esc(s: str) -> str:
    """Minimal HTML escaping."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

def build_html_newspaper(subject_line: str, world_items: list) -> str:
    date_str = subject_line.replace("The 2k Times, ", "")

    if not world_items:
        world_html = "<p style='margin:0;color:#444;'>No qualifying world headlines in the last 24 hours.</p>"
    else:
        cards = []
        for i, it in enumerate(world_items, start=1):
            title = esc(it["title"])
            summary = esc(it["summary"])
            article = esc(it["article_url"])
            clean = esc(it["clean_url"])

            cards.append(
                f"""
                <div style="padding:14px 0;border-top:1px solid #e6e6e6;">
                  <div style="font-size:16px;line-height:1.35;font-weight:700;margin:0 0 6px 0;">
                    {i}) {title}
                  </div>
                  <div style="font-size:14px;line-height:1.55;color:#222;margin:0 0 10px 0;">
                    {summary}
                  </div>
                  <div style="font-size:13px;line-height:1.6;margin:0;">
                    <span style="font-weight:700;">Article Link:</span>
                    <a href="{article}" style="color:#0b57d0;text-decoration:underline;">{article}</a>
                  </div>
                  <div style="font-size:13px;line-height:1.6;margin:0;">
                    <span style="font-weight:700;">Clean Text Link:</span>
                    <a href="{clean}" style="color:#0b57d0;text-decoration:underline;">{clean}</a>
                  </div>
                </div>
                """.strip()
            )
        world_html = "\n".join(cards)

    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f7f7f7;">
    <div style="max-width:760px;margin:0 auto;padding:18px;">
      <div style="background:#ffffff;border:1px solid #e6e6e6;border-radius:14px;overflow:hidden;">
        <div style="padding:18px 20px;border-bottom:2px solid #111;">
          <div style="font-size:30px;letter-spacing:0.5px;font-weight:800;margin:0;color:#111;">
            The 2k Times
          </div>
          <div style="font-size:13px;color:#555;margin-top:6px;">
            {esc(date_str)} • Daily Edition
          </div>
        </div>

        <div style="padding:18px 20px;">
          <div style="font-size:14px;font-weight:800;letter-spacing:1px;color:#111;margin:0 0 10px 0;">
            WORLD HEADLINES
          </div>
          {world_html}
        </div>

        <div style="padding:18px 20px;border-top:1px solid #eee;">
          <div style="font-size:14px;font-weight:800;letter-spacing:1px;color:#111;margin:0 0 10px 0;">
            UK POLITICS
          </div>
          <p style="margin:0;color:#444;">(Placeholder — next step)</p>
        </div>

        <div style="padding:18px 20px;border-top:1px solid #eee;">
          <div style="font-size:14px;font-weight:800;letter-spacing:1px;color:#111;margin:0 0 10px 0;">
            RUGBY UNION
          </div>
          <p style="margin:0;color:#444;">(Placeholder — next step)</p>
        </div>

        <div style="padding:18px 20px;border-top:1px solid #eee;">
          <div style="font-size:14px;font-weight:800;letter-spacing:1px;color:#111;margin:0 0 10px 0;">
            PUNK ROCK
          </div>
          <p style="margin:0;color:#444;">(Placeholder — next step)</p>
        </div>

        <div style="padding:14px 20px;border-top:1px solid #eee;color:#777;font-size:12px;">
          You’re receiving this because you subscribed to The 2k Times.
        </div>
      </div>
    </div>
  </body>
</html>
"""

# ----------------------------
# Build World Headlines (top 3)
# ----------------------------
world_raw = collect_candidates(WORLD_FEEDS)[:3]

world_structured = []
for it in world_raw:
    article_url = it["link"].strip()
    world_structured.append(
        {
            "title": it["title"],
            "summary": two_sentence_summary(it["rss_summary"]),
            "article_url": article_url,
            "clean_url": clean_text_link(article_url),
        }
    )

# Plain-text version (clean + consistent)
lines = ["WORLD HEADLINES"]
if not world_structured:
    lines.append("(No qualifying world headlines in the last 24 hours.)")
else:
    for idx, it in enumerate(world_structured, start=1):
        lines.append(f"{idx}) {it['title']}")
        lines.append(it["summary"])
        lines.append(f"Article Link: {it['article_url']}")
        lines.append(f"Clean Text Link: {it['clean_url']}")
        lines.append("")

plain_body = (
    "\n".join(lines).strip()
    + "\n\nUK POLITICS\n(Placeholder — next step)\n\n"
    + "RUGBY UNION\n(Placeholder — next step)\n\n"
    + "PUNK ROCK\n(Placeholder — next step)\n"
)

# HTML “newspaper” version
html_body = build_html_newspaper(subject, world_structured)

# ----------------------------
# Send email via Mailgun SMTP
# ----------------------------
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO

# Add both versions (HTML + plain text fallback)
msg.set_content(plain_body)
msg.add_alternative(html_body, subtype="html")

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Sent edition:", subject)
print("World headlines included:", len(world_structured))
print("Window (UK):", window_start.isoformat(), "→", window_end.isoformat())
