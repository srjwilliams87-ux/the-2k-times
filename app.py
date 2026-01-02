import os
import re
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, request, Response

from bs4 import BeautifulSoup
from readability import Document

app = Flask(__name__)

UA = "The2kTimesReader/1.0 (+https://the-2k-times.onrender.com)"

DEFAULT_ALLOWED = [
    "bbc.co.uk",
    "reuters.com",
    "theguardian.com",
    "apnews.com",
    "ft.com",
    "independent.co.uk",
    "sky.com",
    "nme.com",
    "pitchfork.com",
    "kerrang.com",
    "planetrugby.com",
    "rugbypass.com",
    "world.rugby",
    "rugbyworld.com",
    "ruck.co.uk",
]

def get_allowed_domains():
    raw = os.environ.get("ALLOWED_DOMAINS", "").strip()
    if not raw:
        return set(DEFAULT_ALLOWED)
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return set(parts)

ALLOWED = get_allowed_domains()

def host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        # allow subdomains
        return any(host == d or host.endswith("." + d) for d in ALLOWED)
    except Exception:
        return False

def fetch_html(url: str) -> str:
    r = requests.get(
        url,
        timeout=15,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    r.raise_for_status()
    return r.text

def clean_text_from_html(html: str):
    """
    Use readability-lxml to extract the main content,
    then BeautifulSoup to normalize paragraphs.
    """
    doc = Document(html)
    title = doc.short_title() or "Reader"
    content_html = doc.summary(html_partial=True)

    soup = BeautifulSoup(content_html, "lxml")

    # Remove junk
    for tag in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
        tag.decompose()

    # Convert <br><br> into paragraphs if any
    # (Readability generally does <p> already, but this helps edge cases)
    text_blocks = []
    for p in soup.find_all(["p", "h2", "h3", "blockquote", "li"]):
        t = " ".join(p.get_text(" ", strip=True).split())
        if t and len(t) > 30:
            text_blocks.append(t)

    # Fallback: if no paragraphs extracted, use full text split
    if not text_blocks:
        raw_text = soup.get_text("\n", strip=True)
        raw_text = re.sub(r"\n{3,}", "\n\n", raw_text)
        chunks = [c.strip() for c in raw_text.split("\n\n") if len(c.strip()) > 40]
        text_blocks = chunks[:80]

    return title, text_blocks

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/read")
def read():
    url = request.args.get("url", "").strip()
    url = unquote(url)

    if not url:
        return Response("Missing url parameter.", mimetype="text/plain", status=400)

    if not host_allowed(url):
        return Response("That domain is not allowed.", mimetype="text/plain", status=403)

    try:
        html = fetch_html(url)
        title, blocks = clean_text_from_html(html)
    except Exception as e:
        return Response(f"Fetch failed: {e}", mimetype="text/plain", status=502)

    # Very clean “newspaper-ish” reader
    page = f"""
    <!doctype html>
    <html>
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{title}</title>
        <style>
          body {{
            margin: 0;
            background: #111;
            color: #f3f3f3;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          }}
          .wrap {{
            max-width: 860px;
            margin: 0 auto;
            padding: 22px 16px 48px 16px;
          }}
          .card {{
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            border-radius: 14px;
            padding: 18px 16px;
          }}
          h1 {{
            font-size: 28px;
            line-height: 1.15;
            margin: 0 0 12px 0;
            font-weight: 900;
          }}
          .meta {{
            color: #bdbdbd;
            font-size: 13px;
            margin-bottom: 14px;
          }}
          .meta a {{
            color: #86a8ff;
            text-decoration: none;
            font-weight: 800;
          }}
          .rule {{
            height: 1px;
            background: #2e2e2e;
            margin: 16px 0;
          }}
          p {{
            font-size: 16px;
            line-height: 1.8;
            margin: 0 0 14px 0;
            color: #e9e9e9;
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h1>{title}</h1>
            <div class="meta">
              Original article: <a href="{url}">{urlparse(url).hostname}</a>
            </div>
            <div class="rule"></div>
            {''.join([f"<p>{re.sub(r'\\s+', ' ', b)}</p>" for b in blocks])}
          </div>
        </div>
      </body>
    </html>
    """

    return Response(page, mimetype="text/html")
