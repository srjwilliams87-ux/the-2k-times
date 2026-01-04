import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-CORE-02"
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
# SOURCES (World Headlines)
# MUST pick 3 stories from 3 different sources
# ----------------------------
WORLD_SOURCES = [
    {
        "name": "BBC",
        "key": "bbc",
        "feeds": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
    },
    {
        "name": "Reuters",
        "key": "reuters",
        "feeds": ["https://feeds.reuters.com/Reuters/worldNews"],
    },
    {
        "name": "The Guardian",
        "key": "guardian",
        "feeds": ["https://www.theguardian.com/world/rss"],
    },
    {
        "name": "The Independent",
        "key": "independent",
        "feeds": ["https://www.independent.co.uk/news/world/rss"],
    },
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
    # feedparser gives struct_time in UTC
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    if getattr(entry, "updated_parsed", None):
        return datetime(*entry.updated_parsed[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None


def looks_like_low_value(title: str) -> bool:
    t = (title or "").lower()
    return any(w in t for w in ["live", "minute-by-minute", "as it happened"])


def collect_articles(feed_urls, source_key, limit=12):
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
                    "source_key": source_key,
                    "title": title,
                    "summary": two_sentence_summary(summary_raw),
                    "url": link,
                    "reader": reader_link(link),
                    "published": published,
                }
            )

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

STOPWORDS = {
    "the","a","an","and","or","but","if","then","else","when","while","as","of","to","in","on","for","with","by",
    "from","at","into","over","after","before","under","against","between","without","within","about","this","that",
    "these","those","it","its","they","their","them","he","she","his","her","you","we","our","us","is","are","was",
    "were","be","been","being","will","would","can","could","should","may","might","must","do","does","did","done",
    "says","said","say","new","latest","live","update","updates"
}

def _tokens(text: str):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    parts = re.split(r"\s+", text)
    toks = []
    for p in parts:
        if not p or len(p) < 3:
            continue
        if p in STOPWORDS:
            continue
        toks.append(p)
    return toks

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def keyphrases(text: str):
    """
    Lightweight 'topic' extraction: keeps bigrams + notable single tokens.
    Helps catch: 'maduro venezuela', 'swiss fire', 'ukraine strike' etc.
    """
    toks = _tokens(text)
    singles = {t for t in toks if len(t) >= 4}
    bigrams = set()
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i+1]
        if a in STOPWORDS or b in STOPWORDS:
            continue
        bigrams.add(f"{a}_{b}")
    return singles | bigrams

def too_similar(candidate, chosen_items) -> bool:
    """
    Returns True if candidate is a near-duplicate of anything already chosen.
    """
    cand_text = f"{candidate.get('title','')} {candidate.get('summary','')}"
    cand_tokens = set(_tokens(cand_text))
    cand_keys = keyphrases(cand_text)

    for it in chosen_items:
        it_text = f"{it.get('title','')} {it.get('summary','')}"
        it_tokens = set(_tokens(it_text))
        it_keys = keyphrases(it_text)

        # Similar title/summary wording
        jac = jaccard(cand_tokens, it_tokens)

        # Same 'topic' phrases even if wording differs
        key_jac = jaccard(cand_keys, it_keys)

        # Tune thresholds here:
        # - jac catches near-paraphrases
        # - key_jac catches same event described differently
        if jac >= 0.42 or key_jac >= 0.28:
            return True

    return False

def pick_three_distinct_sources(source_lists):
    """
    Picks 3 items total, preferring:
      1) three distinct sources
      2) non-duplicated topics (near-duplicate filter)
      3) newest-first

    If it's impossible to get 3 distinct sources without duplication,
    we relax the source constraint BEFORE allowing topic duplication.
    """
    # pointers into each source list
    pointers = {k: 0 for k in source_lists.keys()}

    chosen = []
    used_sources = set()

    def next_candidate_for_source(k):
        items = source_lists.get(k, [])
        idx = pointers.get(k, 0)
        while idx < len(items):
            cand = items[idx]
            idx += 1
            pointers[k] = idx
            # Reject near-duplicates
            if too_similar(cand, chosen):
                continue
            return cand
        return None

    # PASS 1: try to get 3 items from 3 distinct sources (no duplicates)
    while len(chosen) < 3:
        best = None
        best_key = None

        for k in source_lists.keys():
            if k in used_sources:
                continue
            cand = next_candidate_for_source(k)
            if not cand:
                continue

            # we advanced pointer; if not chosen, we need to hold it.
            # simplest: compare and keep best, and if not selected, we store it back by rewinding one step
            if best is None or cand["published"] > best["published"]:
                # rewind previous best if existed (since we consumed one)
                if best_key is not None:
                    pointers[best_key] -= 1
                best = cand
                best_key = k
            else:
                # rewind because not selected
                pointers[k] -= 1

        if best is None:
            break

        chosen.append(best)
        used_sources.add(best_key)

    # PASS 2: if still fewer than 3, relax source constraint but STILL forbid duplicates
    if len(chosen) < 3:
        # Build a merged list newest-first, skipping duplicates
        flat = []
        for k, items in source_lists.items():
            flat.extend(items)
        flat.sort(key=lambda x: x["published"], reverse=True)

        for cand in flat:
            if len(chosen) >= 3:
                break
            if too_similar(cand, chosen):
                continue
            # also avoid exact same title
            if any(cand["title"].lower() == c["title"].lower() for c in chosen):
                continue
            chosen.append(cand)

    # LAST RESORT: if still fewer than 3 (very rare), allow anything newest-first
    if len(chosen) < 3:
        flat = []
        for k, items in source_lists.items():
            flat.extend(items)
        flat.sort(key=lambda x: x["published"], reverse=True)
        for cand in flat:
            if len(chosen) >= 3:
                break
            if any(cand["title"].lower() == c["title"].lower() for c in chosen):
                continue
            chosen.append(cand)

    return chosen[:3]



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
# WEATHER + SUNRISE/SUNSET (Cardiff)
# ----------------------------
def get_cardiff_weather():
    """
    Uses Open-Meteo (no key): current temp + apparent + daily high/low + sunrise/sunset.
    Returns dict with strings, or None on failure.
    """
    try:
        # Cardiff approx
        lat, lon = 51.4816, -3.1791
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,apparent_temperature"
            "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
            "&timezone=Europe%2FLondon"
        )
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        cur_t = data["current"]["temperature_2m"]
        cur_a = data["current"]["apparent_temperature"]

        hi = data["daily"]["temperature_2m_max"][0]
        lo = data["daily"]["temperature_2m_min"][0]

        sunrise_iso = data["daily"]["sunrise"][0]
        sunset_iso = data["daily"]["sunset"][0]

        # Format times as HH:MM
        sunrise = sunrise_iso.split("T")[1][:5]
        sunset = sunset_iso.split("T")[1][:5]

        return {
            "temp": f"{cur_t:.1f}°C",
            "feels": f"{cur_a:.1f}°C",
            "hi": f"{hi:.1f}°C",
            "lo": f"{lo:.1f}°C",
            "sunrise": sunrise,
            "sunset": sunset,
        }
    except Exception:
        return None


# ----------------------------
# WHO'S IN SPACE (whoisinspace.com)
# ----------------------------
def fetch_who_in_space():
    """
    Scrapes whoisinspace.com and returns list of (name, station_label).
    station_label should be 'ISS' or 'Tiangong' etc.
    """
    try:
        r = requests.get("https://whoisinspace.com/", timeout=12, headers={"User-Agent": "The2kTimes/1.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Their page structure: multiple <h2> sections in order.
        # Some h2 are "ISS ..." / "Tiangong ..." (mission/location headings),
        # and the following h2 elements are astronaut names.
        h2s = soup.find_all("h2")
        roster = []
        current_station = None

        def station_from_heading(t: str):
            tl = (t or "").lower()
            if "tiangong" in tl:
                return "Tiangong"
            if "iss" in tl:
                return "ISS"
            # fallback: first word-ish
            return t.strip()

        for h in h2s:
            txt = h.get_text(" ", strip=True)
            if not txt:
                continue

            # Heuristic: station headings usually contain ISS/Tiangong and often a dash/mission.
            if ("iss" in txt.lower() or "tiangong" in txt.lower()) and (" - " in txt or ":" in txt or len(txt) > 8):
                current_station = station_from_heading(txt)
                continue

            # Astronaut name headings: short-ish and no obvious "ISS/Tiangong" text inside
            if current_station and ("iss" not in txt.lower() and "tiangong" not in txt.lower()):
                # Avoid picking page headings like "Who is in space?"
                if "who is in space" in txt.lower():
                    continue
                # Names are usually 2-4 words
                if 2 <= len(txt.split()) <= 5:
                    roster.append((txt, current_station))

        # De-dupe preserving order
        seen = set()
        clean = []
        for name, station in roster:
            key = (name.lower(), station.lower())
            if key in seen:
                continue
            seen.add(key)
            clean.append((name, station))

        if not clean:
            raise ValueError("Empty roster")

        return clean
    except Exception:
        return None


# ----------------------------
# Pull World Headlines with 3 distinct sources
# ----------------------------
source_lists = {}
for src in WORLD_SOURCES:
    source_lists[src["key"]] = collect_articles(src["feeds"], src["key"], limit=12)

world_items = pick_three_distinct_sources(source_lists)

wx = get_cardiff_weather()
space_roster = fetch_who_in_space()

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

    # Prevent client “font boosting”
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

    def story_block(i, it, lead=False):
        headline_size = "18px"   # lead headline must match others per your requirement
        headline_weight = "900" if lead else "700"
        summary_size = "15px" if lead else "13.5px"
        summary_weight = "500" if lead else "400"
        pad_top = "22px" if lead else "16px"

        left_bar = "border-left:4px solid %s;padding-left:12px;" % ink if lead else ""

        kicker_row = ""
        if lead:
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
                             line-height:1.2;
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

    # World section
    world_html = ""
    if world_items:
        for i, it in enumerate(world_items, start=1):
            world_html += story_block(i, it, lead=(i == 1))
    else:
        world_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:18px 0;font-family:{font};color:{muted};font-size:14px;line-height:1.7;{size_fix_inline}">
              No qualifying world headlines in the last 24 hours.
            </td>
          </tr>
        </table>
        """

    # Sidebar blocks (must remain the same layout)
    inside_today_block = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr>
        <td style="font-family:{font};
                   font-size:12px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          🗞️ Inside Today
        </td>
      </tr>
      <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:15px !important;
                   font-weight:600 !important;
                   line-height:1.9;
                   color:{muted};
                   {size_fix_inline}">
          • UK Politics (0 stories)<br/>
          • Rugby Union (0 stories)<br/>
          • Punk Rock (0 stories)
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
    </table>
    """

    if wx:
        weather_line = f"{wx['temp']} (feels {wx['feels']}) · H {wx['hi']} / L {wx['lo']}"
        sunrise_line = f"Sunrise: <b>{wx['sunrise']}</b> &nbsp;·&nbsp; Sunset: <b>{wx['sunset']}</b>"
    else:
        weather_line = "Weather unavailable."
        sunrise_line = "Sunrise: --:-- · Sunset: --:--"

    weather_block = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr><td style="height:22px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:12px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          ⛅ Weather · Cardiff
        </td>
      </tr>
      <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr>
        <td style="font-family:{font};
                   font-size:15px !important;
                   font-weight:600 !important;
                   line-height:1.7;
                   color:{muted};
                   {size_fix_inline}">
          {weather_line}
        </td>
      </tr>

      <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:12px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          🌅 Sunrise / Sunset
        </td>
      </tr>
      <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr>
        <td style="font-family:{font};
                   font-size:15px !important;
                   font-weight:600 !important;
                   line-height:1.7;
                   color:{muted};
                   {size_fix_inline}">
          {sunrise_line}
        </td>
      </tr>
    </table>
    """

    if space_roster:
        # show ALL people
        people_lines = "<br/>".join([f"{esc(n)} ({esc(st)})" for n, st in space_roster])
        space_text = people_lines
    else:
        space_text = "Unable to load space roster."

    space_block = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

      <tr>
        <td style="font-family:{font};
                   font-size:12px !important;
                   font-weight:900 !important;
                   letter-spacing:2px;
                   text-transform:uppercase;
                   color:{ink};
                   {size_fix_inline}">
          🚀 Who&#39;s in Space
        </td>
      </tr>
      <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr>
        <td style="font-family:{font};
                   font-size:15px !important;
                   font-weight:600 !important;
                   line-height:1.5;
                   color:{muted};
                   {size_fix_inline}">
          {space_text}
        </td>
      </tr>
    </table>
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
                <td align="center" style="padding:28px 20px 14px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:46px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.05;
                                                {size_fix_inline}">
                        <span style="font-size:46px !important;font-weight:900 !important;">
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

              <!-- Single thin rule (keep the current look) -->
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- Section header -->
              <tr>
                <td style="padding:16px 20px 10px 20px;">
                  <span style="font-family:{font};
                               font-size:12px !important;
                               font-weight:900 !important;
                               letter-spacing:2px;
                               text-transform:uppercase;
                               color:{ink};">
                    🌍 World Headlines
                  </span>
                </td>
              </tr>

              <tr>
                <td style="padding:0 20px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <!-- Content columns -->
              <tr>
                <td style="padding:12px 20px 22px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <!-- Left column -->
                      <td class="stack colpadR" width="50%" valign="top" style="padding-right:12px;">
                        {world_html}
                      </td>

                      <!-- Divider -->
                      <td class="divider" width="1" style="background:{rule};"></td>

                      <!-- Right column -->
                      <td class="stack colpadL" width="50%" valign="top" style="padding-left:12px;">
                        {inside_today_block}
                        {weather_block}
                        {space_block}
                      </td>

                    </tr>
                  </table>
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

plain_lines.append("")
plain_lines.append("INSIDE TODAY")
plain_lines.append("• UK Politics (0 stories)")
plain_lines.append("• Rugby Union (0 stories)")
plain_lines.append("• Punk Rock (0 stories)")
plain_lines.append("")

if wx:
    plain_lines.append(f"WEATHER (CARDIFF): {wx['temp']} (feels {wx['feels']}) | H {wx['hi']} / L {wx['lo']}")
    plain_lines.append(f"SUNRISE/SUNSET: {wx['sunrise']} / {wx['sunset']}")
else:
    plain_lines.append("WEATHER (CARDIFF): Unavailable")
    plain_lines.append("SUNRISE/SUNSET: --:-- / --:--")

plain_lines.append("")
plain_lines.append("WHO'S IN SPACE")
if space_roster:
    for n, st in space_roster:
        plain_lines.append(f"- {n} ({st})")
else:
    plain_lines.append("Unable to load space roster.")

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
