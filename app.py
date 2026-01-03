import os
from urllib.parse import urlparse

import requests
from flask import Flask, request, abort
from bs4 import BeautifulSoup
from readability import Document


app = Flask(__name__)

READER_BASE_URL = (os.environ.get("READER_BASE_URL", "https://the-2k-times.onrender.com") or "").rstrip("/")

# Allow these domains to be fetched by /read
ALLOWED_DOMAINS = {
    "bbc.co.uk",
    "bbc.com",
    "reuters.com",
    "www.reuters.com",
    "theguardian.com",
    "www.theguardian.com",
    "independent.co.uk",
    "www.independent.co.uk",
}


def is_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False

    if not host:
        return False

    # exact match or subdomain match
    if host in ALLOWED_DOMAINS:
        return True

    for d in ALLOWED_DOMAINS:
        if host.endswith("." + d):
            return True

    return False


@app.get("/")
def home():
    return "OK"


@app.get("/read")
def read():
    url = (request.args.get("url") or "").strip()
    if not url:
        abort(400, "Missing url")

    if not (url.startswith("http://") or url.startswith("https://")):
        abort(400, "Invalid url")

    if not is_allowed(url):
        return "That domain is not allowed.", 403

    headers = {
        "User-Agent": "Mozilla/5.0 (The 2k Times Reader)",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return f"Fetch failed: {e}", 502

    # Clean extraction
    try:
        doc = Document(html)
        title = (doc.short_title() or "").strip() or "Reader"
        content_html = doc.summary(html_partial=True)

        soup = BeautifulSoup(content_html, "html.parser")

        # Remove scripts/styles
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        # Trim mega nav blocks if any slipped through
        # (Readability usually handles this, but keep safe)
        text = soup.get_text("\n", strip=True)
        if len(text) < 200:
            # fallback: attempt to extract main article area from original page
            page = BeautifulSoup(html, "html.parser")
            main = page.find("article") or page.find("main") or page.body
            if main:
                for tag in main(["script", "style", "noscript"]):
                    tag.decompose()
                soup = BeautifulSoup(str(main), "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()

        clean_html = str(soup)

    except Exception:
        title = "Reader"
        clean_html = "<p>Unable to extract article text.</p>"

    # Simple, clean styling (no raw URL printed anywhere)
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #111;
      color: #eee;
    }}
    .wrap {{
      max-width: 860px;
      margin: 0 auto;
      padding: 24px 16px 60px;
    }}
    .card {{
      background: #1b1b1b;
      border-radius: 14px;
      padding: 26px 22px;
      box-shadow: 0 12px 40px rgba(0,0,0,.35);
      border: 1px solid #2a2a2a;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 26px;
      line-height: 1.2;
      color: #fff;
      font-weight: 900;
    }}
    .meta {{
      margin: 0 0 18px;
      color: #bdbdbd;
      font-size: 13px;
      letter-spacing: .3px;
    }}
    .btn {{
      display: inline-block;
      margin: 0 0 18px;
      padding: 10px 14px;
      border-radius: 10px;
      background: #2a2a2a;
      color: #fff;
      text-decoration: none;
      font-weight: 700;
      font-size: 13px;
    }}
    .content {{
      color: #e6e6e6;
      font-size: 17px;
      line-height: 1.75;
    }}
    .content a {{
      color: #8ab4ff;
      text-decoration: none;
    }}
    .content img {{
      max-width: 100%;
      height: auto;
      border-radius: 10px;
    }}
    .content h2, .content h3 {{
      color: #fff;
      margin-top: 24px;
    }}
    .content p {{
      margin: 14px 0;
    }}
    hr {{
      border: 0;
      height: 1px;
      background: #2a2a2a;
      margin: 20px 0;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>{title}</h1>
      <div class="meta">The 2k Times · Reader</div>
      <a class="btn" href="{url}" target="_blank" rel="noopener">Open original</a>
      <hr/>
      <div class="content">
        {clean_html}
      </div>
    </div>
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
