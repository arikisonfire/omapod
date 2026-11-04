"""Podcast directories: Apple Podcasts, fyyd, gpodder.net and Podcast Index.

Same endpoints as AntennaPod's net/discovery module. Podcast Index needs an
API key and secret (free at api.podcastindex.org), which the user enters
in the settings; AntennaPod ships its own, which we must not reuse.
"""

import hashlib
import time
from dataclasses import dataclass
from urllib.parse import quote

from .. import USER_AGENT
from ..i18n import _
from ..util import clip, web_url
from . import http

MAX_DESCRIPTION = 2000  # results show a line or two; a podcast's own description comes from its feed


@dataclass
class SearchResult:
    title: str
    feed_url: str
    image_url: str = ""
    author: str = ""
    episodes: int = 0
    description: str = ""
    needs_lookup: bool = False


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _count(value):
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if 0 <= number < 10 ** 7 else 0


def _result(title, feed_url, image_url="", author="", episodes=0, description="", needs_lookup=False):
    """A result as a directory sent it, held to the rules feeds keep to. Directories list feeds anyone can
    publish: names are clipped like a feed's (util.clip), covers must be web addresses (a file: one would be
    read from this computer) and counts are numbers or 0."""
    feed_url = _text(feed_url)
    return SearchResult(title=clip(_text(title) or feed_url), feed_url=feed_url, image_url=web_url(_text(image_url)),
                        author=clip(_text(author)), episodes=_count(episodes),
                        description=_text(description)[:MAX_DESCRIPTION], needs_lookup=needs_lookup)


def _entries(data, key=None):
    """The entries of a directory's answer that are objects; an answer of another shape has none."""
    if key is not None:
        data = data.get(key) if isinstance(data, dict) else None
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


PROVIDERS = {
    "itunes": "Apple Podcasts",
    "fyyd": "fyyd",
    "gpodder": "gpodder.net",
    "podcastindex": "Podcast Index",
}


def search(provider, query):
    query = query.strip()
    if not query:
        return []
    if query.lower().startswith(("http://", "https://", "feed://")):
        return [_result(query, query)]
    if provider == "fyyd":
        return _fyyd(query)
    if provider == "gpodder":
        return _gpodder(query)
    if provider == "podcastindex":
        return _podcastindex(query)
    return _itunes(query)


def _itunes(query):
    data = http.get_json(f"https://itunes.apple.com/search?media=podcast&term={quote(query)}")
    results = []
    for entry in _entries(data, "results"):
        if not _text(entry.get("feedUrl")):
            continue
        results.append(_result(
            title=_text(entry.get("collectionName")) or entry.get("trackName"),
            feed_url=entry.get("feedUrl"),
            image_url=_text(entry.get("artworkUrl600")) or entry.get("artworkUrl100"),
            author=entry.get("artistName"),
            episodes=entry.get("trackCount"),
        ))
    return results


def _fyyd(query):
    data = http.get_json(f"https://api.fyyd.de/0.2/search/podcast?title={quote(query)}&count=50")
    results = []
    for entry in _entries(data, "data"):
        if not _text(entry.get("xmlURL")):
            continue
        results.append(_result(
            title=entry.get("title"),
            feed_url=entry.get("xmlURL"),
            image_url=_text(entry.get("imgURL")) or entry.get("layoutImageURL"),
            author=entry.get("author"),
            episodes=entry.get("episode_count"),
            description=entry.get("description"),
        ))
    return results


def _gpodder(query, host="https://gpodder.net"):
    data = http.get_json(f"{host}/search.json?q={quote(query)}")
    results = []
    for entry in _entries(data):
        if not _text(entry.get("url")):
            continue
        results.append(_result(
            title=entry.get("title"),
            feed_url=entry.get("url"),
            image_url=_text(entry.get("scaled_logo_url")) or entry.get("logo_url"),
            author=entry.get("author"),
            description=entry.get("description"),
        ))
    return results


def podcastindex_headers(key, secret):
    now = str(int(time.time()))
    digest = hashlib.sha1((key + secret + now).encode("utf-8")).hexdigest()
    return {"X-Auth-Date": now, "X-Auth-Key": key, "Authorization": digest, "User-Agent": USER_AGENT}


def _podcastindex(query):
    from ..config import credentials

    keys = credentials().get("podcastindex")
    key, secret = keys.get("key") or "", keys.get("secret") or ""
    if not key or not secret:
        raise http.HttpError(_("Podcast Index needs an API key and secret. Add them in Settings → Search."))
    data = http.get_json(f"https://api.podcastindex.org/api/1.0/search/byterm?q={quote(query)}",
                         headers=podcastindex_headers(key, secret))
    results = []
    for entry in _entries(data, "feeds"):
        if not _text(entry.get("url")):
            continue
        results.append(_result(
            title=entry.get("title"),
            feed_url=entry.get("url"),
            image_url=_text(entry.get("image")) or entry.get("artwork"),
            author=entry.get("author"),
            episodes=entry.get("episodeCount"),
            description=entry.get("description"),
        ))
    return results


def top_podcasts(country, limit=25, subscribed_titles=()):
    """Apple's top list (needs a lookup per result to find the feed URL)."""
    country = (country or "us").lower()
    url = f"https://itunes.apple.com/{country}/rss/toppodcasts/limit={max(limit, 25)}/explicit=true/json"
    try:
        data = http.get_json(url)
    except http.HttpError:
        if country == "us":
            raise
        data = http.get_json("https://itunes.apple.com/us/rss/toppodcasts/limit=25/explicit=true/json")
    feed = data.get("feed") if isinstance(data, dict) else None
    entries = feed.get("entry") if isinstance(feed, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    subscribed = {t.strip() for t in subscribed_titles}
    results = []
    for entry in _entries(entries):
        try:
            title = _text(entry["im:name"]["label"])
            itunes_id = str(entry["id"]["attributes"]["im:id"])
        except (KeyError, TypeError):
            continue
        if not title or title in subscribed:
            continue
        image = ""
        for img in entry.get("im:image") or []:
            try:
                if int(img["attributes"]["height"]) >= 100:
                    image = img["label"]
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        artist = entry.get("im:artist")
        results.append(_result(title, f"https://itunes.apple.com/lookup?id={quote(itunes_id, safe='')}",
                               image_url=image, author=artist.get("label") if isinstance(artist, dict) else "",
                               needs_lookup=True))
        if len(results) >= limit:
            break
    return results


def resolve_feed_url(result_or_url):
    """Turn an Apple lookup URL into the real feed URL."""
    url = result_or_url.feed_url if isinstance(result_or_url, SearchResult) else result_or_url
    if "itunes.apple.com/lookup" in url or "podcasts.apple.com" in url:
        if "podcasts.apple.com" in url:
            import re

            match = re.search(r"id(\d+)", url)
            if not match:
                return url
            url = f"https://itunes.apple.com/lookup?id={match.group(1)}"
        data = http.get_json(url)
        for entry in _entries(data, "results"):
            if _text(entry.get("feedUrl")):
                return _text(entry["feedUrl"])
        raise http.HttpError(_("Apple Podcasts has no feed address for this podcast."))
    if url.lower().startswith("feed://"):
        return "https://" + url[7:]
    return url
