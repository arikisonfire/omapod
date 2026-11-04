"""OPML import and export (subscription lists for other podcast apps)."""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

from .parser import _repair, _decode


def parse_opml(data):
    """Return [{'url', 'title', 'link'}] for every outline with an xmlUrl, nested ones included."""
    try:
        root = ET.fromstring(data if isinstance(data, bytes) else data.encode("utf-8"))
    except ET.ParseError:
        root = ET.fromstring(_repair(_decode(data)))
    result, seen = [], set()
    for outline in root.iter():
        if not isinstance(outline.tag, str) or outline.tag.split("}")[-1].lower() != "outline":
            continue
        attrs = {k.lower(): v for k, v in outline.attrib.items()}
        url = (attrs.get("xmlurl") or attrs.get("url") or "").strip()
        if not url or not url.lower().startswith(("http://", "https://", "feed://")):
            continue
        if url.lower().startswith("feed://"):
            url = "https://" + url[7:]
        if url in seen:
            continue
        seen.add(url)
        result.append({"url": url, "title": attrs.get("title") or attrs.get("text") or url,
                       "link": attrs.get("htmlurl") or ""})
    return result


def build_opml(feeds, title="omaPOD subscriptions"):
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = format_datetime(datetime.now(timezone.utc))
    body = ET.SubElement(root, "body")
    for feed in feeds:
        attrs = {"text": feed.display_title, "title": feed.display_title, "type": "rss", "xmlUrl": feed.url}
        if feed.link:
            attrs["htmlUrl"] = feed.link
        ET.SubElement(body, "outline", attrs)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def build_html_export(feeds):
    """AntennaPod's 'HTML export': a page to show your subscriptions to a friend."""
    import html

    from ..util import web_url

    rows = []
    for feed in feeds:
        # Only web addresses: the page is opened in a browser, and feeds can name javascript: or file: ones.
        image_url, link, rss = web_url(feed.image_url), web_url(feed.link) or web_url(feed.url), web_url(feed.url)
        image = f'<img src="{html.escape(image_url)}" alt="" loading="lazy">' if image_url else ""
        title = html.escape(feed.display_title)
        if link:
            title = f'<a href="{html.escape(link)}">{title}</a>'
        rss_link = f'<a class="rss" href="{html.escape(rss)}">RSS</a>' if rss else ""
        rows.append(f'<li>{image}<div>{title}<span>{html.escape(feed.author)}</span>{rss_link}</div></li>')
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
        "<title>My podcasts</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222}"
        "ul{list-style:none;padding:0}li{display:flex;gap:14px;align-items:center;padding:10px 0;"
        "border-bottom:1px solid #ddd}img{width:64px;height:64px;object-fit:cover}"
        "div{display:flex;flex-direction:column;gap:2px}span{color:#666;font-size:.9em}"
        ".rss{font-size:.8em;color:#888}</style></head><body>"
        f"<h1>My podcasts</h1><p>{len(feeds)} subscriptions, exported from omaPOD.</p>"
        f"<ul>{''.join(rows)}</ul></body></html>\n"
    )
