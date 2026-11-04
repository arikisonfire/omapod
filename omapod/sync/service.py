"""The synchronization run, following AntennaPod's SyncService step by step:

1. subscriptions: download remote changes since the last sync, apply them,
   upload local changes (everything on the very first sync);
2. refresh podcasts that arrived from the server, so their episodes exist;
3. episode actions: download remote actions, apply only 'play' actions that
   are newer than local ones, upload local actions (on the first sync also
   the played state of every played episode).

omaPOD adds:
- the history follows when an episode was heard, on whichever device;
- the episode another device played last is picked up by the player, and
  before playback starts the latest positions are fetched (switching from
  the phone mid-episode);
- what other devices downloaded is listed on the Downloads page, and
  optionally downloaded here too (from their 'download' and 'delete'
  actions; Nextcloud's GPodder Sync up to 3.18 drops those);
- optionally, queue and favorites are shared between omaPOD computers as
  files in the user's Nextcloud. AntennaPod has no way to exchange them.
"""

import hashlib
import json
import socket
import threading
import time
import traceback
from datetime import datetime, timezone

from ..events import bus
from ..i18n import _, ngettext
from ..models import FEED_PREVIEW, FEED_SUBSCRIBED, LOCAL_PREFIX, NEW, PLAYED, UNPLAYED
from ..util import MAX_SECONDS, now_ms, now_s
from .providers import GpodderNet, Nextcloud, SyncAuthError, SyncError


# Version of the one-time correction of the history (see SyncService._repair_history).
HISTORY_REPAIR = 2
# What a sync can run into without a bug: the server's errors, the network, broken JSON.
EXPECTED_ERRORS = (SyncError, OSError, ValueError, KeyError)


def provider_from_credentials(creds):
    sync = (creds or {}).get("sync") or {}
    kind = sync.get("provider")
    if kind == "gpodder":
        return GpodderNet(sync.get("server") or "gpodder.net", sync.get("username", ""), sync.get("password", ""),
                          sync.get("device_id") or default_device_id())
    if kind == "nextcloud":
        return Nextcloud(sync.get("server", ""), sync.get("username", ""), sync.get("password", ""),
                         sync.get("cert_sha256", ""))
    return None


def default_device_id():
    host = socket.gethostname().split(".")[0] or "desktop"
    return f"omapod-{host}".lower()


def parse_timestamp(value):
    """An episode action's time in UTC, or None. AntennaPod and gpodder.net write UTC without a zone
    ('2026-10-24T19:00:00'); Nextcloud's GPodder Sync answers with one ('2026-10-24T19:00:00+00:00')."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            return datetime.fromtimestamp(int(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        try:
            stamp = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def epoch(value):
    """An episode action's time in epoch seconds, 0 when unknown."""
    stamp = parse_timestamp(value)
    return int(stamp.timestamp()) if stamp is not None else 0


def is_mark(action):
    """'Mark as played' and the bulk upload of a first sync send started = position = total. Their time says when
    that happened, not when the episode was heard."""
    return _int(action.get("started")) == _int(action.get("position")) == _int(action.get("total"))


def listening_times(actions):
    """From the play actions of one episode: when a device last listened to it, and when one first only marked
    it as played."""
    heard = marked = 0
    for action in actions:
        when = epoch(action.get("timestamp"))
        if not when:
            continue
        if is_mark(action):
            marked = min(marked, when) if marked else when
        else:
            heard = max(heard, when)
    return heard, marked


def history_time(episode, heard, marked):
    """completed_at (the history's time) after another device played an episode: when it was last heard
    anywhere. Being marked as played only counts while nothing else is known."""
    if heard:
        return max(episode.completed_at, heard)
    return episode.completed_at or marked or now_s()


def seen_keys(action):
    """sync_seen keys of a play action: the media address and, when there is one, the guid (the server
    matches by either)."""
    keys = [f"url:{action.get('episode')}"] if action.get("episode") else []
    guid = str(action.get("guid") or "").strip()
    if guid and guid != "null":
        keys.append(f"guid:{guid}")
    return keys


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _seconds(value):
    """Seconds in another device's action, or None when they are no usable number (infinite, or beyond what
    SQLite stores once in milliseconds)."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if abs(number) <= MAX_SECONDS else None


def shared_entry(episode):
    item = {"podcast": episode.feed_url, "episode": episode.media_url, "title": episode.title}
    if episode.guid:
        item["guid"] = episode.guid
    return item


def shared_key(item):
    return (str(item.get("podcast") or ""), str(item.get("episode") or ""))


def shared_hash(items):
    return hashlib.sha1(json.dumps([shared_key(i) for i in items]).encode("utf-8")).hexdigest()


def newest_actions(remote, local):
    """AntennaPod's EpisodeActionFilter: remote play actions that are newer than local ones."""
    local_latest = {}
    for action in local:
        key = (action.get("podcast"), action.get("episode"))
        stamp = parse_timestamp(action.get("timestamp"))
        current = local_latest.get(key)
        if current is None or (stamp is not None and (current[0] is None or stamp > current[0])):
            local_latest[key] = (stamp, action)
    winners = {}
    for action in remote:
        if str(action.get("action", "")).lower() != "play":
            continue
        if not action.get("podcast") or not action.get("episode"):
            continue
        key = (action["podcast"], action["episode"])
        stamp = parse_timestamp(action.get("timestamp"))
        local_entry = local_latest.get(key)
        if local_entry and local_entry[0] is not None and (stamp is None or local_entry[0] > stamp):
            continue
        current = winners.get(key)
        if current and current[0] is not None and (stamp is None or current[0] > stamp):
            continue
        winners[key] = (stamp, action)
    return [action for _stamp, action in winners.values()]


class SyncService:
    # Light syncs (pull) that only fetch what other devices played: before playback starts and, on a Nextcloud,
    # every few minutes while nothing plays here, so the player and the bar show where the phone stopped.
    PULL_FRESH_S = 15  # a pull this recent is good enough before playback starts
    PULL_RETRY_S = 300  # after a failed pull (offline), playback does not wait for the server again this soon
    POLL_EVERY_S = {"nextcloud": 180}

    def __init__(self, library, settings, credentials):
        self.library = library
        self.db = library.db
        self.settings = settings
        self.credentials = credentials
        self._lock = threading.Lock()
        self.running = False
        self._last_pull = 0.0
        self._pull_failed = 0.0
        self._queue_token = bus.subscribe("queue-changed", self._on_queue_changed)

    def close(self):
        bus.unsubscribe(self._queue_token)

    def provider(self):
        if not self.settings.get("sync_provider"):
            return None
        return provider_from_credentials(self.credentials.load())

    def status(self, text):
        bus.emit("sync-state", self.running, text)

    def run(self, full=False):
        """Synchronize now. Runs in the calling worker thread. Returns True on success."""
        provider = self.provider()
        if provider is None:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        self.running = True
        self.settings.set("sync_last_attempt", now_s())
        try:
            if full:
                self.settings.set("sync_last_subscriptions", 0)
                self.settings.set("sync_last_actions", 0)
            self.status(_("Sync started"))
            provider.login()
            new_feeds = self._sync_subscriptions(provider)
            if new_feeds:
                self.status(_("Waiting for subscriptions refresh…"))
                self.library.refresh_all(feed_ids=new_feeds, force=True)
            self._sync_actions(provider)
            if self.settings.get("sync_queue_favorites") and provider.shares_files:
                self.status(_("Synchronizing queue and favorites…"))
                self._sync_queue(provider)
                self._sync_favorites(provider)
            provider.logout()
            self._last_pull, self._pull_failed = time.monotonic(), 0.0
            self.settings.set("sync_last_success", True)
            self.settings.set("sync_last_error", "")
            self.running = False
            self.status(_("Synchronization successful"))
            return True
        except Exception as error:  # whatever a broken or hostile server causes, it must not stop the app or timer
            if not isinstance(error, EXPECTED_ERRORS):
                traceback.print_exc()
            self.settings.set("sync_last_success", False)
            message = str(error) or error.__class__.__name__
            self.settings.set("sync_last_error", message)
            self.running = False
            self.status(_("Synchronization failed"))
            if self.settings.get("notify_sync_errors"):
                bus.emit("sync-error", message, isinstance(error, SyncAuthError))
            return False
        finally:
            self.running = False
            self._lock.release()

    def pull_due(self, fresh_s=None):
        """Whether fetching other devices' positions is worth it now (not right after one, not while offline)."""
        if not self.settings.get("sync_provider") or not int(self.settings.get("sync_last_actions") or 0):
            return False
        now = time.monotonic()
        if self._pull_failed and now - self._pull_failed < self.PULL_RETRY_S:
            return False
        return not self._last_pull or now - self._last_pull >= (self.PULL_FRESH_S if fresh_s is None else fresh_s)

    def poll_due(self):
        """Whether the idle poll of this provider is due (Nextcloud only: it is the user's own server)."""
        every = self.POLL_EVERY_S.get(self.settings.get("sync_provider"))
        return bool(every) and self.pull_due(every)

    def pull(self, timeout=4, follow=True):
        """Fetch and apply what other devices played, without uploading anything. Runs in a worker thread and
        gives up quickly, so playback that waits for it starts anyway. Returns True when the server answered."""
        provider = self.provider()
        last = int(self.settings.get("sync_last_actions") or 0)
        if provider is None or not last:
            return False  # the first sync uploads too; it runs as a full sync
        if not self._lock.acquire(timeout=0.5):
            return False  # a full sync is running and does this as well
        try:
            provider.timeout = timeout
            changes = self._pull(provider, last, follow)
            self.settings.set("sync_last_actions", changes.timestamp or last)
            self._last_pull, self._pull_failed = time.monotonic(), 0.0
            return True
        except Exception as error:
            if not isinstance(error, EXPECTED_ERRORS):
                traceback.print_exc()
            self._pull_failed = time.monotonic()
            return False
        finally:
            self._lock.release()

    # -- subscriptions ---------------------------------------------------------

    def _local_subscription_urls(self):
        return [f.url for f in self.db.get_feeds(states=(FEED_SUBSCRIBED,), with_counts=False)
                if not f.url.startswith(LOCAL_PREFIX)]

    def _sync_subscriptions(self, provider):
        last = int(self.settings.get("sync_last_subscriptions") or 0)
        self.status(_("Synchronizing subscriptions…"))
        local = self._local_subscription_urls()
        local_set = set(local)
        changes = provider.get_subscription_changes(last)
        new_timestamp = changes.timestamp
        pending = self.db.sync_pending("sub")
        queued_added = [p["url"] for _id, p in pending if p.get("action") == "add"]
        queued_removed = [p["url"] for _id, p in pending if p.get("action") == "remove"]
        new_feeds = []
        for url in changes.added:
            if not url.startswith("http"):
                continue
            if url in local_set or url in queued_removed:
                continue
            existing = self.db.get_feed_by_url(url)
            if existing is not None:
                if existing.state == FEED_PREVIEW:  # an archived one stays archived: that is local
                    self.db.update_feed(existing.id, state=FEED_SUBSCRIBED, keep_updated=1)
                    new_feeds.append(existing.id)
                continue
            with self.db.tx() as conn:
                feed_id = self.db.insert_feed(conn, {"url": url, "title": _("Unknown podcast"),
                                                     "state": FEED_SUBSCRIBED, "added": now_s()})
            new_feeds.append(feed_id)
        removed_ids = []
        for url in changes.removed:
            if url in queued_added:
                continue
            feed = self.db.get_feed_by_url(url)
            if feed is not None and feed.state == FEED_SUBSCRIBED:
                removed_ids.append(feed.id)
        if removed_ids:
            # The server already knows, so no 'remove' goes back into the sync queue.
            self.library.unsubscribe(removed_ids, sync=False)
        if last == 0:
            queued_added = list(dict.fromkeys(queued_added + local))
        queued_added = [u for u in queued_added if u not in changes.added]
        queued_removed = [u for u in queued_removed if u not in changes.removed]
        if queued_added or queued_removed:
            new_timestamp = provider.upload_subscription_changes(queued_added, queued_removed)
        self.db.sync_remove([row_id for row_id, _p in pending])
        self.settings.set("sync_last_subscriptions", new_timestamp)
        if new_feeds or removed_ids:
            bus.emit("feeds-changed", None)
        return new_feeds

    # -- episode actions ---------------------------------------------------------

    def _sync_actions(self, provider):
        last = int(self.settings.get("sync_last_actions") or 0)
        self.status(_("Downloading episode changes…"))
        changes = self._pull(provider, last)
        new_timestamp = changes.timestamp
        everything = []

        def all_actions():
            # Everything the server knows, not only what is new; fetched once per run.
            if not everything:
                everything.extend(provider.get_episode_actions(0).actions if last else changes.actions)
            return everything

        if int(self.settings.get("history_repair") or 0) < HISTORY_REPAIR:
            self._repair_history(all_actions())
            self.settings.set("history_repair", HISTORY_REPAIR)
        # The first time, everything the server knows: what other devices have downloaded right now.
        initial = not self.settings.get("remote_downloads_ready")
        newly = self._track_downloads(all_actions() if initial else changes.actions, getattr(provider, "device_id", ""))
        self.settings.set("remote_downloads_ready", True)
        if self.settings.get("sync_mirror_downloads"):
            if not self.settings.get("mirror_downloads_ready"):
                # Just switched on: what is downloaded elsewhere right now, not only what is new.
                self._download_here(self.db.remote_downloads())
                self.settings.set("mirror_downloads_ready", True)
            elif not initial:
                self._download_here(newly)
        self.status(_("Uploading episode changes…"))
        played = []
        if last == 0:
            self.status(_("Uploading played status…"))
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            for episode in self.db.get_episodes(("played", "subscribed", "has_media")):
                if episode.feed_url.startswith(LOCAL_PREFIX):
                    continue
                seconds = episode.duration // 1000
                action = {"podcast": episode.feed_url, "episode": episode.media_url, "action": "play",
                          "timestamp": stamp, "started": seconds, "position": seconds, "total": seconds}
                if episode.guid:
                    action["guid"] = episode.guid
                played.append(action)
        uploaded = self._upload_actions(provider, played)
        self.settings.set("sync_last_actions", uploaded or new_timestamp)

    def _upload_actions(self, provider, extra=()):
        """Upload the actions waiting here (plus extra ones). Returns the server's time, or None when there was
        nothing to upload."""
        pending = self.db.sync_pending("action")
        # Play actions that another device's newer one has overtaken meanwhile would only set it back: the server
        # keeps the last one uploaded, not the newest.
        seen = self.db.sync_seen(k for _id, payload in pending for k in seen_keys(payload))
        stale = [row_id for row_id, payload in pending if str(payload.get("action", "")).lower() == "play"
                 and epoch(payload.get("timestamp")) < max((seen.get(k, 0) for k in seen_keys(payload)), default=0)]
        queued = [payload for row_id, payload in pending if row_id not in stale] + list(extra)
        timestamp = None
        if queued:
            timestamp = provider.upload_episode_actions(queued)
            # Asked for again on later syncs (they look back a while); they are this computer's own.
            self.db.mark_sync_seen([(key, epoch(a.get("timestamp"))) for a in queued for key in seen_keys(a)
                                    if str(a.get("action", "")).lower() == "play"]
                                   + [(f"download:{a.get('episode')}", epoch(a.get("timestamp"))) for a in queued
                                      if str(a.get("action", "")).lower() in ("download", "delete")])
        self.db.sync_remove([row_id for row_id, _p in pending])
        return timestamp

    def push(self, timeout=30):
        """Upload what was played here right away, after pausing and when quitting. A phone asks only for actions
        newer than its own last sync, so a position that reaches the server after that would never get there.
        Nothing is downloaded. Returns True when nothing is left waiting."""
        if not self.db.sync_pending("action"):
            return True
        provider = self.provider()
        if provider is None:
            return False
        if not self._lock.acquire(timeout=timeout):
            return False  # a sync still running; it or the next one uploads them
        try:
            provider.timeout = timeout
            self._upload_actions(provider)
            return True
        except Exception as error:  # offline, say: the next sync uploads them
            if not isinstance(error, EXPECTED_ERRORS):
                traceback.print_exc()
            return False
        finally:
            self._lock.release()

    def _pull(self, provider, last, follow=True):
        """Download remote actions and apply them; with follow, the player picks up the episode another device
        played last. Servers are asked a while back: they go by when an action happened, and a phone uploads
        late when it waits for Wi-Fi."""
        since = max(0, last - provider.actions_overlap) if last else 0
        changes = provider.get_episode_actions(since)
        activity = self.db.scalar("SELECT MAX(last_played) FROM episodes", (), 0)  # before anything is applied
        queued = [payload for _id, payload in self.db.sync_pending("action")]
        applied = self._apply_remote(changes.actions, queued)
        if follow:
            self._follow(applied, activity)
        return changes

    def _plays_by_episode(self, remote):
        plays = {}
        for action in remote:
            if str(action.get("action", "")).lower() == "play" and action.get("episode"):
                plays.setdefault((action.get("podcast"), action["episode"]), []).append(action)
        return plays

    def _plays_here(self):
        player = self.library.player
        return player is not None and player.plays_here()

    def _apply_remote(self, remote, local_queued):
        """AntennaPod's processEpisodeActions, plus the history and the time last heard. Actions applied before
        and ones older than listening here are left out. Returns what was applied:
        (episode id, action time, position in ms, kind 'heard', 'finished' or 'marked') per episode."""
        actions = newest_actions(remote, local_queued)
        if not actions:
            return []
        smart = int(self.settings.get("smart_mark_played_secs") or 0) * 1000
        plays = self._plays_by_episode(remote)
        current = self.library.current_episode_id()
        playing = self._plays_here()
        seen = self.db.sync_seen(k for action in actions for k in seen_keys(action))
        changed, dequeue, applied, marks = set(), [], [], []
        with self.db.tx() as conn:
            for action in actions:
                when, keys = epoch(action.get("timestamp")), seen_keys(action)
                if when and max((seen.get(k, 0) for k in keys), default=0) >= when:
                    continue  # applied before, or uploaded from here
                episode = self.db.find_episode_for_sync(action.get("guid"), action.get("episode"),
                                                        action.get("podcast"))
                if episode is None or not episode.media_url:
                    continue
                if episode.id == current and playing:
                    continue  # playing here: this computer is ahead and uploads when it pauses
                if when and episode.last_played > when * 1000:
                    continue  # heard here since then
                position, total = _seconds(action.get("position", -1)), _seconds(action.get("total", -1))
                if position is None or total is None:
                    continue
                position = max(0, position) * 1000
                values = {"position": position}
                duration = episode.duration or (total * 1000 if total > 0 else 0)
                heard, marked = listening_times(plays.get((action.get("podcast"), action["episode"]), []))
                if heard:
                    values["last_played"] = max(episode.last_played, heard * 1000)
                if position > 0 and episode.state == NEW:
                    values["state"] = UNPLAYED
                kind = "marked" if is_mark(action) else "heard"
                if duration > 0 and position >= duration - smart:
                    values.update(state=PLAYED, position=0)
                    dequeue.append(episode.id)
                    kind = "finished" if kind == "heard" else kind
                values["completed_at"] = history_time(episode, heard, marked)
                if not episode.duration and duration:
                    values["duration"] = duration
                self.db.update_episode(episode.id, conn=conn, **values)
                marks += [(key, when) for key in keys]
                changed.add(episode.id)
                applied.append((episode.id, when, values["position"], kind))
            if dequeue:
                conn.execute(f"DELETE FROM queue WHERE episode_id IN ({','.join('?' * len(dequeue))})", dequeue)
            self.db.mark_sync_seen(marks, conn=conn)
        if changed:
            bus.emit("episodes-changed", changed)
            bus.emit("feeds-changed", None)
            if dequeue:
                bus.emit("queue-changed")
        for episode_id, _when, position, kind in applied:
            if episode_id == current:
                # The player has this episode loaded, paused: it moves to the other device's position.
                bus.emit("remote-playback", episode_id, position, kind)
        return applied

    def _follow(self, applied, activity):
        """The episode another device played last, if that was after anything was heard here and it is not
        finished: the player gets it ready at that position (a running playback here is never interrupted)."""
        if not self.settings.get("sync_follow_playback"):
            return
        unfinished = [a for a in applied if a[3] == "heard" and a[1] * 1000 > activity]
        if not unfinished:
            return
        episode_id = max(unfinished, key=lambda a: a[1])[0]
        if episode_id == self.library.current_episode_id():
            return  # already loaded; 'remote-playback' moved it
        if self.library.player is None:
            # Without a window (the background refresh): omaPOD starts with it.
            self.db.set_meta("current_episode", episode_id)
        else:
            bus.emit("remote-follow", episode_id)

    def _repair_history(self, remote):
        """Once: the history follows when episodes were last heard. omaPOD 0.1 put episodes finished elsewhere
        into the history at the time of the sync, could not read the times Nextcloud sends, and left out
        everything not heard to the end."""
        changed = set()
        with self.db.tx() as conn:
            for (podcast, url), actions in self._plays_by_episode(remote).items():
                guid = next((a.get("guid") for a in actions if a.get("guid")), None)
                episode = self.db.find_episode_for_sync(guid, url, podcast)
                if episode is None:
                    continue
                heard, marked = listening_times(actions)
                heard_here = episode.last_played // 1000
                if heard or heard_here:
                    when = max(heard, heard_here)
                elif marked:
                    # Only ever marked as played: the earliest time is closest to when it was heard. An entry an
                    # older omaPOD made at sync time is later than that.
                    when = min(t for t in (episode.completed_at, marked) if t)
                else:
                    continue
                values = {}
                if when != episode.completed_at:
                    values["completed_at"] = when
                if heard * 1000 > episode.last_played:
                    values["last_played"] = heard * 1000
                if values:
                    self.db.update_episode(episode.id, conn=conn, **values)
                    changed.add(episode.id)
            # Heard here but not to the end: AntennaPod's history has these too.
            rows = conn.execute("SELECT id FROM episodes WHERE completed_at = 0 AND last_played > 0").fetchall()
            conn.execute("UPDATE episodes SET completed_at = last_played / 1000 WHERE completed_at = 0 "
                         "AND last_played > 0")
            changed.update(row[0] for row in rows)
        if changed:
            bus.emit("episodes-changed", changed)

    # -- downloads made on other devices ---------------------------------------

    def _track_downloads(self, remote, own_device=""):
        """Remember what other devices downloaded or deleted again (their newest action per episode) for the list
        on the Downloads page. Returns (media address, guid) of what turned up as downloaded elsewhere."""
        latest = {}
        for action in remote:
            kind = str(action.get("action", "")).lower()
            if kind not in ("download", "delete") or not action.get("episode"):
                continue
            if own_device and action.get("device") == own_device:
                continue  # gpodder.net says which device; Nextcloud does not
            when = epoch(action.get("timestamp"))
            current = latest.get(action["episode"])
            if current is None or when >= current[0]:
                latest[action["episode"]] = (when, kind, action)
        if not latest:
            return []
        # GPodder Sync up to 3.18 throws download reports away; now they arrive.
        self.settings.set("remote_downloads_seen", True)
        known = self.db.remote_download_states(latest)
        rows, newly = [], []
        for episode, (when, kind, action) in latest.items():
            downloaded, since = known.get(episode, (None, -1))
            if when < since or (when == since and downloaded == (kind == "download")):
                continue  # known already (sync asks a while back)
            rows.append((episode, action.get("guid"), kind == "download", when))
            if kind == "download":
                newly.append((episode, action.get("guid")))
        if rows:
            self.db.set_remote_downloads(rows)
            bus.emit("downloads-changed")
        return newly

    def _download_here(self, entries):
        """Download what another device downloaded, unless it is played (the setting is on)."""
        ids = []
        for episode_url, guid in entries:
            episode = self.db.find_episode_for_sync(guid, episode_url)
            if (episode is None or episode.is_downloaded or episode.is_played or episode.is_local
                    or episode.feed_state != FEED_SUBSCRIBED):
                continue
            ids.append(episode.id)
        count = self.library.download(ids, manual=False) if ids else 0
        if count:
            bus.emit("message", ngettext("Downloading {n} episode from your other devices",
                                         "Downloading {n} episodes from your other devices", count).format(n=count),
                     "info")

    # -- queue and favorites, shared between omaPOD computers -------------------

    def _queue_entries(self):
        return [shared_entry(e) for e in self.db.get_queue() if not e.is_local]

    def _on_queue_changed(self):
        if not self.settings.get("sync_queue_favorites") or not self.settings.get("sync_provider"):
            return
        if shared_hash(self._queue_entries()) != self.settings.get("queue_sync_local"):
            self.settings.set("queue_sync_changed", now_ms())
            bus.emit("sync-requested")

    def _resolve(self, items, refresh=False):
        """Local episode ids for shared entries. With refresh, podcasts with episodes missing here are
        refreshed first (episodes published after this computer's last refresh)."""

        def lookup():
            ids, missing = [], set()
            for item in items:
                episode = self.db.find_episode_for_sync(item.get("guid"), item.get("episode"), item.get("podcast"))
                if episode is not None:
                    ids.append(episode.id)
                    continue
                feed = self.db.get_feed_by_url(item.get("podcast") or "")
                if feed is not None and feed.state == FEED_SUBSCRIBED:
                    missing.add(feed.id)
            return ids, missing

        ids, missing = lookup()
        if missing and refresh:
            self.library.refresh_all(feed_ids=sorted(missing), force=True)
            ids, _missing = lookup()
        return list(dict.fromkeys(ids))

    def _sync_queue(self, provider):
        """The queue has an order, so the newer change wins as a whole. Joining keeps both queues."""
        local = self._queue_entries()
        remote, etag = provider.get_shared("queue.json")
        shared = remote.get("episodes") if isinstance(remote, dict) else None
        shared = [i for i in shared if isinstance(i, dict)] if isinstance(shared, list) else None
        modified, incoming = now_ms(), False
        if shared is None:
            result = local
        elif not self.settings.get("queue_sync_base"):
            known = {shared_key(i) for i in shared}
            result, incoming = shared + [i for i in local if shared_key(i) not in known], True
        else:
            local_changed = shared_hash(local) != self.settings.get("queue_sync_local")
            remote_changed = shared_hash(shared) != self.settings.get("queue_sync_base")
            local_time = _int(self.settings.get("queue_sync_changed"))
            if local_changed and not (remote_changed and _int(remote.get("modified")) > local_time):
                result, modified = local, local_time or modified
            else:
                result, incoming = shared, remote_changed
        if shared_hash(result) != shared_hash(local):
            ids = self._resolve(result, refresh=incoming)
            if ids != self.db.queue_ids():
                self.library.replace_queue(ids)
        if shared is None or shared_hash(result) != shared_hash(shared):
            data = {"version": 1, "modified": modified, "device": default_device_id(), "episodes": result}
            if provider.put_shared("queue.json", data, etag) is None:
                return  # another computer wrote in between; the next run settles it
        self.settings.update({"queue_sync_base": shared_hash(result),
                              "queue_sync_local": shared_hash(self._queue_entries())})

    def _sync_favorites(self, provider):
        """Favorites are a set: what either side added or removed since the last sync wins, nothing is lost."""
        local_items = {shared_key(i): i for i in map(shared_entry, self.db.get_episodes(("favorite",)))
                       if not i["podcast"].startswith(LOCAL_PREFIX)}
        remote, etag = provider.get_shared("favorites.json")
        shared = remote.get("episodes") if isinstance(remote, dict) else None
        shared_items = {shared_key(i): i for i in shared if isinstance(i, dict)} if isinstance(shared, list) else None
        before = {tuple(k) for k in self.settings.get("favorites_sync_base") or []}
        local_before = {tuple(k) for k in self.settings.get("favorites_sync_local") or []}
        current = set(shared_items) if shared_items is not None else before
        added, removed = set(local_items) - local_before, local_before - set(local_items)
        result = (current | added) - removed
        items = {**local_items, **(shared_items or {})}
        result_items = [items[k] if k in items else {"podcast": k[0], "episode": k[1]} for k in sorted(result)]
        wanted = set(self._resolve(result_items, refresh=bool(current - before)))
        have = {e.id for e in self.db.get_episodes(("favorite",)) if not e.is_local}
        if wanted - have:
            self.library.set_favorite(sorted(wanted - have), True, sync=False)
        if have - wanted:
            self.library.set_favorite(sorted(have - wanted), False, sync=False)
        if shared_items is None or result != set(shared_items):
            data = {"version": 1, "modified": now_ms(), "device": default_device_id(), "episodes": result_items}
            if provider.put_shared("favorites.json", data, etag) is None:
                return
        now_local = [list(shared_key(shared_entry(e))) for e in self.db.get_episodes(("favorite",)) if not e.is_local]
        self.settings.update({"favorites_sync_base": [list(k) for k in sorted(result)],
                              "favorites_sync_local": sorted(now_local)})
