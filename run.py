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

subject = f"The 2k Times, {now_uk.strftime('%d.%m.%Y')}"

WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

# ----------------------------
# Helpers
# ----------------------------
def clean_text_link(url: str) -> str:
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

    s = re.sub(r"!\[.*?\]\(.*?\)", " ", s)                      # image markdown
    s = re.sub(r"\[Image\s*\d+.*?\]", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"https?://\S+", " ", s)                          # embedded URLs
    s = re.sub(r"\s+", " ", s).strip()
    return s

def two_sentence_summary(rss_summary: str) -> str:
    s = remove_rss_noise(rss_summary)
    if not s:
        return "Summary unavailable."
    sentences = re.split(r"(?<=[.!?])\s+", s)
    sentences = [x.strip() for x in sentences if len(x.strip()) > 20]
    if len(sentences) >= 2:
        return f"{sentences[0]} {sentences[1]}"
    if len(sentences) == 1:
        return sentences[0]
    return (s[:220].rstrip() + "…") if len(s) > 220 else s

def parse_entry_time(entry):
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

            items.append({
                "title": title,
                "link": link,
                "published": published_dt,
                "rss_summary": rss_summary,
            })

    items.sort(key=lambda x: x["published"], reverse=True)

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
# HTML builder (Gmail-friendly)
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

def build_html_newspaper(subject_line: str, world_items: list) -> str:
    date_str = subject_line.replace("The 2k Times, ", "")
    rows = []

    rows.append(f"""
      <tr><td style="padding:18px 18px 10px 18px;border-bottom:2px solid #111;">
        <div style="font-size:30px;font-weight:800;margin:0;">The 2k Times</div>
        <div style="font-size:13px;color:#555;margin-top:6px;">{esc(date_str)} • Daily Edition</div>
      </td></tr>
    """)

    rows.append(f"""
      <tr><td style="padding:16px 18px 10px 18px;">
        <div style="font-size:14px;font-weight:800;letter-spacing:1px;margin:0 0 12px 0;">WORLD HEADLINES</div>
      </td></tr>
    """)

    if not world_items:
        rows.append(f"""
          <tr><td style="padding:0 18px 16px 18px;color:#444;">
            No qualifying world headlines in the last 24 hours.
          </td></tr>
        """)
    else:
        for i, it in enumerate(world_items, start=1):
            rows.append(f"""
              <tr><td style="padding:0 18px 16px 18px;border-top:1px solid #e6e6e6;">
                <div style="padding-top:12px;font-size:16px;font-weight:700;line-height:1.35;">{i}) {esc(it['title'])}</div>
                <div style="margin-top:6px;font-size:14px;line-height:1.55;color:#222;">{esc(it['summary'])}</div>
                <div style="margin-top:10px;font-size:13px;line-height:1.6;">
                  <b>Article Link:</b> <a href="{esc(it['article_url'])}" style="color:#0b57d0;text-decoration:underline;">{esc(it['article_url'])}</a>
                </div>
                <div style="margin-top:4px;font-size:13px;line-height:1.6;">
                  <b>Clean Text Link:</b> <a href="{esc(it['clean_url'])}" style="color:#0b57d0;text-decoration:underline;">{esc(it['clean_url'])}</a>
                </div>
              </td></tr>
            """)

    # Placeholder sections
    for section in ["UK POLITICS", "RUGBY UNION", "PUNK ROCK"]:
        rows.append(f"""
          <tr><td style="padding:16px 18px;border-top:1px solid #eee;">
            <div style="font-size:14px;font-weight:800;letter-spacing:1px;margin:0 0 10px 0;">{section}</div>
            <div style="color:#444;">(Placeholder — next step)</div>
          </td></tr>
        """)

    rows.append("""
      <tr><td style="padding:12px 18px;border-top:1px solid #eee;color:#777;font-size:12px;">
        You’re receiving this because you subscribed to The 2k Times.
      </td></tr>
    """)

    return f"""\
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;background:#f7f7f7;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f7f7f7;padding:18px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="720" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e6e6e6;border-radius:14px;overflow:hidden;">
            {''.join(rows)}
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

# ----------------------------
# Build World Headlines (top 3)
# ----------------------------
world_raw = collect_candidates(WORLD_FEEDS)[:3]
world_items = []
for it in world_raw:
    article_url = it["link"].strip()
    world_items.append({
        "title": it["title"],
        "summary": two_sentence_summary(it["rss_summary"]),
        "article_url": article_url,
        "clean_url": clean_text_link(article_url),
    })

# Plain-text version (fallback)
plain_lines = ["WORLD HEADLINES"]
if not world_items:
    plain_lines.append("(No qualifying world headlines in the last 24 hours.)")
else:
    for idx, it in enumerate(world_items, start=1):
        plain_lines.append(f"{idx}) {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Article Link: {it['article_url']}")
        plain_lines.append(f"Clean Text Link: {it['clean_url']}")
        plain_lines.append("")

plain_body = (
    "\n".join(plain_lines).strip()
    + "\n\nUK POLITICS\n(Placeholder — next step)\n\n"
    + "RUGBY UNION\n(Placeholder — next step)\n\n"
    + "PUNK ROCK\n(Placeholder — next step)\n"
)

html_body = build_html_newspaper(subject, world_items)

# ----------------------------
# Send email via Mailgun SMTP
# ----------------------------
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{EMAIL_FROM_NAME} <postmaster@{MAILGUN_DOMAIN}>"
msg["To"] = EMAIL_TO

msg.set_content(plain_body)
msg.add_alternative(html_body, subtype="html")

with smtplib.SMTP("smtp.mailgun.org", 587) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Sent edition:", subject)
print("World headlines included:", len(world_items))
print("Window (UK):", window_start.isoformat(), "→", window_end.isoformat())
