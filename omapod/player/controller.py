"""Playback, modelled on AntennaPod's PlaybackService.

Runs on the GLib main loop. Positions are kept in milliseconds. The bar
plugin (ipc.py), MPRIS (mpris.py) and the window all drive this object and
listen to two bus topics: 'player-state' (anything but the position) and
'player-position' (frequent).
"""

import math
import os
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit

from gi.repository import GLib

from ..events import bus
from ..feeds import chapters as chapter_utils
from ..i18n import _
from ..models import PLAYED, VOLUME_ADAPTATIONS
from ..net.http import auth_allowed
from ..util import fmt_clock, fmt_speed
from .mpv import Mpv, MpvError

SKIP_SILENCE_FILTER = ("@skipsilence:lavfi=[silenceremove=start_periods=0:stop_periods=-1:stop_duration=0.4:"
                       "stop_silence=0.15:stop_threshold=-42dB:timestamp=copy]")
TICK_MS = 500
SAVE_EVERY_MS = 5000
FADE_MS = 10000
SYNC_WAIT_MS = 5000  # longest wait for other devices' positions before playback starts anyway


class PlayerController:
    def __init__(self, library, settings, paths):
        self.library = library
        self.settings = settings
        self.paths = paths
        self.mpv = None
        self.episode = None
        self.feed = None
        self.status = "idle"
        self.position = 0
        self.duration = 0
        self.temp_speed = None
        self.volume = int(settings.get("volume") or 100)
        self.buffering = False
        self.chapters = []
        self.chapter_index = -1
        self.sleep = None
        self.start_position = -1
        self.loaded = False
        self.error = ""
        self._raw_pos = None
        self._counted_until = None
        self._listened = 0
        self._last_save = 0.0
        self._last_tick = None
        self._tick_id = None
        self._played_this_session = False
        self._skip_silence_on = False
        self._ending = False
        self._want_play = False
        self._chapter_token = 0
        # sync_check(done, follow) fetches other devices' positions before playback starts and calls done() on
        # the main loop; it returns False when there is nothing to wait for (set by the app).
        self.sync_check = None
        self._play_token = None
        self._awaiting_sync = False
        settings.connect(self._on_setting)

    # ------------------------------------------------------------ helpers

    def current_id(self):
        return self.episode.id if self.episode else None

    def plays_here(self):
        """Playing on this computer (or starting to), not just waiting for the positions of other devices."""
        return self.status in ("playing", "loading") and not self._awaiting_sync

    @property
    def speed(self):
        if self.temp_speed is not None:
            return self.temp_speed
        if self.feed is not None and self.feed.speed and self.feed.speed > 0:
            return float(self.feed.speed)
        return float(self.settings.get("speed") or 1.0)

    def skip_silence_enabled(self):
        if self.feed is not None and self.feed.skip_silence in (0, 1):
            return bool(self.feed.skip_silence)
        return bool(self.settings.get("skip_silence"))

    def _volume_gain_db(self):
        index = self.feed.volume_adaptation if self.feed is not None else 0
        factor = VOLUME_ADAPTATIONS[index][1] if 0 <= index < len(VOLUME_ADAPTATIONS) else 1.0
        return 20 * math.log10(factor)

    def state(self):
        episode = None
        if self.episode is not None:
            e = self.episode
            episode = {
                "id": e.id, "title": e.title, "podcast": e.feed_title, "feed_id": e.feed_id, "author": e.feed_author,
                "image": e.cover_url(self.settings.get("episode_cover")), "feed_image": e.feed_image,
                "is_video": e.is_video, "link": e.link, "media_url": e.media_url, "downloaded": e.is_downloaded,
                "pub_date": e.pub_date, "favorite": bool(e.favorite),
            }
        chapter = self.chapters[self.chapter_index] if 0 <= self.chapter_index < len(self.chapters) else None
        sleep = None
        if self.sleep is not None:
            sleep = {"mode": self.sleep["mode"], "remaining": int(self.sleep.get("remaining", 0)),
                     "episodes": self.sleep.get("episodes", 0)}
        return {
            "status": self.status,
            "playing": self.status in ("playing", "loading"),
            "episode": episode,
            "position": int(self.position),
            "duration": int(self.duration),
            "speed": self.speed,
            "volume": self.volume,
            "buffering": self.buffering,
            "chapters": self.chapters,
            "chapter_index": self.chapter_index,
            "chapter": chapter["title"] if chapter else "",
            "sleep": sleep,
            "skip_silence": self.skip_silence_enabled(),
            "rewind_secs": int(self.settings.get("rewind_secs") or 10),
            "forward_secs": int(self.settings.get("fast_forward_secs") or 30),
            "error": self.error,
        }

    def _emit_state(self):
        bus.emit("player-state", self.state())

    def _emit_position(self):
        bus.emit("player-position", int(self.position), int(self.duration))

    # ------------------------------------------------------------ mpv

    def ensure_mpv(self):
        if self.mpv is not None and self.mpv.alive:
            return True
        if not Mpv.available():
            self._fail(_("mpv is not installed. Install it with: sudo pacman -S mpv"))
            return False
        self.paths.runtime.mkdir(parents=True, exist_ok=True)
        self.mpv = Mpv(self.paths.mpv_socket, self._on_mpv_message, log_path=str(self.paths.cache / "mpv.log"))
        try:
            self.mpv.start(volume=self.volume)
        except (MpvError, OSError) as error:
            self.mpv = None
            self._fail(_("Could not start mpv: {error}").format(error=error))
            return False
        for prop in ("duration", "pause", "chapter-list", "paused-for-cache", "time-pos", "seeking"):
            try:
                self.mpv.observe(prop)
            except MpvError:
                pass
        self._skip_silence_on = False
        self.loaded = False
        return True

    def _on_mpv_message(self, message):
        """Called from mpv's reader thread."""
        if message.get("event") == "property-change" and message.get("name") == "time-pos":
            self._raw_pos = message.get("data")
            return
        GLib.idle_add(self._handle_mpv, message)

    def _handle_mpv(self, message):
        event = message.get("event")
        if event == "property-change":
            name, data = message.get("name"), message.get("data")
            if name == "duration" and data:
                self.duration = int(data * 1000)
                self._emit_position()
            elif name == "pause" and data is not None and self.loaded:
                if data and self.status == "playing":
                    self._on_paused()
                elif not data and self.status in ("paused", "loading"):
                    self._on_playing()
            elif name == "paused-for-cache":
                self.buffering = bool(data)
                self._emit_state()
            elif name == "chapter-list" and self.episode is not None and self.loaded:
                self._load_chapters(chapter_utils.from_mpv(data))
        elif event == "file-loaded":
            self.loaded = True
            self.error = ""
            self._raw_pos = self.position / 1000
            self._counted_until = self.position
            if self._want_play:
                self._on_playing()
            else:
                self.status = "paused"
                self._emit_state()
        elif event == "playback-restart":
            self.buffering = False
        elif event == "end-file":
            reason = message.get("reason")
            if reason == "eof" and self.loaded and not self._ending:
                self.loaded = False
                self._on_ended()
            elif reason == "error":
                self.loaded = False
                detail = message.get("file_error") or ""
                self._fail(_("The media file could not be played.") + (f" ({detail})" if detail else ""))
        elif event == "omapod-mpv-exit":
            self.mpv = None
            self.loaded = False
            if self.status in ("playing", "loading"):
                self.status = "paused"
                self._stop_tick()
                self._emit_state()
        return False

    def _fail(self, text):
        self.error = text
        if self.status in ("playing", "loading"):
            self.status = "paused"
        self._stop_tick()
        self._emit_state()
        bus.emit("message", text, "error")

    # ------------------------------------------------------------ loading

    def restore_last(self):
        """Show the last episode (paused) after a restart, like AntennaPod does."""
        value = self.library.db.get_meta("current_episode")
        if not value:
            return
        try:
            episode = self.library.db.get_episode(int(value))
        except ValueError:
            episode = None
        if episode is None or not episode.has_media or episode.is_played:
            return
        self._set_episode(episode)
        self.status = "paused"
        self._emit_state()
        self._emit_position()

    def _set_episode(self, episode):
        self.episode = episode
        self.feed = self.library.db.get_feed(episode.feed_id)
        self.temp_speed = None
        self.position = 0 if episode.is_played else episode.position
        self.duration = episode.duration
        self.chapters = list(episode.psc_chapters or [])
        self.chapter_index = chapter_utils.index_at(self.chapters, self.position)
        self.start_position = self.position
        self.loaded = False
        self.error = ""
        self._played_this_session = False
        self._listened = 0

    def play_episode(self, episode_id, start=True, start_at=None, checked=False):
        if not checked:
            self._cancel_wait()  # this choice replaces a start that is still waiting
            if start and start_at is None and self._wait_for_sync(
                    lambda: self.play_episode(episode_id, start, start_at, checked=True), follow=False):
                return True
        episode = self.library.db.get_episode(int(episode_id))
        if episode is None or not episode.has_media:
            return False
        if self.episode is not None and self.episode.id != episode.id:
            self._finish_current(playing_next=True)
        elif self.episode is not None and self.episode.id == episode.id and self.loaded:
            if start_at is not None:
                self.seek(start_at)
            if start:
                self.play(checked=True)
            return True
        self._set_episode(episode)
        if start_at is not None:
            self.position = max(0, int(start_at))
        return self._open(start)

    def _source(self):
        """A file of our own, or a web address; None for anything else. Feeds can name any address, and mpv
        would open file:, av://lavfi: and its other protocols too."""
        episode = self.episode
        if episode.file_path:
            if os.path.exists(episode.file_path):
                return episode.file_path
            # The file vanished: forget it and stream instead.
            self.library.db.update_episode(episode.id, file_path="", downloaded_at=0)
            episode.file_path, episode.downloaded_at = "", 0
            bus.emit("episodes-changed", {episode.id})
        url = episode.media_url
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return None
        if (self.feed is not None and self.feed.username and not parts.username
                and (auth_allowed(self.feed.url, url) or parts.scheme == "https")):
            # As user:password in the address, not as a header: ffmpeg sends a header to every host a redirect
            # points to, but address credentials only to that host and only once it asks for them (401). So the
            # podcast's own server gets them, and an https media host that asks; statistics services never.
            userinfo = f"{quote(self.feed.username, safe='')}:{quote(self.feed.password or '', safe='')}@"
            url = urlunsplit(parts._replace(netloc=userinfo + parts.netloc))
        return url

    def _open(self, start):
        if not self.ensure_mpv():
            return False
        episode = self.episode
        if self.feed is not None and self.feed.skip_intro > 0 and self.position < self.feed.skip_intro * 1000:
            self.position = self.feed.skip_intro * 1000
            bus.emit("message", _("Skipped the first {n} s").format(n=self.feed.skip_intro), "info")
        self.start_position = self.position
        url = self._source()
        if url is None:
            self._fail(_("The episode's file is gone.") if episode.is_local
                       else _("The episode's address is not a web address, so omaPOD does not open it."))
            return False
        options = {"start": f"{self.position / 1000:.3f}"}
        if episode.is_video:
            self.mpv.set("vid", "auto")
            self.mpv.set("force-window", "yes")
            self.mpv.set("title", f"{episode.title} — omaPOD".replace("$", "$$"))  # no ${property} expansion
        else:
            self.mpv.set("vid", "no")
            self.mpv.set("force-window", "no")
        self.mpv.set("speed", self.speed)
        self.mpv.set("volume", self.volume)
        self.mpv.set("volume-gain", self._volume_gain_db())
        self._apply_skip_silence()
        self._want_play = start
        self.mpv.set("pause", not start)
        self.loaded = False
        self._ending = False
        try:
            self.mpv.loadfile(url, options)
        except MpvError as error:
            self._fail(str(error))
            return False
        self.status = "loading" if start else "paused"
        self.library.db.set_meta("current_episode", episode.id)
        if episode.is_new:
            self.library.remove_from_inbox([episode.id])
        if start:
            self._maybe_auto_sleep()
            self._start_tick()
        self._emit_state()
        self._emit_position()
        self._load_chapters(None)
        return True

    def _apply_skip_silence(self):
        want = self.skip_silence_enabled()
        if want == self._skip_silence_on or self.mpv is None:
            return
        try:
            if want:
                self.mpv.command("af", "add", SKIP_SILENCE_FILTER)
            else:
                self.mpv.command("af", "remove", "@skipsilence")
            self._skip_silence_on = want
        except MpvError:
            pass

    def _load_chapters(self, embedded):
        """Merge feed, file and Podcasting 2.0 chapters in a worker, then show them."""
        episode = self.episode
        if episode is None:
            return
        self._chapter_token += 1
        token = self._chapter_token

        def work():
            try:
                chapters = self.library.chapters_for(episode, embedded=embedded)
            except Exception:
                chapters = None
            GLib.idle_add(apply, chapters)

        def apply(chapters):
            if token != self._chapter_token or self.episode is None or self.episode.id != episode.id:
                return False
            if chapters:
                self.chapters = chapters
                self.chapter_index = chapter_utils.index_at(chapters, self.position)
                self._emit_state()
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ transport

    def play(self, checked=False):
        if not checked and self.status not in ("playing", "loading") and self._wait_for_sync(
                lambda: self.play(checked=True), follow=True):
            return True
        if self.episode is None:
            queue = self.library.queue_ids()
            if queue:
                return self.play_episode(queue[0], checked=True)
            return False
        if not self.loaded or self.mpv is None or not self.mpv.alive:
            return self._open(True)
        self._want_play = True
        self.mpv.set("pause", False)
        self._on_playing()
        self._maybe_auto_sleep()
        return True

    def pause(self):
        self._cancel_wait()
        if self.mpv is None or not self.loaded:
            if self.status != "idle":
                self.status = "paused"
                self._emit_state()
            return
        self._want_play = False
        self.mpv.set("pause", True)
        self._on_paused()

    def toggle(self):
        if self.status in ("playing", "loading"):
            self.pause()
        else:
            self.play()

    # ------------------------------------------------------------ other devices (sync)

    def _wait_for_sync(self, start, follow):
        """Before playback starts, let sync fetch where other devices are (switching from the phone mid-episode).
        Returns True when start() runs later; pausing in between cancels it."""
        if self.sync_check is None or self._awaiting_sync:
            return False
        token = object()

        def done():
            if self._play_token is token:
                self._play_token, self._awaiting_sync = None, False
                start()
            return False

        if not self.sync_check(done, follow):
            return False
        self._play_token, self._awaiting_sync = token, True
        if self.episode is not None:
            self.status = "loading"
            self._emit_state()
        GLib.timeout_add(SYNC_WAIT_MS, done)
        return True

    def _cancel_wait(self):
        if self._awaiting_sync:
            self._play_token, self._awaiting_sync = None, False
            if self.status == "loading" and not self.loaded:
                self.status = "paused" if self.episode is not None else "idle"

    def adopt_remote(self, episode_id, position_ms, kind):
        """Sync applied another device's newer position to the loaded episode; follow it unless playing here."""
        if self.episode is None or self.episode.id != episode_id or self.plays_here():
            return False
        if kind != "heard":
            # Finished (or marked as played) elsewhere: let it go, like after the end of an episode.
            title = self.episode.title
            self.stop(save=False)
            bus.emit("message", _("Finished on your other device: {title}").format(title=title), "info")
            return True
        if abs(int(position_ms) - int(self.position)) < 1000:
            return False
        self.position = self.start_position = max(0, int(position_ms))
        self.episode.position = self.position
        self._counted_until = self.position
        self._raw_pos = self.position / 1000
        if self.mpv is not None and self.loaded:
            try:
                self.mpv.command("seek", f"{self.position / 1000:.3f}", "absolute+exact")
            except MpvError:
                pass
        self.chapter_index = chapter_utils.index_at(self.chapters, self.position)
        self._emit_position()
        self._emit_state()
        bus.emit("message", _("Now at {time}, where your other device stopped").format(
            time=fmt_clock(self.position)), "info")
        return True

    def follow_remote(self, episode_id):
        """Another device played this episode after anything was heard here: get it ready where it stopped."""
        if self.plays_here() or (self.episode is not None and self.episode.id == episode_id):
            return False
        episode = self.library.db.get_episode(int(episode_id))
        if episode is None or not episode.has_media or episode.is_played:
            return False
        if self.episode is not None:
            self._update_position()
            self._save_position()
        self._stop_tick()
        if self.mpv is not None and self.mpv.alive and self.loaded:
            self._ending = True  # the old file goes; nothing ended
            try:
                self.mpv.command("stop")
            except MpvError:
                pass
        self._set_episode(episode)
        if not self._awaiting_sync:
            self.status = "paused"
        self.library.db.set_meta("current_episode", episode.id)
        self._emit_state()
        self._emit_position()
        bus.emit("message", _("Ready where your other device stopped: {title}").format(title=episode.title), "info")
        return True

    def _on_playing(self):
        if self.status == "playing":
            return
        self.status = "playing"
        self._played_this_session = True
        self._counted_until = self.position
        self._last_save = time.monotonic()
        self._start_tick()
        self._emit_state()

    def _on_paused(self):
        if self.status == "paused":
            return
        self._update_position()
        self.status = "paused"
        self._stop_tick()
        self._save_position()
        if self.episode is not None:
            episode = self.library.db.get_episode(self.episode.id, full=False)
            if episode is not None:
                self.library.sync_episode_played(episode, self.start_position, False)
        self._emit_state()

    def stop(self, save=True):
        """Stop and unload (AntennaPod's 'no media playing'). Without save, the position stays as it is in the
        library (another device set it)."""
        self._cancel_wait()
        if self.episode is not None and save:
            self._update_position()
            self._save_position()
        if self.mpv is not None and self.mpv.alive:
            self._ending = True
            try:
                self.mpv.command("stop")
            except MpvError:
                pass
        self._stop_tick()
        self.episode = None
        self.feed = None
        self.loaded = False
        self.status = "idle"
        self.position = self.duration = 0
        self.chapters = []
        self.chapter_index = -1
        self.library.db.set_meta("current_episode", "")
        self._emit_state()
        self._emit_position()

    def seek(self, position_ms):
        if self.episode is None:
            return
        position_ms = max(0, int(position_ms))
        if self.duration:
            position_ms = min(position_ms, max(0, self.duration - 500))
        self.position = position_ms
        self._counted_until = position_ms
        self._raw_pos = position_ms / 1000
        if self.mpv is not None and self.loaded:
            try:
                self.mpv.command("seek", f"{position_ms / 1000:.3f}", "absolute+exact")
            except MpvError:
                pass
        self.chapter_index = chapter_utils.index_at(self.chapters, self.position)
        self._save_position()
        self._emit_position()
        self._emit_state()

    def seek_relative(self, delta_ms):
        self._update_position()
        self.seek(self.position + delta_ms)

    def rewind(self):
        self.seek_relative(-int(self.settings.get("rewind_secs") or 10) * 1000)

    def fast_forward(self):
        self.seek_relative(int(self.settings.get("fast_forward_secs") or 30) * 1000)

    def restart(self):
        self.seek(0)

    def next_chapter(self):
        if not self.chapters:
            self.skip()
            return
        index = chapter_utils.index_at(self.chapters, self.position) + 1
        if index >= len(self.chapters):
            self.skip()
            return
        self.seek(self.chapters[index]["start"])

    def previous_chapter(self):
        if not self.chapters:
            self.restart()
            return
        index = chapter_utils.index_at(self.chapters, self.position)
        if index > 0 and self.position - self.chapters[index]["start"] < 3000:
            index -= 1
        self.seek(self.chapters[max(index, 0)]["start"])

    def seek_chapter(self, index):
        if 0 <= index < len(self.chapters):
            self.seek(self.chapters[index]["start"])
            if self.status != "playing":
                self.play()

    def hardware_button(self, which):
        """MPRIS next/previous mapped like AntennaPod's 'Reassign hardware buttons'."""
        action = self.settings.get("forward_button" if which == "next" else "previous_button")
        {"fast_forward": self.fast_forward, "rewind": self.rewind, "skip": self.skip,
         "restart": self.restart}.get(action, self.fast_forward if which == "next" else self.rewind)()

    def skip(self):
        """Skip episode: AntennaPod's startNextInQueue(wasSkipped=true)."""
        if self.episode is None:
            return
        self._advance(ended=False, skipped=True)

    # ------------------------------------------------------------ speed, volume

    def set_speed(self, speed):
        speed = max(0.25, min(4.0, round(float(speed), 2)))
        if self.feed is not None and self.feed.speed and self.feed.speed > 0:
            self.temp_speed = speed
        else:
            self.temp_speed = None
            self.settings.set("speed", speed)
        if self.mpv is not None and self.mpv.alive:
            self.mpv.set("speed", speed)
        self._emit_state()

    def cycle_speed(self):
        presets = sorted({float(s) for s in self.settings.get("speed_presets") or [1.0]})
        current = self.speed
        later = [s for s in presets if s > current + 0.001]
        self.set_speed(later[0] if later else presets[0])
        bus.emit("message", _("Playback speed: {speed}").format(speed=fmt_speed(self.speed)), "info")

    def set_volume(self, volume):
        self.volume = max(0, min(150, int(volume)))
        self.settings.set("volume", self.volume)
        if self.mpv is not None and self.mpv.alive:
            self.mpv.set("volume", self.volume)
        self._emit_state()

    def set_skip_silence(self, enabled):
        if self.feed is not None and self.feed.skip_silence in (0, 1):
            self.library.update_feed_settings(self.feed.id, skip_silence=1 if enabled else 0)
            self.feed.skip_silence = 1 if enabled else 0
        else:
            self.settings.set("skip_silence", bool(enabled))
        self._apply_skip_silence()
        self._emit_state()

    def _on_setting(self, key, value):
        if key in ("skip_silence",):
            self._apply_skip_silence()
            self._emit_state()
        elif key == "speed" and self.temp_speed is None and self.mpv is not None and self.mpv.alive:
            if not (self.feed is not None and self.feed.speed and self.feed.speed > 0):
                self.mpv.set("speed", float(value))
            self._emit_state()
        elif key in ("rewind_secs", "fast_forward_secs", "episode_cover"):
            self._emit_state()

    def feed_settings_changed(self, feed_id):
        if self.feed is not None and self.feed.id == feed_id:
            self.feed = self.library.db.get_feed(feed_id)
            if self.mpv is not None and self.mpv.alive:
                self.mpv.set("speed", self.speed)
                self.mpv.set("volume-gain", self._volume_gain_db())
            self._apply_skip_silence()
            self._emit_state()

    # ------------------------------------------------------------ sleep timer

    def set_sleep_timer(self, minutes=0, episodes=0):
        if episodes:
            self.sleep = {"mode": "episodes", "episodes": int(episodes), "remaining": 0}
        else:
            self.sleep = {"mode": "time", "remaining": int(minutes * 60000), "episodes": 0}
        self.settings.set("sleep_minutes", int(minutes) if minutes else self.settings.get("sleep_minutes"))
        bus.emit("message", _("Sleep timer enabled"), "info")
        self._emit_state()

    def extend_sleep_timer(self, minutes):
        if self.sleep is None:
            self.set_sleep_timer(minutes)
            return
        if self.sleep["mode"] == "time":
            self.sleep["remaining"] += int(minutes * 60000)
        else:
            self.sleep["episodes"] += 1
        self._restore_fade()
        self._emit_state()

    def cancel_sleep_timer(self):
        self.sleep = None
        self._restore_fade()
        self._emit_state()

    def _maybe_auto_sleep(self):
        if self.sleep is not None or not self.settings.get("sleep_auto_enable"):
            return
        hour = datetime.now().hour
        start, end = int(self.settings.get("sleep_auto_from")), int(self.settings.get("sleep_auto_to"))
        in_range = start <= hour < end if start <= end else (hour >= start or hour < end)
        if in_range:
            self.set_sleep_timer(int(self.settings.get("sleep_minutes") or 15))

    def _restore_fade(self):
        if self.mpv is not None and self.mpv.alive:
            self.mpv.set("volume", self.volume)

    # ------------------------------------------------------------ ticking

    def _start_tick(self):
        if self._tick_id is None:
            self._last_tick = time.monotonic()
            self._tick_id = GLib.timeout_add(TICK_MS, self._tick)

    def _stop_tick(self):
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _update_position(self):
        if self._raw_pos is not None and self.loaded:
            self.position = int(self._raw_pos * 1000)

    def _tick(self):
        entry_id = self._tick_id
        result = self._tick_body()
        if not result and self._tick_id == entry_id:
            self._tick_id = None
        return result

    def _tick_body(self):
        if self.status != "playing" or self.episode is None:
            return False
        now = time.monotonic()
        elapsed = (now - self._last_tick) * 1000 if self._last_tick else TICK_MS
        self._last_tick = now
        self._update_position()
        # Listening statistics count content time, like AntennaPod.
        if self._counted_until is not None:
            delta = self.position - self._counted_until
            if 0 < delta <= elapsed * max(self.speed, 1) * 6 + 2000:
                self._listened += delta
            self._counted_until = self.position
        index = chapter_utils.index_at(self.chapters, self.position)
        if index != self.chapter_index:
            self.chapter_index = index
            self._emit_state()
        if (self.feed is not None and self.feed.skip_ending > 0 and self.duration > self.feed.skip_ending * 1000
                and self.position >= self.duration - self.feed.skip_ending * 1000):
            bus.emit("message", _("Skipped the last {n} s").format(n=self.feed.skip_ending), "info")
            self._on_ended()
            return False
        if self.sleep is not None and self.sleep["mode"] == "time":
            self.sleep["remaining"] -= elapsed
            remaining = self.sleep["remaining"]
            if remaining <= 0:
                self.sleep = None
                self.pause()
                self._restore_fade()
                bus.emit("message", _("Sleep timer: playback paused"), "info")
                self._emit_state()
                return False
            if remaining < FADE_MS and self.mpv is not None:
                self.mpv.set("volume", max(0, int(self.volume * remaining / FADE_MS)))
            if int(remaining / 1000) != int((remaining + elapsed) / 1000):
                self._emit_state()
        if (now - self._last_save) * 1000 >= SAVE_EVERY_MS:
            self._save_position()
        self._emit_position()
        return True

    def _save_position(self):
        if self.episode is None:
            return
        self._last_save = time.monotonic()
        listened, self._listened = self._listened, 0
        self.library.save_position(self.episode.id, self.position, self.duration or None, listened)
        self.episode.position = self.position
        if self.duration:
            self.episode.duration = self.duration

    # ------------------------------------------------------------ end of an episode

    def _finish_current(self, playing_next):
        """The user starts another episode while this one is loaded."""
        if self.episode is None:
            return
        self._update_position()
        self._save_position()
        episode = self.library.db.get_episode(self.episode.id, full=False)
        if episode is not None:
            self.library.playback_finished(episode, self.start_position, ended=False, skipped=False,
                                           playing_next=playing_next, played=self._played_this_session)
        self._stop_tick()

    def _on_ended(self):
        if self.episode is None:
            return
        self._ending = True
        self.position = self.duration or self.position
        self._save_position()
        if self.sleep is not None and self.sleep["mode"] == "episodes":
            self.sleep["episodes"] -= 1
            if self.sleep["episodes"] <= 0:
                self.sleep = None
                episode = self.library.db.get_episode(self.episode.id, full=False)
                self.library.playback_finished(episode, self.start_position, ended=True, skipped=False,
                                               playing_next=False, played=True)
                self.stop()
                bus.emit("message", _("Sleep timer: playback stopped"), "info")
                return
        self._advance(ended=True, skipped=False)

    def _advance(self, ended, skipped):
        current = self.episode
        self._update_position()
        self._save_position()
        next_id = self.library.next_in_queue(current.id)
        episode = self.library.db.get_episode(current.id, full=False)
        self.library.playback_finished(episode, self.start_position, ended=ended, skipped=skipped,
                                       playing_next=bool(next_id), played=self._played_this_session)
        self._stop_tick()
        if next_id:
            next_episode = self.library.db.get_episode(next_id)
            if next_episode is not None and next_episode.has_media:
                self._set_episode(next_episode)
                self._open(bool(self.settings.get("continuous_playback")))
                return
        self.stop()
        if skipped:
            bus.emit("message", _("This was the last episode in the queue"), "info")

    def shutdown(self):
        if self.episode is not None:
            self._update_position()
            if self.status == "playing":
                self._on_paused()
            else:
                self._save_position()
        self._stop_tick()
        if self.mpv is not None:
            self.mpv.quit()
            self.mpv = None
