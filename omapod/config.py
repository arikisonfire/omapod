"""Filesystem locations and persistent settings.

Settings mirror AntennaPod's preferences. They live in a small JSON file so
they survive a database import and can be read without opening the database.
Secrets (the sync login, the proxy password, the Podcast Index key) are kept
in a separate file with mode 0600.
"""

import copy
import json
import os
import stat
import tempfile
import threading
from pathlib import Path


def _env_path(var, fallback):
    value = os.environ.get(var)
    return Path(value) if value else fallback


class Paths:
    def __init__(self):
        home = os.environ.get("OMAPOD_HOME")
        if home:
            # Portable/development mode: everything below one folder.
            base = Path(home)
            self.config = base / "config"
            self.data = base / "data"
            self.cache = base / "cache"
        else:
            self.config = _env_path("XDG_CONFIG_HOME", Path.home() / ".config") / "omapod"
            self.data = _env_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / "omapod"
            self.cache = _env_path("XDG_CACHE_HOME", Path.home() / ".cache") / "omapod"
        runtime = os.environ.get("OMAPOD_RUNTIME_DIR")
        if runtime:
            self.runtime = Path(runtime)
        elif os.environ.get("XDG_RUNTIME_DIR") and os.access(os.environ["XDG_RUNTIME_DIR"], os.W_OK):
            self.runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "omapod"
        else:
            self.runtime = self.data / "run"
        self._short_runtime = None

    @property
    def db(self):
        return self.data / "omapod.db"

    @property
    def settings_file(self):
        return self.config / "settings.json"

    @property
    def credentials_file(self):
        return self.config / "credentials.json"

    @property
    def images(self):
        return self.cache / "images"

    @property
    def default_downloads(self):
        return self.data / "episodes"

    @property
    def backups(self):
        return self.data / "backups"

    def _socket(self, name):
        path = self.runtime / name
        if len(str(path).encode()) <= 100:
            return path
        # Unix socket paths are limited to 108 bytes; deep development folders exceed that. The way out is a
        # folder in /tmp, which other users share: it must be this user's own and closed to others (they could
        # otherwise put their own socket in place), or else a new one with a random name.
        if self._short_runtime is None:
            folder = Path(tempfile.gettempdir()) / f"omapod-{os.getuid()}"
            try:
                folder.mkdir(mode=0o700, exist_ok=True)
                info = os.lstat(folder)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    raise PermissionError(f"{folder} belongs to someone else")
                if stat.S_IMODE(info.st_mode) != 0o700:
                    os.chmod(folder, 0o700)
            except OSError:
                folder = Path(tempfile.mkdtemp(prefix="omapod-"))
            self._short_runtime = folder
        return self._short_runtime / name

    @property
    def control_socket(self):
        return self._socket("control.sock")

    @property
    def mpv_socket(self):
        return self._socket(f"mpv-{os.getpid()}.sock")

    @property
    def state_file(self):
        """Last known player state, for the bar plugin while the app is closed."""
        return self.runtime / "state.json"

    def ensure(self):
        for path in (self.config, self.data, self.cache, self.images, self.runtime):
            path.mkdir(parents=True, exist_ok=True)
        # Owner only: listening history, passwords of protected podcasts, the proxy password.
        for path in (self.config, self.data, self.cache, self.runtime):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass


paths = Paths()


# Keys and defaults follow AntennaPod (see its UserPreferences); values that
# only make sense on a phone (mobile data, shake to reset) are left out.
DEFAULTS = {
    # User interface
    "default_page": "home",
    "last_page": "home",
    "subscriptions_view": "grid",
    "subscriptions_columns": 0,
    "subscriptions_show_titles": True,
    "subscriptions_sort": "alpha",
    "subscriptions_counter": "inbox",
    "subscriptions_tag": "",
    "episode_cover": True,
    "show_remaining_time": False,
    "time_respects_speed": False,
    "stream_over_download": False,
    "downloads_play_button": False,
    "default_sort": "date_desc",
    "home_sections": ["continue", "new", "surprise", "classics", "downloads"],
    "home_hidden": [],
    "nav_hidden": [],
    "window_width": 1180,
    "window_height": 760,
    "window_maximized": False,
    "show_sidebar_feeds": True,
    "language": "auto",
    # Playback
    "fast_forward_secs": 30,
    "rewind_secs": 10,
    "speed": 1.0,
    "speed_presets": [1.0, 1.25, 1.5],
    "skip_silence": False,
    "forward_button": "fast_forward",
    "previous_button": "rewind",
    "enqueue_location": "back",
    "enqueue_downloaded": True,
    "continuous_playback": True,
    "smart_mark_played_secs": 30,
    "skip_keeps_episode": True,
    "volume": 100,
    "sleep_minutes": 15,
    "sleep_auto_enable": False,
    "sleep_auto_from": 22,
    "sleep_auto_to": 6,
    "queue_keep_sorted": False,
    "queue_sort": "date_asc",
    "queue_locked": False,
    "audio_output": "auto",
    # Downloads
    "downloads_dir": "",
    "refresh_interval_min": 720,
    "new_episodes_action": "inbox",
    "auto_download": False,
    "auto_download_queue": False,
    "auto_download_on_battery": True,
    "episode_cache": 25,
    "auto_delete": False,
    "auto_delete_local": False,
    "favorite_keeps_episode": True,
    "episode_cleanup": -2,
    "delete_removes_from_queue": False,
    "parallel_downloads": 4,
    "proxy": {"type": "direct", "host": "", "port": 0, "username": ""},  # the password is in credentials.json
    # Notifications
    "notify_download_errors": True,
    "notify_sync_errors": True,
    # Synchronization (credentials are in credentials.json)
    "sync_provider": "",
    "sync_last_subscriptions": 0,
    "sync_last_actions": 0,
    "sync_last_attempt": 0,
    "sync_last_success": None,
    "sync_last_error": "",
    "sync_follow_playback": True,
    "sync_mirror_downloads": False,
    "sync_queue_favorites": False,
    # What the last sync saw, to tell local from remote changes
    "history_repair": 0,
    "remote_downloads_ready": False,
    "remote_downloads_seen": False,
    "mirror_downloads_ready": False,
    "queue_sync_base": "",
    "queue_sync_local": "",
    "queue_sync_changed": 0,
    "favorites_sync_base": [],
    "favorites_sync_local": [],
    # Discovery and search
    "search_provider": "itunes",
    "discover_country": "",
    "discover_hidden": False,
    # Backup
    "auto_db_export": False,
    "auto_db_export_last": 0,
    # Parental control
    "parental_enabled": False,
    "parental_require_subscribe": True,
    "parental_password_hash": "",
    # Statistics
    "stats_include_marked": False,
    "stats_reset_ts": 0,
}


_MISSING = object()

# Settings no page shows. Everything else counts as a change of what the pages show (see Settings.revision).
_NOT_SHOWN = ("last_page", "window_width", "window_height", "window_maximized")


def _atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Settings:
    """JSON-backed settings with change callbacks.

    Writes are immediate and atomic; the file is tiny, so there is no need to
    debounce. Listeners are called synchronously with (key, value).
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._values = {}
        self._on_disk = {}  # the file as last read or written, to tell what another process changed since
        self._listeners = []
        self.revision = 0  # grows with every change a page might show
        self.load()

    def _read(self):
        """The file's settings, or None when there is no readable file."""
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def load(self):
        with self._lock:
            self._values = self._read() or {}
            self._on_disk = copy.deepcopy(self._values)

    def get(self, key):
        with self._lock:
            if key in self._values:
                return copy.deepcopy(self._values[key])
            return copy.deepcopy(DEFAULTS.get(key))

    def __getitem__(self, key):
        return self.get(key)

    def set(self, key, value):
        with self._lock:
            if self._values.get(key, DEFAULTS.get(key)) == value and key in self._values:
                return
            self._values[key] = copy.deepcopy(value)
            self._save()
            if key not in _NOT_SHOWN:
                self.revision += 1
        for listener in list(self._listeners):
            listener(key, value)

    def update(self, values):
        with self._lock:
            self._values.update(copy.deepcopy(values))
            self._save()
            self.revision += 1
        for key, value in values.items():
            for listener in list(self._listeners):
                listener(key, value)

    def reset(self, key):
        with self._lock:
            self._values.pop(key, None)
            self._save()
            self.revision += 1
        for listener in list(self._listeners):
            listener(key, DEFAULTS.get(key))

    def connect(self, listener):
        self._listeners.append(listener)
        return listener

    def disconnect(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)

    def snapshot(self):
        with self._lock:
            merged = copy.deepcopy(DEFAULTS)
            merged.update(copy.deepcopy(self._values))
            return merged

    def _save(self):
        # The app and the background refresh can both run: keep what the other one wrote since (sync
        # bookmarks, for one) and lay over it only what changed here.
        current = self._read()
        if current is not None and current != self._on_disk:
            merged = current
            for key in set(self._values) | set(self._on_disk):
                if self._values.get(key, _MISSING) != self._on_disk.get(key, _MISSING):
                    if key in self._values:
                        merged[key] = self._values[key]
                    else:
                        merged.pop(key, None)
            self._values = merged
        _atomic_write(self.path, json.dumps(self._values, indent=2, sort_keys=True) + "\n")
        self._on_disk = copy.deepcopy(self._values)


class Credentials:
    """Secrets, stored with mode 0600 apart from the settings, in sections: the sync login ("sync"), the proxy
    password ("proxy") and the Podcast Index API key ("podcastindex")."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def load(self):
        with self._lock:
            return self._read()

    def get(self, section):
        value = self.load().get(section)
        return value if isinstance(value, dict) else {}

    def set(self, section, value):
        """Replace one section; an empty value removes it, and the file goes with the last one."""
        with self._lock:
            data = self._read()
            if value:
                data[section] = value
            else:
                data.pop(section, None)
            if data:
                _atomic_write(self.path, json.dumps(data, indent=2) + "\n", mode=0o600)
            else:
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass

    def save(self, data):
        with self._lock:
            _atomic_write(self.path, json.dumps(data, indent=2) + "\n", mode=0o600)


def move_secrets(store, secrets):
    """Up to 0.1.0 the proxy password and the Podcast Index key were kept in settings.json, which ends up in
    dotfile repositories and backups more easily than credentials.json. Moves them there."""
    proxy = store.get("proxy") or {}
    key, secret = store.get("podcastindex_key"), store.get("podcastindex_secret")
    if proxy.get("password"):
        secrets.set("proxy", {"password": proxy["password"]})
    if key or secret:
        secrets.set("podcastindex", {"key": key or "", "secret": secret or ""})
    # Taken out of the settings only once they are safe in the other file.
    if "password" in proxy:
        store.set("proxy", {name: value for name, value in proxy.items() if name != "password"})
    for name in ("podcastindex_key", "podcastindex_secret"):
        if store.get(name) is not None:
            store.reset(name)


_settings = None
_credentials = None


def settings():
    global _settings
    if _settings is None:
        _settings = Settings(paths.settings_file)
    return _settings


def credentials():
    global _credentials
    if _credentials is None:
        _credentials = Credentials(paths.credentials_file)
    return _credentials


def instance_lock(role, stop_refresh=False):
    """The lock that keeps the app and the background refresh (role "refresh") from running at the same time.
    Returns the open lock file, held until it is closed or the process ends, or None when another process has
    it. With stop_refresh (the app), a background refresh that has it is asked to stop (SIGTERM)."""
    import fcntl
    import signal

    paths.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(paths.runtime / "instance.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().split()
        handle.close()
        if stop_refresh and len(holder) == 2 and holder[0] == "refresh" and holder[1].isdigit():
            try:
                os.kill(int(holder[1]), signal.SIGTERM)
            except OSError:
                pass
        return None
    handle.truncate(0)
    handle.write(f"{role} {os.getpid()}\n")
    handle.flush()
    return handle


def downloads_dir():
    custom = settings().get("downloads_dir")
    return Path(custom).expanduser() if custom else paths.default_downloads
