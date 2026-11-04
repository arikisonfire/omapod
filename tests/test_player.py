import json
import os
import shutil
import socket
import time
import unittest
from pathlib import Path

from gi.repository import GLib

from tests.test_library import FIX, make_env

os.environ.setdefault("OMAPOD_MPV_AO", "null")


def run_until(predicate, timeout=15.0):
    loop = GLib.MainLoop()
    deadline = time.monotonic() + timeout

    def check():
        if predicate() or time.monotonic() > deadline:
            loop.quit()
            return False
        return True

    GLib.timeout_add(50, check)
    loop.run()
    return predicate()


@unittest.skipUnless(shutil.which("mpv"), "mpv not installed")
class PlayerTest(unittest.TestCase):
    def setUp(self):
        from omapod.events import bus
        from omapod.player.controller import PlayerController
        from tests.fakeserver import FakeServer

        bus.attach_main_loop()
        self.tmp, self.settings, self.db, self.library = make_env()
        self.server = FakeServer(str(FIX / "tone.mp3")).__enter__()
        from omapod import config

        self.player = PlayerController(self.library, self.settings, config.paths)
        self.library.player = self.player
        self.feed_id = self.library.subscribe_url(self.server.base + "/feed.xml")
        self.episodes = {e.guid or e.title: e for e in self.db.get_episodes(feed_id=self.feed_id)}

    def tearDown(self):
        self.player.shutdown()
        self.server.__exit__(None, None, None)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stream_play_pause_seek_and_continue(self):
        ep3, ep2 = self.episodes["ep-3"], self.episodes["ep-2"]
        self.library.add_to_queue([ep3.id, ep2.id])
        self.assertTrue(self.player.play_episode(ep3.id))
        self.assertTrue(run_until(lambda: self.player.status == "playing" and self.player.position > 1200))
        self.assertEqual(self.player.duration // 1000, 10)
        self.assertTrue(run_until(lambda: [c["title"] for c in self.player.chapters] == ["Intro", "Topic", "Outro"]),
                        self.player.chapters)
        self.player.pause()
        self.assertEqual(self.player.status, "paused")
        saved = self.db.get_episode(ep3.id).position
        self.assertGreater(saved, 1000)
        self.player.seek(8000)
        self.player.set_speed(2.0)
        self.assertEqual(self.settings.get("speed"), 2.0)
        self.player.play()
        # Ends -> marked played, removed from queue, next queued episode starts.
        self.assertTrue(run_until(lambda: self.player.episode is not None and self.player.episode.id == ep2.id
                                  and self.player.status == "playing", timeout=20))
        done = self.db.get_episode(ep3.id)
        self.assertEqual(done.state, 1)
        self.assertGreater(done.completed_at, 0)
        self.assertNotIn(ep3.id, self.library.queue_ids())
        self.assertGreater(done.played_duration, 1000)

    def test_skip_with_last_episode_stops(self):
        # The test file is 10 s long, so the default 'smart mark as played' (30 s) would count any skip as played.
        self.settings.set("smart_mark_played_secs", 0)
        ep2 = self.episodes["ep-2"]
        self.library.add_to_queue([ep2.id])
        self.player.play_episode(ep2.id)
        self.assertTrue(run_until(lambda: self.player.status == "playing"))
        self.player.skip()
        self.assertEqual(self.player.status, "idle")
        self.assertIsNone(self.player.episode)
        self.assertEqual(self.db.get_episode(ep2.id).state, 0)  # skip keeps the episode by default

    def test_sleep_timer_and_skip_silence(self):
        ep1 = self.episodes["Episode 1"]
        self.player.set_skip_silence(True)
        self.player.play_episode(ep1.id)
        self.assertTrue(run_until(lambda: self.player.status == "playing"))
        self.player.set_sleep_timer(minutes=0.02)  # 1.2 s
        self.assertTrue(run_until(lambda: self.player.status == "paused", timeout=10))
        self.assertIsNone(self.player.sleep)

    def test_restore_last(self):
        from omapod import config
        from omapod.player.controller import PlayerController

        ep3 = self.episodes["ep-3"]
        self.player.play_episode(ep3.id)
        self.assertTrue(run_until(lambda: self.player.position > 1500))
        self.player.pause()
        other = PlayerController(self.library, self.settings, config.paths)
        other.restore_last()
        self.assertEqual(other.episode.id, ep3.id)
        self.assertEqual(other.status, "paused")
        self.assertGreater(other.position, 1000)

    def test_waits_for_other_devices_before_playing(self):
        ep3 = self.episodes["ep-3"]
        self.player.play_episode(ep3.id, start=False)  # loaded and paused, like after a restart
        self.assertTrue(run_until(lambda: self.player.loaded))
        asked = []

        def sync_check(done, follow):
            # What a pull does: the phone got further in the meantime.
            asked.append(follow)
            self.db.update_episode(ep3.id, position=5000)
            GLib.timeout_add(300, lambda: (self.player.adopt_remote(ep3.id, 5000, "heard"), done(), False)[2])
            return True

        self.player.sync_check = sync_check
        self.assertTrue(self.player.play())
        self.assertEqual(self.player.status, "loading")
        self.assertFalse(self.player.plays_here())
        self.assertTrue(run_until(lambda: self.player.status == "playing" and self.player.position >= 5000))
        self.assertEqual(asked, [True])
        # Pausing while it waits: nothing starts afterwards.
        self.player.pause()
        self.player.sync_check = lambda done, follow: bool(GLib.timeout_add(300, lambda: (done(), False)[1]))
        self.player.play()
        self.player.pause()
        run_until(lambda: False, timeout=1.0)
        self.assertEqual(self.player.status, "paused")

    def test_picks_up_the_episode_another_device_played(self):
        ep3, ep2 = self.episodes["ep-3"], self.episodes["ep-2"]
        self.player.play_episode(ep3.id, start=False)
        self.assertTrue(run_until(lambda: self.player.loaded))
        self.db.update_episode(ep2.id, position=4000)

        def sync_check(done, follow):
            GLib.timeout_add(200, lambda: (self.player.follow_remote(ep2.id), done(), False)[2])
            return True

        self.player.sync_check = sync_check
        self.player.play()
        self.assertTrue(run_until(lambda: self.player.episode.id == ep2.id and self.player.status == "playing"
                                  and self.player.position >= 4000))
        self.assertEqual(self.db.get_meta("current_episode"), str(ep2.id))
        # While it plays here, other devices change nothing.
        self.assertFalse(self.player.follow_remote(ep3.id))
        self.assertFalse(self.player.adopt_remote(ep2.id, 1000, "heard"))
        # Paused, then finished on the other device: the player lets it go.
        self.player.pause()
        self.assertTrue(self.player.adopt_remote(ep2.id, 0, "finished"))
        self.assertEqual(self.player.status, "idle")
        self.assertIsNone(self.player.episode)

    def test_control_server_presence_and_bye(self):
        from omapod.player.ipc import ControlServer

        class App:
            def window_visible(self):
                return False

        class Images:
            def thumb_if_cached(self, url, size):
                return None

            def request(self, url, size, callback):
                pass

        path = os.path.join(self.tmp, "run", "control.sock")
        presence = os.path.join(self.tmp, "run", "app.json")
        server = ControlServer(App(), self.player, self.library, Images(), path)
        self.assertTrue(server.start())
        with open(presence, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["socket"], path)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        client.setblocking(False)
        received = []

        def got_state():
            try:
                received.append(client.recv(65536))
            except BlockingIOError:
                pass
            return b'"type": "state"' in b"".join(received)

        self.assertTrue(run_until(got_state, timeout=5))
        server.stop()
        self.assertFalse(os.path.exists(presence))
        self.assertFalse(os.path.exists(path))
        # The goodbye arrives before the connection closes.
        client.setblocking(True)
        client.settimeout(2)
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            received.append(chunk)
        client.close()
        last = b"".join(received).strip().split(b"\n")[-1]
        self.assertEqual(json.loads(last), {"type": "bye"})


if __name__ == "__main__":
    unittest.main()
