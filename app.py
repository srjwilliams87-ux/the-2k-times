import os
import re
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, request, jsonify, Response
import trafilatura

app = Flask(__name__)

READER_TITLE = os.environ.get("READER_TITLE", "Reader")

# Comma-separated allowlist. If empty, a safe default list is used.
DEFAULT_ALLOWED = [
    "bbc.co.uk", "bbc.com",
    "reuters.com",
    "theguardian.com",
    "rugbypass.com",
    "planet-rugby.com",
    "world.rugby",
    "rugby365.com",
    "rugbyworld.com",
    "skysports.com",
    "ruck.co.uk",
    "punknews.org",
    "kerrang.com",
    "loudersound.com",
    "nme.com",
]
ALLOWED_DOMAINS = [
    d.strip().lower()
    for d in (os.environ.get("ALLOWED_DOMAINS", "") or "").split(",")
    if d.strip()
]
if not ALLOWED_DOMAINS:
    ALLOWED_DOMAINS = DEFAULT_ALLOWED


def _host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    # allow subdomains of allowed domains
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def _clean_text_to_paragraphs(text: str) -> list[str]:
    """
    Turn extracted text into readable paragraphs:
    - collapse weird whitespace
    - split on blank lines
    - also split very long blocks on sentence boundaries lightly
    """
    text = (text or "").strip()
    if not text:
        return []

    # Normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Collapse 3+ newlines to 2 newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out = []
    for p in paras:
        # Collapse internal whitespace
        p = re.sub(r"[ \t]+", " ", p).strip()

        # If the paragraph is massive, softly split by sentence spacing.
        if len(p) > 700:
            parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])", p)
            buf = ""
            for part in parts:
                if len(buf) + len(part) + 1 <= 480:
                    buf = (buf + " " + part).strip()
                else:
                    if buf:
                        out.append(buf)
                    buf = part.strip()
            if buf:
                out.append(buf)
        else:
            out.append(p)

    return out


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/read")
def read():
    raw_url = request.args.get("url", "").strip()
    if not raw_url:
        return Response("Missing url parameter.", status=400)

    # Some clients double-encode
    url = unquote(raw_url).strip()

    if not (url.startswith("http://") or url.startswith("https://")):
        return Response("Invalid url.", status=400)

    if not _host_allowed(url):
        return Response("That domain is not allowed.", status=403)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }

    try:
        r = requests.get(url, headers=headers, timeout=25)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return Response(f"Fetch failed: {e}", status=502)

    # Extract article text (much cleaner than the jina.ai markdown dumps)
    downloaded = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        include_images=False,
        favor_recall=True,
    )

    if not downloaded:
        # fallback: try trafilatura's URL fetcher
        try:
            fetched = trafilatura.fetch_url(url)
            downloaded = trafilatura.extract(
                fetched or "",
                include_comments=False,
                include_tables=False,
                include_images=False,
                favor_recall=True,
            )
        except Exception:
            downloaded = None

    title = ""
    try:
        # trafilatura metadata is sometimes available
        meta = trafilatura.metadata.extract_metadata(html)
        if meta and meta.title:
            title = meta.title.strip()
    except Exception:
        title = ""

    paras = _clean_text_to_paragraphs(downloaded or "")

    paper = "#f7f5ef"
    ink = "#111111"
    muted = "#4a4a4a"
    rule = "#ddd8cc"
    outer_bg = "#111111"
    font = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'

    content_html = ""
    if paras:
        for p in paras:
            content_html += f"""
              <p style="margin:0 0 14px 0; font-size:16px; line-height:1.75; color:{ink};">
                {p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}
              </p>
            """
    else:
        content_html = f"""
          <p style="margin:0; font-size:16px; line-height:1.75; color:{muted};">
            Couldn’t extract clean article text. This source may block scraping.
          </p>
        """

    page = f"""
    <html>
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{READER_TITLE}</title>
      </head>
      <body style="margin:0;background:{outer_bg};-webkit-text-size-adjust:100%;text-size-adjust:100%;">
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td align="center" style="padding:18px;">
              <table width="860" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:{paper};border-radius:14px;overflow:hidden;">
                <tr>
                  <td style="padding:22px 20px 12px 20px;font-family:{font};">
                    <div style="font-size:22px;font-weight:900;color:{ink};margin:0 0 6px 0;">
                      {title if title else "Reader"}
                    </div>
                    <div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:{muted};">
                      Original: <a href="{url}" style="color:#0b57d0;text-decoration:none;">{url}</a>
                    </div>
                  </td>
                </tr>

                <tr><td style="height:1px;background:{rule};"></td></tr>

                <tr>
                  <td style="padding:18px 20px 22px 20px;font-family:{font};">
                    {content_html}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
