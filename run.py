#!/usr/bin/env python3
import os
import sys
import json
import html
import datetime as dt
from urllib.parse import quote_plus
import requests
import xml.etree.ElementTree as ET

# -----------------------------
# Config / helpers
# -----------------------------

CARDIFF_LAT = 51.4816
CARDIFF_LON = -3.1791
TIMEZONE = "Europe/London"


def now_local() -> dt.datetime:
    # Render runs in UTC; we want UK-local date display
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        return dt.datetime.utcnow()


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def reader_link(original_url: str) -> str:
    """
    Turns a story URL into your Reader URL if READER_BASE_URL is set.
    Example: https://your-reader.com/read?url=<encoded>
    """
    if not original_url:
        return ""
    base = (os.getenv("READER_BASE_URL", "") or "").rstrip("/")
    if not base:
        return original_url
    return f"{base}/read?url={quote_plus(original_url)}"


# -----------------------------
# World headlines (RSS)
# -----------------------------

DEFAULT_WORLD_FEEDS = [
    # BBC World
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    # BBC Top Stories (backup-ish)
    "https://feeds.bbci.co.uk/news/rss.xml",
]


def parse_rss_items(xml_text: str):
    """
    Returns list of dicts: {title, url, source, summary}
    """
    items = []
    root = ET.fromstring(xml_text)

    # RSS 2.0: channel/item
    channel = root.find("channel")
    if channel is not None:
        for it in channel.findall("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = (it.findtext("description") or "").strip()

            # try to strip some HTML-ish noise from RSS description
            summary = desc
            summary = summary.replace("<![CDATA[", "").replace("]]>", "")
            summary = summary.replace("\n", " ").strip()

            if title and link:
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "summary": summary,
                    }
                )
        return items

    # Atom: entry
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link = (link_el.get("href", "").strip() if link_el is not None else "")
        summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        if title and link:
            items.append({"title": title, "url": link, "summary": summary})
    return items


def fetch_world_stories(limit=3):
    """
    Pulls stories from WORLD_FEEDS (comma-separated) or defaults.
    """
    feeds_raw = os.getenv("WORLD_FEEDS", "").strip()
    feeds = [f.strip() for f in feeds_raw.split(",") if f.strip()] if feeds_raw else DEFAULT_WORLD_FEEDS

    seen = set()
    collected = []

    headers = {
        "User-Agent": "2k-times-bot/1.0 (+https://example.com) python-requests"
    }

    for feed_url in feeds:
        try:
            r = requests.get(feed_url, timeout=15, headers=headers)
            r.raise_for_status()
            items = parse_rss_items(r.text)

            # Infer source name
            source = "BBC" if "bbc.co.uk" in feed_url else "News"

            for it in items:
                key = it["url"]
                if key in seen:
                    continue
                seen.add(key)
                collected.append(
                    {
                        "source": source,
                        "title": it["title"],
                        "summary": it.get("summary", "") or "",
                        "url": it["url"],
                        "reader_url": reader_link(it["url"]),
                    }
                )
                if len(collected) >= limit:
                    return collected
        except Exception:
            continue

    return collected[:limit]


# -----------------------------
# Weather + sunrise/sunset (Open-Meteo)
# -----------------------------

def get_cardiff_weather_and_sun():
    """
    Uses Open-Meteo (no API key). Returns:
    weather: dict {temp, feels, hi, lo}
    sun: dict {sunrise, sunset} in local time strings HH:MM
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={CARDIFF_LAT}&longitude={CARDIFF_LON}"
        f"&current=temperature_2m,apparent_temperature"
        f"&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        f"&timezone={quote_plus(TIMEZONE)}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    cur = data.get("current", {}) or {}
    daily = data.get("daily", {}) or {}

    def fmt_c(x):
        try:
            return f"{float(x):.1f}°C"
        except Exception:
            return ""

    temp = fmt_c(cur.get("temperature_2m"))
    feels = fmt_c(cur.get("apparent_temperature"))

    hi = ""
    lo = ""
    if daily.get("temperature_2m_max"):
        hi = fmt_c(daily["temperature_2m_max"][0])
    if daily.get("temperature_2m_min"):
        lo = fmt_c(daily["temperature_2m_min"][0])

    sunrise = ""
    sunset = ""
    if daily.get("sunrise"):
        sunrise = (daily["sunrise"][0] or "").split("T")[-1][:5]
    if daily.get("sunset"):
        sunset = (daily["sunset"][0] or "").split("T")[-1][:5]

    weather = {"location": "Cardiff", "temp": temp, "feels": feels, "hi": hi, "lo": lo}
    sun = {"sunrise": sunrise, "sunset": sunset}
    return weather, sun


# -----------------------------
# Who's in space
# -----------------------------

def get_whos_in_space():
    """
    Mirror whoisinspace.com.
    Returns list[dict] with:
      - name: str
      - mission: str
      - launched_utc: datetime | None
    """
    import json
    import re
    import datetime as dt

    headers = {"User-Agent": "2k-times-bot/1.0"}

    def _parse_iso_dt(s: str):
        if not s:
            return None
        s = s.strip()
        # Normalise "Z" -> "+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            d = dt.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
        except Exception:
            return None

    # 1) Source of truth: whoisinspace.com page (Next.js)
    try:
        r = requests.get("https://whoisinspace.com/", timeout=20, headers=headers)
        r.raise_for_status()
        html = r.text

        # Next.js embeds JSON in a script tag with id="__NEXT_DATA__"
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise RuntimeError("whoisinspace.com: __NEXT_DATA__ not found")

        next_data = json.loads(m.group(1))

        # Walk the JSON and collect likely person entries.
        # We keep this flexible because the internal shape can change.
        people = []

        def walk(obj):
            if isinstance(obj, dict):
                # Heuristic: "name" plus something mission-like
                name = obj.get("name") or obj.get("person") or obj.get("astronaut")
                # mission keys vary
                mission = (
                    obj.get("mission")
                    or obj.get("expedition")
                    or obj.get("vehicle")
                    or obj.get("craft")
                    or obj.get("station")
                )
                # launch keys vary
                launched = (
                    obj.get("launched")
                    or obj.get("launchDate")
                    or obj.get("launch_date")
                    or obj.get("launch")
                    or obj.get("launchTime")
                    or obj.get("launch_time")
                )

                if isinstance(name, str) and name.strip():
                    # Mission might be nested or missing; keep it printable
                    mission_str = ""
                    if isinstance(mission, str):
                        mission_str = mission.strip()
                    elif isinstance(mission, dict):
                        # sometimes mission is {"name": "..."} etc.
                        mission_str = (mission.get("name") or mission.get("title") or "").strip()

                    launched_dt = None
                    if isinstance(launched, str):
                        launched_dt = _parse_iso_dt(launched)
                    elif isinstance(launched, (int, float)):
                        # sometimes epoch seconds/ms
                        ts = float(launched)
                        if ts > 10_000_000_000:  # ms
                            ts = ts / 1000.0
                        launched_dt = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)

                    # Only accept if it looks like a real roster entry:
                    # name + any mission/station/craft hint
                    if mission_str or launched_dt is not None or obj.get("country") or obj.get("agency"):
                        people.append(
                            {
                                "name": name.strip(),
                                "mission": mission_str or "Space mission",
                                "launched_utc": launched_dt,
                            }
                        )

                for v in obj.values():
                    walk(v)

            elif isinstance(obj, list):
                for it in obj:
                    walk(it)

        walk(next_data)

        # De-duplicate by name+mission
        seen = set()
        deduped = []
        for p in people:
            key = (p.get("name", ""), p.get("mission", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)

        if deduped:
            return deduped

        raise RuntimeError("whoisinspace.com: parsed but no people found")

    except Exception:
        pass

    # 2) Fallback: Open Notify (no mission/duration available)
    try:
        r = requests.get("http://api.open-notify.org/astros.json", timeout=15, headers=headers)
        r.raise_for_status()
        data = r.json() or {}
        out = []
        for p in data.get("people", []) or []:
            name = (p.get("name") or "").strip()
            craft = (p.get("craft") or "Space").strip()
            if name:
                out.append({"name": name, "mission": craft, "launched_utc": None})
        return out
    except Exception:
        return []



# -----------------------------
# HTML rendering (matches screenshot layout)
# -----------------------------

def render_story(story, idx: int) -> str:
    title = e(story.get("title"))
    source = e(story.get("source"))
    summary = e(story.get("summary"))

    # Prefer reader_url if present; else url
    raw_link = story.get("reader_url") or story.get("url") or ""
    link = e(raw_link)

    link_html = (
        f'<a href="{link}" style="color:#5aa2ff; text-decoration:none; font-weight:600;">Read in Reader →</a>'
        if raw_link else ""
    )

    # “Top story” styling for first card
    if idx == 1:
        return f"""
        <div style="padding:18px 0; border-bottom:1px solid rgba(255,255,255,0.08);">
          <div style="display:flex; gap:16px;">
            <div style="width:4px; background:#eaeaea; border-radius:2px; opacity:0.9;"></div>
            <div style="flex:1;">
              <div style="font-size:12px; letter-spacing:0.08em; text-transform:uppercase; opacity:0.75; margin-bottom:10px;">
                TOP STORY
              </div>
              <div style="font-size:22px; line-height:1.15; font-weight:800; margin:0 0 10px 0;">
                {idx}. {title}
              </div>
              <div style="font-size:14px; line-height:1.55; opacity:0.9; margin-bottom:12px;">
                {summary}
              </div>
              <div style="font-size:16px;">
                {link_html}
              </div>
            </div>
          </div>
        </div>
        """

    # Normal stories
    return f"""
    <div style="padding:18px 0; border-bottom:1px solid rgba(255,255,255,0.08);">
      <div style="font-size:20px; line-height:1.15; font-weight:800; margin:0 0 10px 0;">
        {idx}. {title}
      </div>
      <div style="font-size:14px; line-height:1.55; opacity:0.9; margin-bottom:12px;">
        {summary}
      </div>
      <div style="font-size:16px;">
        {link_html}
      </div>
    </div>
    """


def render_box(title: str, body_html: str) -> str:
    return f"""
    <div style="padding:16px 0; border-bottom:1px solid rgba(255,255,255,0.08);">
      <div style="font-size:16px; font-weight:800; margin-bottom:10px; display:flex; align-items:center; gap:10px;">
        {title}
      </div>
      <div style="font-size:14px; line-height:1.5; opacity:0.95;">
        {body_html}
      </div>
    </div>
    """


def build_email_html(world, weather, sun, people, edition_tag="v-newspaper-17") -> str:
    local = now_local()
    edition_line = f"{local.strftime('%d.%m.%Y')} · Daily Edition · {edition_tag}"

    # Left column: world headlines
    stories_html = "".join(render_story(s, i + 1) for i, s in enumerate(world[:3]))

    # Right column: inside today + weather + sunrise/sunset + space
    inside_html = """
      <div style="opacity:0.9; margin-bottom:10px;">Curated from the last 24 hours.<br/>Reader links included.</div>
    """

    # Weather
    w = weather or {}
    weather_line = f"{e(w.get('temp'))} (feels {e(w.get('feels'))}) · H {e(w.get('hi'))} / L {e(w.get('lo'))}"
    weather_html = f"""
      <div style="font-weight:700; margin-bottom:6px;">{e(w.get('location', 'Cardiff'))}</div>
      <div>{weather_line}</div>
    """

    # Sunrise/sunset
    s = sun or {}
    sun_html = f"""
      <div>Sunrise: <b>{e(s.get('sunrise'))}</b> &nbsp;·&nbsp; Sunset: <b>{e(s.get('sunset'))}</b></div>
    """

    # Who's in space (mirror whoisinspace.com: mission + time on mission)
if people:
    # people is expected to be list[dict] with: name, mission, launched_utc
    now = dt.datetime.now(dt.timezone.utc)

    def _fmt_duration(delta: dt.timedelta) -> str:
        total = int(delta.total_seconds())
        if total < 0:
            total = 0
        days = total // 86400
        hours = (total % 86400) // 3600
        mins = (total % 3600) // 60
        if days >= 1:
            return f"{days}d {hours:02d}h"
        if hours >= 1:
            return f"{hours}h {mins:02d}m"
        return f"{mins}m"

    rows = []
    for p in people:
        name = e(p.get("name", ""))
        mission = e(p.get("mission", ""))
        launched = p.get("launched_utc")

        dur = "—"
        if isinstance(launched, dt.datetime):
            if launched.tzinfo is None:
                launched = launched.replace(tzinfo=dt.timezone.utc)
            dur = _fmt_duration(now - launched)

        rows.append(f"<div style='margin:0 0 6px 0;'>{name} — {mission} — {dur}</div>")

    ppl_html = "".join(rows) if rows else "Unavailable right now."
else:
    ppl_html = "Unavailable right now."


    right_col = (
        render_box("🔎 Inside today", inside_html)
        + render_box("⛅ Weather · Cardiff", weather_html)
        + render_box("🌅 Sunrise / Sunset", sun_html)
        + render_box("🚀 Who's in space", ppl_html)
    )

    # Whole email
    html_out = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>The 2k Times</title>

<style>
/* Mobile stacking */
@media screen and (max-width: 600px) {{
  .col-stack {{
    display: block !important;
    width: 100% !important;
    max-width: 100% !important;
  }}

  .rule-vert {{
    display: none !important;
  }}

  .pad-reset {{
    padding-left: 0 !important;
    padding-right: 0 !important;
  }}
}}
</style>

</head>
<body style="margin:0; padding:0; background:#0f1115; color:#f2f2f2; font-family: Arial, Helvetica, sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#0f1115;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="680" cellspacing="0" cellpadding="0" border="0"
             style="max-width:680px; background:#15171c; border-radius:14px; border:1px solid rgba(255,255,255,0.08);">
        <tr>
          <td style="padding:26px 18px; font-size:14px; line-height:1.5;">
          
    <div style="text-align:center; margin-bottom:18px;">
      <div style="font-size:64px; font-weight:900; letter-spacing:-0.02em;">The 2k Times</div>
      <div style="margin-top:10px; font-size:18px; opacity:0.9;">{e(edition_line)}</div>
    </div>

    <div style="height:1px; background:rgba(255,255,255,0.12); margin:18px 0 22px;"></div>

    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
  <tr>
    <!-- Left column -->
    <td valign="top" width="64%"
    class="col-stack pad-reset"
    style="width:64%; padding-right:16px;">
  {stories_html}
</td>

    <!-- Vertical divider -->
    <td valign="top" width="1"
    class="rule-vert"
    style="width:1px; background:rgba(255,255,255,0.12); font-size:0; line-height:0;">
&nbsp;
</td>

    <!-- Right column -->
    <td valign="top" width="36%"
    class="col-stack pad-reset"
    style="width:36%; padding-left:16px;">
  {right_col}
</td>

    <div style="height:1px; background:rgba(255,255,255,0.12); margin:22px 0 14px;"></div>
    <div style="font-size:12px; opacity:0.65;">
      You’re receiving this because you subscribed to The 2k Times.
    </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""
    return html_out


# -----------------------------
# Mailgun
# -----------------------------

def send_mailgun(subject: str, html_body: str) -> bool:
    """
    Required env:
      MAILGUN_API_KEY
      MAILGUN_DOMAIN
      MAILGUN_FROM
      MAILGUN_TO
    """
    api_key = env_required("MAILGUN_API_KEY")
    domain = env_required("MAILGUN_DOMAIN")
    from_addr = env_required("MAILGUN_FROM")
    to_addr = env_required("MAILGUN_TO")

    url = f"https://api.mailgun.net/v3/{domain}/messages"
    auth = ("api", api_key)

    data = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": html_body,
    }

    r = requests.post(url, auth=auth, data=data, timeout=20)
    if r.status_code >= 400:
        print(f"Mailgun send failed: {r.status_code} {r.text}")
        return False
    return True


# -----------------------------
# Main
# -----------------------------

def main():
    print(">>> run.py starting")

    world = fetch_world_stories(limit=3)
    if not world:
        # Fail-safe so we never end up sending an empty email
        world = [{
            "source": "System",
            "title": "World headlines temporarily unavailable",
            "summary": "Your feeds didn’t return stories this run. Check WORLD_FEEDS or RSS availability.",
            "url": "",
            "reader_url": "",
        }]

    try:
        weather, sun = get_cardiff_weather_and_sun()
    except Exception:
        weather, sun = ({"location": "Cardiff", "temp": "", "feels": "", "hi": "", "lo": ""}, {"sunrise": "", "sunset": ""})

    people = get_whos_in_space()

    email_html = build_email_html(
        world=world,
        weather=weather,
        sun=sun,
        people=people,
        edition_tag=os.getenv("EDITION_TAG", "v-newspaper-17"),
    )

    # Debug prints (safe)
    print("World stories:")
    for s in world[:3]:
        print(f"- [{s.get('source','')}] {s.get('title','')}")
    print("EMAIL_HTML TYPE:", type(email_html))
    print("EMAIL_HTML LENGTH:", len(email_html) if isinstance(email_html, str) else 0)

    # Write preview in debug mode
    if bool_env("DEBUG_EMAIL", False):
        with open("email_preview.html", "w", encoding="utf-8") as f:
            f.write(email_html)
        print("Wrote email_preview.html")

    # Send / skip
    if bool_env("SEND_EMAIL", False):
        subject = f"The 2k Times · {now_local().strftime('%d.%m.%Y')}"
        ok = send_mailgun(subject, email_html)
        if not ok:
            print("Email failed (continuing — cron safe).")
    else:
        print("SEND_EMAIL=false — skipping send")

    print(">>> run.py finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        # Cron-safe: never crash the job hard
        print("Fatal error:", repr(ex))
        sys.exit(0)
