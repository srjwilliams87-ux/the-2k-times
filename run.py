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
TEMPLATE_VERSION = "v-newspaper-15"
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
# SOURCES
# ----------------------------
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

# ✅ Rugby Union: your preferred list (plus BBC, RugbyPass, Planet Rugby)
RUGBY_UNION_FEEDS = [
    "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "https://www.rugbypass.com/feed/",
    "https://www.planetrugby.com/feed",
    "https://www.world.rugby/rss",
    "https://rugby365.com/feed/",
    "https://www.rugbyworld.com/feed",
    "https://www.theguardian.com/sport/rugby-union/rss",
    "https://www.therugbypaper.co.uk/feed/",
    "https://www.ruck.co.uk/feed/",
]

# You already had these working previously—keep whatever you’re using,
# but here are sane defaults if you want them explicit:
UK_POLITICS_FEEDS = [
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://www.theguardian.com/politics/rss",
    "https://www.ft.com/world/uk?format=rss",  # may be paywalled/limited; still fine as RSS
]

PUNK_ROCK_FEEDS = [
    # Keep your original sources here.
    # If you want suggestions later, we can do that — but leaving as-is.
    # Examples (only if you already use them):
    # "https://www.punknews.org/rss",
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
    return any(w in t for w in [
        "live", "minute-by-minute", "as it happened",
        "blog", "live blog", "live updates"
    ])

def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

def norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def score_article(title: str, summary: str, prefer=None, avoid=None) -> int:
    """
    Lightweight editorial scoring:
      + boosts match-centric / team-news / injury / preview
      - penalises quizzes, “how much do you know”, podcasts, etc
    """
    t = (title or "").lower()
    s = (summary or "").lower()
    score = 0

    if prefer:
        for kw in prefer:
            if kw in t:
                score += 4
            elif kw in s:
                score += 2

    if avoid:
        for kw in avoid:
            if kw in t:
                score -= 8
            elif kw in s:
                score -= 4

    # Extra rugby-specific boosts
    if re.search(r"\b(vs\.?| v )\b", f" {t} "):
        score += 6
    if "team news" in t or "line-up" in t or "lineup" in t or "injury" in t:
        score += 4
    if "preview" in t or "derby" in t:
        score += 3

    return score

def collect_articles(feed_urls, limit, prefer=None, avoid=None):
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
            summary = two_sentence_summary(summary_raw)

            sc = score_article(title, summary, prefer=prefer, avoid=avoid)

            articles.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "reader": reader_link(link),
                    "published": published,
                    "score": sc,
                }
            )

    # Sort by score first, then recency
    articles.sort(key=lambda x: (x["score"], x["published"]), reverse=True)

    # De-dupe by normalised title (good cross-source dedupe)
    seen = set()
    unique = []
    for a in articles:
        k = norm_title(a["title"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(a)

    return unique[:limit]

def extract_key_fixtures(rugby_items, max_items=3):
    """
    Heuristic: Pull “fixtures” from rugby headlines (vs/v/derby/preview/team news).
    """
    fixtures = []
    seen = set()
    for it in rugby_items:
        t = (it.get("title") or "").strip()
        tl = t.lower()
        if any(x in tl for x in [" derby", "preview", "team news", "line-up", "lineup", "injury"]) or re.search(r"\b(vs\.?| v )\b", f" {tl} "):
            k = norm_title(t)
            if k in seen:
                continue
            seen.add(k)
            fixtures.append(it)
        if len(fixtures) >= max_items:
            break
    return fixtures

# ----------------------------
# COLLECT SECTIONS
# ----------------------------
world_items = collect_articles(WORLD_FEEDS, limit=3)

# Rugby: cap to top 5, de-dupe, filter quizzes, prefer match news
RUGBY_AVOID = [
    "quiz", "how much do you know", "test your", "podcast", "listen",
    "highlights", "watch", "gallery", "in pictures", "live", "minute-by-minute",
]
RUGBY_PREFER = [
    "six nations", "urc", "premiership", "top 14", "super rugby",
    "champions cup", "challenge cup", "heineken cup", "world cup",
    "wales", "cardiff", "scarlets", "ospreys", "dragons",
    "england", "ireland", "scotland", "france", "south africa", "new zealand",
    "team news", "injury", "line-up", "preview", "derby"
]
rugby_items = collect_articles(RUGBY_UNION_FEEDS, limit=5, prefer=RUGBY_PREFER, avoid=RUGBY_AVOID)

# UK Politics: keep to a clean “top 3”
POL_AVOID = ["live", "minute-by-minute", "as it happened", "blog", "podcast", "quiz"]
uk_politics_items = collect_articles(UK_POLITICS_FEEDS, limit=3, avoid=POL_AVOID)

# Punk: keep to top 3 (if no feeds configured, will be empty)
punk_items = collect_articles(PUNK_ROCK_FEEDS, limit=3) if PUNK_ROCK_FEEDS else []

# Key fixtures from the rugby selection
key_fixtures = extract_key_fixtures(rugby_items, max_items=3)

# ----------------------------
# HTML (Newspaper)
# ----------------------------
def build_html():
    outer_bg = "#111111"
    paper = "#222222"
    ink = "#ffffff"
    muted = "#cfcfcf"
    rule_light = "#3a3a3a"
    link = "#8ab4ff"

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
      }
    </style>
    """

    def section_title(label, emoji):
        return f"""
        <tr>
          <td style="padding:16px 20px 10px 20px;">
            <span style="font-family:{font};
                         font-size:12px !important;
                         font-weight:900 !important;
                         letter-spacing:2px;
                         text-transform:uppercase;
                         color:{ink};">
              {esc(emoji)} {esc(label)}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:0 20px;">
            <div style="height:1px;background:{rule_light};"></div>
          </td>
        </tr>
        """

    def story_block(i, it, lead=False, top_story_label=False):
        # Your request: Top Story headline same size as others
        headline_size = "18px"
        headline_weight = "700"
        summary_size = "13.5px"
        summary_weight = "400"
        pad_top = "16px"

        left_bar = "border-left:4px solid %s;padding-left:12px;" % ink if (lead and top_story_label) else ""

        kicker_row = ""
        if top_story_label:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:11px;font-weight:900;letter-spacing:2px;
                         text-transform:uppercase;color:{muted};padding:0 0 8px 0;{size_fix_inline}">
                TOP STORY
              </td>
            </tr>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr><td style="height:{pad_top};font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr>
            <td style="{left_bar}{size_fix_inline}">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
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
          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    def build_story_list(items, top_story=False):
        if not items:
            return f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="padding:18px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
                  No stories in the last 24 hours.
                </td>
              </tr>
            </table>
            """
        out = ""
        for idx, it in enumerate(items, start=1):
            out += story_block(idx, it, lead=(top_story and idx == 1), top_story_label=(top_story and idx == 1))
        return out

    # Left column: World
    world_html = build_story_list(world_items, top_story=True)

    # Right column: Inside Today (counts + fixtures)
    inside_counts = f"""
      • UK Politics ({len(uk_politics_items)} stories)<br/>
      • Rugby Union ({len(rugby_items)} stories)<br/>
      • Punk Rock ({len(punk_items)} stories)
    """

    fixtures_html = ""
    if key_fixtures:
        fixtures_html += '<div style="height:10px;"></div>'
        fixtures_html += f"""
        <div style="font-family:{font};font-size:12px !important;font-weight:900 !important;
                    letter-spacing:2px;text-transform:uppercase;color:{ink};{size_fix_inline}">
          🏉 Key fixtures today
        </div>
        <div style="height:10px;"></div>
        """
        for f in key_fixtures:
            fixtures_html += f"""
            <div style="font-family:{font};font-size:14px;line-height:1.6;color:{muted};margin-bottom:8px;{size_fix_inline}">
              • {esc(f['title'])}
            </div>
            """
    else:
        fixtures_html += '<div style="height:10px;"></div>'
        fixtures_html += f"""
        <div style="font-family:{font};font-size:12px !important;font-weight:900 !important;
                    letter-spacing:2px;text-transform:uppercase;color:{ink};{size_fix_inline}">
          🏉 Key fixtures today
        </div>
        <div style="height:10px;"></div>
        <div style="font-family:{font};font-size:14px;line-height:1.6;color:{muted};{size_fix_inline}">
          No obvious fixtures found in today’s headlines.
        </div>
        """

    # Bottom sections: match World Headlines formatting
    uk_pol_html = build_story_list(uk_politics_items, top_story=False)
    rugby_html = build_story_list(rugby_items, top_story=False)
    punk_html = build_story_list(punk_items, top_story=False)

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
                <td align="center" style="padding:28px 20px 14px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:54px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.05;
                                                {size_fix_inline}">
                        <span style="font-size:54px !important;font-weight:900 !important;">
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

              <!-- Thin single rule only (remove the thick/black divider) -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule_light};"></div>
                </td>
              </tr>

              <!-- WORLD HEADLINES -->
              {section_title("World Headlines", "🌍")}

              <tr>
                <td style="padding:12px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="50%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule_light};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="50%" valign="top" style="padding-left:12px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

                          <!-- Nudge down so it aligns with TOP STORY -->
                          <tr><td style="height:20px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:12px !important;
                                       font-weight:900 !important;
                                       letter-spacing:2px;
                                       text-transform:uppercase;
                                       color:{ink};
                                       {size_fix_inline}">
                              🗞️ Inside today
                            </td>
                          </tr>

                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:15px !important;
                                       font-weight:600 !important;
                                       line-height:1.9;
                                       color:{muted};
                                       {size_fix_inline}">
                              {inside_counts}
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

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule_light};font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr><td style="height:16px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td>
                              {fixtures_html}
                            </td>
                          </tr>

                        </table>
                      </td>

                    </tr>
                  </table>
                </td>
              </tr>

              <!-- UK POLITICS -->
              {section_title("UK Politics", "🏛️")}
              <tr>
                <td style="padding:0 20px 22px 20px;">
                  {uk_pol_html}
                </td>
              </tr>

              <!-- RUGBY -->
              {section_title("Rugby Union", "🏉")}
              <tr>
                <td style="padding:0 20px 22px 20px;">
                  {rugby_html}
                </td>
              </tr>

              <!-- PUNK -->
              {section_title("Punk Rock", "🎸")}
              <tr>
                <td style="padding:0 20px 22px 20px;">
                  {punk_html}
                </td>
              </tr>

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

plain_lines += [
    "",
    "UK POLITICS",
    "",
]
if not uk_politics_items:
    plain_lines.append("No stories in the last 24 hours.")
else:
    for i, it in enumerate(uk_politics_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

plain_lines += [
    "",
    "RUGBY UNION",
    "",
]
if not rugby_items:
    plain_lines.append("No stories in the last 24 hours.")
else:
    for i, it in enumerate(rugby_items, start=1):
        plain_lines.append(f"{i}. {it['title']}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

if key_fixtures:
    plain_lines += ["", "KEY FIXTURES TODAY", ""]
    for f in key_fixtures:
        plain_lines.append(f"• {f['title']}")

plain_lines += [
    "",
    "PUNK ROCK",
    "",
]
if not punk_items:
    plain_lines.append("No stories in the last 24 hours.")
else:
    for i, it in enumerate(punk_items, start=1):
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
print("UK politics:", len(uk_politics_items))
print("Rugby union:", len(rugby_items))
print("Punk rock:", len(punk_items))
print("Key fixtures:", len(key_fixtures))
print("SMTP:", SMTP_HOST, SMTP_PORT)
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
