"""RSS 2.0 / Atom / RSS 1.0 parser for podcast feeds.

Understands the iTunes, Podcasting 2.0 (podcastindex), Podlove Simple
Chapters, Media RSS, content and Dublin Core namespaces, like AntennaPod's
SyndHandler. Broken feeds (undeclared prefixes, HTML entities, stray
ampersands, control characters) are repaired before giving up.
"""

import html
import html.entities
import mimetypes
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..util import MAX_SECONDS, clip, parse_time_string


class FeedParseError(Exception):
    pass


class NotAFeedError(FeedParseError):
    """The document is an HTML page. `candidates` lists feeds it links to."""

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


NAMESPACES = {
    "itunes": ("http://www.itunes.com/dtds/podcast-1.0.dtd", "http://www.itunes.com/DTDs/Podcast-1.0.dtd"),
    "podcast": ("https://podcastindex.org/namespace/1.0",
                "https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md"),
    "content": ("http://purl.org/rss/1.0/modules/content/",),
    "media": ("http://search.yahoo.com/mrss/", "http://search.yahoo.com/mrss"),
    "atom": ("http://www.w3.org/2005/Atom", "http://purl.org/atom/ns#"),
    "psc": ("http://podlove.org/simple-chapters", "http://podlove.org/simple-chapters/"),
    "dc": ("http://purl.org/dc/elements/1.1/",),
    "googleplay": ("http://www.google.com/schemas/play-podcasts/1.0",),
    "rdf": ("http://www.w3.org/1999/02/22-rdf-syntax-ns#",),
    "rss1": ("http://purl.org/rss/1.0/",),
}
_URI_TO_PREFIX = {uri.lower().rstrip("/"): prefix for prefix, uris in NAMESPACES.items() for uri in uris}


def _tag(element):
    """'{uri}local' -> 'prefix:local'; plain names stay plain."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        prefix = _URI_TO_PREFIX.get(uri.lower().rstrip("/"))
        if prefix == "rss1":
            return local
        if prefix:
            return f"{prefix}:{local}"
        return f"{{{uri}}}{local}"
    return tag


def _text(element):
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _inner_xml(element):
    """Content of an element that may contain unescaped XHTML."""
    if element is None:
        return ""
    if len(element) == 0:
        return (element.text or "").strip()
    parts = [element.text or ""]
    for child in element:
        child_copy = ET.tostring(child, encoding="unicode", short_empty_elements=True)
        parts.append(re.sub(r"\sxmlns(:\w+)?=\"[^\"]*\"", "", child_copy))
    return "".join(parts).strip()


@dataclass
class ParsedItem:
    guid: str = ""
    title: str = ""
    link: str = ""
    description: str = ""
    pub_date: int = 0
    image_url: str = ""
    media_url: str = ""
    media_type: str = ""
    media_size: int = 0
    duration: int = 0
    season: int = 0
    episode_number: int = 0
    episode_type: str = ""
    chapters_url: str = ""
    chapters_type: str = ""
    transcript_url: str = ""
    transcript_type: str = ""
    psc_chapters: list = field(default_factory=list)
    social_url: str = ""

    @property
    def identifier(self):
        return self.guid or self.media_url or self.title or self.link


@dataclass
class ParsedFeed:
    title: str = ""
    link: str = ""
    description: str = ""
    author: str = ""
    image_url: str = ""
    language: str = ""
    feed_type: str = ""
    podcast_guid: str = ""
    new_feed_url: str = ""
    next_page_url: str = ""
    funding: list = field(default_factory=list)
    items: list = field(default_factory=list)


# ---------------------------------------------------------------- helpers

_MONTHS_DE = {"mär": "Mar", "mai": "May", "okt": "Oct", "dez": "Dec"}


def parse_date(value):
    """RFC 822 and ISO 8601 dates, plus the common broken variants. Returns epoch seconds or 0."""
    value = " ".join((value or "").split())
    if not value or len(value) > 100:  # no date is that long, and the patterns below would take quadratic time
        return 0
    try:
        when = parsedate_to_datetime(value)
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return int(when.timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    iso = value.replace(" ", "T", 1) if re.match(r"^\d{4}-\d\d-\d\d \d", value) else value
    iso = re.sub(r"Z$", "+00:00", iso)
    iso = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", iso)
    try:
        when = datetime.fromisoformat(iso)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return int(when.timestamp())
    except ValueError:
        pass
    cleaned = value
    for wrong, right in _MONTHS_DE.items():
        cleaned = re.sub(wrong, right, cleaned, flags=re.I)
    cleaned = re.sub(r"^\w+,?\s+", "", cleaned) if re.match(r"^[A-Za-z]+,", cleaned) else cleaned
    cleaned = re.sub(r"\s+(GMT|UTC)?([+-]\d{1,2})(:?(\d\d))?$",
                     lambda m: " %+03d%s" % (int(m.group(2)), m.group(4) or "00"), cleaned)
    cleaned = re.sub(r"\.\d+", "", cleaned)
    for fmt in ("%d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M %z", "%d %B %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S",
                "%d %b %Y", "%Y-%m-%d", "%b %d %Y %H:%M:%S", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            when = datetime.strptime(cleaned.strip(), fmt)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return int(when.timestamp())
        except ValueError:
            continue
    return 0


def parse_duration(value):
    """itunes:duration -> milliseconds ('1:02:03', '62:03', '3723', '3723.5')."""
    value = (value or "").strip()
    if not value or len(value) > 50:
        return 0
    try:
        return max(0, parse_time_string(value))  # also a plain number of seconds
    except ValueError:
        match = re.match(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?$", value, re.I)
        if match and any(match.groups()):
            hours, minutes, seconds = (int(g or 0) for g in match.groups())
            total = hours * 3600 + minutes * 60 + seconds
            return total * 1000 if total <= MAX_SECONDS else 0
    return 0


def _int(value, default=0):
    """Season, episode number, file size: default unless a finite number SQLite can store (2^53 is far beyond
    any real one; '1e999' or a 20-digit number made the whole feed fail)."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError, OverflowError):
            return default
    return number if abs(number) <= 2 ** 53 else default


MEDIA_EXT_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/opus", ".flac": "audio/flac", ".wav": "audio/wav", ".mp4": "video/mp4",
    ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
}


def guess_mime(declared, url):
    declared = (declared or "").strip().lower()
    if declared and declared not in ("application/octet-stream", "binary/octet-stream", "unknown"):
        return declared
    path = (url or "").split("?")[0].split("#")[0].lower()
    for ext, mime in MEDIA_EXT_MIME.items():
        if path.endswith(ext):
            return mime
    guessed, _ = mimetypes.guess_type(path)
    return guessed or declared


def is_media_mime(mime):
    mime = (mime or "").lower()
    return mime.startswith(("audio/", "video/")) or mime in ("application/ogg", "application/x-mpegurl")


def is_image_mime(mime):
    return (mime or "").lower().startswith("image/")


TRANSCRIPT_PREFERENCE = ["application/json", "text/vtt", "application/x-subrip", "application/srt", "text/srt",
                         "text/html", "text/plain"]


def _transcript_rank(mime):
    mime = (mime or "").lower()
    return TRANSCRIPT_PREFERENCE.index(mime) if mime in TRANSCRIPT_PREFERENCE else len(TRANSCRIPT_PREFERENCE)


# ---------------------------------------------------------------- repair

_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_BARE_AMP_RE = re.compile(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#\d+|#x[0-9A-Fa-f]+);)")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _decode(data):
    if isinstance(data, str):
        return data
    match = re.match(rb"^\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9_.-]+)[\"']", data)
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")


def _repair(text):
    text = text.lstrip("﻿").lstrip()
    text = re.sub(r"^<\?xml[^>]*\?>", "", text).lstrip()
    text = _CTRL_RE.sub("", text)

    def entity(match):
        name = match.group(1)
        if name in _XML_ENTITIES:
            return match.group(0)
        codepoint = html.entities.name2codepoint.get(name)
        if codepoint is None:
            return "&amp;" + name + ";"
        return f"&#{codepoint};"

    text = _ENTITY_RE.sub(entity, text)
    text = _BARE_AMP_RE.sub("&amp;", text)
    # Declare well-known prefixes that the feed uses but forgot to declare.
    root_match = re.search(r"<(rss|feed|rdf:RDF)\b[^>]*>", text)
    if root_match:
        root_tag = root_match.group(0)
        additions = []
        for prefix, uris in NAMESPACES.items():
            if prefix in ("rss1",):
                continue
            if re.search(rf"<{prefix}:", text) and f"xmlns:{prefix}=" not in root_tag:
                additions.append(f' xmlns:{prefix}="{uris[0]}"')
        if additions:
            fixed = root_tag[:-1].rstrip("/") + "".join(additions) + (" />" if root_tag.endswith("/>") else ">")
            text = text.replace(root_tag, fixed, 1)
    return text


class _AlternateLinkFinder(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        attrs = {k.lower(): (v or "") for k, v in attrs}
        rel = attrs.get("rel", "").lower()
        mime = attrs.get("type", "").lower()
        if "alternate" in rel.split() and mime in ("application/rss+xml", "application/atom+xml", "application/xml",
                                                    "text/xml") and attrs.get("href"):
            self.links.append({"url": urljoin(self.base_url, attrs["href"]), "title": attrs.get("title", "")})


def discover_feeds(html_text, base_url):
    finder = _AlternateLinkFinder(base_url)
    try:
        finder.feed(html_text)
    except Exception:
        pass
    seen, result = set(), []
    for link in finder.links:
        if link["url"] not in seen and _web(link["url"]):
            seen.add(link["url"])
            result.append(link)
    return result


def _looks_like_html(text):
    head = text[:2048].lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or ("<html" in head and "<rss" not in head
                                                                              and "<feed" not in head)


# ---------------------------------------------------------------- parser

def parse_feed(data, url=""):
    """Parse feed bytes. Raises NotAFeedError for HTML pages, FeedParseError for garbage."""
    return _short_names(_web_only(_parse_feed(data, url), url))


def _web(url, base=""):
    url = urljoin(base, (url or "").strip()) if url and url.strip() else ""
    return url if urlsplit(url).scheme.lower() in ("http", "https") else ""


def _web_only(feed, base):
    """Addresses in a feed come from strangers and go to mpv, the image loader and the desktop's URL handlers:
    only web addresses are kept (no file:, smb:, av:lavfi: or the like), relative ones made absolute."""
    names = ("link", "image_url", "next_page_url", "new_feed_url", "media_url", "chapters_url", "transcript_url",
             "social_url")
    for entry in [feed] + feed.items:
        for name in names:
            if hasattr(entry, name):
                setattr(entry, name, _web(getattr(entry, name), base))
    feed.funding = [dict(f, url=_web(f.get("url"), base)) for f in feed.funding if _web(f.get("url"), base)]
    for item in feed.items:
        for chapter in item.psc_chapters:
            chapter["url"], chapter["image"] = _web(chapter["url"], base), _web(chapter["image"], base)
    return feed


def _short_names(feed):
    """Names end up in labels that wrap (see util.clip)."""
    feed.title, feed.author = clip(feed.title), clip(feed.author)
    feed.funding = [dict(f, title=clip(f.get("title"))) for f in feed.funding]
    for item in feed.items:
        item.title = clip(item.title)
        for chapter in item.psc_chapters:
            chapter["title"] = clip(chapter.get("title"))
    return feed


def _parse_feed(data, url):
    text = _decode(data)
    if _looks_like_html(text):
        raise NotAFeedError("The address is a website, not a podcast feed.", discover_feeds(text, url))
    root = None
    first_error = None
    try:
        root = ET.fromstring(data if isinstance(data, bytes) else data.encode("utf-8"))
    except ET.ParseError as error:
        first_error = error
    if root is None:
        try:
            root = ET.fromstring(_repair(text))
        except ET.ParseError as error:
            raise FeedParseError(f"The podcast feed is broken and could not be read ({first_error or error}).")
    name = _tag(root)
    if name == "rss":
        channel = next((c for c in root if _tag(c) == "channel"), None)
        if channel is None:
            raise FeedParseError("The RSS document has no channel.")
        return _parse_rss_channel(channel, items=[c for c in channel if _tag(c) == "item"], base_url=url)
    if name == "atom:feed" or name == "feed":
        return _parse_atom(root, url)
    if name == "rdf:RDF":
        channel = next((c for c in root if _tag(c) == "channel"), None)
        items = [c for c in root if _tag(c) == "item"]
        if channel is None:
            raise FeedParseError("The RDF document has no channel.")
        return _parse_rss_channel(channel, items=items, base_url=url)
    raise FeedParseError(f"Unsupported document type '{name}'.")


def _parse_rss_channel(channel, items, base_url):
    feed = ParsedFeed()
    rss_image = ""
    for child in channel:
        tag = _tag(child)
        if tag == "title":
            feed.title = _text(child)
        elif tag == "link" and not feed.link:
            feed.link = _text(child)
        elif tag == "description":
            feed.description = feed.description or _inner_xml(child)
        elif tag == "itunes:summary":
            summary = _inner_xml(child)
            if len(summary) > len(feed.description):
                feed.description = summary
        elif tag == "itunes:subtitle" and not feed.description:
            feed.description = _text(child)
        elif tag == "language":
            feed.language = _text(child)
        elif tag in ("itunes:author", "googleplay:author") and not feed.author:
            feed.author = html.unescape(_text(child))
        elif tag == "dc:creator" and not feed.author:
            feed.author = _text(child)
        elif tag == "managingEditor" and not feed.author:
            feed.author = _text(child)
        elif tag == "itunes:image":
            href = child.get("href") or _text(child)
            if href:
                feed.image_url = href.strip()
        elif tag == "image":
            url_el = next((c for c in child if _tag(c) == "url"), None)
            rss_image = _text(url_el) if url_el is not None else (child.get("href") or "")
        elif tag == "media:thumbnail" and not rss_image:
            rss_image = child.get("url") or ""
        elif tag == "itunes:type":
            feed.feed_type = _text(child).lower()
        elif tag == "itunes:new-feed-url":
            value = _text(child)
            if value.startswith("http"):
                feed.new_feed_url = value
        elif tag == "atom:link":
            if (child.get("rel") or "").lower() == "next" and child.get("href"):
                feed.next_page_url = urljoin(base_url, child.get("href"))
        elif tag == "podcast:funding":
            url = child.get("url")
            if url:
                feed.funding.append({"url": url.strip(), "title": _text(child)})
        elif tag == "podcast:guid":
            feed.podcast_guid = _text(child)
    if not feed.image_url:
        feed.image_url = rss_image.strip()
    if feed.image_url and base_url:
        feed.image_url = urljoin(base_url, feed.image_url)
    feed.items = [item for item in (_parse_rss_item(element, base_url) for element in items) if item is not None]
    return feed


def _parse_rss_item(element, base_url):
    item = ParsedItem()
    descriptions = []
    transcripts = []
    itunes_duration = 0
    media_default = False
    for child in element:
        tag = _tag(child)
        if tag == "title":
            item.title = html.unescape(_text(child)) if "&" in _text(child) else _text(child)
        elif tag == "link" and not item.link:
            item.link = _text(child) or (child.get("href") or "")
        elif tag == "guid":
            item.guid = _text(child)
        elif tag in ("description", "content:encoded", "itunes:summary", "media:description", "itunes:subtitle"):
            descriptions.append(_inner_xml(child))
        elif tag in ("pubDate", "dc:date") and not item.pub_date:
            item.pub_date = parse_date(_text(child))
        elif tag == "enclosure":
            url = (child.get("url") or "").strip()
            mime = guess_mime(child.get("type"), url)
            valid = is_media_mime(mime)
            if not valid and not is_image_mime(mime) and not item.media_url:
                valid = True
                mime = mime or "audio/*"
            if valid and url and not item.media_url:
                item.media_url = urljoin(base_url, url) if base_url else url
                item.media_type = mime
                size = _int(child.get("length"))
                item.media_size = size if size >= 16384 else 0
        elif tag == "media:content":
            media_default = _media_content(item, child, media_default, base_url)
        elif tag == "media:group":
            for sub in child:
                sub_tag = _tag(sub)
                if sub_tag == "media:content":
                    media_default = _media_content(item, sub, media_default, base_url)
                elif sub_tag == "media:thumbnail" and not item.image_url:
                    item.image_url = sub.get("url") or ""
                elif sub_tag == "media:description":
                    descriptions.append(_inner_xml(sub))
        elif tag == "itunes:image":
            href = child.get("href") or _text(child)
            if href:
                item.image_url = href.strip()
        elif tag == "media:thumbnail" and not item.image_url:
            item.image_url = child.get("url") or ""
        elif tag == "itunes:duration":
            itunes_duration = parse_duration(_text(child))
        elif tag == "itunes:season":
            item.season = _int(_text(child))
        elif tag == "itunes:episode":
            item.episode_number = _int(_text(child))
        elif tag == "itunes:episodeType":
            item.episode_type = _text(child).lower()
        elif tag == "podcast:season" and not item.season:
            item.season = _int(_text(child))
        elif tag == "podcast:episode" and not item.episode_number:
            item.episode_number = _int(_text(child))
        elif tag == "podcast:chapters":
            if child.get("url"):
                item.chapters_url = child.get("url").strip()
                item.chapters_type = (child.get("type") or "application/json+chapters").strip()
        elif tag == "podcast:transcript":
            if child.get("url") and child.get("type"):
                transcripts.append((child.get("type").strip().lower(), child.get("url").strip()))
        elif tag == "podcast:socialInteract":
            if child.get("uri"):
                item.social_url = child.get("uri").strip()
        elif tag == "psc:chapters":
            item.psc_chapters = _parse_psc(child)
    if descriptions:
        item.description = max(descriptions, key=len)
    if itunes_duration and item.media_url:
        item.duration = itunes_duration
    if not item.title:
        from ..util import html_to_text

        item.title = html_to_text(item.description, limit=120)
    if transcripts:
        transcripts.sort(key=lambda t: _transcript_rank(t[0]))
        item.transcript_type, item.transcript_url = transcripts[0]
    if item.image_url and base_url:
        item.image_url = urljoin(base_url, item.image_url)
    if not item.identifier:
        return None
    return item


def _media_content(item, child, had_default, base_url):
    url = (child.get("url") or "").strip()
    medium = (child.get("medium") or "").lower()
    is_default = (child.get("isDefault") or "").lower() == "true"
    mime = guess_mime(child.get("type"), url)
    valid_media = valid_image = False
    if medium == "audio":
        valid_media, mime = True, mime if is_media_mime(mime) else "audio/*"
    elif medium == "video":
        valid_media, mime = True, mime if is_media_mime(mime) else "video/*"
    elif medium == "image" and not is_media_mime(mime):
        valid_image = True
    elif is_media_mime(mime):
        valid_media = True
    elif is_image_mime(mime):
        valid_image = True
    else:
        valid_media = not item.media_url
        mime = "audio/*"
    if valid_media and url and (not item.media_url or (is_default and not had_default)):
        item.media_url = urljoin(base_url, url) if base_url else url
        item.media_type = mime
        item.media_size = _int(child.get("fileSize"))
        duration = _int(child.get("duration"))
        if 0 < duration <= MAX_SECONDS:
            item.duration = duration * 1000
        return had_default or is_default
    if valid_image and url and not item.image_url:
        item.image_url = url
    return had_default


def _parse_psc(element):
    chapters = []
    for chapter in element:
        if _tag(chapter) != "psc:chapter" or not chapter.get("start"):
            continue
        try:
            start = parse_time_string(chapter.get("start"))
        except ValueError:
            continue
        chapters.append({"start": start, "title": chapter.get("title") or "", "url": chapter.get("href") or "",
                         "image": chapter.get("image") or ""})
    chapters.sort(key=lambda c: c["start"])
    return chapters


def _parse_atom(root, base_url):
    feed = ParsedFeed()
    entries = []
    for child in root:
        tag = _tag(child).replace("atom:", "")
        if tag == "title":
            feed.title = _text(child)
        elif tag == "subtitle":
            feed.description = _inner_xml(child)
        elif tag == "link":
            rel = (child.get("rel") or "alternate").lower()
            href = child.get("href") or ""
            if rel == "alternate" and not feed.link:
                feed.link = href
            elif rel == "next" and href:
                feed.next_page_url = urljoin(base_url, href)
            elif rel == "payment" and href:
                feed.funding.append({"url": href, "title": child.get("title") or ""})
        elif tag in ("logo", "icon"):
            if tag == "logo" or not feed.image_url:
                feed.image_url = _text(child)
        elif tag == "author" and not feed.author:
            name = next((c for c in child if _tag(c).replace("atom:", "") == "name"), None)
            feed.author = _text(name)
        elif tag == "itunes:image":
            feed.image_url = child.get("href") or feed.image_url
        elif tag == "itunes:author" and not feed.author:
            feed.author = _text(child)
        elif tag == "itunes:summary" and len(_inner_xml(child)) > len(feed.description):
            feed.description = _inner_xml(child)
        elif tag == "itunes:new-feed-url" and _text(child).startswith("http"):
            feed.new_feed_url = _text(child)
        elif tag == "podcast:funding" and child.get("url"):
            feed.funding.append({"url": child.get("url"), "title": _text(child)})
        elif tag == "entry":
            entries.append(child)
    for entry in entries:
        item = ParsedItem()
        descriptions = []
        transcripts = []
        updated = 0
        for child in entry:
            tag = _tag(child).replace("atom:", "")
            if tag == "id":
                item.guid = _text(child)
            elif tag == "title":
                item.title = _text(child)
            elif tag == "link":
                rel = (child.get("rel") or "alternate").lower()
                href = (child.get("href") or "").strip()
                if rel == "enclosure" and href and not item.media_url:
                    mime = guess_mime(child.get("type"), href)
                    if is_media_mime(mime) or not is_image_mime(mime):
                        item.media_url = urljoin(base_url, href)
                        item.media_type = mime or "audio/*"
                        item.media_size = _int(child.get("length"))
                elif rel == "alternate" and href and not item.link:
                    item.link = href
                elif rel == "payment":
                    pass
            elif tag in ("summary", "content"):
                descriptions.append(_inner_xml(child))
            elif tag == "published":
                item.pub_date = parse_date(_text(child))
            elif tag == "updated":
                updated = parse_date(_text(child))
            elif tag == "itunes:image":
                item.image_url = child.get("href") or item.image_url
            elif tag == "media:thumbnail" and not item.image_url:
                item.image_url = child.get("url") or ""
            elif tag == "itunes:duration":
                item.duration = parse_duration(_text(child))
            elif tag == "itunes:summary":
                descriptions.append(_inner_xml(child))
            elif tag == "itunes:season":
                item.season = _int(_text(child))
            elif tag == "itunes:episode":
                item.episode_number = _int(_text(child))
            elif tag == "itunes:episodeType":
                item.episode_type = _text(child).lower()
            elif tag == "podcast:chapters" and child.get("url"):
                item.chapters_url = child.get("url")
                item.chapters_type = child.get("type") or "application/json+chapters"
            elif tag == "podcast:transcript" and child.get("url") and child.get("type"):
                transcripts.append((child.get("type").lower(), child.get("url")))
            elif tag == "psc:chapters":
                item.psc_chapters = _parse_psc(child)
            elif tag == "media:content":
                _media_content(item, child, False, base_url)
        if not item.pub_date:
            item.pub_date = updated
        if descriptions:
            item.description = max(descriptions, key=len)
        if transcripts:
            transcripts.sort(key=lambda t: _transcript_rank(t[0]))
            item.transcript_type, item.transcript_url = transcripts[0]
        if item.identifier:
            feed.items.append(item)
    return feed
