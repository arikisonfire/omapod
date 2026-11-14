"""Small helpers shared by the core and the UI."""

import hashlib
import html
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from .i18n import _, ngettext


def now_ms():
    return int(time.time() * 1000)


def now_s():
    return int(time.time())


def fmt_clock(ms, force_hours=False):
    """1:02:03 or 45:12 for a duration or position in milliseconds."""
    if ms is None or ms < 0:
        ms = 0
    total = int(ms // 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours or force_hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fmt_human_duration(ms, short=True):
    """'1 h 05 min' style, for list rows and totals."""
    if not ms or ms <= 0:
        return ""
    minutes = int(round(ms / 60000))
    if minutes < 1:
        return _("< 1 min")
    hours, minutes = divmod(minutes, 60)
    if hours and short:
        return _("{h} h {m:02d} min").format(h=hours, m=minutes) if minutes else _("{h} h").format(h=hours)
    if hours:
        return _("{h} h {m} min").format(h=hours, m=minutes)
    return _("{m} min").format(m=minutes)


def fmt_long_duration(ms):
    """Totals for statistics: '3 d 4 h', '12 h 30 min'."""
    if not ms or ms <= 0:
        return _("0 min")
    minutes = int(ms // 60000)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return _("{d} d {h} h").format(d=days, h=hours)
    if hours:
        return _("{h} h {m} min").format(h=hours, m=minutes)
    return _("{m} min").format(m=minutes)


def fmt_size(size):
    if not size or size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1000 or unit == units[-1]:
            if unit in ("B", "KB"):
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1000
    return ""


def fmt_date(ts, relative=True):
    """Publication dates: 'Today', 'Yesterday', '3 days ago', '12 Mar', '12 Mar 2024'."""
    if not ts:
        return ""
    try:
        when = datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError, TypeError):  # a time no calendar has (an imported library's)
        return ""
    today = datetime.now().date()
    delta = (today - when.date()).days
    if relative:
        if delta == 0:
            return _("Today")
        if delta == 1:
            return _("Yesterday")
        if 1 < delta < 7:
            return ngettext("{n} day ago", "{n} days ago", delta).format(n=delta)
    month = _(MONTHS_SHORT[when.month - 1])
    if when.year == today.year:
        return f"{when.day} {month}"
    return f"{when.day} {month} {when.year}"


MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]


def fmt_month(year, month, long=True):
    names = MONTHS_LONG if long else MONTHS_SHORT
    return f"{_(names[month - 1])} {year}" if year else _(names[month - 1])


def fmt_datetime(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def fmt_speed(speed):
    text = f"{speed:.2f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text + "×"


# Times beyond this many seconds (some 31 years) are garbage. The limit also keeps every value far inside the
# 64-bit integers SQLite stores.
MAX_SECONDS = 10 ** 9


def parse_time_string(text):
    """'01:02:03.500', '02:03', '83', '1:02:03' -> milliseconds. Raises ValueError, also for 'inf', 'nan' and
    times beyond MAX_SECONDS."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    total = 0.0
    for part in parts:
        total = total * 60 + float(part.replace(",", ".") or 0)
    if not math.isfinite(total) or abs(total) > MAX_SECONDS:
        raise ValueError(text)
    return int(round(total * 1000))


_TAG_RE = re.compile(r"<[^<>]+>")  # "[^>]" would take quadratic time on a run of "<"
_WS_RE = re.compile(r"\s+")


# Names (titles, authors, chapters) come from feeds and media files and are shown in labels that wrap, which GTK
# lays out in quadratic time for a long word: 20,000 characters take seconds. Real ones stay far below.
MAX_NAME = 500


def clip(text, limit=MAX_NAME):
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def breakable(text, longest=100):
    """text with an invisible break opportunity (a zero-width space) in every run of more than `longest`
    characters without a space: labels of such words take seconds, not milliseconds (see clip)."""
    if len(text) <= longest:
        return text
    parts = re.split(r"(\s+)", text)
    for index in range(0, len(parts), 2):
        word = parts[index]
        if len(word) > longest:
            parts[index] = "\u200b".join(word[i:i + longest] for i in range(0, len(word), longest))
    return "".join(parts)


def text_pieces(text, limit):
    """text cut into pieces of at most limit characters, at a space where there is one (see clip)."""
    pieces = []
    while len(text) > limit:
        cut = text.rfind(" ", 1, limit + 1)
        if cut < 0:
            pieces.append(text[:limit])
            text = text[limit:]
        else:
            pieces.append(text[:cut])
            text = text[cut + 1:]
    pieces.append(text)
    return pieces


def html_to_text(value, limit=None):
    """Plain text from HTML show notes (for search, snippets, MPRIS)."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def looks_like_html(value):
    return bool(value) and bool(re.search(r"<(p|br|a|div|ul|ol|li|b|i|strong|em|h\d|img|span)\b", value, re.I))


def safe_filename(name, max_len=120, max_bytes=240):
    """At most max_len characters and max_bytes bytes: file systems count the 255 they allow in bytes, and a
    Japanese or Chinese character takes three."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", name)
    name = _WS_RE.sub(" ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    if len(name.encode("utf-8")) > max_bytes:
        name = name.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").rstrip(" .")
    return name or "untitled"


def web_url(url):
    """url if it is an http(s) address, else "": for links in pages omaPOD writes from feed data."""
    url = (url or "").strip()
    return url if urlsplit(url).scheme.lower() in ("http", "https") else ""


# Logins in addresses
#
# https://user:password@host/feed carries a login. An address goes wherever addresses go: to the sync server, into
# exports, the download log, the bar widget, a notification. So no address omaPOD keeps or passes on carries one.
# A podcast's login is kept apart from its address (the feeds table's username and password) and is sent to the
# podcast's own server only (net.http.auth_allowed). The rule is applied where addresses come in (the database's
# insert and update, the feed parser, what a sync server answers), to what earlier versions stored (when the
# database opens) and once more where they leave for the sync server.

# scheme://[anything up to the last @ before the path]@rest. The same reading as urllib's and ffmpeg's: the
# login ends at the last "@" in front of the first "/", "?" or "#".
_LOGIN_RE = re.compile(r"(?is)^\s*([a-z][a-z0-9+.\-]*://)([^/?#]*)@(.*)$")
# An address every program reads the same way: http or https, a host, no login, and nothing over which URL
# parsers disagree about where the host ends (a backslash, white space) nor any control character.
_PLAIN_WEB_RE = re.compile(r"(?i)^https?://[^\s/?#@\\\x00-\x1f\x7f]+(?:[/?#][^\x00-\x1f\x7f]*)?$")


def split_login(url):
    """https://user:pass@host/feed -> (https://host/feed, "user", "pass"). Also a password without a name
    (https://:token@host/feed) and a name without a password. An address without a login comes back as it is.
    The host keeps its port and, for an IPv6 address, its brackets."""
    if not isinstance(url, str):
        return "", "", ""
    match = _LOGIN_RE.match(url)
    if match is None:
        return url, "", ""
    scheme, login, rest = match.groups()
    user, _colon, password = login.partition(":")
    return scheme + rest.strip(), unquote(user), unquote(password)


def plain_address(url):
    """The address without a login in it (see split_login)."""
    return split_login(url)[0]


def sync_address(url):
    """The address as it may go to a sync server: a web address without a login that every URL parser reads the
    same way, or "" for anything else. A server and the apps that sync with it read addresses with parsers of
    their own, and what one of them takes for a login must never be sent."""
    address = plain_address(url).strip()
    if not _PLAIN_WEB_RE.match(address):
        return ""
    try:
        parts = urlsplit(address)
        host, _port = parts.hostname, parts.port  # .port raises for a port that is no number
    except ValueError:
        return ""
    return address if host else ""


def sync_feed_address(url):
    """A podcast's address as it goes to a sync server (sync_address), without a fragment: what follows "#"
    never reaches the feed's server, and two podcasts of one feed can differ only there
    (db.Database._free_address)."""
    if not isinstance(url, str):
        return ""
    return sync_address(plain_address(url).split("#", 1)[0])


def scrub_logins(value):
    """A copy of JSON-like data in which no text is an address with a login: the last check on anything that is
    about to leave for a sync server."""
    if isinstance(value, str):
        return plain_address(value) if _LOGIN_RE.match(value) else value
    if isinstance(value, dict):
        return {key: scrub_logins(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_logins(item) for item in value]
    return value


def mail_link(uri):
    """A mailto: link from a feed as it goes to the mail program: whom to write to and, if the link has one, the
    subject. Mail programs take more from such a link (a text, further recipients, some of them files to attach),
    and that is not for a feed to fill in."""
    parts = urlsplit(uri.strip())
    address = "".join(c for c in unquote(parts.path) if c.isprintable())
    subject = next((value for key, value in parse_qsl(parts.query) if key.lower() == "subject"), "")
    subject = "".join(c for c in subject if c.isprintable())
    return "mailto:" + quote(address, safe="@,.+-_") + ("?subject=" + quote(subject, safe="") if subject else "")


def url_hash(url):
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()


def clamp(value, low, high):
    return max(low, min(high, value))


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(value):
    """Like html_to_text but via the HTML parser, keeping entity decoding exact."""
    parser = _TextExtractor()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        return html_to_text(value)
    return _WS_RE.sub(" ", "".join(parser.parts)).strip()


def days_ago(days):
    return int((datetime.now() - timedelta(days=days)).timestamp())
