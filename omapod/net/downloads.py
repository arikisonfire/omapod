"""Episode downloads: a small worker pool with resume, progress and cancel."""

import fcntl
import os
import shutil
import threading
import time
import urllib.error
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..events import bus
from ..i18n import _
from ..util import safe_filename
from . import http

MIME_EXT = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/flac": ".flac", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/x-m4v": ".m4v",
}
KNOWN_EXT = set(MIME_EXT.values()) | {".m4b", ".oga", ".mka", ".m4v"}
# A download that the server does not announce a size for stops here: an endless stream would fill the disk.
# Generous, as feeds often state a wrong size (0 or 1 are common).
UNKNOWN_SIZE_LIMIT = 2 * 1024 * 1024 * 1024
FREE_SPACE_RESERVE = 50 * 1024 * 1024  # what a download leaves free on the disk
SPACE_CHECK_EVERY = 32 * 1024 * 1024


@dataclass
class Job:
    episode_id: int
    manual: bool
    title: str = ""
    received: int = 0
    total: int = 0
    state: str = "queued"
    cancelled: bool = False
    last_emit: float = 0.0


class DownloadManager:
    def __init__(self, library, settings):
        self.library = library
        self.settings = settings
        self._lock = threading.Condition()
        self._pending = deque()
        self._jobs = {}
        self._workers = []
        self._stopping = False

    def start(self):
        count = max(1, min(8, int(self.settings.get("parallel_downloads") or 4)))
        for index in range(count):
            worker = threading.Thread(target=self._run, name=f"download-{index}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def stop(self):
        with self._lock:
            self._stopping = True
            for job in self._jobs.values():
                job.cancelled = True
            self._lock.notify_all()

    # -- public API ------------------------------------------------------

    def enqueue(self, episode, manual=True):
        with self._lock:
            if episode.id in self._jobs:
                return False
            job = Job(episode.id, manual, title=episode.title, total=episode.media_size or 0)
            self._jobs[episode.id] = job
            self._pending.append(job)
            self._lock.notify()
        bus.emit("downloads-changed")
        bus.emit("episodes-changed", {episode.id})
        return True

    def cancel(self, episode_id):
        with self._lock:
            job = self._jobs.get(episode_id)
            if job is None:
                return False
            job.cancelled = True
            if job in self._pending:
                self._pending.remove(job)
                del self._jobs[episode_id]
        bus.emit("downloads-changed")
        bus.emit("episodes-changed", {episode_id})
        return True

    def is_active(self, episode_id):
        with self._lock:
            return episode_id in self._jobs

    def active_ids(self):
        with self._lock:
            return set(self._jobs)

    def progress(self, episode_id):
        with self._lock:
            job = self._jobs.get(episode_id)
            if job is None:
                return None
            return job.received, job.total, job.state

    def jobs(self):
        with self._lock:
            return [(j.episode_id, j.title, j.received, j.total, j.state) for j in self._jobs.values()]

    # -- worker ------------------------------------------------------------

    def _run(self):
        while True:
            with self._lock:
                while not self._pending and not self._stopping:
                    self._lock.wait()
                if self._stopping:
                    return
                job = self._pending.popleft()
                job.state = "running"
            try:
                self._download(job)
            except Exception as error:  # keep the worker alive
                import traceback

                traceback.print_exc()
                self.library.download_failed(job.episode_id, _("Unknown error"), repr(error), job.manual)
            finally:
                with self._lock:
                    self._jobs.pop(job.episode_id, None)
                bus.emit("downloads-changed")
                bus.emit("episodes-changed", {job.episode_id})

    def _target_path(self, episode, feed, mime):
        folder = self.library.feed_folder(feed)
        folder.mkdir(parents=True, exist_ok=True)
        url_name = unquote(os.path.basename(urlsplit(episode.media_url).path))
        ext = os.path.splitext(url_name)[1].lower()
        if ext not in KNOWN_EXT:
            ext = MIME_EXT.get((mime or episode.media_type or "").split(";")[0].strip().lower(), ".mp3")
        base = safe_filename(episode.title or os.path.splitext(url_name)[0] or f"episode-{episode.id}", 100)
        target = folder / f"{base}{ext}"
        counter = 2
        while target.exists():
            target = folder / f"{base} ({counter}){ext}"
            counter += 1
        return target

    def _download(self, job):
        episode = self.library.db.get_episode(job.episode_id, full=False)
        if episode is None or job.cancelled:
            return
        feed = self.library.db.get_feed(episode.feed_id)
        if feed is None:
            return
        part = self.library.feed_folder(feed) / f".{episode.id}.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        # Locked while it is written: the background refresh and the app must never write one file at once.
        with open(part, "ab") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # the other process is downloading it right now
            try:
                self._fetch(job, episode, feed, part)
            finally:
                if part.exists() and not part.stat().st_size:
                    part.unlink(missing_ok=True)

    def _fetch(self, job, episode, feed, part):
        offset = part.stat().st_size
        headers = {"Accept": "*/*"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        # The podcast's password goes only to its own server, also at the end of a redirect (statistics prefixes).
        # Media hosts elsewhere (CDNs, statistics services) never get it, not even when they ask for it.
        auth = (feed.username, feed.password) if feed.username else None
        try:
            response, _chain = http.open_url(episode.media_url, headers=headers, auth=auth, auth_url=feed.url,
                                             timeout=30)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 416 and offset:
                os.truncate(part, 0)  # stays locked: the same file starts again
                return self._fetch(job, episode, feed, part)
            reason, detail = http.describe_error(error, episode.media_url)
            if error.code == 401:
                reason = http.withheld(feed.url, error.geturl()) or reason
            self.library.download_failed(episode.id, reason, detail, job.manual)
            return
        except Exception as error:
            reason, detail = http.describe_error(error, episode.media_url)
            self.library.download_failed(episode.id, reason, detail, job.manual)
            return
        failed = None
        with response:
            status = response.status
            if status == 200 and offset:
                offset = 0
            length = int(response.headers.get("Content-Length") or 0)
            total = offset + length if length else (episode.media_size or 0)
            mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if mime.startswith("text/html"):
                self.library.download_failed(episode.id, _("File type error"),
                                             _("The server sent a web page instead of the episode."), job.manual)
                return
            free = shutil.disk_usage(part.parent).free
            if length and free < length + FREE_SPACE_RESERVE:
                self.library.download_failed(episode.id, _("There is not enough space left on your device."),
                                             f"{length} bytes needed, {free} free", job.manual)
                return
            # With a Content-Length, http.client reads no further than that; without one, the limit applies.
            limit = offset + length if length else max(2 * (episode.media_size or 0), UNKNOWN_SIZE_LIMIT)
            job.total = total
            job.received = offset
            checked = offset
            with open(part, "ab" if offset else "wb") as handle:
                while True:
                    if job.cancelled:
                        break
                    try:
                        chunk = response.read(256 * 1024)
                    except (OSError, http.HttpError) as error:
                        reason, detail = http.describe_error(error, episode.media_url)
                        self.library.download_failed(episode.id, _("The server connection was lost before "
                                                                   "completing the download"), detail, job.manual)
                        return
                    if not chunk:
                        break
                    if job.received + len(chunk) > limit:
                        failed = (_("The server sent far more data than expected."),
                                  f"more than {limit} bytes without a Content-Length")
                        break
                    handle.write(chunk)
                    job.received += len(chunk)
                    if job.received - checked >= SPACE_CHECK_EVERY:
                        checked = job.received
                        free = shutil.disk_usage(part.parent).free
                        if free < FREE_SPACE_RESERVE:
                            failed = (_("There is not enough space left on your device."),
                                      f"{free} bytes free after {job.received} bytes")
                            break
                    now = time.monotonic()
                    if now - job.last_emit > 0.25:
                        job.last_emit = now
                        bus.emit("download-progress", episode.id, job.received, job.total)
        if job.cancelled or failed:
            part.unlink(missing_ok=True)
            if failed:
                self.library.download_failed(episode.id, *failed, job.manual)
            return
        if length and job.received < offset + length:
            self.library.download_failed(episode.id, _("The server connection was lost before completing the "
                                                       "download"), f"{job.received} of {offset + length} bytes",
                                         job.manual)
            return
        target = self._target_path(episode, feed, mime)
        try:
            os.replace(part, target)
        except OSError as error:
            part.unlink(missing_ok=True)
            self.library.download_failed(episode.id, *http.describe_error(error), job.manual)
            return
        self.library.download_finished(episode.id, target, job.received, job.manual)


def files_size(paths):
    total = 0
    for path in paths:
        try:
            total += Path(path).stat().st_size
        except OSError:
            pass
    return total
