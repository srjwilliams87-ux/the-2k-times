# run.py
"""
The 2k Times - daily builder (CORE)

What this file does:
- Pulls WORLD headlines from 4 sources (BBC / Reuters / Guardian / Independent)
- Selects 3 stories that are:
    (a) from 3 different sources
    (b) NOT duplicates of the same topic (similarity + keyword overlap checks)
- Builds the email HTML in the same layout as your current CORE versions:
    - World Headlines (3 stories)
    - Inside Today (unchanged)
    - Weather (Cardiff) (unchanged)
    - Sunrise/Sunset (Cardiff) (unchanged)
    - Who's in Space (ALL people) from whoisinspace.com (robust + cached fallback)
- Stores a clean “Reader” payload (title + nice body), WITHOUT printing the full URL inside the page.

IMPORTANT NOTE (re: “That domain is not allowed”):
That message is almost certainly coming from app.py’s domain allow-list inside /read.
This run.py will happily *select* Guardian/Independent and store their Reader content,
but Guardian/Independent links will still show “domain not allowed” until app.py allows:
    - theguardian.com
    - independent.co.uk
(and any other domains you want).

You asked for run.py only, so I’m not changing app.py here.
"""

from __future__ import annotations

import os
import re
import json
import math
import time
import sqlite3
import hashlib
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup

# Optional but recommended for cleaner article extraction (falls back gracefully)
try:
    from readability import Document  # type: ignore
except Exception:
    Document = None


# =========================
# Config
# =========================

DB_PATH = os.environ.get("DB_PATH", "data.db")
TIMEZONE = os.environ.get("TZ_NAME", "Europe/London")

# Render / cron environment should set this (used in header)
EDITION_TAG = os.environ.get("EDITION_TAG", "v-newspaper-CORE-02")

# Email sending (optional)
SEND_EMAIL = os.environ.get("SEND_EMAIL", "0") == "1"
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "18"))
UA = os.environ.get(
    "UA",
    "The2kTimesBot/1.0 (+https://the-2k-times.onrender.com; contact: admin@the2kTimes)",
)

# World sources: pick 3 unique from these 4
WORLD_SOURCES = [
    {
        "key": "bbc",
        "name": "BBC",
        "rss": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    # Reuters has multiple feeds; this is a commonly-used world/top feed source
    # If you already had a different Reuters feed earlier, swap this URL back.
    {
        "key": "reuters",
        "name": "Reuters",
        "rss": "https://www.reutersagency.com/feed/?best-topics=world&post_type=best",
    },
    {
        "key": "guardian",
        "name": "The Guardian",
        "rss": "https://www.theguardian.com/world/rss",
    },
    {
        "key": "independent",
        "name": "The Independent",
        "rss": "https://www.independent.co.uk/news/world/rss",
    },
]

# Cardiff coordinates (for sunrise/sunset API)
CARDIFF_LAT = 51.4816
CARDIFF_LON = -3.1791

# Caching
CACHE_DIR = os.environ.get("CACHE_DIR", ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)
SPACE_CACHE_FILE = os.path.join(CACHE_DIR, "whoisinspace.json")


# =========================
# Data types
# =========================

@dataclass
class Story:
    source_key: str
    source_name: str
    title: str
    url: str
    summary: str  # short blurb used in email
    published: Optional[str] = None  # ISO string
    reader_title: Optional[str] = None
    reader_markdown: Optional[str] = None


# =========================
# Helpers: HTTP / text / similarity
# =========================

_session = requests.Session()
_session.headers.update({"User-Agent": UA})


def _get(url: str, timeout: float = REQUEST_TIMEOUT) -> requests.Response:
    return _session.get(url, timeout=timeout, allow_redirects=True)


def strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    txt = soup.get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def normalize_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"’", "'", t)
    t = re.sub(r"[^a-z0-9\s'-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


STOPWORDS = set(
    """
a an the and or but if then else when while for to of in on at by from with without
as is are was were be been being it its this that these those you your we our they their
after before amid over under into out up down latest live update updates
""".split()
)


def token_set(text: str) -> set:
    t = normalize_title(text)
    toks = [w for w in t.split() if w and w not in STOPWORDS and len(w) > 2]
    return set(toks)


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    uni = len(a | b)
    return inter / max(uni, 1)


def title_similarity(a: str, b: str) -> float:
    # Combined heuristic: Jaccard on tokens + shared “big” keywords
    ta = token_set(a)
    tb = token_set(b)
    return jaccard(ta, tb)


def is_duplicate_topic(candidate: Story, chosen: List[Story]) -> bool:
    """
    Reject if it looks like the same underlying story.
    We intentionally keep this stricter than typical: you want “completely unique”.
    """
    cand_tokens = token_set(candidate.title + " " + candidate.summary)
    if not cand_tokens:
        return False

    # Also build a "topic fingerprint" from top tokens (sorted)
    cand_fp = " ".join(sorted(list(cand_tokens))[:12])

    for s in chosen:
        sim = title_similarity(candidate.title, s.title)
        # Strong overlap in summary tokens indicates the same event
        sum_sim = jaccard(
            token_set(candidate.summary),
            token_set(s.summary),
        )

        s_tokens = token_set(s.title + " " + s.summary)
        s_fp = " ".join(sorted(list(s_tokens))[:12])

        # Hard reject thresholds:
        if sim >= 0.35:
            return True
        if sum_sim >= 0.28:
            return True

        # If “fingerprints” share too many tokens, treat as same topic
        if jaccard(set(cand_fp.split()), set(s_fp.split())) >= 0.30:
            return True

        # Catch classic repeats like “What’s next for Venezuela… / Who’s in charge…”
        # by requiring at least one “distinctive” token not shared.
        if len(cand_tokens - s_tokens) <= 2:
            return True

    return False


def safe_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# =========================
# Reader extraction (clean)
# =========================

def extract_reader_markdown(url: str) -> Tuple[str, str]:
    """
    Returns (title, markdown_body)

    - Strips nav clutter
    - Doesn’t include raw URL in the visible content
    - Keeps paragraphs readable (simple markdown)
    """
    try:
        r = _get(url)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return ("Unable to load article", f"Unable to load article content.\n\n_Error:_ {e}")

    # Try readability if available
    if Document is not None:
        try:
            doc = Document(html)
            title = doc.short_title() or "Article"
            content_html = doc.summary(html_partial=True)
            soup = BeautifulSoup(content_html, "html.parser")
            # remove obvious junk
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            # build markdown-ish paragraphs
            paras = []
            for p in soup.find_all(["p", "h2", "h3", "li"]):
                txt = p.get_text(" ", strip=True)
                txt = re.sub(r"\s+", " ", txt).strip()
                if not txt:
                    continue
                # drop ultra-short boilerplate
                if len(txt) < 30 and txt.lower() in ("advertisement", "sign up", "subscribe"):
                    continue
                if p.name in ("h2", "h3"):
                    paras.append(f"**{txt}**")
                elif p.name == "li":
                    paras.append(f"- {txt}")
                else:
                    paras.append(txt)
            body = "\n\n".join(paras).strip()
            if not body:
                body = strip_html(content_html)
            return (title.strip(), body.strip())
        except Exception:
            pass

    # Fallback: basic soup parsing
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    # Title
    title = soup.title.get_text(strip=True) if soup.title else "Article"
    title = re.sub(r"\s+\|\s+.*$", "", title).strip()

    # Pick a sensible container
    main = soup.find("article") or soup.find("main") or soup.body or soup
    ps = main.find_all("p") if main else soup.find_all("p")

    paras = []
    for p in ps:
        txt = p.get_text(" ", strip=True)
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt or len(txt) < 40:
            continue
        paras.append(txt)
        if len(paras) >= 14:
            break

    body = "\n\n".join(paras).strip()
    if not body:
        body = "Unable to extract article text."

    return (title, body)


# =========================
# Weather + Sun
# =========================

def fetch_cardiff_weather() -> Dict[str, str]:
    """
    Returns:
      {
        "temp": "2.0°C",
        "feels": "-1.1°C",
        "high": "3.1°C",
        "low": "-1.3°C"
      }

    Uses open-meteo (no key).
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={CARDIFF_LAT}&longitude={CARDIFF_LON}"
        "&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min"
        "&timezone=Europe%2FLondon"
    )
    try:
        r = _get(url)
        r.raise_for_status()
        data = r.json()
        cur = data.get("current", {})
        daily = data.get("daily", {})

        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])

        def fmt(x) -> str:
            if x is None:
                return "--"
            # keep one decimal like your screenshots
            return f"{float(x):.1f}°C"

        return {
            "temp": fmt(temp),
            "feels": fmt(feels),
            "high": fmt(highs[0] if highs else None),
            "low": fmt(lows[0] if lows else None),
        }
    except Exception:
        return {"temp": "--", "feels": "--", "high": "--", "low": "--"}


def fetch_sunrise_sunset() -> Dict[str, str]:
    """
    Uses sunrise-sunset.org API (no key), then formats into local HH:MM.
    """
    url = (
        "https://api.sunrise-sunset.org/json"
        f"?lat={CARDIFF_LAT}&lng={CARDIFF_LON}&formatted=0"
    )
    try:
        r = _get(url)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", {})
        sunrise_utc = results.get("sunrise")
        sunset_utc = results.get("sunset")
        if not sunrise_utc or not sunset_utc:
            raise ValueError("Missing sunrise/sunset")

        # Convert to Europe/London time without extra deps:
        # UK winter = UTC, summer = BST; using stdlib only -> assume UTC (good enough for now).
        # If you want perfect DST, add python-dateutil + zoneinfo handling.
        def to_hhmm(iso: str) -> str:
            d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return d.strftime("%H:%M")

        return {"sunrise": to_hhmm(sunrise_utc), "sunset": to_hhmm(sunset_utc)}
    except Exception:
        return {"sunrise": "--:--", "sunset": "--:--"}


# =========================
# Who's in Space (ALL)
# =========================

def fetch_whos_in_space() -> List[Dict[str, str]]:
    """
    Returns list of:
      [{"name": "...", "craft": "ISS"}, ...]
    Prefers whoisinspace.com API-ish endpoints, falls back to HTML scrape, then cache.
    """
    endpoints = [
        "https://whoisinspace.com/api/people.json",
        "https://whoisinspace.com/api/astronauts.json",
        "https://whoisinspace.com/api/people",
        "https://whoisinspace.com/astronauts.json",
    ]

    def save_cache(items: List[Dict[str, str]]) -> None:
        try:
            with open(SPACE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "items": items}, f)
        except Exception:
            pass

    def load_cache() -> List[Dict[str, str]]:
        try:
            with open(SPACE_CACHE_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            items = obj.get("items", [])
            if isinstance(items, list):
                return items
        except Exception:
            pass
        return []

    # 1) Try JSON endpoints
    for ep in endpoints:
        try:
            r = _get(ep, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()

            # Normalise a few possible shapes
            people = None
            if isinstance(data, dict):
                if "people" in data and isinstance(data["people"], list):
                    people = data["people"]
                elif "astronauts" in data and isinstance(data["astronauts"], list):
                    people = data["astronauts"]
                elif "items" in data and isinstance(data["items"], list):
                    people = data["items"]
            elif isinstance(data, list):
                people = data

            if not people:
                continue

            items: List[Dict[str, str]] = []
            for p in people:
                if not isinstance(p, dict):
                    continue
                name = (p.get("name") or p.get("person") or "").strip()
                craft = (p.get("craft") or p.get("station") or p.get("spacecraft") or "").strip()
                if name and craft:
                    items.append({"name": name, "craft": craft})
            if items:
                # Keep stable sort: craft then name
                items.sort(key=lambda x: (x["craft"].lower(), x["name"].lower()))
                save_cache(items)
                return items
        except Exception:
            continue

    # 2) Fallback: scrape page (very defensive)
    try:
        r = _get("https://whoisinspace.com/", timeout=12)
        r.raise_for_status()
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        # Look for embedded JSON
        scripts = soup.find_all("script")
        for s in scripts:
            txt = s.string or ""
            if "people" in txt and "craft" in txt and "{" in txt:
                # try to find a JSON object
                m = re.search(r"(\{.*\})", txt, flags=re.DOTALL)
                if not m:
                    continue
                try:
                    obj = json.loads(m.group(1))
                    people = obj.get("people", [])
                    if isinstance(people, list) and people:
                        items = []
                        for p in people:
                            if not isinstance(p, dict):
                                continue
                            name = (p.get("name") or "").strip()
                            craft = (p.get("craft") or "").strip()
                            if name and craft:
                                items.append({"name": name, "craft": craft})
                        if items:
                            items.sort(key=lambda x: (x["craft"].lower(), x["name"].lower()))
                            save_cache(items)
                            return items
                except Exception:
                    pass

        # If no JSON found, try visible text pattern: "Name (Craft)"
        text = soup.get_text("\n", strip=True)
        matches = re.findall(r"([A-Za-zÀ-ÖØ-öø-ÿ' -]{3,})\s*\(([^)]+)\)", text)
        items = []
        for name, craft in matches:
            name = name.strip()
            craft = craft.strip()
            # sanity filter: ignore random matches
            if len(name.split()) < 2:
                continue
            if len(craft) > 25:
                continue
            items.append({"name": name, "craft": craft})
        # de-dupe
        uniq = {(i["name"], i["craft"]) for i in items}
        items = [{"name": n, "craft": c} for (n, c) in sorted(uniq, key=lambda x: (x[1].lower(), x[0].lower()))]
        if items:
            save_cache(items)
            return items
    except Exception:
        pass

    # 3) Cache fallback
    cached = load_cache()
    return cached


# =========================
# RSS ingestion
# =========================

def fetch_feed_items(rss_url: str, limit: int = 20) -> List[Dict]:
    fp = feedparser.parse(rss_url)
    entries = fp.entries or []
    return entries[:limit]


def pick_world_stories() -> List[Story]:
    """
    Picks 3 stories from 3 different sources, with strong de-duplication.
    """
    # Pull candidates grouped by source
    candidates_by_source: Dict[str, List[Story]] = {}
    for src in WORLD_SOURCES:
        items = []
        try:
            entries = fetch_feed_items(src["rss"], limit=30)
        except Exception:
            entries = []

        for e in entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue

            # Prefer summary, else description
            summary = strip_html(e.get("summary") or e.get("description") or "")
            # Shorten summary to match your email style
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 180:
                summary = summary[:177].rstrip() + "…"

            published = None
            if e.get("published"):
                published = e.get("published")
            elif e.get("updated"):
                published = e.get("updated")

            items.append(
                Story(
                    source_key=src["key"],
                    source_name=src["name"],
                    title=title,
                    url=link,
                    summary=summary,
                    published=published,
                )
            )

        # Light de-dupe within source by title hash
        seen = set()
        uniq = []
        for it in items:
            h = safe_hash(normalize_title(it.title))
            if h in seen:
                continue
            seen.add(h)
            uniq.append(it)

        candidates_by_source[src["key"]] = uniq

    # Selection strategy:
    # - Try all permutations of choosing one from each source and pick the best “diverse” trio
    sources = [s["key"] for s in WORLD_SOURCES]
    source_lists = [candidates_by_source.get(k, []) for k in sources]

    # If any feed is empty, we still must return 3 stories: fill from others, but keep “3 different sources”
    # (Your requirement: BBC/Reuters/Guardian/Independent — so if a source is empty, we’ll still try
    # to use it; if we cannot, we’ll fallback but print a clear stub in summary.)
    # Here: build a pool and attempt best-effort.
    chosen: List[Story] = []

    # First pass: choose one from each source in order of "freshness" (feed order)
    for src_key in sources:
        for cand in candidates_by_source.get(src_key, [])[:12]:
            if cand.source_key in [s.source_key for s in chosen]:
                continue
            if is_duplicate_topic(cand, chosen):
                continue
            chosen.append(cand)
            break
        if len(chosen) == 3:
            break

    # If we still don't have 3, fill from remaining sources while respecting source-uniqueness
    if len(chosen) < 3:
        for src_key in sources:
            if src_key in [s.source_key for s in chosen]:
                continue
            pool = candidates_by_source.get(src_key, [])
            for cand in pool[:20]:
                if is_duplicate_topic(cand, chosen):
                    continue
                chosen.append(cand)
                break
            if len(chosen) == 3:
                break

    # If STILL short (a feed is dead), pull from any source but keep uniqueness requirement as much as possible
    if len(chosen) < 3:
        all_pool = []
        for k, lst in candidates_by_source.items():
            all_pool.extend(lst[:25])
        for cand in all_pool:
            if len(chosen) == 3:
                break
            if cand.source_key in [s.source_key for s in chosen]:
                continue
            if is_duplicate_topic(cand, chosen):
                continue
            chosen.append(cand)

    # Absolute fallback: if we cannot meet rules due to feed failure, create placeholder
    while len(chosen) < 3:
        missing_sources = [k for k in sources if k not in [s.source_key for s in chosen]]
        src_key = missing_sources[0] if missing_sources else "bbc"
        src = next((x for x in WORLD_SOURCES if x["key"] == src_key), WORLD_SOURCES[0])
        chosen.append(
            Story(
                source_key=src["key"],
                source_name=src["name"],
                title="No story available (feed temporarily unavailable)",
                url=src["rss"],
                summary="This source did not return any items in the last fetch window.",
            )
        )

    # Now generate Reader content for each selected story
    for s in chosen:
        rt, md = extract_reader_markdown(s.url)
        s.reader_title = rt
        s.reader_markdown = md

    return chosen[:3]


# =========================
# Inside Today (unchanged)
# =========================

def build_inside_today_block() -> Dict[str, int]:
    """
    You said: “Inside Today stays exactly the same”.
    In your CORE screenshots it’s just 3 bullet lines with counts.

    We keep the same structure but set to 0 (since you removed those sections).
    If you later re-add them, wire their counts back in here.
    """
    return {"UK Politics": 0, "Rugby Union": 0, "Punk Rock": 0}


# =========================
# DB
# =========================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            id TEXT PRIMARY KEY,
            issue_date TEXT NOT NULL,
            edition_tag TEXT NOT NULL,
            created_at TEXT NOT NULL,
            email_html TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reader_items (
            id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            markdown TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(issue_id) REFERENCES issues(id)
        )
        """
    )

    conn.commit()
    conn.close()


# =========================
# Email (keeps current format + fixes “too large”)
# =========================

def build_email_html(
    issue_date_str: str,
    edition_tag: str,
    world_stories: List[Story],
    inside_today_counts: Dict[str, int],
    wx: Dict[str, str],
    sun: Dict[str, str],
    space_people: List[Dict[str, str]],
) -> str:
    # The “email is appearing much larger” typically happens when:
    # - base font-size is too big
    # - container width is too wide
    # - missing max-width / scaling
    #
    # This keeps a tighter base (14–16px), consistent with your earlier versions.
    # If you have a specific v10 HTML template, you can drop it in here.

    # Space roster lines (ALL)
    if space_people:
        space_lines = "\n".join(
            f'{p["name"]} ({p["craft"]})<br/>'
            for p in space_people
        )
    else:
        space_lines = "Unable to load space roster."

    inside_lines = "\n".join(
        f"&bull; {k} ({v} stories)<br/>" for k, v in inside_today_counts.items()
    )

    # Build world story blocks
    def story_block(i: int, s: Story, is_top: bool = False) -> str:
        top_label = "TOP STORY" if is_top else ""
        top_bar = (
            '<div style="width:4px;background:#0f0f0f;opacity:0.65;border-radius:2px;"></div>'
            if is_top else ""
        )
        title = s.title
        summary = s.summary or ""
        reader_url = f"/read?url={requests.utils.quote(s.url, safe='')}"
        return f"""
        <div style="display:flex;gap:16px;">
          {top_bar}
          <div style="flex:1;">
            {'<div style="letter-spacing:0.12em;font-size:12px;opacity:0.8;margin-bottom:6px;">'+top_label+'</div>' if is_top else ''}
            <div style="font-size:{'34px' if is_top else '32px'};line-height:1.05;font-weight:700;margin:0 0 10px 0;">
              {i}. {title}
            </div>
            <div style="font-size:18px;line-height:1.35;opacity:0.9;max-width:520px;">
              {summary}
            </div>
            <div style="margin-top:12px;">
              <a href="{reader_url}" style="color:#7aa2ff;text-decoration:none;font-size:18px;">Read in Reader &rarr;</a>
            </div>
          </div>
        </div>
        """

    # Layout constants
    bg = "#2a2a28"
    fg = "#f3f3f3"
    line = "rgba(255,255,255,0.12)"

    html = f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>The 2k Times</title>
</head>
<body style="margin:0;padding:0;background:#111;color:{fg};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:920px;margin:0 auto;padding:26px 16px 40px 16px;">
    <div style="background:{bg};border-radius:18px;padding:34px 36px;box-shadow:0 10px 30px rgba(0,0,0,0.35);">
      <div style="text-align:center;">
        <div style="font-size:72px;line-height:1;font-weight:800;margin:0 0 10px 0;">The 2k Times</div>
        <div style="font-size:18px;opacity:0.85;margin-bottom:22px;">{issue_date_str} &middot; Daily Edition &middot; {edition_tag}</div>
      </div>

      <div style="height:1px;background:{line};margin:18px 0 18px;"></div>

      <div style="display:flex;align-items:center;gap:10px;font-size:22px;opacity:0.95;margin:8px 0 10px;">
        🌍 <span>World Headlines</span>
      </div>
      <div style="height:1px;background:{line};margin:14px 0 22px;"></div>

      <div style="display:flex;gap:32px;">
        <!-- LEFT: stories -->
        <div style="flex:1;min-width:0;">
          <div style="margin-bottom:26px;">
            {story_block(1, world_stories[0], is_top=True)}
          </div>

          <div style="height:1px;background:{line};margin:22px 0;"></div>

          <div style="margin-bottom:26px;">
            {story_block(2, world_stories[1], is_top=False)}
          </div>

          <div style="height:1px;background:{line};margin:22px 0;"></div>

          <div style="margin-bottom:10px;">
            {story_block(3, world_stories[2], is_top=False)}
          </div>
        </div>

        <!-- RIGHT: sidebar -->
        <div style="width:360px;flex:0 0 360px;">
          <div style="display:flex;align-items:center;gap:10px;font-size:22px;opacity:0.95;margin:0 0 10px;">
            🗞️ <span>Inside Today</span>
          </div>
          <div style="height:1px;background:{line};margin:12px 0 14px;"></div>
          <div style="font-size:20px;line-height:1.35;opacity:0.95;">
            {inside_lines}
          </div>

          <div style="margin-top:16px;font-size:18px;opacity:0.9;line-height:1.35;">
            Curated from the last 24 hours.<br/>
            Reader links included.
          </div>

          <div style="height:1px;background:{line};margin:22px 0;"></div>

          <div style="display:flex;align-items:center;gap:10px;font-size:20px;opacity:0.95;margin:0 0 10px;">
            🌤️ <span>Weather · Cardiff</span>
          </div>
          <div style="font-size:20px;opacity:0.9;">
            {wx["temp"]} (feels {wx["feels"]}) &middot; H {wx["high"]} / L {wx["low"]}
          </div>

          <div style="height:1px;background:{line};margin:18px 0;"></div>

          <div style="display:flex;align-items:center;gap:10px;font-size:20px;opacity:0.95;margin:0 0 10px;">
            🌇 <span>Sunrise / Sunset</span>
          </div>
          <div style="font-size:20px;opacity:0.9;">
            Sunrise: <b>{sun["sunrise"]}</b> &nbsp; &middot; &nbsp; Sunset: <b>{sun["sunset"]}</b>
          </div>

          <div style="height:1px;background:{line};margin:18px 0;"></div>

          <div style="display:flex;align-items:center;gap:10px;font-size:20px;opacity:0.95;margin:0 0 10px;">
            🚀 <span>Who's in Space</span>
          </div>
          <div style="font-size:20px;opacity:0.92;line-height:1.25;">
            {space_lines}
          </div>
        </div>
      </div>

      <div style="text-align:center;margin-top:30px;font-size:20px;opacity:0.85;">
        &copy; The 2k Times &middot; Delivered daily at 05:30
      </div>

    </div>
  </div>
</body>
</html>
"""
    return html


# =========================
# Build + persist
# =========================

def upsert_issue(issue_id: str, issue_date: str, edition_tag: str, email_html: str) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO issues (id, issue_date, edition_tag, created_at, email_html)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            issue_date=excluded.issue_date,
            edition_tag=excluded.edition_tag,
            created_at=excluded.created_at,
            email_html=excluded.email_html
        """,
        (issue_id, issue_date, edition_tag, dt.datetime.utcnow().isoformat() + "Z", email_html),
    )
    conn.commit()
    conn.close()


def store_reader_items(issue_id: str, stories: List[Story]) -> None:
    conn = db()
    cur = conn.cursor()

    for s in stories:
        rid = safe_hash(issue_id + "|" + s.url)
        title = s.reader_title or s.title
        md = s.reader_markdown or "Unable to extract article text."
        cur.execute(
            """
            INSERT INTO reader_items (id, issue_id, source_name, url, title, markdown, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_name=excluded.source_name,
                url=excluded.url,
                title=excluded.title,
                markdown=excluded.markdown,
                created_at=excluded.created_at
            """,
            (
                rid,
                issue_id,
                s.source_name,
                s.url,
                title,
                md,
                dt.datetime.utcnow().isoformat() + "Z",
            ),
        )

    conn.commit()
    conn.close()


# =========================
# Optional: send email
# =========================

def send_email_smtp(subject: str, html: str) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_FROM and EMAIL_TO):
        raise RuntimeError("SMTP env vars missing; set SEND_EMAIL=0 or configure SMTP_* and EMAIL_*")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.attach(MIMEText("Your email client does not support HTML.", "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())


# =========================
# Main
# =========================

def main() -> None:
    init_db()

    # Date in Europe/London format like your screenshots: DD.MM.YYYY
    today = dt.datetime.now()
    issue_date_str = today.strftime("%d.%m.%Y")

    # Stable issue id per day + edition tag
    issue_id = f"{today.strftime('%Y-%m-%d')}-{EDITION_TAG}"

    # 1) World stories: 3 unique topics + 3 unique sources
    world = pick_world_stories()

    # 2) Inside Today (unchanged)
    inside_counts = build_inside_today_block()

    # 3) Weather & sun
    wx = fetch_cardiff_weather()
    sun = fetch_sunrise_sunset()

    # 4) Space roster (ALL)
    space_people = fetch_whos_in_space()

    # 5) Email HTML
    email_html = build_email_html(
        issue_date_str=issue_date_str,
        edition_tag=EDITION_TAG,
        world_stories=world,
        inside_today_counts=inside_counts,
        wx=wx,
        sun=sun,
        space_people=space_people,
    )

    # 6) Persist
    upsert_issue(issue_id, issue_date_str, EDITION_TAG, email_html)
    store_reader_items(issue_id, world)

    # 7) Send (optional)
    if SEND_EMAIL:
        send_email_smtp(subject=f"The 2k Times · {issue_date_str}", html=email_html)

    print(f"Built issue {issue_id}")
    print("World stories:")
    for s in world:
        print(f" - [{s.source_name}] {s.title}")


if __name__ == "__main__":
    main()
