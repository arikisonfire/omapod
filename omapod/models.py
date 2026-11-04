"""Plain data objects for podcasts (feeds) and episodes."""

import json
from dataclasses import dataclass, field, fields

# Feed.state
FEED_PREVIEW = 0
FEED_SUBSCRIBED = 1
FEED_ARCHIVED = 2

# Episode.state (same values as AntennaPod's FeedItem)
NEW = -1
UNPLAYED = 0
PLAYED = 1

# Feed.new_episodes_action (same codes as AntennaPod's NewEpisodesAction)
ACTION_GLOBAL = -1
ACTION_INBOX = 1
ACTION_NOTHING = 2
ACTION_QUEUE = 3

# Feed.volume_adaptation -> linear gain factor
VOLUME_ADAPTATIONS = [
    ("off", 1.0),
    ("light_reduction", 0.5),
    ("heavy_reduction", 0.2),
    ("light_boost", 1.6),
    ("medium_boost", 2.4),
    ("heavy_boost", 3.6),
]

LOCAL_PREFIX = "local:"

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".mka", ".wma")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi")


def _known_values(row, names):
    """The set columns of a database row that are fields of the model. Lists load thousands of rows at a time."""
    return {key: value for key, value in zip(row.keys(), row) if value is not None and key in names}


def _json_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except ValueError:
        return []


@dataclass
class Feed:
    id: int = 0
    url: str = ""
    title: str = ""
    custom_title: str = ""
    author: str = ""
    description: str = ""
    link: str = ""
    image_url: str = ""
    language: str = ""
    feed_type: str = ""
    podcast_guid: str = ""
    funding: list = field(default_factory=list)
    next_page_url: str = ""
    etag: str = ""
    last_modified: str = ""
    last_refresh: int = 0
    last_refresh_attempt: int = 0
    refresh_error: str = ""
    state: int = FEED_SUBSCRIBED
    added: int = 0
    keep_updated: int = 1
    auto_download: int = -1
    auto_delete: int = -1
    new_episodes_action: int = ACTION_GLOBAL
    speed: float = -1.0
    skip_silence: int = -1
    skip_intro: int = 0
    skip_ending: int = 0
    volume_adaptation: int = 0
    notify: int = 0
    include_terms: str = ""
    exclude_terms: str = ""
    min_duration: int = -1
    username: str = ""
    password: str = ""
    sort_order: str = ""
    episode_filter: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    # Aggregates, filled by list queries
    count_new: int = 0
    count_unplayed: int = 0
    count_downloaded: int = 0
    count_downloaded_unplayed: int = 0
    count_total: int = 0
    count_played: int = 0
    last_pub: int = 0

    @classmethod
    def from_row(cls, row):
        values = _known_values(row, FEED_FIELDS)
        for name in ("funding", "episode_filter", "tags"):
            values[name] = _json_list(values.get(name))
        return cls(**values)

    @property
    def display_title(self):
        return self.custom_title or self.title or self.url

    @property
    def is_local(self):
        return self.url.startswith(LOCAL_PREFIX)

    @property
    def local_path(self):
        return self.url[len(LOCAL_PREFIX):] if self.is_local else ""

    @property
    def is_subscribed(self):
        return self.state == FEED_SUBSCRIBED

    @property
    def is_archived(self):
        return self.state == FEED_ARCHIVED

    @property
    def is_preview(self):
        return self.state == FEED_PREVIEW


@dataclass
class Episode:
    id: int = 0
    feed_id: int = 0
    guid: str = ""
    title: str = ""
    link: str = ""
    description: str = ""
    pub_date: int = 0
    image_url: str = ""
    media_url: str = ""
    media_type: str = ""
    media_size: int = 0
    duration: int = 0
    position: int = 0
    played_duration: int = 0
    last_played: int = 0
    completed_at: int = 0
    state: int = UNPLAYED
    favorite: int = 0
    file_path: str = ""
    downloaded_at: int = 0
    download_failures: int = 0
    auto_download: int = 1
    season: int = 0
    episode_number: int = 0
    episode_type: str = ""
    chapters_url: str = ""
    chapters_type: str = ""
    transcript_url: str = ""
    transcript_type: str = ""
    psc_chapters: list = field(default_factory=list)
    social_url: str = ""
    has_chapters: int = 0
    # Joined columns
    feed_title: str = ""
    feed_image: str = ""
    feed_author: str = ""
    feed_url: str = ""
    feed_state: int = FEED_SUBSCRIBED
    queue_pos: float = None

    @classmethod
    def from_row(cls, row):
        values = _known_values(row, EPISODE_FIELDS)
        values["psc_chapters"] = _json_list(values.get("psc_chapters"))
        return cls(**values)

    @property
    def is_new(self):
        return self.state == NEW

    @property
    def is_played(self):
        return self.state == PLAYED

    @property
    def is_in_progress(self):
        return self.position > 0 and self.state != PLAYED

    @property
    def is_downloaded(self):
        return bool(self.file_path) and bool(self.downloaded_at)

    @property
    def has_media(self):
        return bool(self.media_url) or bool(self.file_path)

    @property
    def in_queue(self):
        return self.queue_pos is not None

    @property
    def is_favorite(self):
        return bool(self.favorite)

    @property
    def is_video(self):
        mime = (self.media_type or "").lower()
        if mime.startswith("video/"):
            return True
        url = (self.file_path or self.media_url or "").lower().split("?")[0]
        return url.endswith(VIDEO_EXTENSIONS) and not mime.startswith("audio/")

    @property
    def is_local(self):
        return self.feed_url.startswith(LOCAL_PREFIX)

    @property
    def remaining(self):
        if self.duration <= 0:
            return 0
        return max(0, self.duration - self.position)

    def cover_url(self, prefer_episode=True):
        if prefer_episode and self.image_url:
            return self.image_url
        return self.feed_image or self.image_url

    @property
    def sync_podcast(self):
        return self.feed_url

    @property
    def sync_episode(self):
        return self.media_url


FEED_FIELDS = frozenset(f.name for f in fields(Feed))
EPISODE_FIELDS = frozenset(f.name for f in fields(Episode))
