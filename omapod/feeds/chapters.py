"""Chapters from the feed (Podlove Simple Chapters), from Podcasting 2.0 JSON
files and from the media file itself, merged the way AntennaPod does it."""

import json

from ..util import MAX_SECONDS, clip


def parse_json_chapters(data):
    """Podcasting 2.0 chapters file -> [{'start', 'title', 'url', 'image'}] or None."""
    try:
        obj = json.loads(data if isinstance(data, str) else data.decode("utf-8", "replace"))
        items = obj.get("chapters")
    except (ValueError, AttributeError):
        return None
    if not isinstance(items, list):
        return None
    chapters = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry.get("startTime", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not 0 <= start <= MAX_SECONDS:  # also NaN and infinity, which would stop the whole list
            continue
        chapters.append({
            "start": int(round(start * 1000)),
            "title": clip(str(entry.get("title") or "")),
            "url": str(entry.get("url") or ""),
            "image": str(entry.get("img") or ""),
        })
    chapters.sort(key=lambda c: c["start"])
    return chapters


def from_mpv(chapter_list):
    """mpv's chapter-list property -> chapters."""
    chapters = []
    for entry in chapter_list or []:
        try:
            start = float(entry.get("time", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not 0 <= start <= MAX_SECONDS:  # the media file decides these
            continue
        chapters.append({"start": int(round(start * 1000)), "title": clip(str(entry.get("title") or "")),
                         "url": "", "image": ""})
    chapters.sort(key=lambda c: c["start"])
    return chapters or None


def _score(chapters):
    return sum(bool(c.get("title")) + bool(c.get("url")) + bool(c.get("image")) for c in chapters)


def merge(first, second):
    """AntennaPod's ChapterMerger: the longer list wins; equal lists are combined."""
    if not first:
        return second or first
    if not second:
        return first
    if len(second) != len(first):
        return second if len(second) > len(first) else first
    merged = []
    for target, other in zip(second, first):
        if abs(target["start"] - other["start"]) > 1000:
            return first if _score(first) > _score(second) else second
        merged.append({
            "start": target["start"],
            "title": target.get("title") or other.get("title") or "",
            "url": target.get("url") or other.get("url") or "",
            "image": target.get("image") or other.get("image") or "",
        })
    return merged


def index_at(chapters, position_ms):
    """Index of the chapter playing at position_ms, or -1."""
    current = -1
    for index, chapter in enumerate(chapters or []):
        if chapter["start"] <= position_ms + 50:
            current = index
        else:
            break
    return current
