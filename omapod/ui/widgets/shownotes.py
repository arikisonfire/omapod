"""Show notes: HTML to Pango markup with clickable links and timestamps.

There is no web view on Omarchy by default, and show notes need little more
than paragraphs, lists, emphasis and links. Timestamps such as 12:34 become
links that seek the player, as in AntennaPod.
"""

import html
import re
from html.parser import HTMLParser

from gi.repository import GLib, Gtk, Pango

from ...util import looks_like_html, parse_time_string

SEEK_SCHEME = "omapod-seek:"
_URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)]")
_TIME_RE = re.compile(r"(?<![\d:])((?:\d{1,2}:)?\d{1,2}:\d{2})(?![\d:])")
BLOCK = {"p", "div", "section", "article", "blockquote", "pre", "table", "tr", "ul", "ol", "h1", "h2", "h3", "h4",
         "h5", "h6", "figure", "header", "footer"}
# GtkLabel is laid out on the main thread, in quadratic time for a long paragraph (worse for a long word: 100 KB
# without a space took five minutes) and in time that grows with links × text (4000 links in 80 KB: seconds).
# So longer show notes are cut, a paragraph breaks after MAX_PARAGRAPH characters (at a space if there is one),
# and links past the budget stay plain text.
MAX_CHARS = 100_000
MAX_PARAGRAPH = 2000
MAX_LINKS = 500
LINK_BUDGET = 10_000_000  # links × characters
_TAG = re.compile(r"(<[^>]*>)")


class _Converter(HTMLParser):
    def __init__(self, link_timestamps):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.lists = []
        self.link_timestamps = link_timestamps
        self.in_link = 0
        self.skip = 0
        # What _newline needs to know about the text so far, kept up to date instead of joining all parts for
        # every block (quadratic time for many paragraphs).
        self.has_text = False
        self.trailing_newlines = 0

    def _emit(self, text):
        self.parts.append(text)
        body = text.rstrip("\n")
        self.trailing_newlines = len(text) - len(body) + (self.trailing_newlines if not body else 0)
        self.has_text = self.has_text or bool(text.strip())

    def _newline(self, count=1):
        if self.has_text and self.trailing_newlines < count:
            self._emit("\n" * (count - self.trailing_newlines))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
            return
        if tag in BLOCK:
            self._newline(2 if tag not in ("li", "tr", "div") else 1)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("<b>")
            self.stack.append("</b>")
        elif tag in ("b", "strong"):
            self._emit("<b>")
        elif tag in ("i", "em", "cite"):
            self._emit("<i>")
        elif tag == "u":
            self._emit("<u>")
        elif tag in ("s", "strike", "del"):
            self._emit("<s>")
        elif tag in ("code", "tt", "kbd"):
            self._emit("<tt>")
        elif tag == "br":
            self._emit("\n")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            self._newline(1)
            depth = min(max(0, len(self.lists) - 1), 8)
            marker = "•"
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}."
            self._emit("    " * depth + marker + " ")
        elif tag == "a":
            href = (attrs.get("href") or "").strip()
            if href and not href.lower().startswith("javascript:"):
                self._emit(f'<a href="{html.escape(href, quote=True)}">')
                self.in_link += 1
                self.stack.append("a")
        elif tag == "img":
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self._emit(f"<i>[{html.escape(alt)}]</i>")
        elif tag == "hr":
            self._newline(2)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if "</b>" in self.stack:
                self.stack.remove("</b>")
                self._emit("</b>")
        elif tag in ("b", "strong"):
            self._emit("</b>")
        elif tag in ("i", "em", "cite"):
            self._emit("</i>")
        elif tag == "u":
            self._emit("</u>")
        elif tag in ("s", "strike", "del"):
            self._emit("</s>")
        elif tag in ("code", "tt", "kbd"):
            self._emit("</tt>")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline(2)
        elif tag == "a" and self.in_link:
            if "a" in self.stack:
                self.stack.remove("a")
            self.in_link -= 1
            self._emit("</a>")
        if tag in BLOCK:
            self._newline(2 if tag not in ("li", "tr", "div") else 1)

    def handle_data(self, data):
        if self.skip:
            return
        data = re.sub(r"\s+", " ", data)
        escaped = html.escape(data, quote=False)
        if not self.in_link:
            escaped = _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
        if self.link_timestamps and not self.in_link:
            escaped = _TIME_RE.sub(lambda m: f'<a href="{SEEK_SCHEME}{m.group(1)}">{m.group(1)}</a>', escaped)
        self._emit(escaped)

    def result(self):
        text = "\n".join(line.rstrip(" \t") for line in "".join(self.parts).split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _plain(value):
    return GLib.markup_escape_text(re.sub(r"<[^<>]+>", " ", value))


def _for_label(markup):
    allowed = min(MAX_LINKS, LINK_BUDGET // max(1, len(markup)))
    links = open_links = run = 0
    out = []
    for token in _TAG.split(markup):
        if token.startswith("<"):
            if token == "</a>":
                if not open_links:
                    continue  # the end of a link that was left out
                open_links -= 1
            elif token.startswith("<a "):
                links += 1
                if links > allowed:
                    continue
                open_links += 1
            out.append(token)
            continue
        for index, line in enumerate(token.split("\n")):
            if index:
                out.append("\n")
                run = 0
            while run + len(line) > MAX_PARAGRAPH:
                room = MAX_PARAGRAPH - run
                cut = line.rfind(" ", 0, room + 1)
                if cut >= 0:
                    out.append(line[:cut] + "\n")
                    line = line[cut + 1:]
                else:  # within a word, but not within an entity such as &amp;
                    cut = room
                    entity = line.rfind("&", max(0, cut - 8), cut)
                    if entity >= 0 and ";" not in line[entity:cut]:
                        cut = entity
                    out.append(line[:cut] + "\n")
                    line = line[cut:]
                run = 0
            out.append(line)
            run += len(line)
    return "".join(out)


def to_markup(value, link_timestamps=True):
    if not value:
        return ""
    if len(value) > MAX_CHARS:
        value = value[:MAX_CHARS] + "…"
    if not looks_like_html(value):
        value = html.escape(value).replace("\n", "<br>")
        value = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', value)
    converter = _Converter(link_timestamps)
    try:
        converter.feed(value)
        converter.close()
        markup = _for_label(converter.result())
    except Exception:
        return _for_label(_plain(value))
    # Validate (GtkLabel handles <a> itself, Pango does not know it); fall back to plain text.
    try:
        Pango.parse_markup(re.sub(r"</?a\b[^>]*>", "", markup), -1, "\0")
    except GLib.Error:
        return _for_label(_plain(value))
    return markup


class ShowNotes(Gtk.Label):
    def __init__(self, ctx, on_seek=None):
        super().__init__(xalign=0, yalign=0, wrap=True, selectable=True)
        self.ctx = ctx
        self.on_seek = on_seek
        self.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.set_max_width_chars(80)
        self.add_css_class("omp-notes")
        self.connect("activate-link", self._on_link)

    def set_notes(self, value, empty_text=""):
        markup = to_markup(value)
        if markup:
            self.set_markup(markup)
        else:
            self.set_markup(f"<i>{GLib.markup_escape_text(empty_text)}</i>")

    def _on_link(self, _label, uri):
        if uri.startswith(SEEK_SCHEME):
            try:
                ms = parse_time_string(uri[len(SEEK_SCHEME):])
            except ValueError:
                return True
            if self.on_seek:
                self.on_seek(ms)
            return True
        self.ctx.open_uri(uri)
        return True
