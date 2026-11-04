import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

FIX = Path(__file__).with_name("fixtures")


def make_env():
    tmp = tempfile.mkdtemp(prefix="omapod-test-")
    os.environ["OMAPOD_HOME"] = tmp
    os.environ["OMAPOD_RUNTIME_DIR"] = os.path.join(tmp, "run")
    import importlib

    from omapod import config

    importlib.reload(config)
    config.paths.ensure()
    from omapod.config import Settings
    from omapod.core.library import Library
    from omapod.db import Database

    settings = Settings(config.paths.settings_file)
    db = Database(config.paths.db)
    library = Library(db, settings)
    return tmp, settings, db, library


class LibraryTest(unittest.TestCase):
    def setUp(self):
        from tests.fakeserver import FakeServer

        self.tmp, self.settings, self.db, self.library = make_env()
        self.server = FakeServer(str(FIX / "tone.mp3")).__enter__()
        self.base = self.server.base

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def subscribe(self):
        feed_id = self.library.subscribe_url(self.base + "/feed.xml")
        return feed_id

    def test_preview_subscribe_and_refresh_merge(self):
        from omapod.models import FEED_PREVIEW, FEED_SUBSCRIBED

        feed_id = self.library.preview(self.base + "/feed.xml")
        feed = self.db.get_feed(feed_id)
        self.assertEqual(feed.state, FEED_PREVIEW)
        self.assertEqual(feed.title, "Test Cast")
        episodes = self.db.get_episodes(feed_id=feed_id)
        self.assertEqual(len(episodes), 4)
        self.assertTrue(all(e.state == 0 for e in episodes))  # first fetch: nothing in the inbox
        self.library.subscribe(feed_id)
        self.assertEqual(self.db.get_feed(feed_id).state, FEED_SUBSCRIBED)
        # A refresh with the same content (304) adds nothing.
        result = self.library.refresh_feed(feed_id)
        self.assertTrue(result.not_modified)
        # Simulate a new episode: remove the newest one and refresh without ETag.
        newest = self.db.get_episodes(feed_id=feed_id, sort="date_desc")[0]
        self.db.execute("DELETE FROM episodes WHERE id = ?", (newest.id,))
        result = self.library.refresh_feed(feed_id, force=True)
        self.assertEqual(len(result.new_ids), 1)
        new = self.db.get_episode(result.new_ids[0])
        self.assertEqual(new.guid, "ep-3")
        self.assertEqual(new.state, -1)  # inbox
        # Refreshing again does not duplicate anything.
        result = self.library.refresh_feed(feed_id, force=True)
        self.assertEqual(result.new_ids, [])
        self.assertEqual(self.db.count_episodes(feed_id=feed_id), 4)

    def test_new_episode_action_queue(self):
        feed_id = self.subscribe()
        self.library.update_feed_settings(feed_id, new_episodes_action=3)
        newest = self.db.get_episodes(feed_id=feed_id, sort="date_desc")[0]
        self.db.execute("DELETE FROM episodes WHERE id = ?", (newest.id,))
        result = self.library.refresh_feed(feed_id, force=True)
        self.assertEqual(self.library.queue_ids(), result.new_ids)
        self.assertEqual(self.db.get_episode(result.new_ids[0]).state, 0)

    def test_moved_feed_and_html_detection(self):
        from omapod.feeds.parser import NotAFeedError

        feed_id = self.library.preview(self.base + "/moved")
        self.assertEqual(self.db.get_feed(feed_id).url, self.base + "/feed.xml")
        # Temporary redirects keep the address the user entered (AntennaPod does the same).
        feed_id = self.library.preview(self.base + "/temp")
        self.assertEqual(self.db.get_feed(feed_id).url, self.base + "/temp")
        with self.assertRaises(NotAFeedError):
            self.library.preview(self.base + "/page.html")

    def test_auth(self):
        from omapod.core.library import AuthRequired

        with self.assertRaises(AuthRequired):
            self.library.preview(self.base + "/auth/feed.xml")
        feed_id = self.library.preview(self.base + "/auth/feed.xml", "user", "pass")
        self.assertEqual(self.db.get_feed(feed_id).username, "user")
        self.assertFalse(self.library.refresh_feed(feed_id, force=True).error)

    def test_full_refresh_follows_pages(self):
        feed_id = self.subscribe()
        result = self.library.refresh_feed(feed_id, full=True)
        self.assertFalse(result.error)
        titles = {e.title for e in self.db.get_episodes(feed_id=feed_id)}
        self.assertIn("First atom", titles)

    def test_queue_operations(self):
        feed_id = self.subscribe()
        ids = [e.id for e in self.db.get_episodes(("has_media",), feed_id=feed_id, sort="date_asc")]
        self.library.add_to_queue(ids[:2])
        self.assertEqual(self.library.queue_ids(), ids[:2])
        self.settings.set("enqueue_location", "front")
        self.library.add_to_queue([ids[2]])
        self.assertEqual(self.library.queue_ids(), [ids[2]] + ids[:2])
        self.library.move_in_queue(ids[2], 2)
        self.assertEqual(self.library.queue_ids(), ids[:2] + [ids[2]])
        self.library.move_to_top([ids[1]])
        self.assertEqual(self.library.queue_ids()[0], ids[1])
        self.library.sort_queue("date_desc")
        self.assertEqual(self.library.queue_ids(), list(reversed(ids)))
        self.assertEqual(self.library.next_in_queue(ids[2]), ids[1])
        self.assertIsNone(self.library.next_in_queue(ids[0]))
        self.library.mark_played([ids[1]])
        self.assertNotIn(ids[1], self.library.queue_ids())
        self.assertEqual(self.db.get_episode(ids[1]).state, 1)
        self.library.clear_queue()
        self.assertEqual(self.library.queue_ids(), [])

    def test_inbox_and_favorites(self):
        feed_id = self.subscribe()
        ids = [e.id for e in self.db.get_episodes(feed_id=feed_id)]
        self.db.update_episodes(ids, state=-1)
        self.assertEqual(self.db.count_episodes(("new",)), 4)
        self.library.add_to_queue([ids[0]])
        self.assertEqual(self.db.get_episode(ids[0]).state, 0)  # queueing leaves the inbox
        self.library.remove_from_inbox([ids[1]])
        self.assertEqual(self.db.count_episodes(("new",)), 2)
        self.assertEqual(self.library.remove_all_from_inbox(), 2)
        self.library.set_favorite([ids[2]])
        self.assertEqual([e.id for e in self.db.get_episodes(("favorite",))], [ids[2]])

    def test_download_and_cleanup(self):
        from omapod.net.downloads import DownloadManager

        feed_id = self.subscribe()
        manager = DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        manager.start()
        episode = next(e for e in self.db.get_episodes(feed_id=feed_id) if e.guid == "ep-3")
        self.assertEqual(self.library.download([episode.id]), 1)
        for _ in range(100):
            if not manager.is_active(episode.id):
                break
            time.sleep(0.05)
        done = self.db.get_episode(episode.id)
        self.assertTrue(done.is_downloaded, self.db.get_log())
        self.assertEqual(Path(done.file_path).read_bytes(), (FIX / "tone.mp3").read_bytes())
        self.assertIn(episode.id, self.library.queue_ids())  # enqueue downloaded
        log = self.db.get_log()
        self.assertTrue(log[0]["success"])
        # Played + auto delete
        self.settings.set("auto_delete", True)
        self.library.mark_played([episode.id])
        self.assertFalse(self.db.get_episode(episode.id).is_downloaded)
        self.assertFalse(Path(done.file_path).exists())
        manager.stop()

    def test_resume_partial_download(self):
        from omapod.net.downloads import DownloadManager

        feed_id = self.subscribe()
        manager = DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        episode = next(e for e in self.db.get_episodes(feed_id=feed_id) if e.guid == "ep-3")
        feed = self.db.get_feed(feed_id)
        part = self.library.feed_folder(feed) / f".{episode.id}.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        data = (FIX / "tone.mp3").read_bytes()
        part.write_bytes(data[:10000])
        manager.start()
        self.library.download([episode.id])
        for _ in range(100):
            if not manager.is_active(episode.id):
                break
            time.sleep(0.05)
        done = self.db.get_episode(episode.id)
        self.assertEqual(Path(done.file_path).read_bytes(), data)
        self.assertIn(("GET", "/ep3.mp3"), self.server.httpd.requests)
        manager.stop()

    def test_auto_download_respects_cache_and_filter(self):
        from omapod.core.library import should_auto_download
        from omapod.models import Feed

        feed = Feed(include_terms='"Episode 3" bonus', exclude_terms="trailer", min_duration=60)
        self.assertTrue(should_auto_download(feed, "Episode 3: Chapters", 120000))
        self.assertFalse(should_auto_download(feed, "Episode 3 trailer", 120000))
        self.assertFalse(should_auto_download(feed, "Episode 2", 120000))
        self.assertFalse(should_auto_download(feed, "bonus", 30000))

        class FakeDownloads:
            def __init__(self):
                self.started = []

            def enqueue(self, episode, manual=True):
                self.started.append(episode.id)
                return True

            def is_active(self, episode_id):
                return False

            def active_ids(self):
                return set()

        feed_id = self.subscribe()
        self.library.downloads = FakeDownloads()
        self.settings.set("auto_download", True)
        self.settings.set("episode_cache", 2)
        ids = [e.id for e in self.db.get_episodes(("has_media",), feed_id=feed_id)]
        self.db.update_episodes(ids, state=-1)
        self.assertEqual(self.library.auto_download(), 2)
        self.assertEqual(len(self.library.downloads.started), 2)

    def test_local_folder(self):
        folder = Path(self.tmp) / "audiobooks"
        folder.mkdir()
        shutil.copy(FIX / "tone.mp3", folder / "01 Chapter.mp3")
        feed_id = self.library.add_local_folder(str(folder))
        episodes = self.db.get_episodes(feed_id=feed_id)
        self.assertEqual([e.title for e in episodes], ["01 Chapter"])
        self.assertTrue(episodes[0].is_downloaded)
        (folder / "01 Chapter.mp3").unlink()
        self.library.refresh_feed(feed_id)
        self.assertEqual(self.db.count_episodes(feed_id=feed_id), 0)


class StubPlayer:
    """What sync asks of the player."""

    def __init__(self, episode_id, status="paused"):
        self.episode_id, self.status = episode_id, status

    def current_id(self):
        return self.episode_id

    def plays_here(self):
        return self.status in ("playing", "loading")


class SyncTest(unittest.TestCase):
    def setUp(self):
        from tests.fakeserver import FakeServer

        self.tmp, self.settings, self.db, self.library = make_env()
        self.server = FakeServer(str(FIX / "tone.mp3")).__enter__()
        self.base = self.server.base

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def service(self, provider):
        from omapod.config import Credentials
        from omapod.sync.service import SyncService

        creds = Credentials(Path(self.tmp) / "creds.json")
        creds.save({"sync": {"provider": provider, "server": self.base, "username": "user", "password": "pass",
                             "device_id": "omapod-test"}})
        self.settings.set("sync_provider", provider)
        service = SyncService(self.library, self.settings, creds)
        self.addCleanup(service.close)
        return service

    def second_computer(self):
        """Another omaPOD with its own library and settings, logged in to the same Nextcloud."""
        from omapod.config import Credentials
        from omapod.sync.service import SyncService

        tmp, settings, db, library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(db.close)
        creds = Credentials(Path(tmp) / "creds.json")
        creds.save({"sync": {"provider": "nextcloud", "server": self.base, "username": "user", "password": "pass"}})
        settings.update({"sync_provider": "nextcloud", "sync_queue_favorites": True})
        service = SyncService(library, settings, creds)
        self.addCleanup(service.close)
        return service

    def episodes(self, library, feed_url=None):
        feed = library.db.get_feed_by_url(feed_url or self.base + "/feed.xml")
        return {(e.guid or e.media_url.rsplit("/", 1)[1]): e.id for e in library.db.get_episodes(feed_id=feed.id)
                if e.media_url}

    def _roundtrip(self, provider):
        service = self.service(provider)
        feed_id = self.library.subscribe_url(self.base + "/feed.xml")
        episodes = {e.guid: e for e in self.db.get_episodes(feed_id=feed_id)}
        self.library.mark_played([episodes["ep-2"].id])
        # Another device subscribed to the Atom feed and played ep-3 halfway.
        state = self.server.sync
        state.apply_subs([self.base + "/atom.xml"], [])
        state.add_actions([{"podcast": self.base + "/feed.xml", "episode": self.base + "/ep3.mp3", "guid": "ep-3",
                            "action": "play", "timestamp": "2099-01-01T00:00:00", "started": 0, "position": 600,
                            "total": 3723}])
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        # Local subscription uploaded (first sync uploads everything), remote one added locally.
        self.assertTrue(state.subscriptions[self.base + "/feed.xml"][0])
        atom = self.db.get_feed_by_url(self.base + "/atom.xml")
        self.assertIsNotNone(atom)
        self.assertEqual(atom.title, "Atom Cast")  # refreshed during the sync
        # Remote play position applied.
        self.assertEqual(self.db.get_episode(episodes["ep-3"].id).position, 600000)
        # Local played state uploaded.
        uploaded = [a for _ts, a in state.actions if a.get("episode") == self.base + "/ep2.m4a"]
        self.assertTrue(uploaded and uploaded[-1]["action"] == "play" and uploaded[-1]["position"] == 2723)
        # Unsubscribe locally -> removal is uploaded on the next sync.
        self.library.unsubscribe([atom.id])
        self.assertTrue(service.run())
        self.assertFalse(state.subscriptions[self.base + "/atom.xml"][0])
        # Remote removal of the main feed removes it locally.
        state.apply_subs([], [self.base + "/feed.xml"])
        self.assertTrue(service.run())
        self.assertIsNone(self.db.get_feed_by_url(self.base + "/feed.xml"))
        return state

    def test_nextcloud(self):
        self._roundtrip("nextcloud")

    def test_gpodder(self):
        state = self._roundtrip("gpodder")
        devices = {a.get("device") for _ts, a in state.actions if a.get("device")}
        self.assertEqual(devices, {"omapod-test"})

    def test_archiving_stays_on_this_computer(self):
        from omapod.models import FEED_ARCHIVED, FEED_SUBSCRIBED

        first = self.service("nextcloud")
        url = self.base + "/feed.xml"
        feed_id = self.library.subscribe_url(url)
        self.assertTrue(first.run())
        second = self.second_computer()
        self.assertTrue(second.run())
        other = second.library
        episode_id = other.db.get_episodes(feed_id=other.db.get_feed_by_url(url).id)[0].id
        other.set_favorite([episode_id], True, sync=False)
        other.db.update_episode(episode_id, position=600000)
        self.library.archive([feed_id])
        self.assertTrue(first.run())
        self.assertTrue(second.run())
        self.assertTrue(self.server.sync.subscriptions[url][0])  # still subscribed on the server
        self.assertEqual(other.db.get_feed_by_url(url).state, FEED_SUBSCRIBED)
        kept = other.db.get_episode(episode_id)
        self.assertEqual((kept.favorite, kept.position), (1, 600000))
        # A full synchronization (everything on the server counts as added) leaves it archived here.
        self.settings.set("sync_last_subscriptions", 0)
        self.assertTrue(first.run())
        self.assertEqual(self.db.get_feed(feed_id).state, FEED_ARCHIVED)
        # Restoring subscribes again, on the server too (podcasts archived by older versions were removed there).
        self.server.sync.apply_subs([], [url])
        self.settings.set("sync_last_subscriptions", self.server.sync.clock)
        self.library.archive([feed_id], archived=False)
        self.assertTrue(first.run())
        self.assertTrue(self.server.sync.subscriptions[url][0])
        self.assertEqual(self.db.get_feed(feed_id).state, FEED_SUBSCRIBED)

    def test_odd_actions_from_the_server_are_skipped(self):
        from omapod.sync.providers import GpodderNet, SyncError

        self.service("gpodder")
        feed_id = self.library.subscribe_url(self.base + "/feed.xml")
        service = self.service("gpodder")
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        episodes = {e.guid: e.id for e in self.db.get_episodes(feed_id=feed_id)}
        state, url = self.server.sync, self.base + "/feed.xml"
        ts = state.tick()

        def play(episode, guid, position):
            return {"podcast": url, "episode": self.base + episode, "guid": guid, "action": "play",
                    "timestamp": "2099-01-01T00:00:00", "started": 0, "position": position, "total": 3723}

        state.actions += [(ts, "not an action"), (ts, {"podcast": url, "episode": ["a", "list"]}),
                          (ts, play("/ep2.m4a", "ep-2", float("inf"))), (ts, play("/ep2.m4a", "ep-2", 1e30)),
                          (ts, play("/ep3.mp3", "ep-3", 600))]
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertEqual(self.db.get_episode(episodes["ep-3"]).position, 600000)
        self.assertEqual(self.db.get_episode(episodes["ep-2"]).position, 0)

        class Answer:
            def json(self):
                return ["not", "an", "object"]

        client = GpodderNet(self.base, "user", "pass", "omapod-test")
        with mock.patch.object(GpodderNet, "_request", lambda *_a, **_k: Answer()):
            with self.assertRaises(SyncError):
                client.get_episode_actions(0)
            self.assertFalse(service.run())  # reported, not raised
        self.assertTrue(self.settings.get("sync_last_error"))

    def test_old_remote_action_does_not_override_newer_local(self):
        from omapod.sync.service import newest_actions

        local = [{"podcast": "p", "episode": "e", "action": "play", "timestamp": "2026-10-10T10:00:00"}]
        remote = [{"podcast": "p", "episode": "e", "action": "play", "timestamp": "2026-10-09T10:00:00",
                   "position": 5},
                  {"podcast": "p", "episode": "f", "action": "play", "timestamp": "2026-10-09T10:00:00",
                   "position": 7},
                  {"podcast": "p", "episode": "f", "action": "play", "timestamp": "2026-10-09T11:00:00",
                   "position": 9},
                  {"podcast": "p", "episode": "f", "action": "download", "timestamp": "2026-10-09T12:00:00"}]
        winners = newest_actions(remote, local)
        self.assertEqual([(w["episode"], w["position"]) for w in winners], [("f", 9)])

    def test_login_flow(self):
        from omapod.sync.providers import NextcloudLoginFlow

        flow = NextcloudLoginFlow(self.base)
        self.assertTrue(flow.start().endswith("/login/v2/flow/abc"))
        self.assertIsNone(flow.poll())
        self.assertEqual(flow.poll(), (self.base, "user", "pass"))

    def test_login_flow_rejects_unexpected_answers(self):
        from omapod.sync.providers import NextcloudLoginFlow, SyncError

        for path in ("/missing", "/broken", "/elsewhere"):
            with self.subTest(path=path), self.assertRaises(SyncError):
                NextcloudLoginFlow(self.base + path).start()

    def test_login_flow_asks_before_using_http_for_a_bare_address(self):
        from omapod.sync.providers import NextcloudLoginFlow, PlainHttpOnly, SyncError

        bare = self.base.removeprefix("http://")
        with self.assertRaises(PlainHttpOnly) as caught:
            NextcloudLoginFlow(bare).start()
        self.assertEqual(caught.exception.server, self.base)
        self.assertTrue(caught.exception.private)  # 127.0.0.1: the dialog may offer http
        # What the user chose explicitly works.
        flow = NextcloudLoginFlow(caught.exception.server)
        self.assertTrue(flow.start().endswith("/login/v2/flow/abc"))
        # An address typed with https:// stays on https://.
        with self.assertRaises(SyncError) as caught:
            NextcloudLoginFlow("https://" + bare).start()
        self.assertNotIsInstance(caught.exception, PlainHttpOnly)

    def test_only_local_network_addresses_count_as_private(self):
        from omapod.sync.providers import is_private_host

        for host in ("127.0.0.1", "localhost", "192.168.1.20", "10.0.0.5", "100.101.102.103", "fd00::1", "fe80::1"):
            self.assertTrue(is_private_host(host), host)
        for host in ("8.8.8.8", "2001:4860:4860::8888", "", "no-such-host.invalid"):
            self.assertFalse(is_private_host(host), host)

    def test_login_never_steps_down_to_http(self):
        from omapod.sync.providers import NextcloudLoginFlow

        flow = NextcloudLoginFlow("https://cloud.example")

        class Answer:
            def __init__(self, server):
                self.server = server

            def json(self):
                return {"server": self.server, "loginName": "user", "appPassword": "secret"}

        with mock.patch("omapod.net.http.request", lambda *_a, **_k: Answer("http://cloud.example/")):
            self.assertEqual(flow.poll(), ("https://cloud.example", "user", "secret"))
        with mock.patch("omapod.net.http.request", lambda *_a, **_k: Answer("https://cloud.example/nc/")):
            self.assertEqual(flow.poll(), ("https://cloud.example/nc", "user", "secret"))

    def test_login_flow_asks_before_trusting_a_certificate(self):
        import ssl

        from omapod.net import http
        from omapod.sync.providers import Nextcloud, NextcloudLoginFlow, SyncError, UntrustedCertificate
        from tests.fakeserver import FakeServer, self_signed_certificate

        tls = self_signed_certificate(self.tmp)
        if tls is None:
            self.skipTest("openssl is not installed")
        with FakeServer(str(FIX / "tone.mp3"), tls=tls) as server:
            bare = server.base.removeprefix("https://")
            with self.assertRaises(UntrustedCertificate) as caught:
                NextcloudLoginFlow(bare).start()
            certificate = caught.exception.certificate
            der = ssl.PEM_cert_to_DER_cert(Path(tls[0]).read_text())
            self.assertEqual(certificate["sha256"], http.certificate_sha256(der))
            self.assertIn("127.0.0.1", certificate["subject"])
            flow = NextcloudLoginFlow(caught.exception.server, certificate["sha256"])
            self.assertTrue(flow.start().startswith(server.base))
            self.assertIsNone(flow.poll())
            self.assertEqual(flow.poll(), (server.base, "user", "pass"))
            # Sync requests keep accepting exactly this certificate and no other one.
            changes = Nextcloud(server.base, "user", "pass", certificate["sha256"]).get_subscription_changes(0)
            self.assertEqual(changes.added, [])
            with self.assertRaises(SyncError):
                Nextcloud(server.base, "user", "pass", "0" * 64).get_subscription_changes(0)


    def test_history_follows_when_episodes_were_heard(self):
        from datetime import datetime, timezone

        from omapod.models import PLAYED

        def at(text):
            return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())

        service = self.service("nextcloud")
        self.library.subscribe_url(self.base + "/feed.xml")
        ids = self.episodes(self.library)
        # ep-2 was heard here; ep1 carries an entry an older omaPOD gave it at sync time.
        self.db.update_episode(ids["ep-2"], state=PLAYED, last_played=at("2026-10-22T12:00:00") * 1000,
                               completed_at=at("2026-10-22T12:00:00"))
        self.db.update_episode(ids["ep1.mp3"], state=PLAYED, completed_at=at("2026-10-24T20:00:00"))
        self.server.sync.add_actions([
            # Heard to the end on the phone.
            {"podcast": self.base + "/feed.xml", "episode": self.base + "/ep3.mp3", "guid": "ep-3", "action": "play",
             "timestamp": "2026-10-20T08:00:00", "started": 100, "position": 3723, "total": 3723},
            # AntennaPod's first sync: everything played, all with one timestamp.
            {"podcast": self.base + "/feed.xml", "episode": self.base + "/ep1.mp3", "action": "play",
             "timestamp": "2026-10-19T00:00:00", "started": 2710, "position": 2710, "total": 2710},
            {"podcast": self.base + "/feed.xml", "episode": self.base + "/ep2.m4a", "guid": "ep-2", "action": "play",
             "timestamp": "2026-10-23T00:00:00", "started": 2723, "position": 2723, "total": 2723},
        ])
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        ep1, ep2, ep3 = (self.db.get_episode(ids[k]) for k in ("ep1.mp3", "ep-2", "ep-3"))
        self.assertEqual(ep3.completed_at, at("2026-10-20T08:00:00"))
        self.assertEqual(ep3.last_played, at("2026-10-20T08:00:00") * 1000)
        self.assertEqual(ep1.completed_at, at("2026-10-19T00:00:00"))
        self.assertEqual(ep1.last_played, 0)
        self.assertEqual(ep2.completed_at, at("2026-10-22T12:00:00"))
        history = self.db.get_episodes(extra="e.completed_at > 0", sort="completed_desc")
        self.assertEqual([e.id for e in history], [ids["ep-2"], ids["ep-3"], ids["ep1.mp3"]])
        # Same time (a bulk upload): the newer episode first.
        self.db.update_episodes([ids["ep-2"], ids["ep-3"]], completed_at=at("2026-10-19T00:00:00"))
        history = self.db.get_episodes(extra="e.completed_at > 0", sort="completed_desc")
        self.assertEqual([e.id for e in history][:2], [ids["ep-3"], ids["ep-2"]])

    def test_history_repaired_once_from_all_actions(self):
        from datetime import datetime, timezone

        from omapod.models import PLAYED

        def at(text):
            return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())

        # gpodder.net keeps every action of an episode (Nextcloud only the last one uploaded).
        service = self.service("gpodder")
        self.library.subscribe_url(self.base + "/feed.xml")
        ids = self.episodes(self.library)
        feed = self.base + "/feed.xml"
        # Synced long ago by an older omaPOD, which put ep-3 into the history at sync time and re-uploaded
        # it in its own first sync; the phone had heard ep-2 to the end after it was last heard here.
        self.server.sync.add_actions([
            {"podcast": feed, "episode": self.base + "/ep3.mp3", "guid": "ep-3", "action": "play",
             "timestamp": "2026-10-19T00:00:00", "started": 3723, "position": 3723, "total": 3723},
            {"podcast": feed, "episode": self.base + "/ep3.mp3", "guid": "ep-3", "action": "play",
             "timestamp": "2026-10-24T20:00:00", "started": 3723, "position": 3723, "total": 3723},
            {"podcast": feed, "episode": self.base + "/ep2.m4a", "guid": "ep-2", "action": "play",
             "timestamp": "2026-10-23T09:00:00", "started": 10, "position": 2723, "total": 2723},
        ])
        self.db.update_episode(ids["ep-3"], state=PLAYED, completed_at=at("2026-10-24T20:00:05"))
        self.db.update_episode(ids["ep-2"], state=PLAYED, last_played=at("2026-10-22T12:00:00") * 1000,
                               completed_at=at("2026-10-22T12:00:00"))
        self.settings.update({"sync_last_subscriptions": self.server.sync.clock,
                              "sync_last_actions": self.server.sync.clock})
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertEqual(self.db.get_episode(ids["ep-3"]).completed_at, at("2026-10-19T00:00:00"))
        self.assertEqual(self.db.get_episode(ids["ep-2"]).completed_at, at("2026-10-23T09:00:00"))
        self.assertEqual(self.settings.get("history_repair"), 2)
        full_fetches = [r for r in self.server.httpd.requests if "episodes/user.json?since=0" in r[1]]
        self.assertEqual(len(full_fetches), 1)
        self.assertTrue(service.run())
        self.assertEqual([r for r in self.server.httpd.requests if "episodes/user.json?since=0" in r[1]],
                         full_fetches)

    def test_downloads_from_other_devices(self):
        class Downloads:
            def __init__(self):
                self.queued = []

            def enqueue(self, episode, manual=True):
                self.queued.append(episode.id)
                return True

            def active_ids(self):
                return set()

        # Nextcloud's GPodder Sync drops download actions; gpodder.net keeps them.
        service = self.service("gpodder")
        self.library.downloads = Downloads()
        self.library.subscribe_url(self.base + "/feed.xml")
        ids = self.episodes(self.library)
        feed = self.base + "/feed.xml"
        self.server.sync.add_actions([
            {"podcast": feed, "episode": self.base + "/ep3.mp3", "action": "download", "timestamp": "2026-10-20T08:00:00"},
            {"podcast": feed, "episode": self.base + "/ep2.m4a", "action": "download", "timestamp": "2026-10-20T08:00:00"},
            {"podcast": feed, "episode": self.base + "/ep2.m4a", "action": "delete", "timestamp": "2026-10-21T08:00:00"},
            {"podcast": feed, "episode": self.base + "/ep1.mp3", "action": "download", "timestamp": "2026-10-20T08:00:00"},
            {"podcast": feed, "episode": self.base + "/ep1.mp3", "action": "play", "timestamp": "2026-10-21T08:00:00",
             "started": 0, "position": 2710, "total": 2710},
        ])
        self.assertTrue(service.run())
        self.assertEqual(self.library.downloads.queued, [])  # switched off
        self.settings.update({"sync_mirror_downloads": True, "mirror_downloads_ready": False})
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        # Still on the other device and not played: only ep-3.
        self.assertEqual(self.library.downloads.queued, [ids["ep-3"]])
        self.server.sync.add_actions([{"podcast": feed, "episode": self.base + "/ep2.m4a", "action": "download",
                                       "timestamp": "2026-10-22T08:00:00"}])
        self.assertTrue(service.run())
        self.assertEqual(self.library.downloads.queued, [ids["ep-3"], ids["ep-2"]])
        # Asked for again (sync looks back a day), a download is followed only once: deleting it here sticks.
        self.assertTrue(service.run())
        self.assertEqual(self.library.downloads.queued, [ids["ep-3"], ids["ep-2"]])
        self.assertEqual(self.server.sync.nextcloud_actions[-1]["action"], "PLAY")  # Nextcloud kept only the play

    def test_queue_shared_between_computers(self):
        a = self.service("nextcloud")
        self.settings.set("sync_queue_favorites", True)
        self.library.subscribe_url(self.base + "/feed.xml")
        ids_a = self.episodes(self.library)
        self.library.add_to_queue([ids_a["ep-3"], ids_a["ep-2"]], location="back")
        self.assertTrue(a.run(), self.settings.get("sync_last_error"))
        shared = json.loads(self.server.httpd.dav["/remote.php/dav/files/user-id/.omapod/queue.json"][0])
        self.assertEqual([i["guid"] for i in shared["episodes"]], ["ep-3", "ep-2"])
        # The second computer gets the podcast through the subscription sync and joins with its own queue.
        b = self.second_computer()
        self.assertTrue(b.run(), b.settings.get("sync_last_error"))
        ids_b = self.episodes(b.library)
        self.assertEqual(b.db.queue_ids(), [ids_b["ep-3"], ids_b["ep-2"]])
        b.library.add_to_queue([ids_b["ep1.mp3"]], location="front")
        self.assertTrue(b.run())
        self.assertTrue(a.run())
        self.assertEqual(self.db.queue_ids(), [ids_a["ep1.mp3"], ids_a["ep-3"], ids_a["ep-2"]])
        # Both change the queue; the later change wins as a whole. Both computers share one event bus
        # here, so each would see the other's change: set the change times explicitly.
        self.library.remove_from_queue([ids_a["ep-3"]])
        b.library.move_to_bottom([ids_b["ep1.mp3"]])
        self.settings.set("queue_sync_changed", 1000)
        b.settings.set("queue_sync_changed", 2000)
        self.assertTrue(b.run())
        self.assertTrue(a.run())
        self.assertEqual(self.db.queue_ids(), [ids_a["ep-3"], ids_a["ep-2"], ids_a["ep1.mp3"]])
        self.assertTrue(b.run())
        self.assertEqual(b.db.queue_ids(), [ids_b["ep-3"], ids_b["ep-2"], ids_b["ep1.mp3"]])

    def test_favorites_shared_between_computers(self):
        def favorites(service):
            names = {v: k for k, v in self.episodes(service.library).items()}
            return {names[e.id] for e in service.db.get_episodes(("favorite",))}

        a = self.service("nextcloud")
        self.settings.set("sync_queue_favorites", True)
        self.library.subscribe_url(self.base + "/feed.xml")
        ids_a = self.episodes(self.library)
        self.library.set_favorite([ids_a["ep-3"]])
        self.assertTrue(a.run(), self.settings.get("sync_last_error"))
        b = self.second_computer()
        self.assertTrue(b.run(), b.settings.get("sync_last_error"))  # gets the podcast and ep-3
        ids_b = self.episodes(b.library)
        self.assertEqual(favorites(b), {"ep-3"})
        b.library.set_favorite([ids_b["ep1.mp3"]])
        self.assertTrue(b.run())
        self.assertTrue(a.run())
        self.assertEqual(favorites(a), {"ep-3", "ep1.mp3"})
        # One removes, the other adds at the same time: both changes survive.
        self.library.set_favorite([ids_a["ep-3"]], False)
        b.library.set_favorite([ids_b["ep-2"]])
        self.assertTrue(a.run())
        self.assertTrue(b.run())
        self.assertTrue(a.run())
        self.assertEqual(favorites(a), {"ep1.mp3", "ep-2"})
        self.assertEqual(favorites(b), {"ep1.mp3", "ep-2"})

    # -- what the phone played (AntennaPod through Nextcloud's GPodder Sync) ---------------------------------

    def phone_played(self, episode, seconds_ago, position, started=0, total=3723):
        """AntennaPod's play action as it reaches the server, `seconds_ago` after it happened."""
        from datetime import datetime, timedelta, timezone

        stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        files = {"ep-3": ("ep3.mp3", "ep-3"), "ep-2": ("ep2.m4a", "ep-2"), "ep1.mp3": ("ep1.mp3", None)}
        name, guid = files[episode]
        action = {"podcast": self.base + "/feed.xml", "episode": f"{self.base}/{name}", "action": "play",
                  "timestamp": stamp, "started": started, "position": position, "total": total}
        if guid:
            action["guid"] = guid
        self.server.sync.add_actions([action])
        return int(time.time()) - seconds_ago

    def synced_library(self, provider="nextcloud"):
        service = self.service(provider)
        self.library.subscribe_url(self.base + "/feed.xml")
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))  # the first sync
        return service, self.episodes(self.library)

    def test_timestamps_in_every_format_the_servers_send(self):
        from datetime import datetime, timezone

        from omapod.sync.service import epoch

        base = int(datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc).timestamp())
        # AntennaPod and gpodder.net write UTC without a zone, Nextcloud's GPodder Sync answers with one.
        for value in ("2026-10-24T19:00:00", "2026-10-24T19:00:00+00:00", "2026-10-24T19:00:00Z",
                      "2026-10-24T21:00:00+02:00", "2026-10-24 19:00:00", "2026-10-24T19:00:00.250Z", base, str(base)):
            with self.subTest(value=value):
                self.assertEqual(epoch(value), base)
        for junk in (None, "", "yesterday", True):
            self.assertEqual(epoch(junk), 0)

    def test_phone_listening_reaches_position_and_history(self):
        service, ids = self.synced_library()
        heard = self.phone_played("ep-3", 600, 1200, started=100)
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        ep3 = self.db.get_episode(ids["ep-3"])
        self.assertEqual(ep3.position, 1200000)
        self.assertEqual(ep3.last_played, heard * 1000)
        # In the history although it is not finished, at the time it was heard on the phone.
        self.assertEqual(ep3.completed_at, heard)
        history = self.db.get_episodes(extra="e.completed_at > 0", sort="completed_desc")
        self.assertEqual([e.id for e in history], [ids["ep-3"]])

    def test_late_upload_from_a_phone_without_wifi_is_still_applied(self):
        service, ids = self.synced_library()
        # Heard an hour ago, uploaded only now (AntennaPod waits for Wi-Fi), after omaPOD's last sync.
        self.phone_played("ep-3", 3600, 1200)
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 1200000)

    def test_actions_seen_again_are_not_applied_twice(self):
        service, ids = self.synced_library()
        self.phone_played("ep-3", 600, 1200)
        self.assertTrue(service.run())
        # Started over here without listening yet; the phone's action comes back with the next sync.
        self.library.reset_position([ids["ep-3"]])
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 0)
        # Marked as played here and uploaded, then unplayed here: Nextcloud drops that 'new' action and keeps
        # the 'played' one, which must not mark the episode as played again.
        self.library.mark_played([ids["ep-2"]])
        self.assertTrue(service.run())
        self.library.mark_played([ids["ep-2"]], played=False)
        self.assertTrue(service.run())
        self.assertFalse(self.db.get_episode(ids["ep-2"]).is_played)

    def test_remote_action_older_than_listening_here_is_ignored(self):
        service, ids = self.synced_library()
        self.phone_played("ep-3", 3600, 1200)
        self.library.save_position(ids["ep-3"], 300000, listened_ms=5000)  # heard here just now
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 300000)

    def test_local_action_overtaken_by_a_newer_one_is_not_uploaded(self):
        from datetime import datetime, timedelta, timezone

        service, ids = self.synced_library()
        # Paused here 20 minutes ago, not uploaded yet; the phone went on since.
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
        self.db.sync_enqueue("action", {"podcast": self.base + "/feed.xml", "episode": self.base + "/ep3.mp3",
                                        "guid": "ep-3", "action": "play", "timestamp": stamp, "started": 0,
                                        "position": 300, "total": 3723})
        self.phone_played("ep-3", 600, 1200)
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 1200000)
        # The server keeps the last action uploaded, so the older one from here would have set the phone back.
        row = next(r for r in self.server.sync.nextcloud_actions if r["guid"] == "ep-3")
        self.assertEqual(row["position"], 1200)
        self.assertEqual(self.db.sync_pending("action"), [])

    def test_loaded_episode_moves_to_the_other_devices_position(self):
        from omapod.events import bus

        events = []
        token = bus.subscribe("remote-playback", lambda *args: events.append(args))
        self.addCleanup(bus.unsubscribe, token)
        service, ids = self.synced_library()
        player = StubPlayer(ids["ep-3"])
        self.library.player = player
        self.phone_played("ep-3", 600, 1200)
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 1200000)
        self.assertEqual(events, [(ids["ep-3"], 1200000, "heard")])
        # Playing here: this computer is ahead, nothing moves.
        player.status = "playing"
        self.phone_played("ep-3", 300, 1500)
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 1200000)
        self.assertEqual(len(events), 1)
        # Finished on the phone while it is loaded here, paused.
        player.status = "paused"
        self.phone_played("ep-3", 60, 3723, started=1500)
        self.assertTrue(service.run())
        self.assertTrue(self.db.get_episode(ids["ep-3"]).is_played)
        self.assertEqual(events[-1], (ids["ep-3"], 0, "finished"))

    def test_player_picks_up_what_another_device_played_last(self):
        from omapod.events import bus

        follows = []
        token = bus.subscribe("remote-follow", follows.append)
        self.addCleanup(bus.unsubscribe, token)
        service, ids = self.synced_library()
        self.library.player = StubPlayer(ids["ep-2"])
        self.db.update_episode(ids["ep-2"], last_played=(int(time.time()) - 7200) * 1000)  # heard here 2 h ago
        self.phone_played("ep1.mp3", 3600, 600, total=2710)
        self.phone_played("ep-3", 1800, 1200)  # the phone's latest
        self.assertTrue(service.run())
        self.assertEqual(follows, [ids["ep-3"]])
        # Heard here after the phone: nothing to pick up.
        self.db.update_episode(ids["ep-2"], last_played=int(time.time()) * 1000)
        self.phone_played("ep1.mp3", 600, 900, total=2710)
        self.assertTrue(service.run())
        self.assertEqual(follows, [ids["ep-3"]])
        # Finished on the phone: nothing to continue.
        self.db.update_episode(ids["ep-2"], last_played=(int(time.time()) - 7200) * 1000)
        self.phone_played("ep1.mp3", 60, 2710, started=900, total=2710)
        self.assertTrue(service.run())
        self.assertEqual(follows, [ids["ep-3"]])
        # Switched off: the player stays as it is.
        self.settings.set("sync_follow_playback", False)
        self.phone_played("ep-3", 30, 1300)
        self.assertTrue(service.run())
        self.assertEqual(follows, [ids["ep-3"]])
        # Without a window (the background refresh), omaPOD starts with it.
        self.settings.set("sync_follow_playback", True)
        self.library.player = None
        self.phone_played("ep-3", 10, 1400)
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_meta("current_episode"), str(ids["ep-3"]))

    def test_history_has_episodes_that_were_not_heard_to_the_end(self):
        self.library.subscribe_url(self.base + "/feed.xml")
        ids = self.episodes(self.library)
        self.library.save_position(ids["ep-3"], 60000, listened_ms=60000)
        ep3 = self.db.get_episode(ids["ep-3"])
        self.assertGreater(ep3.completed_at, 0)
        self.assertGreater(ep3.last_played, 0)
        history = self.db.get_episodes(extra="e.completed_at > 0", sort="completed_desc")
        self.assertEqual([e.id for e in history], [ids["ep-3"]])
        # Saved again without listening (pausing, quitting): neither the history nor the time last heard move.
        self.db.update_episode(ids["ep-3"], last_played=1000, completed_at=1)
        self.library.save_position(ids["ep-3"], 60000)
        ep3 = self.db.get_episode(ids["ep-3"])
        self.assertEqual((ep3.last_played, ep3.completed_at), (1000, 1))

    def test_history_repair_adds_what_was_heard_here_but_not_finished(self):
        service = self.service("nextcloud")
        self.library.subscribe_url(self.base + "/feed.xml")
        ids = self.episodes(self.library)
        self.db.update_episode(ids["ep-3"], position=60000, last_played=1761300000 * 1000)
        self.settings.set("history_repair", 1)
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertEqual(self.db.get_episode(ids["ep-3"]).completed_at, 1761300000)

    def test_pull_fetches_positions_without_uploading(self):
        service = self.service("nextcloud")
        self.library.subscribe_url(self.base + "/feed.xml")
        ids = self.episodes(self.library)
        self.assertFalse(service.pull())  # before the first sync, which uploads too
        self.assertFalse(service.pull_due())
        self.assertTrue(service.run())
        self.assertFalse(service.pull_due())  # just synced
        service._last_pull = time.monotonic() - 60
        self.assertTrue(service.pull_due())
        self.library.mark_played([ids["ep1.mp3"]])  # waits for the next full sync
        self.phone_played("ep-3", 600, 1200)
        posts = len([r for r in self.server.httpd.requests if r[0] == "POST"])
        self.assertTrue(service.pull())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 1200000)
        self.assertEqual(len([r for r in self.server.httpd.requests if r[0] == "POST"]), posts)
        self.assertEqual(len(self.db.sync_pending("action")), 1)
        self.assertFalse(service.pull_due())
        # Offline: playback does not wait for the server again for a while.
        import socket

        from omapod.config import Credentials

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
        creds = Credentials(Path(self.tmp) / "creds.json")
        creds.save({"sync": {**creds.load()["sync"], "server": closed}})
        service._last_pull = time.monotonic() - 60
        self.assertFalse(service.pull(timeout=2))
        self.assertFalse(service.pull_due())

    def test_played_positions_go_up_right_away(self):
        from omapod.events import bus

        events = []
        tokens = [bus.subscribe(topic, lambda topic=topic: events.append(topic))
                  for topic in ("sync-push", "sync-requested")]
        for token in tokens:
            self.addCleanup(bus.unsubscribe, token)
        service, ids = self.synced_library()
        self.assertTrue(service.push())  # nothing waiting
        events.clear()
        self.library.save_position(ids["ep-3"], 600000, listened_ms=600000)
        self.library.sync_episode_played(self.db.get_episode(ids["ep-3"]), 0, False)  # paused at 10:00
        self.library.delete_downloads([ids["ep-3"]])  # nothing downloaded: no action
        self.library.sync_action(self.db.get_episode(ids["ep-2"]), "download")
        self.assertEqual(events, ["sync-push", "sync-requested"])
        requests = len(self.server.httpd.requests)
        self.assertTrue(service.push())
        # Only the upload: nothing is asked for, the phone's next sync finds the position.
        self.assertEqual([r[0] for r in self.server.httpd.requests[requests:]], ["POST"])
        row = next(r for r in self.server.sync.nextcloud_actions if r["guid"] == "ep-3")
        self.assertEqual((row["started"], row["position"]), (0, 600))
        self.assertEqual(self.db.sync_pending("action"), [])
        # Asked for again by the next sync (it looks back a while), it is known as this computer's own.
        position = self.db.get_episode(ids["ep-3"]).position
        self.library.reset_position([ids["ep-3"]])
        self.assertTrue(service.run())
        self.assertEqual(self.db.get_episode(ids["ep-3"]).position, 0)
        self.assertEqual(position, 600000)
        self.settings.set("sync_provider", "")
        self.library.sync_action(self.db.get_episode(ids["ep-3"]), "play", 0, 1, 2)  # not logged in: not queued
        self.assertEqual(self.db.sync_pending("action"), [])

    def phone_action(self, kind, episode, seconds_ago, device=None):
        """A download or delete action of AntennaPod, as it reaches the server."""
        from datetime import datetime, timedelta, timezone

        stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        files = {"ep-3": ("ep3.mp3", "ep-3"), "ep-2": ("ep2.m4a", "ep-2"), "ep1.mp3": ("ep1.mp3", None)}
        name, guid = files[episode]
        action = {"podcast": self.base + "/feed.xml", "episode": f"{self.base}/{name}", "action": kind,
                  "timestamp": stamp, "started": -1, "position": -1, "total": -1}
        if guid:
            action["guid"] = guid
        if device:
            action["device"] = device
        self.server.sync.add_actions([action])

    def test_downloads_on_other_devices(self):
        service, ids = self.synced_library()
        # GPodder Sync up to 3.18 throws download reports away: nothing to list, and the settings say why.
        self.phone_action("download", "ep-3", 900)
        self.assertTrue(service.run())
        self.assertEqual(self.library.downloads_elsewhere(), [])
        self.assertFalse(self.settings.get("remote_downloads_seen"))
        # With the fix, the phone's downloads arrive; what it deleted again or heard to the end stays out.
        self.server.sync.nextcloud_keeps_other_actions = True
        self.phone_action("download", "ep-3", 600)
        self.phone_action("download", "ep-2", 500)
        self.phone_action("delete", "ep-2", 400)
        self.phone_action("download", "ep1.mp3", 300)
        self.phone_played("ep1.mp3", 200, 2710, total=2710)
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertTrue(self.settings.get("remote_downloads_seen"))
        self.assertEqual([e.id for e in self.library.downloads_elsewhere()], [ids["ep-3"]])
        # Downloaded here as well: no longer listed. Downloaded again on the phone: listed again.
        self.db.update_episode(ids["ep-3"], file_path="/tmp/ep3.mp3", downloaded_at=int(time.time()))
        self.phone_action("download", "ep-2", 100)
        self.assertTrue(service.run())
        self.assertEqual([e.id for e in self.library.downloads_elsewhere()], [ids["ep-2"]])
        # Asked for again (sync looks back a week), an older action does not undo a newer one.
        self.server.sync.add_actions([])
        self.assertTrue(service.run())
        self.assertEqual([e.id for e in self.library.downloads_elsewhere()], [ids["ep-2"]])

    def test_downloads_of_this_computer_are_not_listed_as_elsewhere(self):
        service, ids = self.synced_library("gpodder")
        # gpodder.net says which device an action came from.
        self.phone_action("download", "ep-3", 300, device="omapod-test")
        self.phone_action("download", "ep-2", 200, device="phone")
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertEqual([e.id for e in self.library.downloads_elsewhere()], [ids["ep-2"]])

    def test_downloads_from_other_devices_with_the_setting_on(self):
        class Downloads:
            def __init__(self):
                self.queued = []

            def enqueue(self, episode, manual=True):
                self.queued.append(episode.id)
                return True

            def active_ids(self):
                return set()

        service, ids = self.synced_library()
        self.library.downloads = Downloads()
        self.server.sync.nextcloud_keeps_other_actions = True
        self.phone_action("download", "ep-3", 600)
        self.assertTrue(service.run())
        self.assertEqual(self.library.downloads.queued, [])  # switched off: only listed
        self.settings.update({"sync_mirror_downloads": True, "mirror_downloads_ready": False})
        self.assertTrue(service.run())
        self.assertEqual(self.library.downloads.queued, [ids["ep-3"]])  # what is on the phone right now
        self.phone_action("download", "ep-2", 300)
        self.assertTrue(service.run())
        self.assertTrue(service.run())  # asked for again: followed once
        self.assertEqual(self.library.downloads.queued, [ids["ep-3"], ids["ep-2"]])

    def test_shared_file_is_not_overwritten_blindly(self):
        from omapod.sync.providers import Nextcloud

        cloud = Nextcloud(self.base, "user", "pass")
        self.assertEqual(cloud.get_shared("queue.json"), (None, ""))
        first = cloud.put_shared("queue.json", {"episodes": []})
        self.assertTrue(first)
        self.assertIsNone(cloud.put_shared("queue.json", {"episodes": []}))  # exists, but no ETag given
        second = cloud.put_shared("queue.json", {"episodes": [{"podcast": "p", "episode": "e"}]}, first)
        self.assertNotEqual(second, first)
        self.assertIsNone(cloud.put_shared("queue.json", {"episodes": []}, first))  # stale ETag
        self.assertEqual(cloud.get_shared("queue.json")[0]["episodes"][0]["episode"], "e")


if __name__ == "__main__":
    unittest.main()
