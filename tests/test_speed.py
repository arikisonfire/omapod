"""What opening a page relies on: telling cheaply whether anything changed, counting in the index, row conversion,
covers decoded off the main thread."""

import json
import struct
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

from gi.repository import GLib

from omapod.config import Settings
from omapod.core.images import ImageCache, TextureCache
from omapod.db import Database
from omapod.models import Episode, Feed



def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


# The smallest PNG: one transparent pixel.
PNG = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0")) + chunk(b"IEND", b""))


class Library(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.db.close)

    def add_feed(self, url="http://x.invalid/feed"):
        with self.db.tx() as conn:
            return self.db.insert_feed(conn, {"url": url, "title": "Feed", "state": 1})

    def add_episode(self, feed_id, **values):
        columns = {"feed_id": feed_id, "guid": str(time.monotonic_ns()), "title": "Episode", **values}
        names = ", ".join(columns)
        marks = ", ".join("?" * len(columns))
        self.db.execute(f"INSERT INTO episodes ({names}) VALUES ({marks})", tuple(columns.values()))


class RevisionTest(Library):
    def test_stays_the_same_without_writes(self):
        self.add_feed()
        before = self.db.revision()
        self.db.get_feeds()
        self.assertEqual(self.db.revision(), before)

    def test_changes_with_a_write_on_this_connection(self):
        before = self.db.revision()
        self.add_feed()
        self.assertNotEqual(self.db.revision(), before)

    def test_changes_with_a_write_from_another_thread(self):
        self.db.get_feeds()
        before = self.db.revision()

        def write():
            self.add_feed()
            self.db.close()

        thread = threading.Thread(target=write)
        thread.start()
        thread.join()
        self.assertNotEqual(self.db.revision(), before)

    def test_changes_with_a_write_from_another_process(self):
        before = self.db.revision()
        other = Database(self.db.path)
        with other.tx() as conn:
            other.insert_feed(conn, {"url": "http://other.invalid/feed", "title": "Other", "state": 1})
        other.close()
        self.assertNotEqual(self.db.revision(), before)


class SettingsRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(Path(self.tmp.name) / "settings.json")

    def test_a_change_counts(self):
        before = self.settings.revision
        self.settings.set("episode_cover", False)
        self.assertGreater(self.settings.revision, before)

    def test_the_same_value_does_not_count(self):
        self.settings.set("episode_cover", False)
        before = self.settings.revision
        self.settings.set("episode_cover", False)
        self.assertEqual(self.settings.revision, before)

    def test_what_no_page_shows_does_not_count(self):
        before = self.settings.revision
        self.settings.set("last_page", "queue")
        self.settings.set("window_width", 900)
        self.assertEqual(self.settings.revision, before)

    def test_reset_and_update_count(self):
        before = self.settings.revision
        self.settings.update({"episode_cover": False})
        self.settings.reset("episode_cover")
        self.assertEqual(self.settings.revision, before + 2)


class CountsTest(Library):
    def test_numbers_per_podcast_never_read_the_table(self):
        plan = [row["detail"] for row in self.db.query("EXPLAIN QUERY PLAN " + self.db.feed_counts_sql())]
        reads = [detail for detail in plan if " e" in detail and detail.split()[0] in ("SEARCH", "SCAN")]
        self.assertEqual(len(reads), 7, plan)
        for detail in reads:
            self.assertIn("COVERING INDEX", detail)

    def test_the_numbers(self):
        feed_id = self.add_feed()
        self.add_episode(feed_id, state=-1, media_url="http://x.invalid/1.mp3", pub_date=100)
        self.add_episode(feed_id, state=0, media_url="http://x.invalid/2.mp3", pub_date=300)
        self.add_episode(feed_id, state=1, media_url="http://x.invalid/3.mp3", pub_date=200, downloaded_at=5)
        self.add_episode(feed_id, state=0, downloaded_at=5, file_path="/x.mp3")
        self.add_episode(feed_id, state=0)  # no media: not counted as unplayed
        feed = self.db.get_feeds()[0]
        self.assertEqual((feed.count_new, feed.count_unplayed, feed.count_downloaded, feed.count_downloaded_unplayed,
                          feed.count_total, feed.count_played, feed.last_pub), (1, 3, 2, 1, 5, 1, 300))

    def test_history_uses_its_index(self):
        plan = " ".join(row["detail"] for row in self.db.query(
            "EXPLAIN QUERY PLAN SELECT id FROM episodes e WHERE e.completed_at > 0 "
            "ORDER BY e.completed_at DESC LIMIT 5"))
        self.assertIn("episodes_completed", plan)


class FromRowTest(Library):
    def test_episode(self):
        feed_id = self.add_feed()
        self.add_episode(feed_id, title="Title", duration=5000, psc_chapters=json.dumps([{"start": 1}]))
        episode = self.db.get_episodes(full=True)[0]
        self.assertIsInstance(episode, Episode)
        self.assertEqual((episode.title, episode.duration, episode.feed_title, episode.feed_id),
                         ("Title", 5000, "Feed", feed_id))
        self.assertEqual(episode.psc_chapters, [{"start": 1}])

    def test_unset_columns_keep_the_default(self):
        self.add_episode(self.add_feed())
        self.assertIsNone(self.db.get_episodes()[0].queue_pos)

    def test_columns_that_are_no_fields_are_ignored(self):
        row = self.db.conn().execute("SELECT 'x' AS title, 'y' AS not_a_field").fetchone()
        self.assertEqual(Episode.from_row(row).title, "x")

    def test_feed(self):
        feed_id = self.add_feed()
        self.db.update_feed(feed_id, tags=["News"], custom_title="Mine")
        feed = self.db.get_feeds()[0]
        self.assertIsInstance(feed, Feed)
        self.assertEqual((feed.tags, feed.display_title), (["News"], "Mine"))


class TextureCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.images = ImageCache(Path(self.tmp.name))
        self.textures = TextureCache(self.images)
        self.url = "/nonexistent/cover.png"

    def wait_for(self, condition):
        context = GLib.MainContext.default()
        end = time.monotonic() + 5
        while not condition() and time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        return condition()

    def test_loads_later_and_is_in_memory_afterwards(self):
        self.images.thumb_path(self.url, 128).write_bytes(PNG)
        ready = []
        self.assertIsNone(self.textures.get(self.url, 100, ready.append))
        self.assertTrue(self.wait_for(lambda: ready))
        self.assertIs(self.textures.get(self.url, 100, ready.append), ready[0])

    def test_everyone_waiting_gets_the_texture_from_one_decoding(self):
        self.images.thumb_path(self.url, 128).write_bytes(PNG)
        ready = []
        for _ in range(3):
            self.textures.get(self.url, 100, ready.append)
        self.assertTrue(self.wait_for(lambda: len(ready) == 3))
        self.assertIs(ready[0], ready[2])

    def test_a_broken_file_is_tried_again_next_time(self):
        self.images.thumb_path(self.url, 128).write_bytes(b"no picture")
        ready = []
        self.textures.get(self.url, 100, ready.append)
        self.assertTrue(self.wait_for(lambda: not self.textures._waiting))
        self.assertEqual(ready, [])
        self.images.thumb_path(self.url, 128).write_bytes(PNG)
        self.textures.get(self.url, 100, ready.append)
        self.assertTrue(self.wait_for(lambda: ready))

    def test_no_address(self):
        self.assertIsNone(self.textures.get("", 100, self.fail))


if __name__ == "__main__":
    unittest.main()
