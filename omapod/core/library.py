"""Podcast library rules.

Everything that changes episodes or podcasts goes through Library, so the
side effects AntennaPod has (inbox, queue, sync actions, auto-download,
cleanup, download log) happen in one place regardless of who triggers
them: the UI, the player, the bar plugin or the headless refresh.
"""

import os
import random
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..events import bus
from ..feeds import chapters as chapter_utils
from ..feeds.parser import FeedParseError, NotAFeedError, parse_feed
from ..i18n import _, ngettext
from ..models import (ACTION_GLOBAL, ACTION_INBOX, ACTION_NOTHING, ACTION_QUEUE, AUDIO_EXTENSIONS, FEED_ARCHIVED,
                      FEED_PREVIEW, FEED_SUBSCRIBED, LOCAL_PREFIX, NEW, PLAYED, UNPLAYED, VIDEO_EXTENSIONS)
from ..net import http
from ..util import now_ms, now_s

ACTION_CODES = {"inbox": ACTION_INBOX, "queue": ACTION_QUEUE, "nothing": ACTION_NOTHING}


class AuthRequired(Exception):
    def __init__(self, url, feed_id=0):
        super().__init__(_("This podcast needs a username and password."))
        self.url = url
        self.feed_id = feed_id


@dataclass
class RefreshResult:
    feed_id: int = 0
    new_ids: list = field(default_factory=list)
    error: str = ""
    not_modified: bool = False


def _norm_title(title):
    return re.sub(r"\W+", " ", (title or "").casefold()).strip()


def parse_terms(text):
    """AntennaPod's FeedFilter.parseTerms: words, or "quoted phrases"."""
    return [m.group(1).replace('"', "") for m in re.finditer(r'([^"]\S*|".+?")\s*', text or "") if m.group(1).strip()]


def should_auto_download(feed, episode_title, duration_ms):
    """AntennaPod's FeedFilter.shouldAutoDownload."""
    include = [t.lower() for t in parse_terms(feed.include_terms)]
    exclude = [t.lower() for t in parse_terms(feed.exclude_terms)]
    if not include and not exclude and feed.min_duration <= -1:
        return True
    if feed.min_duration > -1 and duration_ms > 0 and duration_ms / 1000 < feed.min_duration:
        return False
    title = (episode_title or "").lower()
    if any(term in title for term in exclude):
        return False
    if include:
        return any(term in title for term in include)
    return True


def on_mains_power():
    """True when charging or on AC (or when we cannot tell)."""
    base = Path("/sys/class/power_supply")
    try:
        supplies = list(base.iterdir())
    except OSError:
        return True
    saw_battery = False
    for supply in supplies:
        try:
            kind = (supply / "type").read_text().strip()
        except OSError:
            continue
        if kind == "Mains":
            try:
                if (supply / "online").read_text().strip() == "1":
                    return True
            except OSError:
                pass
        elif kind == "Battery":
            saw_battery = True
            try:
                if (supply / "status").read_text().strip() in ("Charging", "Full"):
                    return True
            except OSError:
                pass
    return not saw_battery


def move_to_trash(path):
    """Moves a file into the desktop's trash (Gio follows the freedesktop.org trash spec), so it can be restored."""
    from gi.repository import Gio, GLib

    try:
        Gio.File.new_for_path(str(path)).trash(None)
    except GLib.Error as error:
        raise OSError(error.message) from None


class Library:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.downloads = None
        self.player = None
        self._refresh_lock = threading.Lock()
        self._feed_locks = {}
        self.refreshing = False
        self.stopping = False  # the background refresh was asked to make way for the app

    # ------------------------------------------------------------ helpers

    def _feed_lock(self, feed_id):
        lock = self._feed_locks.get(feed_id)
        if lock is None:
            lock = self._feed_locks.setdefault(feed_id, threading.Lock())
        return lock

    def _changed(self, episode_ids=None, feeds=True, queue=False):
        bus.emit("episodes-changed", set(episode_ids) if episode_ids is not None else None)
        if feeds:
            bus.emit("feeds-changed", None)
        if queue:
            bus.emit("queue-changed")

    def current_episode_id(self):
        return self.player.current_id() if self.player else None

    def global_new_action(self):
        return ACTION_CODES.get(self.settings.get("new_episodes_action"), ACTION_INBOX)

    def sync_enabled(self):
        return bool(self.settings.get("sync_provider"))

    # ------------------------------------------------------------ sync queue hooks

    def sync_feed(self, url, added):
        if not self.sync_enabled() or not url or url.startswith(LOCAL_PREFIX):
            return
        kind = "sub"
        for row_id, payload in self.db.sync_pending(kind):
            if payload.get("url") == url:
                self.db.sync_remove([row_id])
        self.db.sync_enqueue(kind, {"url": url, "action": "add" if added else "remove"})
        bus.emit("sync-requested")

    def sync_action(self, episode, action, started=-1, position=-1, total=-1):
        if not self.sync_enabled() or not episode or not episode.media_url:
            return
        if episode.feed_url.startswith(LOCAL_PREFIX) or episode.feed_state == FEED_PREVIEW:
            return
        payload = {
            "podcast": episode.feed_url,
            "episode": episode.media_url,
            "action": action,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if episode.guid:
            payload["guid"] = episode.guid
        if action == "play":
            payload.update(started=int(started), position=int(position), total=int(total))
        self.db.sync_enqueue("action", payload)
        # Positions go up within seconds, not with the next sync: a phone that syncs in between would never get
        # them (servers hand out only actions newer than its last sync).
        bus.emit("sync-push" if action == "play" else "sync-requested")

    def sync_episode_played(self, episode, start_ms, completed):
        """AntennaPod's enqueueEpisodePlayed."""
        if episode is None or start_ms < 0:
            return
        if not completed and start_ms >= episode.position:
            return
        duration = episode.duration
        position = duration if completed else episode.position
        self.sync_action(episode, "play", start_ms // 1000, position // 1000, duration // 1000)

    # ------------------------------------------------------------ fetching

    def fetch(self, url, username="", password="", etag="", last_modified="", auth_url=""):
        """auth_url: the podcast's own address when fetching another one for it (its next pages). The password
        goes only to that address's server, never to another host, even one that asks for it. A 401 raises
        AuthRequired with the address that asked, which a redirect may have led to."""
        auth = (username, password) if username else None
        try:
            response = http.request(url, auth=auth, etag=etag, last_modified=last_modified, auth_url=auth_url,
                                    headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                                                       "text/xml;q=0.8, */*;q=0.5"})
        except http.HttpError as error:
            if error.status == 401:
                raise AuthRequired(error.final_url or url) from None
            raise
        return response

    def preview(self, url, username="", password=""):
        """Fetch a feed and store it as a preview (or return the existing podcast). Returns feed id."""
        url = url.strip()
        if url.lower().startswith("feed://"):
            url = "https://" + url[7:]
        if not re.match(r"^https?://", url, re.I) and not url.startswith(LOCAL_PREFIX):
            url = "https://" + url
        existing = self.db.get_feed_by_url(url)
        if existing is not None:
            return existing.id
        response = self.fetch(url, username, password)
        parsed = parse_feed(response.body, response.url)
        final_url = response.permanent_url or url
        if username and not http.auth_allowed(url, final_url):
            final_url = url  # the password belongs to the address it was entered for (see _move_feed)
        existing = self.db.get_feed_by_url(final_url)
        if existing is not None:
            return existing.id
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {
                "url": final_url, "state": FEED_PREVIEW, "added": now_s(), "username": username,
                "password": password, "etag": response.etag, "last_modified": response.last_modified,
            })
            self._merge(conn, feed_id, parsed, first_fetch=True)
            self.db.update_feed(feed_id, conn=conn, last_refresh=now_s(), last_refresh_attempt=now_s())
        self._changed(feeds=True)
        return feed_id

    def subscribe_url(self, url, username="", password=""):
        feed_id = self.preview(url, username, password)
        self.subscribe(feed_id)
        return feed_id

    def subscribe(self, feed_id):
        feed = self.db.get_feed(feed_id)
        if feed is None or feed.state == FEED_SUBSCRIBED:
            return
        self.db.update_feed(feed_id, state=FEED_SUBSCRIBED, keep_updated=1, added=feed.added or now_s())
        self.sync_feed(feed.url, True)
        # Played state of episodes listened to during the preview goes to the sync server too.
        for episode in self.db.get_episodes(("played",), feed_id=feed_id):
            episode.feed_state = FEED_SUBSCRIBED
            self.sync_action(episode, "play", episode.duration // 1000, episode.duration // 1000,
                             episode.duration // 1000)
        self._changed(feeds=True)
        bus.emit("message", _("Subscribed to {title}").format(title=feed.display_title), "info")

    def unsubscribe(self, feed_ids, sync=True):
        for feed_id in list(feed_ids):
            feed = self.db.get_feed(feed_id)
            if feed is None:
                continue
            episodes = self.db.get_episodes(feed_id=feed_id)
            current = self.current_episode_id()
            if current and any(e.id == current for e in episodes) and self.player:
                self.player.stop()
            if self.downloads:
                for episode in episodes:
                    self.downloads.cancel(episode.id)
            if not feed.is_local:
                for episode in episodes:
                    self._remove_file(episode)
                folder = self.feed_folder(feed)
                try:
                    if folder.exists() and not any(folder.iterdir()):
                        folder.rmdir()
                except OSError:
                    pass
            self.db.delete_feed(feed_id)
            if feed.state == FEED_SUBSCRIBED and sync:
                self.sync_feed(feed.url, False)
        self._changed(queue=True)
        bus.emit("downloads-changed")

    def archive(self, feed_ids, archived=True):
        """Archiving stays on this computer. Synchronized, it could only be an unsubscribe, which removes the
        podcast with its downloads, favorites and playback state on the other devices. Restoring subscribes
        again, also on the server for podcasts archived before this was so."""
        for feed_id in feed_ids:
            feed = self.db.get_feed(feed_id)
            if feed is None:
                continue
            if archived:
                self.db.update_feed(feed_id, state=FEED_ARCHIVED, keep_updated=0)
            else:
                self.db.update_feed(feed_id, state=FEED_SUBSCRIBED, keep_updated=1)
                self.sync_feed(feed.url, True)
        self._changed()

    def cleanup_previews(self):
        """Forget previewed podcasts nobody subscribed to, unless something of them is still in use."""
        cutoff = now_s() - 86400
        for feed in self.db.get_feeds(states=(FEED_PREVIEW,), with_counts=False):
            if feed.added > cutoff:
                continue
            in_use = self.db.scalar(
                "SELECT COUNT(*) FROM episodes e LEFT JOIN queue q ON q.episode_id = e.id WHERE e.feed_id = ? AND "
                "(e.position > 0 OR e.downloaded_at > 0 OR e.completed_at > 0 OR e.favorite = 1 OR q.episode_id IS NOT NULL)",
                (feed.id,), 0)
            if not in_use:
                self.db.delete_feed(feed.id)

    # ------------------------------------------------------------ refresh

    def refresh_feed(self, feed_id, force=False, full=False):
        with self._feed_lock(feed_id):
            feed = self.db.get_feed(feed_id)
            if feed is None:
                return RefreshResult(feed_id, error="gone")
            if feed.is_local:
                return self._refresh_local(feed)
            result = RefreshResult(feed_id)
            self.db.update_feed(feed_id, last_refresh_attempt=now_s())
            try:
                response = self.fetch(feed.url, feed.username, feed.password,
                                      etag="" if force or full else feed.etag,
                                      last_modified="" if force or full else feed.last_modified)
                parsed = parse_feed(response.body, response.url)
                pages = [parsed]
                if full:
                    seen = {feed.url}
                    next_url = parsed.next_page_url
                    while next_url and next_url not in seen and len(pages) < 200:
                        seen.add(next_url)
                        page = parse_feed(self.fetch(next_url, feed.username, feed.password, auth_url=feed.url).body,
                                          next_url)
                        pages.append(page)
                        next_url = page.next_page_url
            except http.NotModified:
                self.db.update_feed(feed_id, last_refresh=now_s(), refresh_error="")
                result.not_modified = True
                return result
            except AuthRequired as error:
                # A host the feed redirects to never gets the podcast's password, so entering it again would not
                # help: name the host that asks instead.
                withheld = http.withheld(feed.url, error.url)
                result.error = withheld or _("Authentication error. Make sure that username and password are "
                                             "correct.")
                self._refresh_failed(feed, result.error, f"HTTP 401: {error.url}" if withheld else "HTTP 401")
                if not withheld:
                    bus.emit("auth-required", feed_id)
                return result
            except http.HttpError as error:
                result.error = str(error)
                self._refresh_failed(feed, result.error, error.detail)
                return result
            except NotAFeedError as error:
                result.error = _("The podcast host's server sent a website, not a podcast.")
                self._refresh_failed(feed, result.error, str(error))
                return result
            except FeedParseError as error:
                result.error = _("The podcast host's server sent a broken podcast feed.")
                self._refresh_failed(feed, result.error, str(error))
                return result
            with self.db.tx() as conn:
                for page in pages:
                    result.new_ids += self._merge(conn, feed_id, page, first_fetch=False,
                                                  update_meta=page is pages[0])
                self.db.update_feed(feed_id, conn=conn, etag=response.etag, last_modified=response.last_modified,
                                    last_refresh=now_s(), refresh_error="")
            moved_to = parsed.new_feed_url or response.permanent_url
            if moved_to and moved_to != feed.url:
                self._move_feed(feed, moved_to)
            if result.new_ids:
                self._after_new_episodes(feed_id, result.new_ids)
            return result

    def _refresh_failed(self, feed, reason, detail):
        self.db.update_feed(feed.id, refresh_error=reason)
        self.db.add_log("feed", feed.id, feed.display_title, feed.url, False, reason, detail)
        bus.emit("downloads-changed")

    def _move_feed(self, feed, new_url):
        if self.db.get_feed_by_url(new_url) is not None:
            return
        if feed.username and not http.auth_allowed(feed.url, new_url):
            # Its password would go to the new host with every refresh. The old address still answers or
            # redirects (the new host never gets the password); moving to another host is up to the user, by
            # editing the address.
            return
        self.db.update_feed(feed.id, url=new_url)
        if feed.state == FEED_SUBSCRIBED:
            self.sync_feed(feed.url, False)
            self.sync_feed(new_url, True)

    def _merge(self, conn, feed_id, parsed, first_fetch, update_meta=True):
        """Merge a parsed feed into the database, AntennaPod style. Returns ids of new episodes."""
        feed = self.db.get_feed(feed_id)
        if update_meta:
            values = {
                "title": parsed.title or feed.title,
                "author": parsed.author,
                "description": parsed.description,
                "link": parsed.link,
                "image_url": parsed.image_url or feed.image_url,
                "language": parsed.language,
                "feed_type": parsed.feed_type,
                "funding": parsed.funding,
                "podcast_guid": parsed.podcast_guid,
                "next_page_url": parsed.next_page_url,
            }
            self.db.update_feed(feed_id, conn=conn, **values)
        rows = self.db.feed_episode_rows(conn, feed_id)
        by_guid = {r["guid"]: r for r in rows if r["guid"]}
        by_media = {r["media_url"]: r for r in rows if r["media_url"]}
        by_title = {}
        for r in rows:
            by_title.setdefault(_norm_title(r["title"]), []).append(r)
        prior_most_recent = max((r["pub_date"] for r in rows), default=0)
        subscribed = feed.state == FEED_SUBSCRIBED
        action = feed.new_episodes_action if feed.new_episodes_action != ACTION_GLOBAL else self.global_new_action()
        if self._auto_download_enabled(feed):
            action = ACTION_INBOX
        new_ids, to_queue = [], []
        seen_in_feed = set()
        items = sorted(parsed.items, key=lambda i: i.pub_date or 0)
        for item in items:
            key = item.guid or item.media_url or _norm_title(item.title)
            if key in seen_in_feed:
                self.db.add_log("feed", feed_id, item.title, feed.url, False,
                                _("The podcast host appears to have added the same episode twice."),
                                f"GUID: {item.guid}\nURL: {item.media_url}")
                continue
            seen_in_feed.add(key)
            existing = by_guid.get(item.guid) if item.guid else None
            if existing is None and item.media_url:
                existing = by_media.get(item.media_url)
            if existing is None:
                for candidate in by_title.get(_norm_title(item.title), []):
                    same_day = abs((candidate["pub_date"] or 0) - (item.pub_date or 0)) < 86400 * 2
                    durations = candidate["duration"] or 0, item.duration or 0
                    similar = not all(durations) or abs(durations[0] - durations[1]) < max(durations) * 0.1 + 5000
                    if same_day and similar and _norm_title(item.title):
                        existing = candidate
                        break
            if existing is not None:
                updates = {
                    "title": item.title or existing["title"],
                    "link": item.link,
                    "description": item.description if len(item.description) >= len(existing["description"] or "") * 0.5
                    else existing["description"],
                    "image_url": item.image_url,
                    "chapters_url": item.chapters_url,
                    "chapters_type": item.chapters_type,
                    "transcript_url": item.transcript_url,
                    "transcript_type": item.transcript_type,
                    "season": item.season,
                    "episode_number": item.episode_number,
                    "episode_type": item.episode_type,
                    "social_url": item.social_url,
                }
                if item.psc_chapters:
                    updates["psc_chapters"] = item.psc_chapters
                if item.pub_date:
                    updates["pub_date"] = item.pub_date
                if item.guid and item.guid != existing["guid"]:
                    updates["guid"] = item.guid
                if item.media_url and item.media_url != existing["media_url"]:
                    if not existing["file_path"]:
                        updates["media_url"] = item.media_url
                        updates["media_type"] = item.media_type
                        updates["media_size"] = item.media_size
                    if not existing["media_url"]:
                        updates["state"] = NEW if subscribed else UNPLAYED
                if not existing["file_path"]:
                    if item.duration:
                        updates["duration"] = item.duration
                    if item.media_size:
                        updates["media_size"] = item.media_size
                    if item.media_type:
                        updates["media_type"] = item.media_type
                self.db.update_episode(existing["id"], conn=conn, **updates)
                continue
            state = UNPLAYED
            newer = not item.pub_date or not prior_most_recent or item.pub_date >= prior_most_recent
            enqueue = False
            if subscribed and not first_fetch and newer:
                if action == ACTION_INBOX:
                    state = NEW
                elif action == ACTION_QUEUE:
                    enqueue = True
            values = {
                "feed_id": feed_id, "guid": item.guid, "title": item.title, "link": item.link,
                "description": item.description, "pub_date": item.pub_date or now_s(), "image_url": item.image_url,
                "media_url": item.media_url, "media_type": item.media_type, "media_size": item.media_size,
                "duration": item.duration, "state": state, "season": item.season,
                "episode_number": item.episode_number, "episode_type": item.episode_type,
                "chapters_url": item.chapters_url, "chapters_type": item.chapters_type,
                "transcript_url": item.transcript_url, "transcript_type": item.transcript_type,
                "psc_chapters": item.psc_chapters, "social_url": item.social_url,
                "has_chapters": 1 if (item.psc_chapters or item.chapters_url) else 0,
            }
            episode_id = self.db.insert_episode(conn, values)
            if item.guid:
                by_guid[item.guid] = {"id": episode_id, **values}
            if item.media_url:
                by_media[item.media_url] = {"id": episode_id, **values}
            if not first_fetch:
                new_ids.append(episode_id)
            if enqueue:
                to_queue.append(episode_id)
        if to_queue:
            self._enqueue(conn, to_queue)
        return new_ids

    def _after_new_episodes(self, feed_id, new_ids):
        feed = self.db.get_feed(feed_id)
        self._changed(new_ids, queue=True)
        if feed is not None and feed.notify and feed.state == FEED_SUBSCRIBED:
            episodes = self.db.get_episodes_by_ids(new_ids)
            bus.emit("new-episodes", feed, episodes)

    def refresh_all(self, force=False, feed_ids=None, only_due=False):
        """Refresh every podcast that is kept updated. Runs in the calling (worker) thread."""
        if not self._refresh_lock.acquire(blocking=False):
            return []
        try:
            self.refreshing = True
            if feed_ids is None:
                feeds = [f for f in self.db.get_feeds(with_counts=False) if f.keep_updated]
            else:
                feeds = [f for f in (self.db.get_feed(i) for i in feed_ids) if f is not None]
            if only_due:
                interval = max(1, int(self.settings.get("refresh_interval_min") or 0)) * 60
                feeds = [f for f in feeds if now_s() - (f.last_refresh or 0) >= interval - 60]
            total = len(feeds)
            bus.emit("refresh-state", True, 0, total)
            results = []
            done = 0
            with ThreadPoolExecutor(max_workers=4) as pool:
                for result in pool.map(lambda f: self._safe_refresh(f.id, force), feeds):
                    results.append(result)
                    done += 1
                    bus.emit("refresh-state", True, done, total)
            self.db.set_meta("last_refresh_all", now_s())
            self.auto_download()
            new_total = sum(len(r.new_ids) for r in results)
            failed = [r for r in results if r.error]
            bus.emit("refresh-state", False, done, total)
            self._changed(queue=True)
            if failed and self.settings.get("notify_download_errors"):
                bus.emit("refresh-errors", failed)
            return results
        finally:
            self.refreshing = False
            self._refresh_lock.release()

    def _safe_refresh(self, feed_id, force):
        if self.stopping:
            return RefreshResult(feed_id)
        try:
            return self.refresh_feed(feed_id, force=force)
        except Exception as error:  # never let one podcast stop the others
            import traceback

            traceback.print_exc()
            return RefreshResult(feed_id, error=str(error))

    # ------------------------------------------------------------ local folders

    def add_local_folder(self, folder):
        folder = os.path.abspath(os.path.expanduser(folder))
        url = LOCAL_PREFIX + folder
        existing = self.db.get_feed_by_url(url)
        if existing is not None:
            return existing.id
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {
                "url": url, "title": os.path.basename(folder.rstrip("/")) or folder, "state": FEED_SUBSCRIBED,
                "added": now_s(), "description": _("This virtual podcast was created by adding a folder to omaPOD."),
                "auto_download": 0,
            })
        self.refresh_feed(feed_id)
        self._changed()
        return feed_id

    def _refresh_local(self, feed):
        folder = Path(feed.local_path)
        result = RefreshResult(feed.id)
        if not folder.is_dir():
            result.error = _("The folder does not exist any more.")
            self._refresh_failed(feed, result.error, str(folder))
            return result
        files = sorted(p for p in folder.rglob("*") if p.is_file()
                       and p.suffix.lower() in AUDIO_EXTENSIONS + VIDEO_EXTENSIONS)
        cover = next((p for p in folder.iterdir() if p.is_file() and p.stem.lower() in ("cover", "folder", "front")
                      and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")), None)
        with self.db.tx() as conn:
            rows = {r["guid"]: r for r in self.db.feed_episode_rows(conn, feed.id)}
            listed = set()
            for path in files:
                guid = str(path.relative_to(folder))
                listed.add(guid)
                if guid in rows:
                    continue
                stat = path.stat()
                episode_id = self.db.insert_episode(conn, {
                    "feed_id": feed.id, "guid": guid, "title": path.stem, "pub_date": int(stat.st_mtime),
                    "media_url": path.as_uri(), "media_type": "video/*" if path.suffix.lower() in VIDEO_EXTENSIONS
                    else "audio/*", "media_size": stat.st_size, "file_path": str(path),
                    "downloaded_at": int(stat.st_mtime), "state": UNPLAYED,
                })
                result.new_ids.append(episode_id)
            gone = [r["id"] for g, r in rows.items() if g not in listed]
            if gone:
                placeholders = ",".join("?" * len(gone))
                conn.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", gone)
            self.db.update_feed(feed.id, conn=conn, last_refresh=now_s(), refresh_error="",
                                image_url=cover.as_uri() if cover else feed.image_url)
        if result.new_ids:
            self._changed(result.new_ids)
        return result

    # ------------------------------------------------------------ podcast settings

    def update_feed_settings(self, feed_id, **values):
        self.db.update_feed(feed_id, **values)
        self._changed(feeds=True)

    def rename_feed(self, feed_id, title):
        self.db.update_feed(feed_id, custom_title=title.strip())
        self._changed()

    def set_tags(self, feed_id, tags):
        clean = sorted({t.strip() for t in tags if t.strip()}, key=str.casefold)
        self.db.update_feed(feed_id, tags=clean)
        self._changed(feeds=True)

    def rename_tag(self, old, new):
        for feed in self.db.get_feeds(states=(FEED_SUBSCRIBED, FEED_ARCHIVED), with_counts=False):
            if old in feed.tags:
                tags = [new if t == old else t for t in feed.tags]
                self.db.update_feed(feed.id, tags=sorted(set(tags), key=str.casefold))
        self._changed(feeds=True)

    def delete_tag(self, tag):
        for feed in self.db.get_feeds(states=(FEED_SUBSCRIBED, FEED_ARCHIVED), with_counts=False):
            if tag in feed.tags:
                self.db.update_feed(feed.id, tags=[t for t in feed.tags if t != tag])
        self._changed(feeds=True)

    def edit_feed_url(self, feed_id, new_url):
        feed = self.db.get_feed(feed_id)
        if feed is None or not new_url or self.db.get_feed_by_url(new_url):
            return False
        self.db.update_feed(feed_id, url=new_url, etag="", last_modified="")
        if feed.state == FEED_SUBSCRIBED:
            self.sync_feed(feed.url, False)
            self.sync_feed(new_url, True)
        self._changed()
        return True

    # ------------------------------------------------------------ episode state

    def mark_played(self, ids, played=True, sync=True, from_player=False):
        """Mark as played (or unplayed). Played episodes leave the inbox and the queue."""
        episodes = self.db.get_episodes_by_ids(ids)
        if not episodes:
            return
        ids = [e.id for e in episodes]
        with self.db.tx() as conn:
            if played:
                self.db.update_episodes(ids, conn=conn, state=PLAYED, position=0)
                conn.execute(f"UPDATE episodes SET completed_at = ? WHERE completed_at = 0 AND id IN "
                             f"({','.join('?' * len(ids))})", [now_s()] + ids)
                self._dequeue(conn, ids)
            else:
                self.db.update_episodes(ids, conn=conn, state=UNPLAYED)
        if sync:
            for episode in episodes:
                if played and episode.duration:
                    seconds = episode.duration // 1000
                    self.sync_action(episode, "play", seconds, seconds, seconds)
                elif not played:
                    self.sync_action(episode, "new")
        if played:
            self._auto_delete_after_played(episodes, allow_current=from_player)
        self._changed(ids, queue=played)

    def mark_feed_played(self, feed_id):
        ids = [e.id for e in self.db.get_episodes(("unplayed",), feed_id=feed_id)]
        self.mark_played(ids)

    def remove_from_inbox(self, ids):
        ids = [int(i) for i in ids]
        if not ids:
            return
        with self.db.tx() as conn:
            conn.execute(f"UPDATE episodes SET state = 0 WHERE state = -1 AND id IN ({','.join('?' * len(ids))})", ids)
        self._changed(ids)

    def remove_all_from_inbox(self, feed_id=None):
        ids = [e.id for e in self.db.get_episodes(("new",), feed_id=feed_id)]
        self.remove_from_inbox(ids)
        return len(ids)

    def set_favorite(self, ids, favorite=True, sync=True):
        ids = [int(i) for i in ids]
        self.db.update_episodes(ids, favorite=1 if favorite else 0)
        self._changed(ids, feeds=False)
        if sync and self.sync_enabled() and self.settings.get("sync_queue_favorites"):
            bus.emit("sync-requested")

    def reset_position(self, ids):
        ids = [int(i) for i in ids]
        self.db.update_episodes(ids, position=0)
        self._changed(ids, feeds=False)

    def save_position(self, episode_id, position_ms, duration_ms=None, listened_ms=0):
        """Called by the player every few seconds and on pause."""
        episode = self.db.get_episode(episode_id, full=False)
        if episode is None:
            return
        values = {"position": max(0, int(position_ms))}
        if listened_ms > 0 or values["position"] != episode.position:
            # Heard or moved just now (not merely saved on quitting): like AntennaPod, an episode is in the
            # history as soon as it is played. Sync compares this time with other devices'.
            values.update(last_played=now_ms(), completed_at=now_s())
        if duration_ms and duration_ms > 0:
            values["duration"] = int(duration_ms)
        if listened_ms > 0:
            values["played_duration"] = episode.played_duration + int(listened_ms)
        if episode.state == NEW:
            values["state"] = UNPLAYED
        self.db.update_episode(episode_id, **values)
        if listened_ms > 0:
            self.db.add_listened(episode_id, episode.feed_id, listened_ms, date.today().isoformat())
        bus.emit("episodes-changed", {episode_id})

    def add_to_history(self, episode_id):
        self.db.update_episode(episode_id, completed_at=now_s())
        bus.emit("episodes-changed", {episode_id})

    def remove_from_history(self, ids):
        self.db.update_episodes(ids, completed_at=0)
        self._changed(ids, feeds=False)

    def clear_history(self):
        self.db.execute("UPDATE episodes SET completed_at = 0 WHERE completed_at > 0")
        self._changed(None, feeds=False)

    def playback_finished(self, episode, start_ms, ended, skipped, playing_next=False, played=True):
        """AntennaPod's updateDatabaseAfterPlayback."""
        if episode is None:
            return
        smart = int(self.settings.get("smart_mark_played_secs") or 0) * 1000
        almost_ended = episode.duration > 0 and episode.position >= episode.duration - smart
        self.sync_episode_played(episode, start_ms, ended or almost_ended)
        if ended or almost_ended:
            self.mark_played([episode.id], sync=False, from_player=True)
        elif skipped and not self.settings.get("skip_keeps_episode"):
            with self.db.tx() as conn:
                self._dequeue(conn, [episode.id])
            self._auto_delete_after_played([episode], allow_current=True)
            bus.emit("queue-changed")
        if ended or ((skipped or playing_next) and played):
            self.add_to_history(episode.id)

    def _auto_delete_after_played(self, episodes, allow_current=False):
        for episode in episodes:
            if not episode.is_downloaded:
                continue
            feed = self.db.get_feed(episode.feed_id)
            if feed is None:
                continue
            global_on = self.settings.get("auto_delete") and (not feed.is_local or self.settings.get("auto_delete_local"))
            delete = feed.auto_delete == 1 or (feed.auto_delete == -1 and global_on)
            if delete and not (episode.favorite and self.settings.get("favorite_keeps_episode")):
                if self.current_episode_id() == episode.id and not allow_current:
                    continue
                self.delete_downloads([episode.id], quiet=True, stop_player=False)

    # ------------------------------------------------------------ queue

    def queue_ids(self):
        return self.db.queue_ids()

    def _enqueue(self, conn, ids, location=None):
        queue = [r[0] for r in conn.execute("SELECT episode_id FROM queue ORDER BY pos").fetchall()]
        ids = [i for i in ids if i not in queue]
        if not ids:
            return []
        location = location or self.settings.get("enqueue_location")
        if location == "front":
            index = 0
            downloading = self.downloads.active_ids() if self.downloads else set()
            while index < len(queue) and queue[index] in downloading:
                index += 1
            queue[index:index] = ids
        elif location == "after_current":
            current = self.current_episode_id()
            index = queue.index(current) + 1 if current in queue else 0
            queue[index:index] = ids
        elif location == "random":
            for episode_id in ids:
                queue.insert(random.randint(0, len(queue)), episode_id)
        else:
            queue.extend(ids)
        self.db.set_queue_order(conn, queue)
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"UPDATE episodes SET state = 0 WHERE state = -1 AND id IN ({placeholders})", ids)
        return ids

    def _dequeue(self, conn, ids):
        ids = [int(i) for i in ids]
        if ids:
            conn.execute(f"DELETE FROM queue WHERE episode_id IN ({','.join('?' * len(ids))})", ids)

    def add_to_queue(self, ids, location=None):
        with self.db.tx() as conn:
            added = self._enqueue(conn, [int(i) for i in ids], location)
            if added and self.settings.get("queue_keep_sorted"):
                self._sort_queue(conn, self.settings.get("queue_sort"))
        if added:
            self._changed(added, queue=True)
            if self.settings.get("auto_download_queue"):
                threading.Thread(target=self.auto_download, daemon=True).start()
        return added

    def remove_from_queue(self, ids):
        with self.db.tx() as conn:
            self._dequeue(conn, ids)
        self._changed(ids, queue=True)

    def move_in_queue(self, episode_id, index):
        if self.settings.get("queue_locked") or self.settings.get("queue_keep_sorted"):
            return False
        with self.db.tx() as conn:
            queue = [r[0] for r in conn.execute("SELECT episode_id FROM queue ORDER BY pos").fetchall()]
            if episode_id not in queue:
                return False
            queue.remove(episode_id)
            queue.insert(max(0, min(index, len(queue))), episode_id)
            self.db.set_queue_order(conn, queue)
        bus.emit("queue-changed")
        return True

    def move_to_top(self, ids):
        self._move_block(ids, top=True)

    def move_to_bottom(self, ids):
        self._move_block(ids, top=False)

    def _move_block(self, ids, top):
        ids = [int(i) for i in ids]
        with self.db.tx() as conn:
            queue = [r[0] for r in conn.execute("SELECT episode_id FROM queue ORDER BY pos").fetchall()]
            block = [i for i in queue if i in ids]
            rest = [i for i in queue if i not in ids]
            self.db.set_queue_order(conn, block + rest if top else rest + block)
        bus.emit("queue-changed")

    def replace_queue(self, ids):
        """Take over the queue another computer shared (queue sync)."""
        ids = list(dict.fromkeys(int(i) for i in ids))
        with self.db.tx() as conn:
            self.db.set_queue_order(conn, ids)
            if ids:
                conn.execute(f"UPDATE episodes SET state = 0 WHERE state = -1 AND id IN ({','.join('?' * len(ids))})",
                             ids)
        self._changed(ids, queue=True)
        if ids and self.settings.get("auto_download_queue"):
            threading.Thread(target=self.auto_download, daemon=True).start()

    def clear_queue(self):
        with self.db.tx() as conn:
            conn.execute("DELETE FROM queue")
        self._changed(None, queue=True)

    def _sort_queue(self, conn, order):
        episodes = self.db.get_episodes(("queued",), sort=order if order != "queue" else "date_asc")
        self.db.set_queue_order(conn, [e.id for e in episodes])

    def sort_queue(self, order):
        with self.db.tx() as conn:
            self._sort_queue(conn, order)
        bus.emit("queue-changed")

    def next_in_queue(self, after_id):
        """AntennaPod's getNextInQueue: only an episode that is itself queued has a successor."""
        queue = self.db.queue_ids()
        if after_id in queue:
            index = queue.index(after_id)
            return queue[index + 1] if index + 1 < len(queue) else None
        return None

    def queue_remaining_ms(self):
        total = 0
        speed = float(self.settings.get("speed") or 1.0)
        for episode in self.db.get_queue():
            total += episode.remaining
        if self.settings.get("time_respects_speed") and speed > 0:
            total = int(total / speed)
        return total

    # ------------------------------------------------------------ downloads

    def feed_folder(self, feed):
        from ..config import downloads_dir
        from ..util import safe_filename

        return downloads_dir() / safe_filename(feed.display_title or f"podcast-{feed.id}", 80)

    def download(self, ids, manual=True):
        if self.downloads is None:
            return 0
        count = 0
        for episode in self.db.get_episodes_by_ids(ids):
            if episode.is_downloaded or not episode.media_url or episode.is_local:
                continue
            if self.downloads.enqueue(episode, manual=manual):
                count += 1
        if count and manual:
            self.remove_from_inbox(ids)
        return count

    def download_finished(self, episode_id, path, size, manual=True):
        episode = self.db.get_episode(episode_id, full=False)
        if episode is None:
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        self.db.update_episode(episode_id, file_path=str(path), downloaded_at=now_s(), media_size=size or
                               episode.media_size, download_failures=0)
        self.db.add_log("media", episode_id, episode.title, episode.media_url, True)
        episode.file_path, episode.downloaded_at = str(path), now_s()
        self.sync_action(episode, "download")
        if self.settings.get("enqueue_downloaded") and not episode.in_queue and not episode.is_played:
            self.add_to_queue([episode_id])
        self._changed([episode_id])
        bus.emit("downloads-changed")

    def download_failed(self, episode_id, reason, detail, manual=True):
        episode = self.db.get_episode(episode_id, full=False)
        if episode is None:
            return
        failures = episode.download_failures + 1
        values = {"download_failures": failures}
        if not manual and failures >= 3:
            values["auto_download"] = 0
        self.db.update_episode(episode_id, **values)
        self.db.add_log("media", episode_id, episode.title, episode.media_url, False, reason, detail)
        bus.emit("downloads-changed")
        bus.emit("episodes-changed", {episode_id})
        if self.settings.get("notify_download_errors"):
            bus.emit("download-error", episode, reason)

    def _remove_file(self, episode):
        if episode.file_path and not episode.is_local:
            try:
                os.unlink(episode.file_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                bus.emit("message", str(error), "error")

    def delete_downloads(self, ids, quiet=False, stop_player=True, local_files=False):
        """Delete downloaded files. Files in local folders are the user's own: they go to the trash, and only
        when the user confirmed it (local_files) or lets automatic deletion include them (auto_delete_local)."""
        allow_local = local_files or (quiet and bool(self.settings.get("auto_delete_local")))
        episodes = [e for e in self.db.get_episodes_by_ids(ids) if e.is_downloaded and (allow_local or not e.is_local)]
        current = self.current_episode_id()
        deleted = []
        for episode in episodes:
            if episode.id == current and self.player and stop_player:
                self.player.stop()
            if episode.is_local:
                if not self._trash_local(episode):
                    continue
                self.db.execute("DELETE FROM episodes WHERE id = ?", (episode.id,))
            else:
                self._remove_file(episode)
                self.db.update_episode(episode.id, file_path="", downloaded_at=0)
                self.sync_action(episode, "delete")
            deleted.append(episode.id)
        if not deleted:
            return 0
        if self.settings.get("delete_removes_from_queue"):
            with self.db.tx() as conn:
                self._dequeue(conn, deleted)
        self._changed(deleted, queue=True)
        bus.emit("downloads-changed")
        if not quiet:
            bus.emit("message", ngettext("{n} downloaded episode deleted.", "{n} downloaded episodes deleted.",
                                         len(deleted)).format(n=len(deleted)), "info")
        return len(deleted)

    def _trash_local(self, episode):
        try:
            if os.path.lexists(episode.file_path):
                move_to_trash(episode.file_path)
        except OSError as error:
            bus.emit("message", _("Could not move {file} to the trash: {error}").format(
                file=os.path.basename(episode.file_path), error=error), "error")
            return False
        return True

    def delete_played_downloads(self):
        """Played downloads; files in local folders are not downloads and stay."""
        ids = [e.id for e in self.db.get_episodes(("downloaded", "played")) if not e.is_local]
        return self.delete_downloads(ids)

    def downloads_elsewhere(self):
        """What other devices downloaded (from sync) and is neither downloaded nor played here, most recent first."""
        episodes, seen = [], set()
        for episode_url, guid in self.db.remote_downloads():
            episode = self.db.find_episode_for_sync(guid, episode_url)
            if (episode is None or episode.id in seen or episode.is_downloaded or episode.is_played
                    or episode.is_local or not episode.media_url or episode.feed_state != FEED_SUBSCRIBED):
                continue
            seen.add(episode.id)
            episodes.append(episode)
        return episodes

    def _auto_download_enabled(self, feed):
        if feed.is_local:
            return False
        if feed.auto_download == 1:
            return True
        return feed.auto_download == -1 and bool(self.settings.get("auto_download"))

    def auto_download(self):
        """AntennaPod's AutomaticDownloadAlgorithm."""
        if self.downloads is None or self.stopping:
            return 0
        if not self.settings.get("auto_download_on_battery") and not on_mains_power():
            return 0
        global_on = bool(self.settings.get("auto_download"))
        queue_on = bool(self.settings.get("auto_download_queue"))
        feeds = {f.id: f for f in self.db.get_feeds(with_counts=False)}
        candidates = []
        for episode in self.db.get_episodes(("not_downloaded",), sort="date_desc",
                                            extra="e.auto_download != 0 AND e.media_url != ''"):
            feed = feeds.get(episode.feed_id)
            if feed is None or feed.is_local:
                continue
            take = (queue_on and episode.in_queue) or (
                episode.is_new and self._auto_download_enabled(feed)
                and should_auto_download(feed, episode.title, episode.duration))
            if take and not self.downloads.is_active(episode.id):
                candidates.append(episode)
        if not candidates:
            return 0
        cache = int(self.settings.get("episode_cache") or -1)
        downloaded = self.db.count_episodes(("downloaded",)) + len(self.downloads.active_ids())
        deleted = self.cleanup(len(candidates))
        if cache == -1 or cache >= downloaded + len(candidates):
            space = len(candidates)
        else:
            space = max(0, cache - (downloaded - deleted))
        started = 0
        for episode in candidates[:space]:
            if self.downloads.enqueue(episode, manual=False):
                started += 1
        return started

    def cleanup(self, room_needed=0):
        """AntennaPod's episode cleanup algorithms. Returns the number of deleted downloads."""
        value = int(self.settings.get("episode_cleanup"))
        cache = int(self.settings.get("episode_cache") or -1)
        if value == -2 or cache == -1:
            return 0
        downloaded_count = self.db.count_episodes(("downloaded",))
        needed = downloaded_count + room_needed - cache if downloaded_count + room_needed >= cache else 0
        if needed <= 0:
            return 0
        allow_local = bool(self.settings.get("auto_delete_local"))
        current = self.current_episode_id()
        candidates = []
        for episode in self.db.get_episodes(("downloaded",), sort="date_asc"):
            if episode.id == current or episode.favorite or (episode.is_local and not allow_local):
                continue
            if value == -3:
                candidates.append(episode)
            elif value == -1:
                if not episode.in_queue:
                    candidates.append(episode)
            elif value >= 0:
                if episode.in_queue or not episode.is_played:
                    continue
                last = (episode.last_played // 1000) or episode.completed_at
                if last and last <= now_s() - value * 3600:
                    candidates.append(episode)
        if value >= 0:
            candidates.sort(key=lambda e: (e.last_played // 1000) or e.completed_at)
        victims = candidates[:needed]
        if victims:
            self.delete_downloads([e.id for e in victims], quiet=True)
        return len(victims)

    # ------------------------------------------------------------ chapters

    def chapters_for(self, episode, embedded=None, fetch_remote=True):
        """Merged chapters for an episode: feed (Podlove), media file (mpv), Podcasting 2.0 JSON."""
        cached = [dict(start=r["start_ms"], title=r["title"], url=r["url"], image=r["image_url"])
                  for r in self.db.get_chapters(episode.id)]
        source_rows = self.db.query("SELECT DISTINCT source FROM chapters WHERE episode_id = ?", (episode.id,))
        sources = {r["source"] for r in source_rows}
        psc = episode.psc_chapters or None
        if not psc:
            full = self.db.get_episode(episode.id)
            psc = (full.psc_chapters or None) if full else None
        merged = chapter_utils.merge(psc, embedded)
        remote = None
        if episode.chapters_url and fetch_remote:
            if "remote" in sources and cached:
                remote = cached
            else:
                try:
                    response = http.request(episode.chapters_url, timeout=15, max_bytes=2 * 1024 * 1024)
                    remote = chapter_utils.parse_json_chapters(response.body)
                except (http.HttpError, ValueError):
                    remote = None
        merged = chapter_utils.merge(merged, remote)
        if merged is None and cached:
            merged = cached
        if merged:
            source = "remote" if remote and merged is remote else ("embedded" if embedded else "feed")
            self.db.set_chapters(episode.id, merged, source)
        return merged or []

    # ------------------------------------------------------------ statistics

    def statistics(self, start=0, end=None, include_marked=None):
        """Played time per podcast: AntennaPod's 'Subscriptions' statistics."""
        include_marked = self.settings.get("stats_include_marked") if include_marked is None else include_marked
        end = end or now_s() + 86400
        start_day = date.fromtimestamp(start).isoformat() if start else "0000-00-00"
        end_day = date.fromtimestamp(end).isoformat()
        rows = self.db.query(
            "SELECT f.id AS feed_id, COALESCE(NULLIF(f.custom_title,''), f.title) AS title, f.image_url, f.state, "
            "COALESCE((SELECT SUM(l.ms) FROM listen_log l WHERE l.feed_id = f.id AND l.day >= ? AND l.day <= ?), 0) AS played_ms, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id) AS episodes, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND (e.position > 0 OR e.state = 1)) AS started, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND e.downloaded_at > 0) AS downloaded, "
            "COALESCE((SELECT SUM(e.media_size) FROM episodes e WHERE e.feed_id = f.id AND e.downloaded_at > 0), 0) AS space, "
            "COALESCE((SELECT SUM(e.duration) FROM episodes e WHERE e.feed_id = f.id), 0) AS total_ms, "
            "COALESCE((SELECT SUM(e.duration) FROM episodes e WHERE e.feed_id = f.id AND e.state = 1 AND e.played_duration = 0 "
            "AND e.completed_at BETWEEN ? AND ?), 0) AS marked_ms "
            "FROM feeds f WHERE f.state != 0",
            (start_day, end_day, start or 0, end))
        result = []
        for row in rows:
            played = row["played_ms"] + (row["marked_ms"] if include_marked else 0)
            result.append(dict(row) | {"played": played})
        result.sort(key=lambda r: r["played"], reverse=True)
        return result

    def monthly_statistics(self):
        rows = self.db.query("SELECT substr(day, 1, 7) AS month, SUM(ms) AS ms FROM listen_log GROUP BY month ORDER BY month")
        return [(row["month"], row["ms"]) for row in rows]

    def reset_statistics(self):
        self.db.execute("DELETE FROM listen_log")
        self.db.execute("UPDATE episodes SET played_duration = 0")
        self.settings.set("stats_reset_ts", now_s())
        bus.emit("episodes-changed", None)

    # ------------------------------------------------------------ storage

    def move_downloads(self, new_dir):
        """Move every downloaded file into a new downloads folder."""
        new_dir = Path(new_dir).expanduser()
        new_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for episode in self.db.get_episodes(("downloaded",)):
            if episode.is_local or not episode.file_path:
                continue
            source = Path(episode.file_path)
            target_folder = new_dir / source.parent.name
            target_folder.mkdir(parents=True, exist_ok=True)
            target = target_folder / source.name
            try:
                shutil.move(str(source), str(target))
                self.db.update_episode(episode.id, file_path=str(target))
                moved += 1
            except OSError:
                continue
        self.settings.set("downloads_dir", str(new_dir))
        bus.emit("downloads-changed")
        return moved
