"""What servers, other programs and other accounts get to see or decide, beyond a podcast's login (test_logins).

Found by looking at omaPOD the way the plugin marketplace's review does (review of 0.1.4): what an imported
library can send, where a login could travel in plain text, what follows a redirect, what a slow or odd server
can hold up, what reaches the desktop shell and other programs.

The last test is a census: every place in the code that reaches outside the process (network, programs, files,
secrets) is listed in tests/census.txt. A change that adds one fails the test until it has been looked at.
"""

import ast
import importlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_hardening import Servers
from tests.test_library import FIX, make_env

ROOT = Path(__file__).resolve().parent.parent


class ImportedLibraries(Servers):
    def sync(self, server):
        from omapod.config import Credentials
        from omapod.sync.service import SyncService

        creds = Credentials(Path(self.tmp) / "creds.json")
        creds.save({"sync": {"provider": "nextcloud", "server": server.base, "username": "user", "password": "pass"}})
        self.settings.set("sync_provider", "nextcloud")
        service = SyncService(self.library, self.settings, creds)
        self.addCleanup(service.close)
        return service

    def test_an_imported_library_brings_nothing_for_the_sync_server(self):
        """What a library has waiting for its sync server was imported with it and sent from here: a backup from
        someone else could unsubscribe podcasts on the account's other devices (which delete them, downloads and
        all) and move positions there."""
        from omapod import __main__ as main
        from omapod import config
        from omapod.core.library import Library
        from omapod.db import Database
        from omapod.ui import preferences
        from tests.fakeserver import FakeServer

        with FakeServer(str(FIX / "tone.mp3")) as server:
            service = self.sync(server)
            address = server.base + "/feed.xml"
            self.library.subscribe_url(address)
            self.assertTrue(service.run())
            self.assertTrue(server.sync.subscriptions[address][0])
            backup = os.path.join(self.tmp, "backup.db")
            with open(backup, "wb") as handle:
                self.db.write_snapshot(handle)
            conn = sqlite3.connect(backup)
            conn.execute("INSERT INTO sync_queue(kind, payload, created) VALUES('sub', ?, 1)",
                         (json.dumps({"url": address, "action": "remove"}),))
            conn.execute("INSERT INTO sync_queue(kind, payload, created) VALUES('action', ?, 1)",
                         (json.dumps({"podcast": address, "episode": server.base + "/ep3.mp3", "guid": "ep-3",
                                      "action": "play", "timestamp": "2099-01-01T00:00:00", "started": 0,
                                      "position": 3723, "total": 3723}),))
            conn.execute("INSERT INTO sync_seen(key, ts) VALUES('guid:ep-2', 4102444800)")
            conn.execute("INSERT INTO remote_downloads(episode, downloaded, ts) VALUES(?, 1, 5)",
                         (server.base + "/ep2.m4a",))
            conn.commit()
            conn.close()
            self.settings.update({"queue_sync_base": "abc", "queue_sync_local": "abc", "queue_sync_changed": 5,
                                  "favorites_sync_base": [["a", "b"]], "favorites_sync_local": [["a", "b"]]})
            self.assertIsNone(preferences.stage_import(backup))
            self.db.close()
            main._apply_pending_import()
            self.db = Database(config.paths.db)
            self.library = Library(self.db, self.settings)
            service.library, service.db = self.library, self.db
            for table in ("sync_queue", "sync_seen", "remote_downloads"):
                self.assertEqual(self.db.scalar(f"SELECT COUNT(*) FROM {table}"), 0, table)
            # Queue and favorites of the imported library are not changes made here: the next sync combines.
            self.settings.load()
            for name in ("queue_sync_base", "queue_sync_local", "favorites_sync_base", "favorites_sync_local"):
                self.assertFalse(self.settings.get(name), name)
            self.assertTrue(service.run())
            self.assertTrue(server.sync.subscriptions[address][0])
            self.assertEqual(server.sync.nextcloud_actions, [])


class ForeignStructure(unittest.TestCase):
    """An imported library brought its own structure along: column types, CHECK constraints, defaults, extra or
    missing columns. With them, a backup decided what omaPOD could later write (a constraint that refuses every
    new position), and values of the wrong type or text that is no UTF-8 stopped the pages that met them."""

    HOSTILE = """
    CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT, title BLOB, state TEXT CHECK (state != 'x'),
        speed REAL, funding TEXT, tags TEXT, evil TEXT DEFAULT 'evil', keep_updated INTEGER CHECK (keep_updated = 1));
    CREATE TABLE episodes (id INTEGER PRIMARY KEY, feed_id INTEGER, guid TEXT, title BLOB, media_url TEXT,
        position INTEGER CHECK (position = 0), duration TEXT, pub_date INTEGER, psc_chapters TEXT, favorite TEXT);
    CREATE TABLE queue (episode_id INTEGER, pos REAL);
    CREATE TABLE chapters (episode_id INTEGER, start_ms INTEGER, title TEXT);
    CREATE TABLE listen_log (day TEXT, episode_id INTEGER, feed_id INTEGER, ms INTEGER);
    CREATE TABLE meta (key TEXT, value TEXT);
    CREATE INDEX odd ON episodes (title COLLATE NOCASE, position DESC);
    """

    def setUp(self):
        self.tmp, self.settings, self.db, self.library = make_env()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def hostile_backup(self):
        path = os.path.join(self.tmp, "hostile.db")
        conn = sqlite3.connect(path)
        conn.executescript(self.HOSTILE)
        rows = {
            "feeds": [(1, "https://podcast.example/feed.xml", b"Caf\xe9 \xff", "1", 1e308, '[1, {"url": "https://p.example/pay"}]',
                       '["news", 5]', "x", 1),
                      (2, None, b"no address", "1", 1.0, "[]", "[]", "", 1),
                      (3, "https://podcast.example/feed.xml", b"the same address", "1", 1.0, "[]", "[]", "", 1)],
            "episodes": [(1, 1, "g1", b"\xc3\x28 broken", "https://podcast.example/1.mp3", 0, "long", 10 ** 15,
                          '[{"start": 1000, "title": "Intro"}, {"start": "x"}, 7]', "yes"),
                         (2, 99, "g2", b"of no podcast", "https://podcast.example/2.mp3", 0, "5", 0, "[]", "0")],
            "queue": [(1, 0.0), (2, 1.0), (55, 2.0)],
            "chapters": [(1, 0, "One"), (77, 0, "of no episode")],
            "listen_log": [("2026-11-01", 1, 1, 5000), ("garbage", 1, 1, 2 ** 62)],
            "meta": [("current_episode", "1")],
        }
        for table, values in rows.items():
            marks = ",".join("?" * len(values[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", values)
        conn.commit()
        conn.close()
        return path

    def test_an_imported_library_brings_its_rows_never_its_structure(self):
        from omapod import __main__ as main
        from omapod import config
        from omapod.core.library import Library
        from omapod.db import SCHEMA, Database
        from omapod.ui import preferences
        from omapod.util import fmt_date, fmt_datetime

        self.assertIsNone(preferences.stage_import(self.hostile_backup()))
        self.db.close()
        main._apply_pending_import()
        self.db = Database(config.paths.db)
        self.library = Library(self.db, self.settings)
        # Exactly omaPOD's own structure.
        reference = sqlite3.connect(":memory:")
        reference.executescript(SCHEMA)
        expected = sorted(tuple(row) for row in reference.execute("SELECT type, name, sql FROM sqlite_master"))
        self.assertEqual(sorted(tuple(row) for row in self.db.query("SELECT type, name, sql FROM sqlite_master")),
                         expected)
        # Rows that fit, each value of its column's type.
        (feed,) = self.db.get_feeds(with_counts=False)
        self.assertEqual((feed.url, feed.title, feed.state), ("https://podcast.example/feed.xml", "Caf\ufffd \ufffd", 1))
        self.assertEqual(feed.speed, float(2 ** 53))
        self.assertEqual(feed.funding, [{"url": "https://p.example/pay"}])
        self.assertEqual(feed.tags, ["news"])
        (episode,) = self.db.get_episodes(feed_id=feed.id)
        self.assertEqual((episode.title, episode.duration, episode.favorite), ("\ufffd( broken", 0, 0))
        self.assertEqual(self.db.get_episode(episode.id).psc_chapters, [{"start": 1000, "title": "Intro"}])
        self.assertEqual(self.db.queue_ids(), [episode.id])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM chapters"), 1)
        self.assertEqual([tuple(row) for row in self.db.query("SELECT day, ms FROM listen_log")], [("2026-11-01", 5000)])
        # What the backup's constraints refused, omaPOD can write; what no calendar has is not shown.
        self.library.save_position(episode.id, 5000, 60000, 1000)
        self.assertEqual(self.db.get_episode(episode.id).position, 5000)
        self.assertEqual((fmt_date(episode.pub_date), fmt_datetime(episode.pub_date)), ("", ""))
        self.library.update_feed_settings(feed.id, keep_updated=0)
        self.library.statistics()
        self.library.monthly_statistics()

    def test_lists_from_feeds_and_media_files_have_a_length(self):
        from omapod.feeds import chapters, parser

        many = [{"startTime": index, "title": f"Chapter {index}"} for index in range(5000)]
        self.assertEqual(len(chapters.parse_json_chapters(json.dumps({"chapters": many}))), chapters.MAX_CHAPTERS)
        self.assertEqual(len(chapters.from_mpv([{"time": index, "title": "x"} for index in range(5000)])),
                         chapters.MAX_CHAPTERS)
        self.assertIsNone(chapters.from_mpv("not a list"))
        psc = "".join(f'<psc:chapter start="{index}" title="c"/>' for index in range(5000))
        feed = (f"<rss version='2.0' xmlns:psc='http://podlove.org/simple-chapters'><channel><title>t</title>"
                f"<item><title>i</title><guid>g</guid><enclosure url='https://p.example/1.mp3' type='audio/mpeg'/>"
                f"<psc:chapters>{psc}</psc:chapters></item>"
                + "".join(f"<item><title>{index}</title><guid>{index}</guid></item>" for index in range(30))
                + "</channel></rss>").encode()
        with mock.patch.object(parser, "MAX_ITEMS", 20):
            parsed = parser.parse_feed(feed, "https://p.example/feed.xml")
        self.assertEqual(len(parsed.items), 20)
        self.assertEqual(len(parsed.items[0].psc_chapters), chapters.MAX_CHAPTERS)
        self.assertEqual(len(chapters.merge([{"start": i} for i in range(3000)], None)), chapters.MAX_CHAPTERS)


class SyncLogins(Servers):
    def test_an_address_without_a_scheme_never_means_http(self):
        """'host:80' was read the way AntennaPod reads it, as http://host: the Nextcloud login and a gpodder.net
        password went out in plain text without the question SECURITY.md promises."""
        from omapod.sync.providers import GpodderNet, NextcloudLoginFlow, PlainHttpOnly, normalize_host

        for typed, server in (("cloud.example.com:80", "https://cloud.example.com:80"),
                              ("cloud.example.com", "https://cloud.example.com"),
                              ("cloud.example.com:8080/nc/", "https://cloud.example.com:8080/nc"),
                              ("HTTPS://cloud.example.com:443", "https://cloud.example.com"),
                              ("http://cloud.example.com:80", "http://cloud.example.com"),  # typed: the user's choice
                              ("someone:word@cloud.example.com", "https://cloud.example.com")):
            self.assertEqual(normalize_host(typed), server)
        self.assertEqual(GpodderNet("sync.example.com:80", "u", "p", "d").base, "https://sync.example.com:80")
        # A Nextcloud that answers only without TLS at the address typed without a scheme: the user is asked.
        from tests.fakeserver import FakeServer

        with FakeServer(str(FIX / "tone.mp3")) as plain:
            address = plain.base.removeprefix("http://")
            flow = NextcloudLoginFlow(address)
            self.assertEqual((flow.server, flow.http_fallback), ("https://" + address, True))
            with self.assertRaises(PlainHttpOnly) as asked:
                flow.start()
            self.assertEqual(asked.exception.server, plain.base)
            self.assertEqual((flow.token, flow.endpoint), ("", ""))  # nothing was kept from the answer over http

    def test_a_server_stored_with_http_moves_to_https_when_it_answers_there(self):
        """Up to 0.1.1 the address a Nextcloud named for itself was stored after the login: behind a proxy that
        ends TLS that is http://, though the user typed https://, and every sync sent the app password in plain
        text. Nothing rechecked a login that was stored that way."""
        from omapod.config import Credentials
        from omapod.sync import service as sync_service
        from omapod.sync.providers import https_upgrade
        from tests.fakeserver import FakeServer

        for stored, https in (("http://cloud.example.com", "https://cloud.example.com"),
                              ("http://cloud.example.com:80/nc/", "https://cloud.example.com/nc"),
                              ("http://cloud.example.com:8080", ""), ("https://cloud.example.com", ""),
                              ("http://[::1]", "https://[::1]"), ("http://host:x", ""), ("", "")):
            self.assertEqual(https_upgrade(stored), https, stored)
        tls = self.trusted_tls()
        with FakeServer(str(FIX / "tone.mp3")) as plain, FakeServer(str(FIX / "tone.mp3"), tls=tls) as secure:
            creds = Credentials(Path(self.tmp) / "creds.json")
            creds.save({"sync": {"provider": "nextcloud", "server": plain.base, "username": "user",
                                 "password": "pass"}})
            self.settings.set("sync_provider", "nextcloud")
            service = sync_service.SyncService(self.library, self.settings, creds)
            self.addCleanup(service.close)
            tried = []
            # Nothing answers over https: the login stays as it is, and is not tried again and again.
            with mock.patch.object(sync_service, "https_upgrade", lambda server: tried.append(server) or
                                   "https://127.0.0.1:1"):
                self.assertTrue(service.run(), self.settings.get("sync_last_error"))
                self.assertTrue(service.run())
            self.assertEqual(creds.get("sync")["server"], plain.base)
            self.assertTrue(any(method == "GET" for method, _path in plain.httpd.requests))
            # It does answer there, with a certificate the system trusts: from then on the login uses https.
            service._https_tried = ""
            plain.httpd.requests.clear()
            with mock.patch.object(sync_service, "https_upgrade", lambda server: secure.base):
                self.assertTrue(service.run(), self.settings.get("sync_last_error"))
            self.assertEqual(creds.get("sync")["server"], secure.base)
            self.assertEqual(plain.httpd.requests, [])
            self.assertEqual(service.provider().base, secure.base)

    def test_the_account_is_named_before_anything_is_sent(self):
        """The login page's address goes to the browser as a program argument, which other accounts on the
        computer can read. Whoever opens it first and logs in decides whose account omaPOD gets."""
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gtk

        if Gdk.Display.get_default() is None:
            self.skipTest("needs a display, e.g. xvfb-run")
        Adw.init()
        from omapod.ui.dialogs import ask_sync_account

        answers = []
        window = Gtk.Window()
        for response in ("ok", "cancel"):
            dialog = ask_sync_account(window, "someone <b>else</b>", "cloud.example.com",
                                      lambda: answers.append("yes"), lambda: answers.append("no"))
            self.assertIn("someone <b>else</b>", dialog.get_body())
            self.assertIn("cloud.example.com", dialog.get_body())
            self.assertFalse(dialog.get_body_use_markup())
            dialog.emit("response", response)
            dialog.emit("response", "cancel")  # closing after an answer: no second one, no revoked login
        self.assertEqual(answers, ["yes", "no"])
        # The settings ask before they keep a login (and nothing else calls _finish_login for a Nextcloud).
        source = (ROOT / "omapod" / "ui" / "preferences.py").read_text("utf-8")
        self.assertEqual(source.count('self._finish_login("nextcloud"'), 1)
        self.assertIn("ask_sync_account(self, login, host_of(server)", source)

    def test_a_login_not_gone_through_with_is_given_back(self):
        from omapod.sync.providers import Nextcloud
        from tests.fakeserver import FakeServer

        with FakeServer(str(FIX / "tone.mp3")) as server:
            self.assertTrue(Nextcloud(server.base, "user", "pass").revoke())
            self.assertTrue(server.sync.revoked)
            self.assertFalse(Nextcloud(server.base, "user", "wrong").revoke())


class Redirects(Servers):
    def test_a_redirect_to_another_server_takes_only_harmless_headers_along(self):
        """urllib copies every header of a request to wherever a redirect points. The Podcast Index key is such
        a header; so would be anything a later change adds for one server."""
        from omapod.net import http

        first, second = self.host("127.0.0.1"), self.host("localhost")
        first.routes["/api"] = (302, {"Location": second.base + "/landed"}, b"")
        first.routes["/same"] = (302, {"Location": first.base + "/landed"}, b"")
        first.routes["/landed"] = second.routes["/landed"] = (200, {}, b"{}")
        headers = {"X-Auth-Key": "index-key", "X-Auth-Date": "1", "OCS-APIRequest": "true",
                   "Accept": "application/json", "If-None-Match": '"tag"'}
        http.request(first.base + "/api", headers=headers)
        landed = dict(second.requests)["/landed"]
        self.assertEqual([landed.get(name) for name in ("X-Auth-Key", "X-Auth-Date", "OCS-APIRequest")], [None] * 3)
        self.assertEqual((landed.get("Accept"), landed.get("If-None-Match")), ("application/json", '"tag"'))
        self.assertIn("omaPOD", landed.get("User-Agent"))
        http.request(first.base + "/same", headers=headers)  # the same server keeps what was meant for it
        self.assertEqual(dict(first.requests)["/landed"].get("X-Auth-Key"), "index-key")

    def test_an_address_whose_port_is_no_number_is_nobodys_server(self):
        """Python reads no port out of 'host:8443x' and omaPOD took the default one for it; ffmpeg, which mpv
        streams with, reads 8443. So a stream there counted as the podcast's own server and got its login."""
        from omapod.net.http import auth_allowed

        for target in ("https://a.example:8443x/ep.mp3", "https://a.example:x/ep.mp3", "https://[a.example/ep.mp3"):
            self.assertFalse(auth_allowed("https://a.example/feed", target), target)
        self.assertFalse(auth_allowed("https://a.example:x/feed", "https://a.example:x/feed"))


class Dribbler:
    """A server that sends its answer a few bytes at a time and never finishes."""

    def __init__(self, slow_head=False, every=0.05):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.slow_head, self.every, self.open = slow_head, every, True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.open:
            try:
                conn, _address = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn):
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\nX-Padding: " + b"x" * 400 + b"\r\n\r\n"
        try:
            conn.recv(65536)
            if self.slow_head:
                for index in range(len(head)):
                    conn.sendall(head[index:index + 1])
                    time.sleep(self.every)
            else:
                conn.sendall(head)
            while self.open:
                conn.sendall(b"<!-- -->")
                time.sleep(self.every)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.open = False
        self.sock.close()


class SlowServers(Servers):
    def test_a_server_that_dribbles_cannot_hold_a_request(self):
        """Each wait for the next bytes had a time limit, the request as a whole had none: one podcast whose
        server sends a byte now and then kept the refresh of all podcasts from ever finishing."""
        from omapod.net import http

        for slow_head in (False, True):
            server = Dribbler(slow_head)
            self.addCleanup(server.close)
            started = time.monotonic()
            with self.assertRaises(http.HttpError) as caught:
                http.request(server.base + "/feed.xml", timeout=2, total=1)
            self.assertLess(time.monotonic() - started, 4)
            self.assertIn("too long", caught.exception.detail)
        self.assertEqual(http.total_time(30), 300)
        # A refresh of such a podcast ends with an error instead of holding the others.
        server = Dribbler()
        self.addCleanup(server.close)
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": server.base + "/feed.xml", "title": "Slow", "state": 1})
        with mock.patch.object(http, "total_time", lambda _timeout: 1):
            started = time.monotonic()
            result = self.library.refresh_feed(feed_id)
        self.assertTrue(result.error)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_download_that_dribbles_is_given_up(self):
        from omapod.models import FEED_SUBSCRIBED
        from omapod.net import downloads

        host = self.host()

        def dribble(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "audio/mpeg")
            handler.send_header("Content-Length", "100000000")
            handler.end_headers()
            try:
                while True:
                    handler.wfile.write(b"0123456789")
                    handler.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

        host.routes["/ep.mp3"] = dribble
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": host.base + "/feed.xml", "title": "Podcast",
                                                 "state": FEED_SUBSCRIBED})
            episode_id = self.db.insert_episode(conn, {"feed_id": feed_id, "guid": "g", "title": "Episode",
                                                       "media_url": host.base + "/ep.mp3"})
        manager = downloads.DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        self.addCleanup(manager.stop)
        with mock.patch.object(downloads, "SLOW_SECONDS", 0.5), mock.patch.object(downloads, "SLOW_BYTES", 100000):
            manager.start()
            self.library.download([episode_id])
            deadline = time.monotonic() + 10
            while manager.active_ids() and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual(manager.active_ids(), set())  # the worker is free again
        entry = self.db.get_log(1)[0]
        self.assertFalse(entry["success"])
        self.assertIn("less than", entry["detail"])
        folder = self.library.feed_folder(self.db.get_feed(feed_id))
        self.assertEqual([p.name for p in folder.iterdir()], [f".{episode_id}.part"])  # kept, to go on later


class Playback(Servers):
    @unittest.skipUnless(shutil.which("mpv"), "mpv not installed")
    def test_the_player_takes_media_files_not_playlists(self):
        """mpv reads an answer that is a playlist as one, however long, and goes on to play the addresses in it:
        a podcast's media host could fill the player's memory and have it fetch whatever it lists."""
        from omapod.player.mpv import Mpv

        host = self.host()
        entries = "".join(f"{host.base}/entry{index}.mp3\n" for index in range(2000))
        host.routes["/episode.mp3"] = (200, {"Content-Type": "audio/x-mpegurl"}, ("#EXTM3U\n" + entries).encode())
        host.routes["/real.mp3"] = (200, {"Content-Type": "audio/mpeg"}, (FIX / "tone.mp3").read_bytes())
        events = []
        with mock.patch.dict(os.environ, {"OMAPOD_MPV_AO": "null"}):
            mpv = Mpv(os.path.join(self.tmp, "mpv.sock"), events.append, log_path=os.path.join(self.tmp, "mpv.log"))
            mpv.start()
        self.addCleanup(mpv.quit)

        def wait(name):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                found = [event for event in events if event.get("event") == name]
                if found:
                    return found[-1]
                time.sleep(0.05)
            self.fail(f"mpv sent no {name}: {events}")

        mpv.loadfile(host.base + "/episode.mp3")
        self.assertEqual(wait("end-file").get("reason"), "error")
        self.assertEqual(mpv.get("playlist-count"), 1)
        self.assertEqual([path for path, _auth in host.seen if path.startswith("/entry")], [])
        events.clear()
        mpv.loadfile(host.base + "/real.mp3")
        wait("file-loaded")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, "mpv.log")).st_mode), 0o600)


class OtherPrograms(Servers):
    def test_a_mail_link_keeps_whom_to_write_to_and_the_subject(self):
        """Mail programs take more from a mailto: link than an address: a text, further recipients, some of them
        files to attach. Show notes are a feed's text."""
        from types import SimpleNamespace

        from omapod.ui import app as app_module
        from omapod.util import mail_link

        for link, expected in (
                ("mailto:show@example.org", "mailto:show@example.org"),
                ("mailto:show@example.org?subject=Hello%20there&body=x&attach=/etc/passwd&bcc=other@example.org",
                 "mailto:show@example.org?subject=Hello%20there"),
                ("MAILTO:a@example.org,b@example.org?Subject=x", "mailto:a@example.org,b@example.org?subject=x"),
                ("mailto:show@example.org%0D%0ABcc:other@example.org", "mailto:show@example.orgBcc%3Aother@example.org"),
                ("mailto:?attach=~/.ssh/id_ed25519", "mailto:")):
            self.assertEqual(mail_link(link), expected)
        opened = []
        fake = SimpleNamespace(window=None, _fallback_open=lambda uri: False)
        with mock.patch.object(app_module.Gio.AppInfo, "launch_default_for_uri",
                               lambda uri, _context: opened.append(uri) or True), \
                mock.patch.object(app_module.Gdk.Display, "get_default", lambda: None):
            self.assertTrue(app_module.OmaPodApp.open_uri(fake, " mailto:show@example.org?attach=/etc/passwd"))
            self.assertTrue(app_module.OmaPodApp.open_uri(fake, " https://example.org/page "))
        self.assertEqual(opened, ["mailto:show@example.org", "https://example.org/page"])

    def test_the_bar_widget_gets_names_and_a_thumbnail_and_nothing_long(self):
        """The widget runs inside the desktop shell, which reads every line omaPOD sends as a whole. A feed can
        make an address as long as it likes, and the address of a private feed's episode carries its token."""
        from types import SimpleNamespace

        from omapod import config
        from omapod.player.controller import PlayerController
        from omapod.player.ipc import ControlServer

        long = "x" * 100000
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": "https://podcast.example/feed.xml", "title": "T" + long,
                                                 "author": "A" + long, "image_url": "https://podcast.example/" + long})
            episode_id = self.db.insert_episode(conn, {
                "feed_id": feed_id, "guid": "g", "title": "E" + long, "link": "https://podcast.example/" + long,
                "media_url": "https://podcast.example/ep.mp3?token=" + long, "image_url": "https://x.example/" + long})
            self.db.set_queue_order(conn, [episode_id])
        player = PlayerController(self.library, self.settings, config.paths)
        player.episode, player.feed = self.db.get_episode(episode_id), self.db.get_feed(feed_id)
        player.error = "failed " + long
        player.chapters, player.chapter_index = [{"start": 0, "title": "C" + long}], 0
        images = SimpleNamespace(thumb_if_cached=lambda *_a: "/cache/thumb.png", request=lambda *_a: None)
        control = ControlServer(SimpleNamespace(), player, self.library, images, Path(self.tmp) / "control.sock")
        state = control.snapshot()
        line = json.dumps(state)
        self.assertLess(len(line), 8000)
        self.assertEqual(set(state["episode"]), {"id", "feed_id", "title", "podcast", "author", "is_video",
                                                 "downloaded", "pub_date", "favorite", "cover"})
        self.assertEqual(state["episode"]["cover"], "file:///cache/thumb.png")
        self.assertNotIn("https://", line)
        sent = []
        control.clients = [object()]
        control._send = lambda _client, message: sent.append(message)
        control._last_position_push = 0
        control._on_position(1000, 2000)
        self.assertLess(len(json.dumps(sent)), 2000)

    def test_the_widget_shows_text_as_it_is_and_takes_no_typing(self):
        """What the review looks for in every widget: text a feed chose shown as markup (which can load pictures
        from anywhere), pictures loaded from addresses, and fields to type into, which read the clipboard."""
        source = (ROOT / "plugin" / "BarWidget.qml").read_text("utf-8")
        lines = source.split("\n")
        for number, line in enumerate(lines):
            if re.match(r"\s*Text \{\s*$", line):
                indent = len(line) - len(line.lstrip())
                end = next(i for i in range(number + 1, len(lines))
                           if lines[i].strip() == "}" and len(lines[i]) - len(lines[i].lstrip()) == indent)
                self.assertIn("textFormat: Text.PlainText", "\n".join(lines[number:end]), f"line {number + 1}")
        for kind in ("TextField", "TextInput", "TextEdit", "TextArea"):
            self.assertNotRegex(source, rf"\b{kind}\b")
        sources = re.findall(r"^\s*source:\s*(.+)$", source, re.M)
        self.assertTrue(sources)
        for value in sources:  # only the thumbnails omaPOD made
            self.assertRegex(value, r"\.cover\b")
        self.assertIn("if (line.length > 65536) return", source)


class Search(Servers):
    def test_only_apples_own_hosts_are_asked_for_a_feed_address(self):
        from omapod.net import http, search

        asked = []
        answer = {"results": [{"feedUrl": "https://feeds.example.org/show.xml"}]}
        with mock.patch.object(http, "get_json", lambda url, **_kwargs: asked.append(url) or answer):
            for address in ("https://private.example/feeds/podcasts.apple.com/id123.xml",
                            "https://private.example/itunes.apple.com/lookup?id=5",
                            "https://user:word@private.example/feed?from=podcasts.apple.com&id1"):
                self.assertEqual(search.resolve_feed_url(address), address)
            self.assertEqual(asked, [])
            self.assertEqual(search.resolve_feed_url("https://podcasts.apple.com/us/podcast/a-show/id123456"),
                             "https://feeds.example.org/show.xml")
            self.assertEqual(search.resolve_feed_url("https://itunes.apple.com/lookup?id=7"),
                             "https://feeds.example.org/show.xml")
        self.assertEqual(asked, ["https://itunes.apple.com/lookup?id=123456", "https://itunes.apple.com/lookup?id=7"])


class PrivateFolders(unittest.TestCase):
    def test_folders_are_private_from_the_moment_they_exist(self):
        """They were made with the usual permissions and closed a moment later."""
        from omapod import config

        tmp = tempfile.mkdtemp(prefix="omapod-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"OMAPOD_HOME": os.path.join(tmp, "home"),
                                          "OMAPOD_RUNTIME_DIR": os.path.join(tmp, "run")}):
            importlib.reload(config)
            self.addCleanup(importlib.reload, config)
            old = os.umask(0)
            try:
                with mock.patch.object(os, "chmod", lambda *_args, **_kwargs: None):
                    config.paths.ensure()
            finally:
                os.umask(old)
            for path in (config.paths.config, config.paths.data, config.paths.cache, config.paths.runtime):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)


# ---------------------------------------------------------------- census

# Calls that reach outside the process, by what they reach. A name ending in a dot is a prefix.
REACHES = {
    "program": ("subprocess.", "os.exec", "os.spawn", "os.system", "os.popen", "os.kill", "os.fork", "pty.",
                "Gio.AppInfo.launch", "Gio.Subprocess", "GLib.spawn", "Gtk.UriLauncher", "Gtk.show_uri",
                "webbrowser."),
    "code": ("eval", "exec", "compile", "__import__", "pickle.", "marshal.", "shelve.", "importlib.import_module",
             "ctypes."),
    "network": ("urllib.request.", "http.client.", "socket.", "ssl.", "ftplib.", "smtplib.", "telnetlib."),
    # omaPOD's own ways to the network: every caller says where a request goes and which login it takes along.
    "request": ("http.request", "http.open_url", "http.get_json", "self.fetch", "self._request",
                "self._dav_request", "self.images.request", "images.request", "search.search",
                "search.top_podcasts", "resolve_feed_url"),
    "sync": ("provider.upload_subscription_changes", "provider.upload_episode_actions", "provider.put_shared",
             "self.db.sync_enqueue", "self._upload_actions"),
    "file": ("os.unlink", "os.remove", "os.rmdir", "os.rename", "os.replace", "os.truncate", "os.ftruncate",
             "os.link", "os.symlink", "os.chmod", "os.fchmod", "os.chown", "os.mkdir", "os.makedirs", "os.open",
             "os.utime", "shutil.", "tempfile.", "sqlite3.connect", "move_to_trash", "open"),
    "bus": ("Gio.bus_own_name", "connection.register_object", "self.connection.emit_signal",
            "self.send_notification", "Gio.Notification."),
}
# Methods that change files whatever they are called on (paths, open files, Gio files).
FILE_METHODS = {"unlink", "rmdir", "mkdir", "write_text", "write_bytes", "touch", "truncate", "trash", "rename",
                "savev", "backup"}
# Where a podcast's login, the sync login, the proxy password or an API key is read.
SECRET_NAMES = {"password", "login", "_auth", "credentials", "authorization"}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "?")
    return ".".join(reversed(parts))


def _kind(name):
    for kind, names in REACHES.items():
        if any(name == entry or (entry.endswith(".") and name.startswith(entry))
               or (entry.startswith(("os.exec", "os.spawn", "Gio.AppInfo.launch", "Gio.Notification."))
                   and name.startswith(entry)) for entry in names):
            return kind
    return "file" if name.rsplit(".", 1)[-1] in FILE_METHODS and "." in name else ""


class _Census(ast.NodeVisitor):
    def __init__(self, module):
        self.module, self.stack, self.found = module, [], set()

    def _visit_scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def _note(self, kind, what):
        self.found.add(f"{kind:8} {self.module:28} {'.'.join(self.stack) or '-':45} {what}")

    def visit_Call(self, node):
        name = _dotted(node.func)
        kind = _kind(name)
        if kind == "file" and name == "open":
            mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else \
                next((k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)),
                     "r")
            kind = "file" if set(str(mode)) & set("wax+") else ""
        if kind:
            self._note(kind, name)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in SECRET_NAMES and isinstance(node.ctx, ast.Load):
            self._note("secret", "." + node.attr)
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in SECRET_NAMES and isinstance(node.ctx, ast.Load):
            self._note("secret", node.id)


def census():
    """One line for every function that reaches outside the process, and how: 'kind module function call'."""
    found = set()
    for path in sorted((ROOT / "omapod").rglob("*.py")):
        module = str(path.relative_to(ROOT / "omapod"))
        walker = _Census(module)
        walker.visit(ast.parse(path.read_text("utf-8"), str(path)))
        found |= walker.found
    return sorted(found)


class Census(unittest.TestCase):
    def test_nothing_new_reaches_outside_without_a_look(self):
        """tests/census.txt lists every place that starts a program, opens a connection, makes a request, hands
        something to the sync server, changes a file, talks on the session bus or reads a secret. Each one has
        been looked at: where it sends what, what it deletes, whose word it takes.

        This test fails when the code and the list differ. Then look at the new place the same way (what does it
        reach, with which data, from whom; does SECURITY.md still say what happens; which test shows it), and
        only then update the list:

            python3 -c "from tests.test_exposure import census; print('\\n'.join(census()))" > tests/census.txt
        """
        listed = [line for line in (ROOT / "tests" / "census.txt").read_text("utf-8").split("\n")
                  if line and not line.startswith("#")]
        found = census()
        new = [line for line in found if line not in listed]
        gone = [line for line in listed if line not in found]
        self.assertFalse(new or gone, "\nnot in the list:\n  " + "\n  ".join(new) + "\nno longer in the code:\n  "
                         + "\n  ".join(gone))


if __name__ == "__main__":
    unittest.main()
