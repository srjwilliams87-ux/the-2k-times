import os
import re
from urllib.parse import urlparse

import requests
from flask import Flask, request, Response
from readability import Document
from bs4 import BeautifulSoup

app = Flask(__name__)

ALLOWED_DOMAINS = [d.strip().lower() for d in (os.environ.get("ALLOWED_DOMAINS", "")).split(",") if d.strip()]
READER_BRAND = os.environ.get("READER_BRAND", "The 2k Times Reader")

USER_AGENT = os.environ.get(
    "READER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
)

TIMEOUT = int(os.environ.get("READER_TIMEOUT_SECONDS", "15"))


def is_allowed(url: str) -> bool:
    if not ALLOWED_DOMAINS:
        return True
    try:
        host = urlparse(url).netloc.lower().split(":")[0].replace("www.", "")
        return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def sentence_split(text: str):
    """
    Simple sentence splitter that behaves reasonably for news copy.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    # Split on punctuation + space, but avoid splitting initials too aggressively
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])", text)
    return [p.strip() for p in parts if p.strip()]


def split_long_paragraphs(soup: BeautifulSoup, max_chars: int = 420, min_chunk_chars: int = 140):
    """
    If a <p> is huge, split it into multiple paragraphs at sentence boundaries.
    """
    for p in list(soup.find_all("p")):
        txt = p.get_text(" ", strip=True)

        # Skip short paragraphs or ones with lots of links
        if len(txt) <= max_chars:
            continue
        if len(p.find_all("a")) >= 3:
            continue

        sentences = sentence_split(txt)
        if len(sentences) < 4:
            continue

        chunks = []
        current = ""
        for s in sentences:
            if not current:
                current = s
            elif len(current) + 1 + len(s) <= max_chars:
                current += " " + s
            else:
                chunks.append(current)
                current = s
        if current:
            chunks.append(current)

        # Avoid creating silly tiny paragraphs
        merged = []
        for c in chunks:
            if merged and len(c) < min_chunk_chars:
                merged[-1] = merged[-1] + " " + c
            else:
                merged.append(c)

        # Replace original <p> with multiple <p>
        new_ps = [soup.new_tag("p") for _ in merged]
        for tag, c in zip(new_ps, merged):
            tag.string = c

        for new_p in reversed(new_ps):
            p.insert_after(new_p)
        p.decompose()


def clean_extracted_html(article_html: str) -> str:
    soup = BeautifulSoup(article_html, "html.parser")

    # Remove scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Keep basic formatting only
    allowed = {"p", "h1", "h2", "h3", "blockquote", "ul", "ol", "li", "strong", "em", "a"}
    for tag in soup.find_all(True):
        if tag.name not in allowed:
            tag.unwrap()
        else:
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in ["href"]}

    # Ensure links open safely
    for a in soup.find_all("a"):
        a.attrs["rel"] = "noopener noreferrer"
        a.attrs["target"] = "_blank"

    # ✅ NEW: better paragraph splitting
    split_long_paragraphs(soup)

    html = str(soup)
    html = re.sub(r"\n{3,}", "\n\n", html).strip()
    return html


def extract_readable(url: str):
    r = requests.get(
        url,
        timeout=TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    r.raise_for_status()

    doc = Document(r.text)
    title = doc.short_title() or "Article"
    content_html = doc.summary(html_partial=True)
    content_html = clean_extracted_html(content_html)

    # Fallback if too empty
    if len(BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)) < 400:
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)

        paras = []
        for p in text.split("\n\n"):
            p = p.strip()
            if len(p) < 60:
                continue
            paras.append(f"<p>{html_escape(p)}</p>")

        # run splitting on fallback too
        fallback_soup = BeautifulSoup("\n".join(paras[:80]), "html.parser")
        split_long_paragraphs(fallback_soup)
        content_html = str(fallback_soup)

    return title[:160], content_html


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
        title, body_html = extract_readable(url)
        host = urlparse(url).netloc.replace("www.", "")

        html = f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html_escape(title)}</title>
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
      line-height: 1.75;
    }}
    .wrap {{
      max-width: 860px;
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
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 10px;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: 32px;
      line-height: 1.15;
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
    blockquote {{
      margin: 18px 0;
      padding: 10px 14px;
      border-left: 3px solid var(--rule);
      color: var(--muted);
    }}
    a {{
      color: var(--link);
    }}
    ul, ol {{
      margin: 0 0 14px 22px;
      font-size: 18px;
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
      <h1>{html_escape(title)}</h1>
      <div class="meta">
        Source: <b>{html_escape(host)}</b> ·
        <a href="{html_escape(url)}" rel="noopener noreferrer" target="_blank">Open original</a>
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
