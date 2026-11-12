"""Filesystem locations and persistent settings.

Settings mirror AntennaPod's preferences. They live in a small JSON file so
they survive a database import and can be read without opening the database.
Secrets (the sync login, the proxy password, the Podcast Index key) are kept
in a separate file with mode 0600.
"""

import contextlib
import copy
import errno
import json
import os
import secrets
import shutil
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
    # Folders the user chose under Add local folder on this computer (see trusted_folders).
    "local_folders": [],
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
            os.fchmod(handle.fileno(), mode)
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


def own_downloads_dir():
    """Whether downloads go to omaPOD's own folder in its data folder, which holds nothing but downloads. A folder
    the user chose may hold other files as well."""
    return downloads_dir() == paths.default_downloads


def in_folder(path, folder):
    """Whether the file `path` lies somewhere in `folder`, judged by the folder it is in, with symbolic links
    resolved. The file itself may be a link: deleting, moving or trashing it affects only the link."""
    if not isinstance(path, str) or not path or not folder or os.path.basename(path) in ("", ".", ".."):
        return False
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    root = os.path.realpath(folder)
    return parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)


def is_download(path):
    """Whether omaPOD may delete or move `path` as one of its downloads: a file in a podcast's folder in the
    download folder (<download folder>/<podcast>/<file>, where DownloadManager saves them). The database names
    the files, and an imported backup could name any file of the user."""
    if not in_folder(path, downloads_dir()) or os.path.isdir(path):
        return False
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    return os.path.dirname(parent) == os.path.realpath(downloads_dir())


def trusted_folders():
    """The folders the user chose under Add local folder on this computer, as their podcasts' addresses name them
    (local:<folder>). Only these are read as podcasts, and only their files may go to the trash. The list is kept
    in the settings, which a database import does not replace: an imported library brings podcasts of folders,
    never the permission to use them, so the folder has to be chosen here first."""
    value = settings().get("local_folders")
    return [folder for folder in value if isinstance(folder, str) and folder] if isinstance(value, list) else []


def trust_folder(folder, trusted=True):
    """Grants (when the user chose the folder) or withdraws (when its podcast goes) the permission above."""
    current = trusted_folders()
    if trusted and folder not in current:
        settings().set("local_folders", current + [folder])
    elif not trusted and folder in current:
        settings().set("local_folders", [entry for entry in current if entry != folder])


def chosen_folder(feed_url):
    """The folder of a podcast made from an added folder (feed address local:<folder>) if the user chose that
    folder on this computer, else ""."""
    from .models import LOCAL_PREFIX

    if isinstance(feed_url, str) and feed_url.startswith(LOCAL_PREFIX):
        folder = feed_url[len(LOCAL_PREFIX):]
        if folder in trusted_folders():
            return folder
    return ""


def in_chosen_folder(path):
    """Whether `path` lies in one of the folders the user chose (cover pictures of added folders)."""
    return any(in_folder(path, folder) for folder in trusted_folders())


def owns_file(path, feed_url):
    """Whether the file an episode names is one omaPOD may delete, move or trash: a download in the download
    folder, or for a podcast made from a folder the user chose here (see trusted_folders), an audio or video file
    in that folder. The database names the files and, for added folders, the folder too; an imported backup could
    name any of the user's files or folders."""
    from .models import AUDIO_EXTENSIONS, LOCAL_PREFIX, VIDEO_EXTENSIONS

    if isinstance(feed_url, str) and feed_url.startswith(LOCAL_PREFIX):
        folder = chosen_folder(feed_url)
        return (bool(folder) and in_folder(path, folder)
                and os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS + VIDEO_EXTENSIONS)
    return is_download(path)


# Folders other people may be able to write
#
# An export folder, a download folder the user chose and the folder downloads move to can be shared ones, which
# another account may write too. Whoever can write a folder can put a symbolic link under any name in it, at any
# moment: a file that omaPOD opens, truncates, renames or deletes *by its name* there may by then be a link to one
# of the user's other files. So in these folders the folder is opened once and everything else happens relative to
# that open folder and through files that are already open, without following links.

_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
COPY_CHUNK = 1024 * 1024


def _new_file(folder_fd, mode, prefix=".omapod-", suffix=".tmp"):
    """A new, empty file under a random name in an open folder: (name, descriptor). O_EXCL never opens what is
    there already, be it a file or a link."""
    for _attempt in range(100):
        name = f"{prefix}{secrets.token_hex(8)}{suffix}"
        try:
            return name, os.open(name, _NEW_FILE, mode, dir_fd=folder_fd)
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), prefix + "*" + suffix)


def others_can_change(path):
    """Whether an account other than the user's (root aside) can change which file `path` leads to. That is so
    when a folder on the way to it belongs to somebody else or can be written by its group or by everyone (a
    sticky folder like /tmp aside, where everybody can rename only what is theirs): such an account can rename
    what lies in that folder and put a link to another place under the name, at any moment, so that a check of
    where the path leads means nothing a moment later. The way is walked as the system walks it, through every
    folder a symbolic link on it leads through. What cannot be examined counts as changeable. Where this is
    False, nobody else can interfere, and the file may be handled by its path."""
    me = os.getuid()

    def changeable(folder):
        info = os.lstat(folder)
        return (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, me)
                or bool(info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX))

    parts = os.path.dirname(os.path.abspath(path)).split(os.sep)
    current, links = os.sep, 0
    try:
        if changeable(current):
            return True
        while parts:
            part = parts.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                current = os.path.dirname(current)  # examined on the way down
                continue
            step = os.path.join(current, part)
            info = os.lstat(step)
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > 40 or info.st_uid not in (0, me):  # in a sticky folder, its owner can replace it
                    return True
                target = os.readlink(step)
                if target.startswith(os.sep):
                    current = os.sep
                parts = target.split(os.sep) + parts
            elif changeable(step):
                return True
            else:
                current = step
    except OSError:
        return True
    return False


@contextlib.contextmanager
def private_file(path):
    """For a file the user exports: yields a new file, open for writing bytes, that only the owner can read from
    the start (0600) and that replaces `path` once it is complete. Whoever had an old file at `path` open keeps
    only the old content.

    Everything is written through this one open file, which is never opened again by a name: in a folder another
    account can write, a link could wait under the name by then, and the export would go through it into one of
    the user's other files, or into a file the other account can read. The folder is opened once; the file is
    created, renamed and (when writing fails) removed relative to it."""
    folder, name = os.path.split(os.path.abspath(path))
    folder_fd = os.open(folder, _FOLDER)
    try:
        tmp, fd = _new_file(folder_fd, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                yield handle
            os.replace(tmp, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=folder_fd)
            raise
    finally:
        os.close(folder_fd)


def copy_regular_file(path, target, limit):
    """Copies the file the user chose (`path`) into the open file `target`, at most `limit` bytes. Only a regular
    file: a pipe or a device under that name would block or never end, and the folder may be anyone's. Raises
    ValueError with a text for the user otherwise."""
    from .i18n import _

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)  # O_NONBLOCK: a pipe would wait for a writer
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(_("This is not a regular file."))
        copied = 0
        while info.st_size <= limit:
            chunk = source.read(COPY_CHUNK)
            if not chunk:
                return
            copied += len(chunk)
            if copied > limit:  # more than its size said: it grew while it was read
                break
            target.write(chunk)
        raise ValueError(_("This file is too large."))


def open_podcast_folder(name, create=False, root=None):
    """A podcast's folder in the download folder (<download folder>/<name>), opened: a descriptor the caller
    closes. Files in it are then created, renamed and deleted relative to the descriptor. The podcast's folder
    must be a real folder, not a symbolic link (NotADirectoryError): through a link, downloads would land in, and
    be deleted from, wherever it points. The download folder itself is taken as the user named it.
    `root`: another download folder than the current one (the one downloads move to)."""
    if not name or name in (".", "..") or os.sep in name:
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), name)
    root = str(downloads_dir() if root is None else root)
    if create:
        os.makedirs(root, exist_ok=True)
    root_fd = os.open(root, _FOLDER)
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, dir_fd=root_fd)
        return os.open(name, _FOLDER | os.O_NOFOLLOW, dir_fd=root_fd)
    finally:
        os.close(root_fd)


@contextlib.contextmanager
def opened_download(path):
    """To delete or move the download `path` names: yields (descriptor of its podcast's folder, file name), or
    None when omaPOD may not touch it (is_download). The folder is opened from the download folder down, without
    following a link, and the file is addressed relative to it: whatever another account does in the download
    folder between the check and the deletion, nothing outside <download folder>/<podcast>/ is reached."""
    if not is_download(path):
        yield None
        return
    parent, name = os.path.split(os.path.abspath(path))
    try:
        folder_fd = open_podcast_folder(os.path.basename(parent))
    except OSError:
        yield None
        return
    try:
        yield folder_fd, name
    finally:
        os.close(folder_fd)


def remove_download(path):
    """Deletes the download `path` names. False when it is not a download (is_download) and stays where it is."""
    with opened_download(path) as download:
        if download is None:
            return False
        folder_fd, name = download
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=folder_fd)
        return True


def name_taken(folder_fd, name):
    """Whether anything has this name in the open folder, a link that leads nowhere included."""
    try:
        os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def open_partial(folder_fd, name):
    """The file a download is written to until it is complete (<podcast's folder>/.<episode>.part), opened for
    reading and writing bytes; created if it is not there, kept if it is (the download resumes).

    Its name can be guessed, so somebody else could have put something there first. It is opened without
    following a link and has to be a plain file with this one name. Anything else (a link, a pipe, a second name
    of another file, a file omaPOD may not write) is not written to; it loses the name, which is omaPOD's, and a
    new file takes it."""
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o666, dir_fd=folder_fd)
    except OSError as error:
        if error.errno not in (errno.ELOOP, errno.ENXIO, errno.EACCES):  # a link, a socket, somebody else's file
            raise
    else:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            return os.fdopen(fd, "r+b")
        os.close(fd)
    os.unlink(name, dir_fd=folder_fd)
    return os.fdopen(os.open(name, flags | os.O_EXCL, 0o666, dir_fd=folder_fd), "r+b")


def is_same_file(folder_fd, name, handle):
    """Whether `name` in the open folder is (still) the open file `handle`."""
    try:
        there, ours = os.stat(name, dir_fd=folder_fd, follow_symlinks=False), os.fstat(handle.fileno())
    except OSError:
        return False
    return (there.st_dev, there.st_ino) == (ours.st_dev, ours.st_ino)


def move_into(source_fd, name, target_fd):
    """Moves the file `name` from one open folder into another and returns the name it has there: its own, or
    "name (2).ext" and so on when that is taken. It never replaces what is there and never follows a link: the
    move is a second name for the file in the new folder (which fails if the name is taken, whatever has it) and
    the loss of the old one. Across file systems, and on those without such second names, the content is copied
    into a file created anew (O_EXCL). Only regular files move."""
    if not stat.S_ISREG(os.stat(name, dir_fd=source_fd, follow_symlinks=False).st_mode):
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), name)
    stem, suffix = os.path.splitext(name)
    for counter in range(1, 10000):
        new_name = name if counter == 1 else f"{stem} ({counter}){suffix}"
        try:
            try:
                os.link(name, new_name, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)
            except FileExistsError:
                continue
            except OSError:  # another file system, or one without hard links
                _copy_into(source_fd, name, target_fd, new_name)
        except FileExistsError:
            continue
        try:
            os.unlink(name, dir_fd=source_fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(new_name, dir_fd=target_fd)  # not moved after all: it stays where it was, once
            raise
        return new_name
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), name)


def _copy_into(source_fd, name, target_fd, new_name):
    with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_fd), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), name)
        fd = os.open(new_name, _NEW_FILE, 0o666, dir_fd=target_fd)
        try:
            with os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target, COPY_CHUNK)
                target.flush()
                os.utime(target.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(new_name, dir_fd=target_fd)
            raise
