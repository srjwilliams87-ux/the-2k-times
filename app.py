import os
import re
from urllib.parse import urlparse

import requests
from flask import Flask, request, Response

app = Flask(__name__)

# Optional: allow only these domains (comma-separated), blank = allow all
ALLOWED_DOMAINS = [d.strip().lower() for d in (os.environ.get("ALLOWED_DOMAINS", "")).split(",") if d.strip()]

# Optional: set a custom title in the reader header
READER_BRAND = os.environ.get("READER_BRAND", "The 2k Times Reader")

USER_AGENT = os.environ.get(
    "READER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
)

TIMEOUT = int(os.environ.get("READER_TIMEOUT_SECONDS", "12"))


def is_allowed(url: str) -> bool:
    if not ALLOWED_DOMAINS:
        return True
    try:
        host = urlparse(url).netloc.lower()
        host = host.split(":")[0]
        return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def jina_clean_text(url: str) -> str:
    """
    Fetch "clean text" using r.jina.ai, then lightly sanitize.
    This avoids ad-heavy pages and is very reliable.
    """
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise ValueError("URL must start with http:// or https://")

    # r.jina.ai expects full URL appended
    clean_url = f"https://r.jina.ai/{url}"
    r = requests.get(clean_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    text = r.text

    # Basic cleanup: collapse excessive blank lines and trim
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()

    return text


def guess_title_from_text(text: str) -> str:
    """
    r.jina.ai output usually starts with a title-ish line.
    We'll pick the first non-empty line that's not a nav/header junk.
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for ln in lines[:25]:
        bad = ("skip to content", "home", "news", "subscribe", "sign in")
        if len(ln) >= 12 and not any(b in ln.lower() for b in bad):
            return ln[:140]
    return "Article"


def html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def text_to_html_paragraphs(text: str) -> str:
    """
    Convert plain text to readable HTML blocks:
    - preserve paragraphs
    - preserve short lines as headings-ish by making them bold
    """
    blocks = []
    paragraphs = re.split(r"\n\s*\n", text)

    for p in paragraphs:
        p = p.strip()
        if not p:
            continue

        # If it looks like a short heading (all caps / very short)
        if len(p) < 80 and (p.isupper() or p.endswith(":")):
            blocks.append(f"<h2>{html_escape(p)}</h2>")
            continue

        # Otherwise normal paragraph
        blocks.append(f"<p>{html_escape(p)}</p>")

    return "\n".join(blocks)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/read")
def read():
    url = request.args.get("url", "").strip()
    if not url:
        return Response("Missing ?url=", status=400)

    if not (url.startswith("http://") or url.startswith("https://")):
        return Response("URL must start with http:// or https://", status=400)

    if not is_allowed(url):
        return Response("That domain is not allowed.", status=403)

    try:
        clean_text = jina_clean_text(url)
        title = guess_title_from_text(clean_text)
        body_html = text_to_html_paragraphs(clean_text)

        host = urlparse(url).netloc.replace("www.", "")
        safe_title = html_escape(title)
        safe_url = html_escape(url)

        html = f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    :root {{
      --bg: #0f0f10;
      --card: #17181a;
      --text: #f2f2f2;
      --muted: #b9b9b9;
      --rule: #2a2b2e;
      --link: #8ab4ff;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f6f6f7;
        --card: #ffffff;
        --text: #141416;
        --muted: #5b5b5f;
        --rule: #e7e7ea;
        --link: #0b57d0;
      }}
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-serif, Georgia, "Times New Roman", Times, serif;
      line-height: 1.7;
    }}
    .wrap {{
      max-width: 820px;
      margin: 0 auto;
      padding: 18px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--rule);
      border-radius: 16px;
      padding: 18px 18px 8px 18px;
    }}
    .brand {{
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      font-weight: 800;
      letter-spacing: 0.5px;
      font-size: 14px;
      color: var(--muted);
      margin-bottom: 10px;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: 30px;
      line-height: 1.2;
      margin: 0 0 10px 0;
      font-weight: 800;
    }}
    .meta {{
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 14px;
    }}
    .meta a {{
      color: var(--link);
      text-decoration: none;
      border-bottom: 1px solid transparent;
    }}
    .meta a:hover {{
      border-bottom-color: var(--link);
    }}
    hr {{
      border: 0;
      border-top: 1px solid var(--rule);
      margin: 14px 0 16px 0;
    }}
    p {{
      margin: 0 0 14px 0;
      font-size: 18px;
    }}
    h2 {{
      margin: 18px 0 10px 0;
      font-size: 18px;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }}
    .footer {{
      margin: 18px 0 10px 0;
      color: var(--muted);
      font-size: 12px;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="brand">{html_escape(READER_BRAND)}</div>
      <h1>{safe_title}</h1>
      <div class="meta">
        Source: <b>{html_escape(host)}</b> ·
        <a href="{safe_url}" rel="noopener noreferrer" target="_blank">Open original</a>
      </div>
      <hr />
      {body_html}
      <div class="footer">
        Reader view generated for easier reading. If formatting looks odd, use “Open original”.
      </div>
    </div>
  </div>
</body>
</html>
"""
        return Response(html, mimetype="text/html")

    except requests.HTTPError as e:
        return Response(f"Fetch failed: {str(e)}", status=502)
    except Exception as e:
        return Response(f"Error: {str(e)}", status=500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
