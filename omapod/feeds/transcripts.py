"""Podcasting 2.0 transcripts: SRT, WebVTT, JSON, HTML and plain text."""

import html
import json
import re
from dataclasses import dataclass

from ..util import breakable, parse_time_string, text_pieces


@dataclass
class Segment:
    start: int
    end: int
    speaker: str
    text: str


# Not starting inside a run of digits, and tags without "<" inside: either would let a line of digits or of
# "<" take quadratic time.
_SRT_TIME = re.compile(r"(?<!\d)(\d+:)?\d+:\d+[,.]\d+\s*-->\s*(\d+:)?\d+:\d+[,.]\d+")
_VOICE = re.compile(r"<v(?:\.[\w.-]+)?\s+([^<>]+)>")
_TAGS = re.compile(r"<[^<>]+>")
_HTML_TAG = re.compile(r"<(/?)(cite|time|p)\b[^<>]*>", re.I)


def _clock(value):
    return parse_time_string(value.strip().replace(",", "."))


def _clean(text):
    return html.unescape(_TAGS.sub("", text)).strip()


def parse_srt_vtt(text):
    segments = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("﻿", ""))
    for block in blocks:
        lines = [line for line in block.strip().splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if _SRT_TIME.search(line)), None)
        if timing_index is None:
            continue
        start_text, _, end_text = lines[timing_index].partition("-->")
        end_text = end_text.strip().split(" ")[0]
        try:
            start, end = _clock(start_text), _clock(end_text)
        except ValueError:
            continue
        body = " ".join(lines[timing_index + 1:])
        speaker = ""
        voice = _VOICE.search(body)
        if voice:
            speaker = voice.group(1).strip()
        body = _clean(body)
        if not speaker:
            match = re.match(r"^([A-Z][\w .'-]{0,40}):\s+(.*)$", body)
            if match:
                speaker, body = match.group(1), match.group(2)
        if body:
            segments.append(Segment(start, end, speaker, body))
    return segments


def parse_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("segments") if isinstance(data, dict) else data
    segments = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            start = int(float(item.get("startTime", 0)) * 1000)
            end = int(float(item.get("endTime", item.get("startTime", 0))) * 1000)
        except (TypeError, ValueError, OverflowError):
            continue
        body = str(item.get("body") or "").strip()
        if body:
            segments.append(Segment(start, end, str(item.get("speaker") or ""), body))
    return segments


def parse_html(text):
    """<cite>Speaker:</cite> <time>0:00</time> <p>Text</p> as in the Podcasting 2.0 spec. One pass over the
    tags: a pattern that looks ahead for each closing tag takes quadratic time when they are missing."""
    segments = []
    speaker, start, opened = "", None, None
    for match in _HTML_TAG.finditer(text):
        closing, name = match.group(1), match.group(2).lower()
        if not closing:
            opened = (name, match.end())
            continue
        if opened is None or opened[0] != name:
            continue
        content, opened = text[opened[1]:match.start()], None
        if name == "cite":
            speaker = _clean(content).rstrip(":")
        elif name == "time":
            try:
                start = _clock(_clean(content))
            except ValueError:
                start = None
        elif start is not None:
            body = _clean(content)
            if body:
                segments.append(Segment(start, start, speaker, body))
            speaker, start = "", None
    for index, segment in enumerate(segments[:-1]):
        segment.end = segments[index + 1].start
    if not segments:
        body = _clean(re.sub(r"</p>|<br\s*/?>", "\n", text, flags=re.I))
        if body:
            segments.append(Segment(0, 0, "", body))
    return segments


def parse_transcript(data, mime):
    text = data if isinstance(data, str) else data.decode("utf-8", "replace")
    mime = (mime or "").lower()
    if "json" in mime:
        segments = parse_json(text)
    elif "html" in mime:
        segments = parse_html(text)
    elif "vtt" in mime or "srt" in mime or "subrip" in mime or _SRT_TIME.search(text[:2000]):
        segments = parse_srt_vtt(text)
    else:
        segments = [Segment(0, 0, "", text.strip())] if text.strip() else []
    return group(segments)


def group(segments, min_span=8000, max_span=40000, max_chars=2000):
    """Join short cues of the same speaker into readable paragraphs of at most max_chars: joining grows the
    text, so endless cues at one time would take quadratic time, and so does GTK for a long paragraph (a plain
    text transcript is a single one)."""
    grouped = []
    for segment in segments:
        last = grouped[-1] if grouped else None
        if (last is not None and segment.speaker == last.speaker and segment.start - last.start < max_span
                and len(last.text) < max_chars
                and (last.end - last.start < min_span or not last.text.rstrip('"').endswith((".", "!", "?", "…")))):
            last.text = f"{last.text} {segment.text}".strip()
            last.end = max(last.end, segment.end)
        else:
            grouped.append(Segment(segment.start, segment.end, segment.speaker, segment.text))
    return [Segment(segment.start, segment.end, segment.speaker if index == 0 else "", breakable(piece))
            for segment in grouped for index, piece in enumerate(text_pieces(segment.text, max_chars))]
