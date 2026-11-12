"""SQLite storage.

One connection per thread (WAL mode), explicit transactions through
Database.tx(). All SQL lives here; the rules (what marking an episode as
played implies, auto-download, cleanup) live in core/library.py.
"""

import glob
import json
import os
import random
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager, suppress

from .models import FEED_PREVIEW, FEED_SUBSCRIBED, NEW, PLAYED, UNPLAYED, Episode, Feed

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    custom_title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    feed_type TEXT NOT NULL DEFAULT '',
    podcast_guid TEXT NOT NULL DEFAULT '',
    funding TEXT NOT NULL DEFAULT '[]',
    next_page_url TEXT NOT NULL DEFAULT '',
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    last_refresh INTEGER NOT NULL DEFAULT 0,
    last_refresh_attempt INTEGER NOT NULL DEFAULT 0,
    refresh_error TEXT NOT NULL DEFAULT '',
    state INTEGER NOT NULL DEFAULT 1,
    added INTEGER NOT NULL DEFAULT 0,
    keep_updated INTEGER NOT NULL DEFAULT 1,
    auto_download INTEGER NOT NULL DEFAULT -1,
    auto_delete INTEGER NOT NULL DEFAULT -1,
    new_episodes_action INTEGER NOT NULL DEFAULT -1,
    speed REAL NOT NULL DEFAULT -1,
    skip_silence INTEGER NOT NULL DEFAULT -1,
    skip_intro INTEGER NOT NULL DEFAULT 0,
    skip_ending INTEGER NOT NULL DEFAULT 0,
    volume_adaptation INTEGER NOT NULL DEFAULT 0,
    notify INTEGER NOT NULL DEFAULT 0,
    include_terms TEXT NOT NULL DEFAULT '',
    exclude_terms TEXT NOT NULL DEFAULT '',
    min_duration INTEGER NOT NULL DEFAULT -1,
    username TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    sort_order TEXT NOT NULL DEFAULT '',
    episode_filter TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    pub_date INTEGER NOT NULL DEFAULT 0,
    image_url TEXT NOT NULL DEFAULT '',
    media_url TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    media_size INTEGER NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    played_duration INTEGER NOT NULL DEFAULT 0,
    last_played INTEGER NOT NULL DEFAULT 0,
    -- The history's time, like AntennaPod's 'last played time (history)': when the episode was last heard here or
    -- on another device, finished or marked as played. 0: not in the history.
    completed_at INTEGER NOT NULL DEFAULT 0,
    state INTEGER NOT NULL DEFAULT 0,
    favorite INTEGER NOT NULL DEFAULT 0,
    file_path TEXT NOT NULL DEFAULT '',
    downloaded_at INTEGER NOT NULL DEFAULT 0,
    download_failures INTEGER NOT NULL DEFAULT 0,
    auto_download INTEGER NOT NULL DEFAULT 1,
    season INTEGER NOT NULL DEFAULT 0,
    episode_number INTEGER NOT NULL DEFAULT 0,
    episode_type TEXT NOT NULL DEFAULT '',
    chapters_url TEXT NOT NULL DEFAULT '',
    chapters_type TEXT NOT NULL DEFAULT '',
    transcript_url TEXT NOT NULL DEFAULT '',
    transcript_type TEXT NOT NULL DEFAULT '',
    psc_chapters TEXT NOT NULL DEFAULT '[]',
    social_url TEXT NOT NULL DEFAULT '',
    has_chapters INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS episodes_feed ON episodes(feed_id, pub_date);
CREATE INDEX IF NOT EXISTS episodes_state ON episodes(state);
CREATE INDEX IF NOT EXISTS episodes_last_played ON episodes(last_played);
CREATE INDEX IF NOT EXISTS episodes_downloaded ON episodes(downloaded_at);
CREATE INDEX IF NOT EXISTS episodes_media_url ON episodes(media_url);
CREATE INDEX IF NOT EXISTS episodes_guid ON episodes(guid);
CREATE INDEX IF NOT EXISTS episodes_favorite ON episodes(favorite);
CREATE INDEX IF NOT EXISTS episodes_pub ON episodes(pub_date);
CREATE INDEX IF NOT EXISTS episodes_completed ON episodes(completed_at);
-- Holds what the numbers per podcast count, so that they stay in the index: the rows of the table are long with
-- their show notes, and every page shows such numbers.
CREATE INDEX IF NOT EXISTS episodes_counts ON episodes(feed_id, state, downloaded_at, pub_date, position, duration,
    media_size, played_duration, completed_at, media_url, file_path);

CREATE TABLE IF NOT EXISTS queue (
    episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
    pos REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS queue_pos ON queue(pos);

CREATE TABLE IF NOT EXISTS chapters (
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS chapters_episode ON chapters(episode_id, start_ms);

CREATE TABLE IF NOT EXISTS download_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS download_log_ts ON download_log(ts);

CREATE TABLE IF NOT EXISTS listen_log (
    day TEXT NOT NULL,
    episode_id INTEGER NOT NULL,
    feed_id INTEGER NOT NULL,
    ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, episode_id)
);
CREATE INDEX IF NOT EXISTS listen_log_feed ON listen_log(feed_id);

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created INTEGER NOT NULL
);

-- The newest episode action time (epoch seconds) handled per episode ('url:…', 'guid:…', 'download:…'), applied
-- from the server or uploaded from here. Sync asks for actions again a while back; these are not applied twice.
CREATE TABLE IF NOT EXISTS sync_seen (
    key TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);

-- What other devices downloaded: their newest download or delete action per episode (media address as the server
-- knows it), for the Downloads page.
CREATE TABLE IF NOT EXISTS remote_downloads (
    episode TEXT PRIMARY KEY,
    guid TEXT NOT NULL DEFAULT '',
    downloaded INTEGER NOT NULL,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

EPISODE_LIST_COLUMNS = (
    "e.id, e.feed_id, e.guid, e.title, e.link, e.pub_date, e.image_url, e.media_url, e.media_type, "
    "e.media_size, e.duration, e.position, e.played_duration, e.last_played, e.completed_at, e.state, "
    "e.favorite, e.file_path, e.downloaded_at, e.download_failures, e.auto_download, e.season, "
    "e.episode_number, e.episode_type, e.chapters_url, e.chapters_type, e.transcript_url, "
    "e.transcript_type, e.social_url, e.has_chapters"
)
EPISODE_JOIN = (
    "COALESCE(NULLIF(f.custom_title, ''), f.title, f.url) AS feed_title, f.image_url AS feed_image, "
    "f.author AS feed_author, f.url AS feed_url, f.state AS feed_state, q.pos AS queue_pos "
    "FROM episodes e JOIN feeds f ON f.id = e.feed_id LEFT JOIN queue q ON q.episode_id = e.id"
)
SELECT_EPISODES = f"SELECT {EPISODE_LIST_COLUMNS}, {EPISODE_JOIN}"
SELECT_EPISODES_FULL = f"SELECT {EPISODE_LIST_COLUMNS}, e.description, e.psc_chapters, {EPISODE_JOIN}"

# Filter tokens (AntennaPod's FeedItemFilter) -> SQL condition.
FILTERS = {
    "new": "e.state = -1",
    "not_new": "e.state != -1",
    "unplayed": "e.state != 1",
    "played": "e.state = 1",
    "paused": "e.position > 0 AND e.state != 1",
    "not_paused": "e.position = 0",
    "downloaded": "e.downloaded_at > 0",
    "not_downloaded": "e.downloaded_at = 0",
    "queued": "q.episode_id IS NOT NULL",
    "not_queued": "q.episode_id IS NULL",
    "favorite": "e.favorite = 1",
    "not_favorite": "e.favorite = 0",
    "has_media": "e.media_url != ''",
    "no_media": "e.media_url = '' AND e.file_path = ''",
    "subscribed": "f.state = 1",
}

# Sort keys (AntennaPod's SortOrder) -> SQL ORDER BY. Random/smart shuffle are done in Python.
SORTS = {
    "date_desc": "e.pub_date DESC, e.id DESC",
    "date_asc": "e.pub_date ASC, e.id ASC",
    "title_asc": "e.title COLLATE NOCASE ASC",
    "title_desc": "e.title COLLATE NOCASE DESC",
    "duration_asc": "e.duration ASC",
    "duration_desc": "e.duration DESC",
    "size_asc": "e.media_size ASC",
    "size_desc": "e.media_size DESC",
    "feed_asc": "feed_title COLLATE NOCASE ASC, e.pub_date DESC",
    "feed_desc": "feed_title COLLATE NOCASE DESC, e.pub_date DESC",
    "filename_asc": "e.file_path COLLATE NOCASE ASC, e.media_url ASC",
    "filename_desc": "e.file_path COLLATE NOCASE DESC, e.media_url DESC",
    "played_desc": "e.last_played DESC",
    "downloaded_desc": "e.downloaded_at DESC",
    "completed_desc": "e.completed_at DESC, e.pub_date DESC",
    "queue": "q.pos ASC",
}

FEED_COLUMNS = [
    "url", "title", "custom_title", "author", "description", "link", "image_url", "language",
    "feed_type", "podcast_guid", "funding", "next_page_url", "etag", "last_modified", "last_refresh",
    "last_refresh_attempt", "refresh_error", "state", "added", "keep_updated", "auto_download",
    "auto_delete", "new_episodes_action", "speed", "skip_silence", "skip_intro", "skip_ending",
    "volume_adaptation", "notify", "include_terms", "exclude_terms", "min_duration", "username",
    "password", "sort_order", "episode_filter", "tags",
]

EPISODE_COLUMNS = [
    "feed_id", "guid", "title", "link", "description", "pub_date", "image_url", "media_url",
    "media_type", "media_size", "duration", "position", "played_duration", "last_played",
    "completed_at", "state", "favorite", "file_path", "downloaded_at", "download_failures",
    "auto_download", "season", "episode_number", "episode_type", "chapters_url", "chapters_type",
    "transcript_url", "transcript_type", "psc_chapters", "social_url", "has_chapters",
]


def _encode(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def smart_shuffle(episodes, newest_first=False):
    """AntennaPod's smart shuffle: interleave podcasts, keep each podcast in date order."""
    by_feed = {}
    for episode in episodes:
        by_feed.setdefault(episode.feed_id, []).append(episode)
    for items in by_feed.values():
        items.sort(key=lambda e: e.pub_date, reverse=newest_first)
    order = sorted(by_feed.values(), key=lambda items: items[0].pub_date, reverse=newest_first)
    result = []
    while order:
        next_round = []
        for items in order:
            result.append(items.pop(0))
            if items:
                next_round.append(items)
        order = next_round
    return result


def make_private(path):
    """Owner only (0600) for a database and its journal files: they hold the listening history and the passwords
    of protected podcasts. SQLite gives new journal files the database file's permissions."""
    path = str(path)
    for name in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(name, 0o600)
        except FileNotFoundError:
            pass


def discard_stale(folder, prefix):
    """Removes omaPOD's working copies (.snapshot-*, .import-*) that a killed run left in its data folder."""
    for stale in glob.glob(os.path.join(glob.escape(str(folder)), prefix + "*")):
        with suppress(OSError):
            os.unlink(stale)


def write_snapshot(conn, handle, folder):
    """Writes a consistent copy of the database behind `conn` (with what is still in its write-ahead log) into
    the open file `handle`: an export or a backup.

    SQLite writes only to a file it opens by name, and the folder an export goes to may be one that another
    account can write, where a symbolic link could wait under that name. So SQLite makes the copy in `folder`,
    which has to be omaPOD's own data folder (0700: nobody else can enter it, the library itself lives there),
    and the bytes go from there into the file that is already open (config.private_file)."""
    discard_stale(folder, ".snapshot-")
    fd, staging = tempfile.mkstemp(dir=folder, prefix=".snapshot-", suffix=".db")
    try:
        with os.fdopen(fd, "rb") as source:  # kept open: the copy is read back through this descriptor
            copy = sqlite3.connect(staging)
            try:
                conn.backup(copy)
            finally:
                copy.close()
            # The copy took over the library's write-ahead mode. As a plain single file it can be read wherever it
            # ends up (a read-only medium, say) without -wal and -shm files next to it. A new connection, which
            # reads the mode from the file; if that fails, the copy is complete all the same.
            with suppress(sqlite3.Error):
                plain = sqlite3.connect(staging)
                try:
                    plain.execute("PRAGMA journal_mode=DELETE")
                finally:
                    plain.close()
            shutil.copyfileobj(source, handle, 1024 * 1024)
    finally:
        for leftover in (staging, staging + "-journal", staging + "-wal", staging + "-shm"):
            with suppress(OSError):
                os.unlink(leftover)


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        if not os.path.exists(self.path):
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))  # an empty file is a new database
        make_private(self.path)
        self.migrate()

    # -- connections ---------------------------------------------------

    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def write_snapshot(self, handle):
        """A copy of the library into an open file (see write_snapshot above), made next to the library itself."""
        write_snapshot(self.conn(), handle, os.path.dirname(self.path))

    @contextmanager
    def tx(self):
        with self._write_lock:
            conn = self.conn()
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def revision(self):
        """Differs after every commit: data_version counts those of other connections (worker threads, the
        background refresh), total_changes this one's."""
        conn = self.conn()
        return conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes

    def query(self, sql, params=()):
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql, params=(), default=None):
        row = self.conn().execute(sql, params).fetchone()
        return row[0] if row is not None and row[0] is not None else default

    def execute(self, sql, params=()):
        with self.tx() as conn:
            return conn.execute(sql, params)

    def migrate(self):
        with self._write_lock:
            conn = self.conn()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"The database was written by a newer omaPOD (schema {version}). Update omaPOD first."
                )
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- meta ------------------------------------------------------------

    def get_meta(self, key, default=None):
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))

    # -- feeds -----------------------------------------------------------

    def feed_counts_sql(self):
        return (
            "SELECT f.*, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND e.state = -1) AS count_new, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND e.state != 1 AND (e.media_url != '' OR e.file_path != '')) AS count_unplayed, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND e.downloaded_at > 0) AS count_downloaded, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND e.downloaded_at > 0 AND e.state != 1) AS count_downloaded_unplayed, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id) AS count_total, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id AND e.state = 1) AS count_played, "
            "(SELECT MAX(e.pub_date) FROM episodes e WHERE e.feed_id = f.id) AS last_pub "
            "FROM feeds f"
        )

    def get_feeds(self, states=(FEED_SUBSCRIBED,), with_counts=True):
        placeholders = ",".join("?" * len(states))
        base = self.feed_counts_sql() if with_counts else "SELECT f.* FROM feeds f"
        rows = self.query(f"{base} WHERE f.state IN ({placeholders}) ORDER BY "
                          "COALESCE(NULLIF(f.custom_title, ''), f.title) COLLATE NOCASE", tuple(states))
        return [Feed.from_row(row) for row in rows]

    def get_feed(self, feed_id, with_counts=False):
        base = self.feed_counts_sql() if with_counts else "SELECT f.* FROM feeds f"
        row = self.query_one(f"{base} WHERE f.id = ?", (feed_id,))
        return Feed.from_row(row) if row else None

    def get_feed_by_url(self, url):
        row = self.query_one("SELECT * FROM feeds WHERE url = ?", (url,))
        return Feed.from_row(row) if row else None

    def insert_feed(self, conn, values):
        cols = [c for c in FEED_COLUMNS if c in values]
        sql = f"INSERT INTO feeds ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        return conn.execute(sql, [_encode(values[c]) for c in cols]).lastrowid

    def update_feed(self, feed_id, conn=None, **values):
        cols = [c for c in FEED_COLUMNS if c in values]
        if not cols:
            return
        sql = f"UPDATE feeds SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?"
        params = [_encode(values[c]) for c in cols] + [feed_id]
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.execute(sql, params)

    def delete_feed(self, feed_id):
        with self.tx() as conn:
            conn.execute("DELETE FROM listen_log WHERE feed_id = ?", (feed_id,))
            conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))

    def all_tags(self):
        tags = set()
        for row in self.query("SELECT tags FROM feeds WHERE state != 0"):
            try:
                tags.update(json.loads(row["tags"] or "[]"))
            except ValueError:
                pass
        return sorted(tags, key=str.casefold)

    # -- episodes --------------------------------------------------------

    def _where(self, filters, feed_id=None, extra=None, params=None):
        clauses = []
        params = params if params is not None else []
        for token in filters or ():
            if token in FILTERS:
                clauses.append(FILTERS[token])
        if feed_id is not None:
            clauses.append("e.feed_id = ?")
            params.append(feed_id)
        if extra:
            clauses.append(extra)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def get_episodes(self, filters=(), sort="date_desc", feed_id=None, limit=None, offset=0, extra=None,
                     params=None, full=False):
        where, params = self._where(filters, feed_id, extra, list(params or []))
        sql = (SELECT_EPISODES_FULL if full else SELECT_EPISODES) + where
        python_sort = sort in ("random", "smart_shuffle_asc", "smart_shuffle_desc")
        if not python_sort:
            sql += " ORDER BY " + SORTS.get(sort, SORTS["date_desc"])
            if limit:
                sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        episodes = [Episode.from_row(row) for row in self.query(sql, params)]
        if sort == "random":
            random.shuffle(episodes)
        elif sort.startswith("smart_shuffle"):
            episodes = smart_shuffle(episodes, newest_first=sort.endswith("desc"))
        if python_sort and limit:
            episodes = episodes[offset:offset + limit]
        return episodes

    def count_episodes(self, filters=(), feed_id=None, extra=None, params=None):
        where, params = self._where(filters, feed_id, extra, list(params or []))
        return self.scalar("SELECT COUNT(*) FROM episodes e JOIN feeds f ON f.id = e.feed_id "
                           "LEFT JOIN queue q ON q.episode_id = e.id" + where, params, 0)

    def get_episode(self, episode_id, full=True):
        sql = (SELECT_EPISODES_FULL if full else SELECT_EPISODES) + " WHERE e.id = ?"
        row = self.query_one(sql, (episode_id,))
        return Episode.from_row(row) if row else None

    def get_episodes_by_ids(self, ids, full=False):
        ids = [int(i) for i in ids]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.query((SELECT_EPISODES_FULL if full else SELECT_EPISODES) + f" WHERE e.id IN ({placeholders})", ids)
        by_id = {row["id"]: Episode.from_row(row) for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    def feed_episode_rows(self, conn, feed_id):
        return conn.execute(
            "SELECT id, guid, title, media_url, pub_date, state, file_path, downloaded_at, duration, media_size, "
            "image_url, link, description, media_type, chapters_url, chapters_type, transcript_url, transcript_type, "
            "psc_chapters, season, episode_number, episode_type, social_url FROM episodes WHERE feed_id = ?",
            (feed_id,),
        ).fetchall()

    def insert_episode(self, conn, values):
        cols = [c for c in EPISODE_COLUMNS if c in values]
        sql = f"INSERT INTO episodes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        return conn.execute(sql, [_encode(values[c]) for c in cols]).lastrowid

    def update_episode(self, episode_id, conn=None, **values):
        cols = [c for c in EPISODE_COLUMNS if c in values]
        if not cols:
            return
        sql = f"UPDATE episodes SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?"
        params = [_encode(values[c]) for c in cols] + [episode_id]
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.execute(sql, params)

    def update_episodes(self, ids, conn=None, **values):
        ids = [int(i) for i in ids]
        cols = [c for c in EPISODE_COLUMNS if c in values]
        if not ids or not cols:
            return
        placeholders = ",".join("?" * len(ids))
        sql = f"UPDATE episodes SET {', '.join(c + ' = ?' for c in cols)} WHERE id IN ({placeholders})"
        params = [_encode(values[c]) for c in cols] + ids
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.execute(sql, params)

    def find_episode_for_sync(self, guid, media_url, podcast_url=None):
        """The episode another device's action means. AntennaPod matches by guid, then by media URL, but plenty
        of podcasts use guids like "1" or "episode-1": a guid counts together with its podcast's address, then
        the media URL decides, and a guid alone only when no other episode has it."""
        guid = guid.strip() if isinstance(guid, str) and guid.strip() != "null" else ""
        order = " ORDER BY f.state = 1 DESC LIMIT 1"
        if guid and podcast_url:
            row = self.query_one(SELECT_EPISODES + " WHERE e.guid = ? AND f.url = ?" + order, (guid, podcast_url))
            if row:
                return Episode.from_row(row)
        if media_url:
            row = self.query_one(SELECT_EPISODES + " WHERE e.media_url = ?" + order, (media_url,))
            if row:
                return Episode.from_row(row)
        if guid:
            rows = self.query(SELECT_EPISODES + " WHERE e.guid = ? LIMIT 2", (guid,))
            if len(rows) == 1:
                return Episode.from_row(rows[0])
        return None

    def search_episodes(self, text, feed_id=None, limit=200):
        like = f"%{text}%"
        extra = "(e.title LIKE ? OR e.description LIKE ? OR f.title LIKE ? OR f.custom_title LIKE ? OR f.author LIKE ?)"
        return self.get_episodes(("subscribed",) if feed_id is None else (), "date_desc", feed_id=feed_id,
                                 limit=limit, extra=extra, params=[like] * 5)

    def search_feeds(self, text):
        like = f"%{text}%"
        rows = self.query(self.feed_counts_sql() + " WHERE f.state != 0 AND (f.title LIKE ? OR f.custom_title LIKE ? "
                          "OR f.author LIKE ? OR f.description LIKE ?) ORDER BY f.title COLLATE NOCASE",
                          (like, like, like, like))
        return [Feed.from_row(row) for row in rows]

    # -- queue -----------------------------------------------------------

    def queue_ids(self):
        return [row[0] for row in self.query("SELECT episode_id FROM queue ORDER BY pos")]

    def get_queue(self):
        return self.get_episodes(("queued",), sort="queue")

    def set_queue_order(self, conn, ids):
        conn.execute("DELETE FROM queue")
        conn.executemany("INSERT INTO queue(episode_id, pos) VALUES(?, ?)",
                         [(int(eid), float(index)) for index, eid in enumerate(ids)])

    # -- chapters --------------------------------------------------------

    def get_chapters(self, episode_id):
        return self.query("SELECT start_ms, title, url, image_url, source FROM chapters "
                          "WHERE episode_id = ? ORDER BY start_ms", (episode_id,))

    def set_chapters(self, episode_id, chapters, source):
        with self.tx() as conn:
            conn.execute("DELETE FROM chapters WHERE episode_id = ?", (episode_id,))
            conn.executemany(
                "INSERT INTO chapters(episode_id, start_ms, title, url, image_url, source) VALUES(?, ?, ?, ?, ?, ?)",
                [(episode_id, int(c["start"]), c.get("title") or "", c.get("url") or "",
                  c.get("image") or "", source) for c in chapters],
            )
            conn.execute("UPDATE episodes SET has_chapters = ? WHERE id = ?", (1 if chapters else 0, episode_id))

    # -- download log ------------------------------------------------------

    def add_log(self, kind, ref_id, title, url, success, reason="", detail=""):
        from .util import now_s

        with self.tx() as conn:
            conn.execute("INSERT INTO download_log(ts, kind, ref_id, title, url, success, reason, detail) "
                         "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                         (now_s(), kind, ref_id or 0, title or "", url or "", 1 if success else 0, reason, detail))
            conn.execute("DELETE FROM download_log WHERE id NOT IN (SELECT id FROM download_log ORDER BY ts DESC LIMIT 500)")

    def get_log(self, limit=200):
        return self.query("SELECT * FROM download_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def clear_log(self):
        self.execute("DELETE FROM download_log")

    # -- listening statistics ---------------------------------------------

    def add_listened(self, episode_id, feed_id, ms, day):
        if ms <= 0:
            return
        self.execute("INSERT INTO listen_log(day, episode_id, feed_id, ms) VALUES(?, ?, ?, ?) "
                     "ON CONFLICT(day, episode_id) DO UPDATE SET ms = ms + excluded.ms",
                     (day, episode_id, feed_id, int(ms)))

    # -- sync queue --------------------------------------------------------

    def sync_enqueue(self, kind, payload):
        from .util import now_s

        self.execute("INSERT INTO sync_queue(kind, payload, created) VALUES(?, ?, ?)",
                     (kind, json.dumps(payload, ensure_ascii=False), now_s()))

    def sync_pending(self, kind):
        rows = self.query("SELECT id, payload FROM sync_queue WHERE kind = ? ORDER BY id", (kind,))
        result = []
        for row in rows:
            try:
                result.append((row["id"], json.loads(row["payload"])))
            except ValueError:
                pass
        return result

    def sync_remove(self, ids):
        ids = list(ids)
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self.execute(f"DELETE FROM sync_queue WHERE id IN ({placeholders})", ids)

    def sync_clear(self):
        with self.tx() as conn:
            conn.execute("DELETE FROM sync_queue")
            conn.execute("DELETE FROM sync_seen")
            conn.execute("DELETE FROM remote_downloads")

    def remote_download_states(self, episodes):
        """{media address: (downloaded, time)} for episodes other devices downloaded or deleted."""
        episodes = list(dict.fromkeys(episodes))
        states = {}
        for start in range(0, len(episodes), 500):
            chunk = episodes[start:start + 500]
            rows = self.query(f"SELECT episode, downloaded, ts FROM remote_downloads WHERE episode IN "
                              f"({','.join('?' * len(chunk))})", chunk)
            states.update((row["episode"], (bool(row["downloaded"]), row["ts"])) for row in rows)
        return states

    def set_remote_downloads(self, rows):
        """rows: (media address, guid, downloaded, time); an older action never replaces a newer one."""
        with self.tx() as conn:
            conn.executemany(
                "INSERT INTO remote_downloads(episode, guid, downloaded, ts) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(episode) DO UPDATE SET guid = excluded.guid, downloaded = excluded.downloaded, "
                "ts = excluded.ts WHERE excluded.ts >= remote_downloads.ts",
                [(episode, guid or "", 1 if downloaded else 0, int(ts)) for episode, guid, downloaded, ts in rows])

    def remote_downloads(self):
        """(media address, guid) of what other devices have downloaded, most recent first."""
        return [(row["episode"], row["guid"]) for row in
                self.query("SELECT episode, guid FROM remote_downloads WHERE downloaded = 1 ORDER BY ts DESC")]

    def sync_seen(self, keys):
        keys = list(dict.fromkeys(keys))
        seen = {}
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            rows = self.query(f"SELECT key, ts FROM sync_seen WHERE key IN ({','.join('?' * len(chunk))})", chunk)
            seen.update((row["key"], row["ts"]) for row in rows)
        return seen

    def mark_sync_seen(self, pairs, conn=None):
        """pairs: (key, epoch seconds); keeps the newest time per key."""
        pairs = [(str(key), int(ts)) for key, ts in pairs if key and ts]
        if not pairs:
            return
        sql = "INSERT INTO sync_seen(key, ts) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET ts = MAX(ts, excluded.ts)"
        if conn is not None:
            conn.executemany(sql, pairs)
        else:
            with self.tx() as own:
                own.executemany(sql, pairs)


def state_after_marking(played):
    return PLAYED if played else UNPLAYED


__all__ = ["Database", "FEED_PREVIEW", "FEED_SUBSCRIBED", "NEW", "PLAYED", "UNPLAYED", "smart_shuffle"]
