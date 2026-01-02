import os
import re
from urllib.parse import urlparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

app = FastAPI()

# ----------------------------
# Allowlist (base domains)
# ----------------------------
DEFAULT_ALLOWED_BASES = [
    # World / general
    "bbc.co.uk",
    "reuters.com",
    "theguardian.com",

    # Rugby sources (as requested)
    "rugbypass.com",
    "planetrugby.com",
    "world.rugby",
    "rugby365.com",
    "rugbyworld.com",
    "skysports.com",
    "therugbypaper.co.uk",
    "ruck.co.uk",

    # Optional: if you end up using these
    "thetimes.co.uk",
    "ft.com",
]

def _env_list(name: str):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return []
    return [x.strip().lower() for x in raw.split(",") if x.strip()]

# You can override/extend these in Render via env var:
# READER_ALLOWED_BASES="bbc.co.uk,reuters.com,theguardian.com,...."
ALLOWED_BASES = _env_list("READER_ALLOWED_BASES") or DEFAULT_ALLOWED_BASES


def is_allowed(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        host = (u.hostname or "").lower()
        if not host:
            return False
        # allow exact base or any subdomain of base
        return any(host == base or host.endswith("." + base) for base in ALLOWED_BASES)
    except Exception:
        return False


# ----------------------------
# Fetch + readability-ish extract
# ----------------------------
UA = os.environ.get(
    "READER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

TIMEOUT_SECONDS = float(os.environ.get("READER_TIMEOUT", "12"))
MAX_CHARS = int(os.environ.get("READER_MAX_CHARS", "120000"))  # safety cap


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    r.raise_for_status()
    # Avoid extreme payloads
    text = r.text or ""
    return text[:MAX_CHARS]


def normalize_ws(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s or "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_into_paragraphs(text: str):
    """
    Turns a blob into nicer paragraphs.
    - Keeps blank lines
    - Also breaks on sentence boundaries when lines are too long (fallback)
    """
    text = normalize_ws(text)
    if not text:
        return []

    # Already paragraph-ish?
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) >= 4:
        return paras

    # Fallback: split by sentence groups to avoid giant single paragraph
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, buf = [], []
    char_count = 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        buf.append(s)
        char_count += len(s) + 1
        if char_count > 420:  # paragraph size target
            out.append(" ".join(buf).strip())
            buf, char_count = [], 0
    if buf:
        out.append(" ".join(buf).strip())
    return out


def extract_main_text(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # Title
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = title or "Reader"

    # Remove junk
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe", "form"]):
        tag.decompose()

    # Prefer article/main containers
    container = soup.find("article") or soup.find("main") or soup.body or soup

    # Remove typical non-content blocks inside container
    for selector in ["header", "footer", "nav", "aside"]:
        for t in container.find_all(selector):
            t.decompose()

    # Get text from paragraphs + headings + list items
    chunks = []
    for el in container.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = el.get_text(" ", strip=True)
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        # Drop obvious noise
        if len(txt) < 30 and txt.lower() in {"cookie", "cookies", "subscribe", "sign in"}:
            continue
        chunks.append(txt)

    # If extraction is weak, fall back to full container text
    if len(" ".join(chunks)) < 800:
        fallback = container.get_text("\n", strip=True)
        chunks = [line.strip() for line in fallback.split("\n") if len(line.strip()) > 40]

    text = "\n\n".join(chunks)
    text = normalize_ws(text)

    # Guard against runaway pages (e.g. huge nav text)
    text = text[:MAX_CHARS]
    return title, text


# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
def health():
    return JSONResponse({"ok": True})


@app.get("/read", response_class=HTMLResponse)
def read(url: str = Query(..., description="Article URL")):
    url = (url or "").strip()

    if not url:
        return PlainTextResponse("Missing url.", status_code=400)

    if not is_allowed(url):
        return PlainTextResponse("That domain is not allowed.", status_code=400)

    try:
        html = fetch_html(url)
        title, text = extract_main_text(html)
        paras = split_into_paragraphs(text)

        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        host = (urlparse(url).hostname or "").lower()

        # Build a clean “reader” page
        page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{escape_html(title)}</title>
  <style>
    :root {{
      --bg: #0f0f0f;
      --paper: #f7f5ef;
      --ink: #111111;
      --muted: #5a5a5a;
      --rule: #d7d2c7;
      --link: #0b57d0;
    }}
    body {{
      margin:0;
      background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: var(--ink);
    }}
    .wrap {{
      max-width: 880px;
      margin: 18px auto;
      padding: 0 14px 30px;
    }}
    .paper {{
      background: var(--paper);
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 10px 40px rgba(0,0,0,.35);
    }}
    .top {{
      padding: 18px 18px 10px;
      border-bottom: 1px solid var(--rule);
    }}
    .kicker {{
      font-size: 12px;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 800;
    }}
    h1 {{
      margin: 10px 0 8px;
      font-size: 28px;
      line-height: 1.15;
      font-weight: 900;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 13px;
      color: var(--muted);
      line-height: 1.4;
    }}
    .meta a {{
      color: var(--link);
      text-decoration: none;
      font-weight: 700;
    }}
    .content {{
      padding: 18px;
      font-size: 18px;
      line-height: 1.75;
    }}
    .content p {{
      margin: 0 0 16px 0;
    }}
    .content p:last-child {{
      margin-bottom: 0;
    }}
    .hr {{
      height: 1px;
      background: var(--rule);
      margin: 14px 0;
    }}
    @media (max-width: 640px) {{
      h1 {{ font-size: 24px; }}
      .content {{ font-size: 17px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="paper">
      <div class="top">
        <div class="kicker">The 2k Times Reader</div>
        <h1>{escape_html(title)}</h1>
        <div class="meta">
          <span><strong>Source:</strong> {escape_html(host)}</span>
          <span>·</span>
          <span><strong>Fetched:</strong> {escape_html(fetched_at)}</span>
          <span>·</span>
          <a href="{escape_attr(url)}" rel="noreferrer noopener" target="_blank">Open original ↗</a>
        </div>
      </div>
      <div class="content">
        {"".join(f"<p>{escape_html(p)}</p>" for p in paras) if paras else "<p>Could not extract readable text.</p>"}
      </div>
    </div>
  </div>
</body>
</html>
        """.strip()

        return HTMLResponse(page)

    except requests.HTTPError as e:
        return PlainTextResponse(f"Fetch failed: {str(e)}", status_code=502)
    except requests.RequestException as e:
        return PlainTextResponse(f"Fetch failed: {str(e)}", status_code=502)
    except Exception as e:
        return PlainTextResponse(f"Reader error: {str(e)}", status_code=500)


def escape_html(s: str) -> str:
    s = s or ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )

def escape_attr(s: str) -> str:
    # good enough for href attributes
    return escape_html(s).replace(" ", "%20")
