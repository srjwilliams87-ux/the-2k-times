import os
import re
from urllib.parse import urlparse, quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response, jsonify

app = Flask(__name__)

# Default allowlist (you can override with env var ALLOWED_DOMAINS, comma-separated)
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


def _reader_url(original_url: str) -> str:
    # Use Jina "reader" endpoint
    # r.jina.ai/http(s)://example.com/...
    # Keep original scheme.
    original_url = original_url.strip()
    if not original_url.startswith(("http://", "https://")):
        original_url = "https://" + original_url
    return "https://r.jina.ai/" + original_url


def _clean_text_to_html(text: str) -> str:
    """
    Convert plain-ish extracted text into readable HTML:
    - split into paragraphs
    - lightly format headings
    """
    text = (text or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove obvious boilerplate lines
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    # Build paragraphs: treat blank lines (already removed) by grouping via simple heuristics
    paragraphs = []
    buff = []

    def flush():
        nonlocal buff
        if buff:
            paragraphs.append(" ".join(buff).strip())
            buff = []

    for ln in lines:
        # Start new paragraph on "short line" that looks like a heading
        if len(ln) <= 60 and ln.isupper():
            flush()
            paragraphs.append(f"## {ln}")
            continue

        # If a line looks like a heading (title-ish)
        if len(ln) <= 90 and re.match(r"^[A-Z0-9].{10,}$", ln) and ln.endswith((".", "!", "?", "”", '"')) is False:
            # don't over-split: only treat as heading if it's quite short
            if len(ln) <= 70:
                flush()
                paragraphs.append(f"## {ln}")
                continue

        # Otherwise accumulate into paragraph buffer
        buff.append(ln)

        # If line ends with sentence punctuation, allow paragraph breaks more naturally
        if ln.endswith((".", "!", "?", "…", ".”", '."')):
            if len(" ".join(buff)) > 220:
                flush()

    flush()

    # Render
    out = []
    for p in paragraphs:
        if p.startswith("## "):
            out.append(f"<h2>{_esc(p[3:])}</h2>")
        else:
            out.append(f"<p>{_esc(p)}</p>")

    return "\n".join(out)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/read")
def read():
    url = (request.args.get("url") or "").strip()
    if not url:
        return Response("Missing url parameter.", status=400)

    if not _host_allowed(url):
        return Response("That domain is not allowed.", status=403)

    try:
        r = requests.get(_reader_url(url), timeout=20)
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        return Response(f"Fetch failed: {e}", status=502)

    # Jina returns a page containing extracted text; we’ll take body text.
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text("\n")
    body_html = _clean_text_to_html(text)

    page = f"""
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
          max-width:860px;
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
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h1>Reader</h1>
          <div class="meta">
            Original: <a href="{_esc(url)}">{_esc(url)}</a>
          </div>
          <div class="rule"></div>
          {body_html}
        </div>
      </div>
    </body>
    </html>
    """
    return Response(page, mimetype="text/html")
