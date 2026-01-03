import os
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response, jsonify

app = Flask(__name__)

# Default allowlist (override with env var ALLOWED_DOMAINS, comma-separated)
DEFAULT_ALLOWED = [
    "bbc.co.uk",
    "reuters.com",
    "theguardian.com",
    "rugbyworld.com",
    "rugbypass.com",
    "planetrugby.com",
    "world.rugby",
    "skysports.com",
    "ruck.co.uk",
    "therugbypaper.co.uk",
    "kerrang.com",
    "punknews.org",
]

ENV_ALLOWED = os.environ.get("ALLOWED_DOMAINS", "").strip()
if ENV_ALLOWED:
    ALLOWED_DOMAINS = [d.strip().lower() for d in ENV_ALLOWED.split(",") if d.strip()]
else:
    ALLOWED_DOMAINS = DEFAULT_ALLOWED


def _host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _reader_url(original_url: str) -> str:
    # Jina "reader" endpoint
    original_url = _normalize_url(original_url)
    return "https://r.jina.ai/" + original_url


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")

    # Remove junk
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    # Prefer <article> if present
    article = soup.find("article")
    if article:
        text = article.get_text("\n")
    else:
        # fallback: body text
        body = soup.find("body") or soup
        text = body.get_text("\n")

    # Normalize whitespace / keep paragraphs
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _clean_text_to_html(text: str) -> str:
    """
    Convert extracted text into readable HTML:
    - basic headings detection
    - paragraphs
    """
    text = (text or "").strip()
    if not text:
        return "<p>Unable to extract readable text.</p>"

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    paragraphs = []
    buff = []

    def flush():
        nonlocal buff
        if buff:
            paragraphs.append(" ".join(buff).strip())
            buff = []

    for ln in lines:
        # Heading-ish
        if len(ln) <= 80 and (ln.isupper() or re.match(r"^[A-Z][A-Za-z0-9 ,:'\"’\-–—]{10,}$", ln)):
            # Only treat as heading if it's not a normal sentence line
            if not ln.endswith((".", "!", "?", "…")) and len(ln) <= 70:
                flush()
                paragraphs.append(f"## {ln}")
                continue

        buff.append(ln)

        # Break up overly-long paragraphs
        if ln.endswith((".", "!", "?", "…")) and len(" ".join(buff)) > 260:
            flush()

    flush()

    out = []
    for p in paragraphs:
        if p.startswith("## "):
            out.append(f"<h2>{_esc(p[3:])}</h2>")
        else:
            out.append(f"<p>{_esc(p)}</p>")

    return "\n".join(out)


def _render_page(original_url: str, body_html: str, note: str = "") -> str:
    note_html = f'<div class="note">{_esc(note)}</div>' if note else ""
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Reader</title>
      <style>
        :root {{
          --bg:#0f0f10;
          --paper:#ffffff;
          --ink:#121212;
          --muted:#555;
          --rule:#e5e5e5;
          --link:#0b57d0;
        }}
        body {{
          margin:0;
          background:var(--bg);
          font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
        }}
        .wrap {{
          max-width:900px;
          margin:18px auto;
          padding:18px;
        }}
        .card {{
          background:var(--paper);
          color:var(--ink);
          border-radius:14px;
          padding:22px;
        }}
        a {{ color: var(--link); }}
        h1 {{
          margin:0 0 10px 0;
          font-size:22px;
        }}
        h2 {{
          margin:22px 0 10px 0;
          font-size:18px;
          line-height:1.25;
        }}
        p {{
          margin:0 0 14px 0;
          font-size:16px;
          line-height:1.75;
        }}
        .meta {{
          margin:0 0 18px 0;
          font-size:13px;
          color:var(--muted);
        }}
        .rule {{
          height:1px;
          background:var(--rule);
          margin:14px 0 14px 0;
        }}
        .note {{
          margin:0 0 14px 0;
          padding:10px 12px;
          background:#fff7d6;
          border:1px solid #f1d27a;
          border-radius:10px;
          color:#553b00;
          font-size:13px;
          line-height:1.5;
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h1>Reader</h1>
          <div class="meta">
            Original: <a href="{_esc(original_url)}">{_esc(original_url)}</a>
          </div>
          {note_html}
          <div class="rule"></div>
          {body_html}
        </div>
      </div>
    </body>
    </html>
    """


@app.get("/health")
def health():
    return jsonify({"ok": True, "allowed_domains": ALLOWED_DOMAINS})


@app.get("/read")
def read():
    url = _normalize_url(request.args.get("url") or "")
    if not url:
        return Response("Missing url parameter.", status=400)

    if not _host_allowed(url):
        return Response("That domain is not allowed.", status=403)

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; The2kTimesReader/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }

    # 1) Try Jina first (fast when it works)
    note = ""
    try:
        r = requests.get(_reader_url(url), timeout=12, headers=headers, allow_redirects=True)
        r.raise_for_status()

        # Jina returns HTML wrapper; pull text from it
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n")
        body_html = _clean_text_to_html(text)

        page = _render_page(url, body_html)
        return Response(page, mimetype="text/html")

    except Exception as e:
        # 2) Fallback: fetch original page directly
        note = f"Reader fallback: Jina timed out/failed, loaded directly from source."

    try:
        r2 = requests.get(url, timeout=18, headers=headers, allow_redirects=True)
        r2.raise_for_status()

        ctype = (r2.headers.get("Content-Type") or "").lower()
        if "text/html" not in ctype and "application/xhtml+xml" not in ctype:
            # Not HTML (e.g., PDF). Just show link.
            page = _render_page(
                url,
                f"<p>Content isn’t HTML ({_esc(ctype)}). Open the original link above.</p>",
                note=note,
            )
            return Response(page, mimetype="text/html")

        extracted_text = _extract_text_from_html(r2.text)
        body_html = _clean_text_to_html(extracted_text)

        page = _render_page(url, body_html, note=note)
        return Response(page, mimetype="text/html")

    except Exception as e2:
        return Response(f"Fetch failed (both methods): {e2}", status=502)
