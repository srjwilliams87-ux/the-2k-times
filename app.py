import os
import re
from urllib.parse import urlparse

import requests
import trafilatura
from flask import Flask, request, abort

app = Flask(__name__)

# Allow these domains in Reader
DEFAULT_ALLOWED = {
    "bbc.co.uk",
    "bbc.com",
    "reuters.com",
    "www.reuters.com",
    "theguardian.com",
    "www.theguardian.com",
    "independent.co.uk",
    "www.independent.co.uk",
}

# Optional: extend with comma-separated list in env
extra = os.environ.get("ALLOWED_DOMAINS", "")
EXTRA_ALLOWED = {d.strip().lower() for d in extra.split(",") if d.strip()}
ALLOWED_DOMAINS = DEFAULT_ALLOWED | EXTRA_ALLOWED


def is_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        # allow subdomains
        for d in ALLOWED_DOMAINS:
            if host == d or host.endswith("." + d):
                return True
        return False
    except Exception:
        return False


def clean_title(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:200]


@app.get("/")
def home():
    return "OK"


@app.get("/read")
def read():
    url = (request.args.get("url") or "").strip()
    if not url:
        abort(400, "Missing ?url=")

    if not is_allowed(url):
        return ("That domain is not allowed.", 403)

    # Fetch
    try:
        r = requests.get(
            url,
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; The2kTimesReader/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return (f"Fetch failed: {e}", 502)

    # Extract main content
    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        include_images=False,
        output_format="txt",
    )

    # Try to also grab title
    downloaded = trafilatura.extract_metadata(html)
    title = clean_title(downloaded.title if downloaded and downloaded.title else "")
    if not title:
        title = "Reader"

    # Convert extracted text to paragraphs
    if not extracted:
        body_html = "<p>Sorry — I couldn’t extract the article text.</p>"
    else:
        paras = [p.strip() for p in extracted.split("\n") if p.strip()]
        # Keep it readable: remove ultra-short noise lines
        paras = [p for p in paras if len(p) > 25]
        body_html = "\n".join([f"<p>{escape_html(p)}</p>" for p in paras[:80]])

    source = source_name(url)

    # Clean Reader page (no full URL printed)
    page = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{escape_html(title)}</title>
      <style>
        body {{
          margin: 0;
          background: #111;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          color: #111;
        }}
        .wrap {{
          max-width: 820px;
          margin: 28px auto;
          padding: 0 16px;
        }}
        .card {{
          background: #f7f5ef;
          border-radius: 14px;
          padding: 22px 22px;
          box-shadow: 0 10px 30px rgba(0,0,0,0.35);
        }}
        h1 {{
          margin: 0 0 6px 0;
          font-size: 22px;
          line-height: 1.2;
        }}
        .meta {{
          color: #4a4a4a;
          font-size: 12px;
          font-weight: 700;
          letter-spacing: 1px;
          text-transform: uppercase;
          margin-bottom: 16px;
        }}
        p {{
          margin: 0 0 14px 0;
          color: #222;
          line-height: 1.75;
          font-size: 15px;
        }}
        .actions {{
          margin-top: 18px;
          padding-top: 14px;
          border-top: 1px solid #ddd8cc;
          display: flex;
          gap: 12px;
          flex-wrap: wrap;
        }}
        a.button {{
          display: inline-block;
          background: #0b57d0;
          color: #fff;
          text-decoration: none;
          padding: 10px 12px;
          border-radius: 10px;
          font-weight: 800;
          letter-spacing: .5px;
          font-size: 13px;
        }}
        a.ghost {{
          display: inline-block;
          color: #0b57d0;
          text-decoration: none;
          padding: 10px 0;
          font-weight: 800;
          font-size: 13px;
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h1>{escape_html(title)}</h1>
          <div class="meta">{escape_html(source)}</div>
          {body_html}
          <div class="actions">
            <a class="button" href="{escape_attr(url)}" target="_blank" rel="noopener noreferrer">Open original</a>
            <a class="ghost" href="javascript:history.back()">← Back</a>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return page


def source_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "bbc" in host:
        return "BBC"
    if "reuters" in host:
        return "Reuters"
    if "theguardian" in host or host.endswith("guardian.com"):
        return "The Guardian"
    if "independent" in host:
        return "The Independent"
    return host


def escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def escape_attr(s: str) -> str:
    return escape_html(s)
