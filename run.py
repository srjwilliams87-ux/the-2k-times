import os
import re
import json
import smtplib
import urllib.request
import urllib.parse
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

# ----------------------------
# DEBUG / VERSION
# ----------------------------
TEMPLATE_VERSION = "v-newspaper-CORE-04"
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

# ----------------------------
# TIME WINDOW
# ----------------------------
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
# Requirement: 3 stories from 3 different sources among BBC/Reuters/Guardian/Independent
# ----------------------------
WORLD_SOURCES = [
    {"id": "bbc", "name": "BBC", "feeds": ["https://feeds.bbci.co.uk/news/world/rss.xml"]},
    {"id": "reuters", "name": "Reuters", "feeds": ["https://feeds.reuters.com/Reuters/worldNews"]},
    {"id": "guardian", "name": "The Guardian", "feeds": ["https://www.theguardian.com/world/rss"]},
    {"id": "independent", "name": "The Independent", "feeds": ["https://www.independent.co.uk/news/world/rss"]},
]

# ----------------------------
# HTTP helpers (no extra deps)
# ----------------------------
def http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "The-2k-Times/1.0 (+https://the-2k-times.onrender.com)",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def http_get_json(url: str, timeout: int = 15):
    return json.loads(http_get(url, timeout=timeout))

# ----------------------------
# HELPERS
# ----------------------------
def reader_link(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return f"{READER_BASE_URL}/read?url={urllib.parse.quote(url, safe='')}"


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
    return any(w in t for w in ["minute-by-minute", "as it happened", "watch live"])


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
# STRONG DEDUPE / TOPIC UNIQUENESS
# ----------------------------
STOPWORDS = {
    "the","a","an","and","or","but","to","of","in","on","for","with","by","as","at",
    "from","after","before","over","under","into","amid","says","say","said","new","latest",
    "update","updates","live","what","we","know","about","today","yesterday","tomorrow",
    "first","second","third","fourth","fifth","report","reports","reporting",
}

def normalize_words(s: str):
    s = (s or "").lower()
    s = re.sub(r"[\u2018\u2019]", "'", s)
    s = re.sub(r"[^a-z0-9\s'-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    out = []
    for w in s.split():
        w = w.strip("-'")
        if not w or w in STOPWORDS:
            continue
        # tiny stem
        w = re.sub(r"(ing|ed|es|s)$", "", w)
        if w and w not in STOPWORDS and len(w) > 2:
            out.append(w)
    return out


def jaccard_set(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def title_similarity(a: str, b: str) -> float:
    return jaccard_set(set(normalize_words(a)), set(normalize_words(b)))


def extract_entities_like(text: str) -> set:
    """
    Lightweight 'entity-ish' extractor from the ORIGINAL casing.
    Pulls capitalised tokens and 2-word capitalised phrases.
    """
    text = strip_html(text or "")
    # Capture words like "Venezuela", "Maduro", "Donald", "Trump", "Congress"
    tokens = re.findall(r"\b[A-Z][a-z]{2,}\b", text)
    # Also capture simple "Two Word" phrases: "Donald Trump"
    phrases = re.findall(r"\b([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b", text)
    ents = set(tokens + phrases)
    # Remove generic/section words
    bad = {"Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","January","February","March","April",
           "May","June","July","August","September","October","November","December"}
    ents = {e for e in ents if e not in bad}
    return ents


def topic_fingerprint(title: str, summary: str) -> set:
    """
    Topic fingerprint is mostly keywords from title+summary, plus entity-ish tokens.
    """
    kw = set(normalize_words(f"{title} {summary}"))
    ent = extract_entities_like(f"{title} {summary}")
    # convert entities to lower keyword-ish tokens too
    ent_kw = set(normalize_words(" ".join(ent)))
    return kw | ent_kw


def too_similar(candidate, chosen_items) -> bool:
    """
    Hard uniqueness rules:
    - Title similarity too high
    - Topic fingerprint overlap too high
    - Entity overlap too high
    """
    c_title = candidate["title"]
    c_sum = candidate["summary"]
    c_fp = topic_fingerprint(c_title, c_sum)
    c_ent = extract_entities_like(f"{c_title} {c_sum}")

    for it in chosen_items:
        t_title = it["title"]
        t_sum = it["summary"]

        # 1) title-level similarity
        if title_similarity(c_title, t_title) >= 0.55:
            return True

        # 2) topic fingerprint similarity
        t_fp = topic_fingerprint(t_title, t_sum)
        if jaccard_set(c_fp, t_fp) >= 0.40:
            return True

        # 3) entity overlap (prevents 3x Venezuela/Maduro/Trump)
        t_ent = extract_entities_like(f"{t_title} {t_sum}")
        if jaccard_set({e.lower() for e in c_ent}, {e.lower() for e in t_ent}) >= 0.30:
            return True

    return False

# ----------------------------
# COLLECT WORLD FEEDS
# ----------------------------
def collect_from_feeds(feed_urls, limit_per_feed=18):
    items = []
    for feed_url in feed_urls:
        feed = feedparser.parse(feed_url)
        for e in getattr(feed, "entries", [])[:limit_per_feed]:
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
            items.append(
                {
                    "title": title,
                    "summary": two_sentence_summary(summary_raw),
                    "url": link,
                    "reader": reader_link(link),
                    "published": published,
                }
            )

    # newest first
    items.sort(key=lambda x: x["published"], reverse=True)

    # de-dupe exact titles
    seen = set()
    uniq = []
    for it in items:
        k = it["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq


def pick_world_three_distinct_sources():
    # Collect candidates per source
    per_source = {}
    for src in WORLD_SOURCES:
        per_source[src["id"]] = collect_from_feeds(src["feeds"], limit_per_feed=20)

    # Flatten with source info
    candidates = []
    for src in WORLD_SOURCES:
        sid = src["id"]
        for it in per_source.get(sid, [])[:25]:
            candidates.append({**it, "source_id": sid, "source_name": src["name"]})

    # Global newest-first
    candidates.sort(key=lambda x: x["published"], reverse=True)

    chosen = []
    chosen_sources = set()

    # 1) pick the newest story overall (any source)
    for c in candidates:
        if c["source_id"] in chosen_sources:
            continue
        chosen.append(c)
        chosen_sources.add(c["source_id"])
        break

    if not chosen:
        return []

    # 2) pick remaining stories ensuring HARD uniqueness
    #    We *prefer* newest, but will walk back until unique.
    while len(chosen) < 3:
        needed_sources = [s["id"] for s in WORLD_SOURCES if s["id"] not in chosen_sources]

        best_pick = None
        for c in candidates:
            if c["source_id"] not in needed_sources:
                continue
            if too_similar(c, chosen):
                continue
            best_pick = c
            break

        if best_pick is None:
            # If we cannot find a unique story from the remaining sources,
            # relax by allowing any remaining source but still requiring uniqueness.
            for c in candidates:
                if c["source_id"] in chosen_sources:
                    continue
                if too_similar(c, chosen):
                    continue
                best_pick = c
                break

        if best_pick is None:
            # No unique stories found in window: stop.
            break

        chosen.append(best_pick)
        chosen_sources.add(best_pick["source_id"])

    return chosen


world_items = pick_world_three_distinct_sources()

# ----------------------------
# WEATHER (Cardiff) + sunrise/sunset (Open-Meteo)
# ----------------------------
CARDIFF_LAT = 51.4816
CARDIFF_LON = -3.1791


def get_cardiff_weather():
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={CARDIFF_LAT}&longitude={CARDIFF_LON}"
            "&current=temperature_2m,apparent_temperature"
            "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
            "&timezone=Europe%2FLondon"
        )
        data = http_get_json(url, timeout=15)
        cur = data.get("current", {}) or {}
        daily = data.get("daily", {}) or {}

        temp = cur.get("temperature_2m", None)
        feels = cur.get("apparent_temperature", None)

        tmax = daily.get("temperature_2m_max", [None])[0] if isinstance(daily.get("temperature_2m_max"), list) else None
        tmin = daily.get("temperature_2m_min", [None])[0] if isinstance(daily.get("temperature_2m_min"), list) else None
        sunrise = daily.get("sunrise", [None])[0] if isinstance(daily.get("sunrise"), list) else None
        sunset = daily.get("sunset", [None])[0] if isinstance(daily.get("sunset"), list) else None

        def hhmm(ts):
            if not ts:
                return "--:--"
            m = re.search(r"T(\d{2}:\d{2})", ts)
            return m.group(1) if m else "--:--"

        return {
            "ok": True,
            "temp_c": temp,
            "feels_c": feels,
            "hi_c": tmax,
            "lo_c": tmin,
            "sunrise": hhmm(sunrise),
            "sunset": hhmm(sunset),
        }
    except Exception:
        return {
            "ok": False,
            "temp_c": None,
            "feels_c": None,
            "hi_c": None,
            "lo_c": None,
            "sunrise": "--:--",
            "sunset": "--:--",
        }


wx = get_cardiff_weather()

# ----------------------------
# WHO'S IN SPACE (whoisinspace.com)
# ----------------------------
def get_space_roster():
    try:
        html = http_get("https://whoisinspace.com/", timeout=15)
        compact = re.sub(r"\s+", " ", html)

        blocks = re.findall(r"<h2[^>]*>(.*?)</h2>(.*?)(?=<h2[^>]*>|$)", compact, flags=re.IGNORECASE)

        roster = []
        for craft_title_raw, block in blocks:
            craft_title = strip_html(craft_title_raw)

            craft_tag = None
            if re.search(r"\bISS\b", craft_title, flags=re.IGNORECASE) or "International Space Station" in craft_title:
                craft_tag = "ISS"
            elif re.search(r"\bTiangong\b", craft_title, flags=re.IGNORECASE) or "China" in craft_title:
                craft_tag = "Tiangong"
            else:
                craft_tag = craft_title.strip() if craft_title.strip() else None

            if not craft_tag:
                continue

            names = re.findall(r"<h3[^>]*>(.*?)</h3>", block, flags=re.IGNORECASE)
            for n in names:
                name = strip_html(n).strip()
                if name and len(name) <= 80:
                    roster.append((name, craft_tag))

        seen = set()
        out = []
        for name, craft in roster:
            k = (name.lower(), craft.lower())
            if k in seen:
                continue
            seen.add(k)
            out.append((name, craft))

        return {"ok": True, "people": out}
    except Exception:
        return {"ok": False, "people": []}


space = get_space_roster()

# ----------------------------
# HTML (Newspaper) — matches your CORE layout
# ----------------------------
def build_html():
    outer_bg = "#111111"
    panel = "#1b1b1b"
    ink = "#f3f3f3"
    muted = "#cfcfcf"
    muted2 = "#9e9e9e"
    rule = "#2c2c2c"
    link = "#7aa2ff"
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

    def story_block(i, it, lead=False):
        headline_size = "34px" if lead else "26px"
        summary_size = "16px" if lead else "15px"
        left_bar = f"border-left:4px solid {rule};padding-left:14px;" if lead else ""
        kicker_row = ""
        if lead:
            kicker_row = f"""
            <tr>
              <td style="font-family:{font};font-size:13px;font-weight:900;letter-spacing:2px;
                         text-transform:uppercase;color:{muted2};padding:0 0 10px 0;{size_fix_inline}">
                TOP STORY
              </td>
            </tr>
            """

        src_row = ""
        if it.get("source_name"):
            src_row = f"""
            <tr>
              <td style="font-family:{font};font-size:12px;font-weight:700;letter-spacing:1px;
                         text-transform:uppercase;color:{muted2};padding:10px 0 0 0;{size_fix_inline}">
                {esc(it["source_name"])}
              </td>
            </tr>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr>
            <td style="{left_bar}{size_fix_inline}">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                {kicker_row}
                <tr>
                  <td style="font-family:{font};
                             font-size:{headline_size} !important;
                             font-weight:900 !important;
                             line-height:1.12;
                             color:{ink};
                             padding:0;
                             {size_fix_inline}">
                    <span style="font-size:{headline_size} !important;font-weight:900 !important;">
                      {i}. {esc(it['title'])}
                    </span>
                  </td>
                </tr>

                <tr><td style="height:12px;font-size:0;line-height:0;">&nbsp;</td></tr>

                <tr>
                  <td style="font-family:{font};
                             font-size:{summary_size} !important;
                             font-weight:500 !important;
                             line-height:1.6;
                             color:{muted};
                             padding:0;
                             {size_fix_inline}">
                    <span style="font-size:{summary_size} !important;font-weight:500 !important;">
                      {esc(it['summary'])}
                    </span>
                  </td>
                </tr>

                {src_row}

                <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

                <tr>
                  <td style="font-family:{font};
                             font-size:18px !important;
                             font-weight:700 !important;
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

          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    # World section
    if world_items:
        world_html = ""
        for i, it in enumerate(world_items, start=1):
            world_html += story_block(i, it, lead=(i == 1))
    else:
        world_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:18px 0;font-family:{font};color:{muted};font-size:15px;line-height:1.7;{size_fix_inline}">
              No qualifying world headlines in the last 24 hours.
            </td>
          </tr>
          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        """

    inside_today_counts = {"UK Politics": 0, "Rugby Union": 0, "Punk Rock": 0}

    def fmt_c(x):
        if x is None:
            return "--"
        try:
            return f"{float(x):.1f}°C"
        except Exception:
            return "--"

    wx_line = "—"
    if wx.get("ok"):
        wx_line = (
            f"{fmt_c(wx.get('temp_c'))} (feels {fmt_c(wx.get('feels_c'))}) · "
            f"H {fmt_c(wx.get('hi_c'))} / L {fmt_c(wx.get('lo_c'))}"
        )

    sunrise_str = wx.get("sunrise", "--:--")
    sunset_str = wx.get("sunset", "--:--")

    if space.get("ok") and space.get("people"):
        space_lines = "<br/>".join([f"{esc(n)} ({esc(c)})" for (n, c) in space["people"]])
    else:
        space_lines = "Unable to load space roster."

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
            <table class="container" width="760" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse;background:{panel};border-radius:18px;overflow:hidden;{size_fix_inline}">

              <tr>
                <td align="center" style="padding:34px 20px 14px 20px;{size_fix_inline}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:64px !important;
                                                font-weight:900 !important;
                                                color:{ink};
                                                line-height:1.02;
                                                {size_fix_inline}">
                        <span style="font-size:64px !important;font-weight:900 !important;">
                          The 2k Times
                        </span>
                      </td>
                    </tr>
                    <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                    <tr>
                      <td align="center" style="font-family:{font};
                                                font-size:16px !important;
                                                font-weight:700 !important;
                                                letter-spacing:2px;
                                                text-transform:uppercase;
                                                color:{muted2};
                                                {size_fix_inline}">
                        <span style="font-size:16px !important;font-weight:700 !important;">
                          {date_line} · Daily Edition · {TEMPLATE_VERSION}
                        </span>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <tr>
                <td style="padding:0 22px 16px 22px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <tr>
                <td style="padding:16px 22px 10px 22px;">
                  <span style="font-family:{font};
                               font-size:20px !important;
                               font-weight:800 !important;
                               color:{ink};">
                    🌍 World Headlines
                  </span>
                </td>
              </tr>

              <tr>
                <td style="padding:0 22px 0 22px;">
                  <div style="height:1px;background:{rule};"></div>
                </td>
              </tr>

              <tr>
                <td style="padding:14px 22px 22px 22px;">
                  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>

                      <td class="stack colpadR" width="62%" valign="top" style="padding-right:14px;">
                        {world_html}
                      </td>

                      <td class="divider" width="1" style="background:{rule};"></td>

                      <td class="stack colpadL" width="38%" valign="top" style="padding-left:14px;">
                        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">

                          <tr>
                            <td style="font-family:{font};
                                       font-size:20px !important;
                                       font-weight:800 !important;
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
                                       font-size:18px !important;
                                       font-weight:600 !important;
                                       line-height:1.7;
                                       color:{muted};
                                       {size_fix_inline}">
                              • UK Politics ({inside_today_counts["UK Politics"]} stories)<br/>
                              • Rugby Union ({inside_today_counts["Rugby Union"]} stories)<br/>
                              • Punk Rock ({inside_today_counts["Punk Rock"]} stories)
                            </td>
                          </tr>

                          <tr><td style="height:14px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:16px !important;
                                       font-weight:500;
                                       line-height:1.6;
                                       color:{muted2};
                                       {size_fix_inline}">
                              Curated from the last 24 hours.<br/>
                              Reader links included.
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:1px;background:{rule};font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:18px !important;
                                       font-weight:800 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              ⛅ Weather · Cardiff
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:18px !important;
                                       font-weight:600 !important;
                                       color:{muted};
                                       line-height:1.5;
                                       {size_fix_inline}">
                              {esc(wx_line)}
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:18px !important;
                                       font-weight:800 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              🌅 Sunrise / Sunset
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
                                       font-size:18px !important;
                                       font-weight:600 !important;
                                       color:{muted};
                                       line-height:1.5;
                                       {size_fix_inline}">
                              Sunrise: <span style="font-weight:900;color:{ink};">{esc(sunrise_str)}</span>
                              &nbsp;·&nbsp;
                              Sunset: <span style="font-weight:900;color:{ink};">{esc(sunset_str)}</span>
                            </td>
                          </tr>

                          <tr><td style="height:18px;font-size:0;line-height:0;">&nbsp;</td></tr>

                          <tr>
                            <td style="font-family:{font};
                                       font-size:18px !important;
                                       font-weight:800 !important;
                                       color:{ink};
                                       {size_fix_inline}">
                              🚀 Who&#39;s in Space
                            </td>
                          </tr>
                          <tr><td style="height:10px;font-size:0;line-height:0;">&nbsp;</td></tr>
                          <tr>
                            <td style="font-family:{font};
;
                                       font-size:18px !important;
                                       font-weight:600 !important;
                                       color:{muted};
                                       line-height:1.45;
                                       {size_fix_inline}">
                              {space_lines}
                            </td>
                          </tr>

                        </table>
                      </td>

                    </tr>
                  </table>
                </td>
              </tr>

              <tr>
                <td style="padding:18px;text-align:center;font-family:{font};
                           font-size:13px !important;color:{muted2};{size_fix_inline}">
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
# Plain text fallback (safe)
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
        src = it.get("source_name", "")
        src_part = f" ({src})" if src else ""
        plain_lines.append(f"{i}. {it['title']}{src_part}")
        plain_lines.append(it["summary"])
        plain_lines.append(f"Read in Reader: {it['reader']}")
        plain_lines.append("")

plain_lines += [
    "WEATHER · CARDIFF",
    "",
]

def fmt_c_plain(x):
    if x is None:
        return "--"
    try:
        return f"{float(x):.1f}°C"
    except Exception:
        return "--"

if wx.get("ok"):
    plain_lines.append(
        f"{fmt_c_plain(wx.get('temp_c'))} (feels {fmt_c_plain(wx.get('feels_c'))}) · "
        f"H {fmt_c_plain(wx.get('hi_c'))} / L {fmt_c_plain(wx.get('lo_c'))}"
    )
else:
    plain_lines.append("Unable to load weather.")

plain_lines += [
    "",
    "SUNRISE / SUNSET",
    "",
    f"Sunrise: {wx.get('sunrise','--:--')} · Sunset: {wx.get('sunset','--:--')}",
    "",
    "WHO'S IN SPACE",
    "",
]

if space.get("ok") and space.get("people"):
    for name, craft in space["people"]:
        plain_lines.append(f"{name} ({craft})")
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
print("World headlines:", len(world_items), "sources:", [x.get("source_id") for x in world_items])
print("Titles:", [x.get("title") for x in world_items])
print("Weather ok:", wx.get("ok"))
print("Space ok:", space.get("ok"), "count:", len(space.get("people", [])))
print("Reader base:", READER_BASE_URL)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)

print("Edition sent.")
