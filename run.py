import os
import re
import json
import time
import math
import hashlib
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple

import requests
import feedparser


# ----------------------------
# Config (Render env vars)
# ----------------------------
ALLOWED_DOMAINS = os.environ.get(
    "ALLOWED_DOMAINS",
    "bbc.co.uk,bbc.com,reuters.com,theguardian.com,independent.co.uk,whoisinspace.com"
)

READER_BASE_URL = os.environ.get("READER_BASE_URL", "").rstrip("/")
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip()
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "The 2k Times").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip()  # REQUIRED for Mailgun send
DEBUG_EMAIL = os.environ.get("DEBUG_EMAIL", "0") == "1"
SEND_EMAIL = os.environ.get("SEND_EMAIL", "0") == "1"
SEND_TIMEZONE = os.environ.get("SEND_TIMEZONE", "Europe/London").strip()

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "").strip()
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "").strip()

# Optional Mailgun SMTP envs (not used in this file; kept for compatibility)
MAILGUN_SMTP_USER = os.environ.get("MAILGUN_SMTP_USER", "").strip()
MAILGUN_SMTP_PASS = os.environ.get("MAILGUN_SMTP_PASS", "").strip()

REQUEST_TIMEOUT = 20
USER_AGENT = "The2kTimesBot/1.0 (+render)"

HEADERS = {"User-Agent": USER_AGENT}

ALLOWED_DOMAIN_SET = {d.strip().lower() for d in ALLOWED_DOMAINS.split(",") if d.strip()}


# ----------------------------
# Data models
# ----------------------------
@dataclass
class Story:
    title: str
    summary: str
    url: str
    source: str  # display label
    domain: str  # normalized domain
    published: Optional[str] = None


# ----------------------------
# Utilities
# ----------------------------
def now_utc_iso_z() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def norm_domain(url: str) -> str:
    url = url.strip()
    m = re.match(r"^https?://([^/]+)/?", url, re.I)
    host = (m.group(1) if m else "").lower()
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_allowed_domain(url: str) -> bool:
    d = norm_domain(url)
    # allow subdomains of allowed domains too
    for allowed in ALLOWED_DOMAIN_SET:
        if d == allowed or d.endswith("." + allowed):
            return True
    return False


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s


def content_fingerprint(story: Story) -> str:
    """
    Used for "no duplication" across the 3 world stories.
    Fingerprint based on normalized title+summary (first 200 chars).
    """
    base = (clean_text(story.title).lower() + " " + clean_text(story.summary).lower())[:300]
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def jaccard_sim(a: str, b: str) -> float:
    def toks(x: str) -> set:
        x = clean_text(x).lower()
        x = re.sub(r"[^a-z0-9 ]+", " ", x)
        parts = [p for p in x.split() if len(p) > 2]
        return set(parts)

    A = toks(a)
    B = toks(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def is_too_similar(candidate: Story, chosen: List[Story], thresh: float = 0.38) -> bool:
    """
    Prevent "same story 3 times" even from different sources.
    Checks similarity of title+summary.
    """
    cand_text = f"{candidate.title} {candidate.summary}"
    for s in chosen:
        s_text = f"{s.title} {s.summary}"
        if jaccard_sim(cand_text, s_text) >= thresh:
            return True
    return False


# ----------------------------
# RSS Sources (core world)
# ----------------------------
WORLD_SOURCES: List[Tuple[str, str]] = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Reuters", "https://www.reutersagency.com/feed/?best-topics=world&post_type=best"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("The Independent", "https://www.independent.co.uk/news/world/rss"),
]

# NOTE: Reuters RSS availability varies; we defensively handle failures.


def fetch_rss(source_name: str, feed_url: str, max_items: int = 20) -> List[Story]:
    parsed = feedparser.parse(feed_url)
    stories: List[Story] = []
    for e in parsed.entries[:max_items]:
        url = e.get("link", "") or ""
        if not url:
            continue
        title = clean_text(e.get("title", ""))
        # feedparser summary/detail handling
        summary = e.get("summary", "") or e.get("description", "") or ""
        summary = re.sub(r"<[^>]+>", "", summary)  # strip tags
        summary = clean_text(summary)
        published = e.get("published", None)

        domain = norm_domain(url)
        stories.append(
            Story(
                title=title or "(No title)",
                summary=summary or "",
                url=url,
                source=source_name,
                domain=domain,
                published=published,
            )
        )
    return stories


# ----------------------------
# Weather + sunrise/sunset
# (Uses Open-Meteo free endpoints)
# ----------------------------
def fetch_cardiff_weather() -> Dict[str, str]:
    """
    Returns dict with:
    temp, feels, high, low (C), sunrise, sunset (HH:MM)
    """
    # Cardiff approx
    lat, lon = 51.4816, -3.1791

    # Forecast (current + today max/min + apparent)
    # Open-Meteo:
    # https://open-meteo.com/
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        "&timezone=Europe%2FLondon"
    )
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    cur = data.get("current", {})
    daily = data.get("daily", {})

    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")

    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    sunrise_iso = (daily.get("sunrise") or [""])[0]
    sunset_iso = (daily.get("sunset") or [""])[0]

    def hhmm(iso: str) -> str:
        if not iso:
            return "--:--"
        # iso like "2026-01-04T08:17"
        return iso.split("T")[-1][:5]

    return {
        "temp": f"{temp:.1f}°C" if isinstance(temp, (int, float)) else "--°C",
        "feels": f"{feels:.1f}°C" if isinstance(feels, (int, float)) else "--°C",
        "high": f"{tmax:.1f}°C" if isinstance(tmax, (int, float)) else "--°C",
        "low": f"{tmin:.1f}°C" if isinstance(tmin, (int, float)) else "--°C",
        "sunrise": hhmm(sunrise_iso),
        "sunset": hhmm(sunset_iso),
    }


# ----------------------------
# Who's in space
# ----------------------------
def fetch_whos_in_space() -> List[Tuple[str, str]]:
    """
    Returns list of (name, craft/station).
    Uses https://whoisinspace.com/ (as requested).
    We'll scrape the JSON if present; otherwise parse HTML.
    """
    url = "https://whoisinspace.com/"
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html = r.text

    # Look for a JSON blob commonly embedded in a script tag
    # We'll try a few patterns.
    json_candidates: List[str] = []

    # pattern 1: window.__NUXT__=...
    m = re.search(r"window\.__NUXT__\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if m:
        json_candidates.append(m.group(1))

    # pattern 2: application/ld+json or application/json with roster-like structure
    for m2 in re.finditer(r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', html, re.S):
        json_candidates.append(m2.group(1).strip())

    # Try parsing candidates
    for blob in json_candidates:
        try:
            data = json.loads(blob)
        except Exception:
            continue

        # Heuristic: find list of people with name + craft/station
        found = []

        def walk(x):
            if isinstance(x, dict):
                # possible keys
                if "people" in x and isinstance(x["people"], list):
                    return x["people"]
                for v in x.values():
                    res = walk(v)
                    if res:
                        return res
            elif isinstance(x, list):
                for item in x:
                    res = walk(item)
                    if res:
                        return res
            return None

        people = walk(data)
        if isinstance(people, list):
            for p in people:
                if not isinstance(p, dict):
                    continue
                name = p.get("name") or p.get("person") or p.get("title")
                craft = p.get("craft") or p.get("station") or p.get("location")
                if name and craft:
                    found.append((clean_text(str(name)), clean_text(str(craft))))
            if found:
                return found

    # Fallback: HTML parse (simple)
    # Look for lines like: <span class="name">X</span> ... <span class="craft">ISS</span>
    people: List[Tuple[str, str]] = []
    # This is intentionally loose.
    rows = re.findall(r"(ISS|Tiangong|Shenzhou|Crew Dragon|Starliner|Soyuz|Axiom|CSS)", html, re.I)
    # If no obvious, attempt to parse visible text blocks:
    # We'll extract candidate "Name (Station)" patterns already shown in your UI:
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"\n{2,}", "\n", text)
    for line in text.splitlines():
        line = clean_text(line)
        if not line or len(line) > 90:
            continue
        m3 = re.match(r"^([A-Z][A-Za-z \-']+)\s+\(([^)]+)\)$", line)
        if m3:
            people.append((m3.group(1), m3.group(2)))
    # If still empty, return empty and handle gracefully in HTML
    return people


# ----------------------------
# Email HTML builder (keeps current look)
# ----------------------------
def build_email_html(
    issue_date_str: str,
    edition_tag: str,
    world: List[Story],
    inside_today_counts: Dict[str, int],
    weather: Dict[str, str],
    space_people: List[Tuple[str, str]],
) -> str:
    """
    Keep the same style as your CORE templates:
    - Dark background, two columns
    - World Headlines (3 stories)
    - Inside Today + Weather + Sunrise/Sunset + Who's in Space unchanged
    """
    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    # Right column sections must stay exactly the same text/format
    inside_lines = "\n".join(
        [
            f"<div class='bullet'>• {esc(k)} ({v} stories)</div>"
            for k, v in inside_today_counts.items()
        ]
    )

    space_lines = ""
    if space_people:
        for name, craft in space_people:
            space_lines += f"<div class='space-line'>{esc(name)} ({esc(craft)})</div>"
    else:
        space_lines = "<div class='muted'>Unable to load space roster.</div>"

    # World stories
    world_blocks = []
    for idx, s in enumerate(world, start=1):
        world_blocks.append(
            f"""
            <div class="story {'top' if idx==1 else ''}">
              {"<div class='top-label'>TOP STORY</div>" if idx==1 else ""}
              <div class="story-title">{idx}. {esc(s.title)}</div>
              <div class="story-dek">{esc(s.summary)}</div>
              <a class="reader-link" href="{esc(READER_BASE_URL)}/read?url={esc(s.url)}">Read in Reader →</a>
            </div>
            """
        )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>The 2k Times</title>
<style>
  body {{
    margin: 0;
    background: #0f0f10;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #f2f2f2;
  }}
  .wrap {{
    max-width: 920px;
    margin: 0 auto;
    padding: 28px 18px 40px;
  }}
  .mast {{
    text-align: center;
    padding: 22px 0 10px;
  }}
  .brand {{
    font-size: 64px;
    font-weight: 800;
    letter-spacing: 0.5px;
  }}
  .meta {{
    margin-top: 8px;
    opacity: 0.9;
    font-weight: 600;
  }}
  .rule {{
    margin: 18px 0 22px;
    height: 1px;
    background: rgba(255,255,255,0.12);
  }}
  .section-title {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 22px;
    font-weight: 700;
    padding: 12px 0;
    opacity: 0.95;
  }}
  .grid {{
    display: grid;
    grid-template-columns: 1.35fr 1fr;
    gap: 26px;
  }}
  .card {{
    background: rgba(255,255,255,0.02);
    border-top: 1px solid rgba(255,255,255,0.10);
    border-bottom: 1px solid rgba(255,255,255,0.10);
  }}
  .story {{
    padding: 18px 0 18px;
    border-top: 1px solid rgba(255,255,255,0.10);
  }}
  .story:first-child {{
    border-top: none;
  }}
  .top {{
    padding-left: 18px;
    border-left: 4px solid rgba(255,255,255,0.9);
  }}
  .top-label {{
    font-size: 14px;
    letter-spacing: 1px;
    opacity: 0.85;
    margin-bottom: 6px;
  }}
  .story-title {{
    font-size: 34px;
    font-weight: 900;
    line-height: 1.05;
    margin-bottom: 10px;
  }}
  .story-dek {{
    font-size: 18px;
    line-height: 1.35;
    opacity: 0.92;
    max-width: 520px;
  }}
  .reader-link {{
    display: inline-block;
    margin-top: 14px;
    color: #77a7ff;
    text-decoration: none;
    font-weight: 700;
  }}
  .right {{
    padding-top: 10px;
  }}
  .right .box {{
    padding: 14px 0 14px;
    border-top: 1px solid rgba(255,255,255,0.10);
  }}
  .right .box:first-child {{
    border-top: none;
  }}
  .box-title {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 800;
    font-size: 20px;
    margin-bottom: 10px;
    opacity: 0.95;
  }}
  .bullet {{
    font-size: 18px;
    margin: 6px 0;
    opacity: 0.92;
  }}
  .muted {{
    opacity: 0.85;
    font-size: 18px;
    line-height: 1.35;
  }}
  .space-line {{
    font-size: 18px;
    margin: 4px 0;
    opacity: 0.92;
  }}
  .footer {{
    text-align: center;
    margin-top: 26px;
    opacity: 0.9;
    font-weight: 700;
  }}
  @media (max-width: 820px) {{
    .grid {{ grid-template-columns: 1fr; }}
    .brand {{ font-size: 48px; }}
    .story-title {{ font-size: 30px; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="mast">
      <div class="brand">The 2k Times</div>
      <div class="meta">{esc(issue_date_str)} · Daily Edition · {esc(edition_tag)}</div>
    </div>

    <div class="rule"></div>

    <div class="section-title">🌍 World Headlines</div>
    <div class="rule" style="margin-top:0;"></div>

    <div class="grid">
      <div class="card">
        {''.join(world_blocks)}
      </div>

      <div class="right">
        <div class="box">
          <div class="box-title">🗞️ Inside Today</div>
          {inside_lines}
          <div class="muted" style="margin-top:12px;">
            Curated from the last 24 hours.<br/>Reader links included.
          </div>
        </div>

        <div class="box">
          <div class="box-title">⛅ Weather · Cardiff</div>
          <div class="bullet">{esc(weather['temp'])} (feels {esc(weather['feels'])}) · H {esc(weather['high'])} / L {esc(weather['low'])}</div>
        </div>

        <div class="box">
          <div class="box-title">🌅 Sunrise / Sunset</div>
          <div class="bullet">Sunrise: <strong>{esc(weather['sunrise'])}</strong> &nbsp;&nbsp;•&nbsp;&nbsp; Sunset: <strong>{esc(weather['sunset'])}</strong></div>
        </div>

        <div class="box">
          <div class="box-title">🚀 Who's in Space</div>
          {space_lines}
        </div>
      </div>
    </div>

    <div class="footer">© The 2k Times · Delivered daily at 05:30</div>
  </div>
</body>
</html>
"""


# ----------------------------
# Reader fetch (server-side cleaner)
# ----------------------------
def fetch_article_text(url: str) -> Tuple[str, str]:
    """
    Fetches the original article URL and extracts a readable title + plain paragraphs.
    Keeps output clean: NO "ORIGINAL: <url>" line.
    """
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html = r.text

    # Basic title extraction
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = clean_text(re.sub(r"<[^>]+>", "", m.group(1)))

    # Crude paragraph extraction
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.I | re.S)
    cleaned = []
    for p in paras:
        p = re.sub(r"<[^>]+>", "", p)
        p = clean_text(p)
        if p and len(p) > 40:
            cleaned.append(p)
    body = "\n\n".join(cleaned[:18])  # keep it concise

    if not title:
        title = "Reader"

    if not body:
        body = "Unable to extract article text cleanly."

    return title, body


# ----------------------------
# Mailgun send
# ----------------------------
def send_mailgun(subject: str, html: str) -> None:
    if not MAILGUN_DOMAIN or not MAILGUN_API_KEY:
        raise RuntimeError("Mailgun is not configured (MAILGUN_DOMAIN / MAILGUN_API_KEY missing).")
    if not EMAIL_FROM:
        raise RuntimeError("EMAIL_FROM is required (e.g. news@YOUR_MAILGUN_DOMAIN).")
    if not EMAIL_TO:
        raise RuntimeError("EMAIL_TO is required.")

    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    auth = ("api", MAILGUN_API_KEY)

    data = {
        "from": f"{EMAIL_FROM_NAME} <{EMAIL_FROM}>",
        "to": [EMAIL_TO],
        "subject": subject,
        "html": html,
    }

    resp = requests.post(url, auth=auth, data=data, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f"Mailgun send failed: {resp.status_code} {resp.text}")


# ----------------------------
# Main: build issue
# ----------------------------
def select_world_stories() -> List[Story]:
    """
    Requirements:
    - 3 stories
    - Prefer 3 different sources
    - No duplication (content uniqueness)
    - Allowed domains only (for Reader proxy)
    """
    pool_by_source: Dict[str, List[Story]] = {}

    for src, feed_url in WORLD_SOURCES:
        try:
            stories = fetch_rss(src, feed_url, max_items=30)
            # keep only allowed domains
            stories = [s for s in stories if is_allowed_domain(s.url)]
            pool_by_source[src] = stories
        except Exception as e:
            print(f"RSS fetch failed for {src}: {e}")
            pool_by_source[src] = []

    chosen: List[Story] = []
    used_sources: set = set()
    used_fps: set = set()

    # First pass: try to pick 1 from each unique source
    for src in [s[0] for s in WORLD_SOURCES]:
        for cand in pool_by_source.get(src, []):
            fp = content_fingerprint(cand)
            if fp in used_fps:
                continue
            if is_too_similar(cand, chosen):
                continue
            chosen.append(cand)
            used_sources.add(src)
            used_fps.add(fp)
            break
        if len(chosen) == 3:
            break

    # Second pass: fill remaining from any source, still unique
    if len(chosen) < 3:
        all_stories = []
        for lst in pool_by_source.values():
            all_stories.extend(lst)

        for cand in all_stories:
            fp = content_fingerprint(cand)
            if fp in used_fps:
                continue
            if is_too_similar(cand, chosen):
                continue
            chosen.append(cand)
            used_fps.add(fp)
            if len(chosen) == 3:
                break

    # If still short, just take first allowed stories (last resort)
    if len(chosen) < 3:
        for src in pool_by_source:
            for cand in pool_by_source[src]:
                if cand not in chosen:
                    chosen.append(cand)
                    if len(chosen) == 3:
                        break
            if len(chosen) == 3:
                break

    return chosen[:3]


def main():
    # Date string for masthead (dd.mm.yyyy)
    today = dt.datetime.now(dt.timezone.utc).astimezone().date()
    issue_date_str = today.strftime("%d.%m.%Y")

    edition_tag = "v-newspaper-CORE-02"

    # Inside Today must stay exactly the same format/logic for now
    inside_today_counts = {
        "UK Politics": 0,
        "Rugby Union": 0,
        "Punk Rock": 0,
    }

    # World stories
    world = select_world_stories()

    print("World stories:")
    for s in world:
        print(f" - [{s.source}] {s.title}")

    # Weather + sunrise/sunset
    try:
        weather = fetch_cardiff_weather()
    except Exception as e:
        print(f"Weather fetch failed: {e}")
        weather = {"temp":"--°C","feels":"--°C","high":"--°C","low":"--°C","sunrise":"--:--","sunset":"--:--"}

    # Who's in space
    try:
        space_people = fetch_whos_in_space()
    except Exception as e:
        print(f"Space roster fetch failed: {e}")
        space_people = []

    email_html = build_email_html(
        issue_date_str=issue_date_str,
        edition_tag=edition_tag,
        world=world,
        inside_today_counts=inside_today_counts,
        weather=weather,
        space_people=space_people,
    )

    # Persist output (optional)
    out_dir = os.path.join(os.getcwd(), "out")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{issue_date_str}-{edition_tag}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(email_html)
    print(f"Built issue {issue_date_str}-{edition_tag}")

    # Send email via Mailgun
    if SEND_EMAIL:
        subj = f"The 2k Times · {issue_date_str}"
        print(f"Sending email to {EMAIL_TO} via Mailgun domain {MAILGUN_DOMAIN} ...")
        send_mailgun(subject=subj, html=email_html)
        print("Email sent.")
    else:
        print("SEND_EMAIL is disabled; skipping send.")

    # Optional debug: save a minimal preview pointer
    if DEBUG_EMAIL:
        print(f"Saved HTML to {out_path}")


if __name__ == "__main__":
    main()
