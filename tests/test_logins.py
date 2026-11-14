"""A podcast's login never travels in an address.

https://user:password@host/feed carries a login, and an address goes wherever addresses go: to the sync server,
into exports, the download log, the bar widget. omaPOD keeps a podcast's login apart from its address and sends
it to the podcast's own server only. These tests take every way an address gets into the library (typed, a link,
an OPML file, a feed that moves, a sync server, an imported or older library) and look at everything that leaves:
no request to the sync server, no export and no stored address may contain the password.

Review of 0.1.4 in the plugin marketplace (Medium): a library from 0.1.2 kept the address as it was typed, and
the first synchronization after the upgrade sent it, password and all, to the sync server.
"""

import base64
import json
import shutil
import sqlite3
import unittest
from pathlib import Path

from tests.fakeserver import DAV, FakeServer
from tests.test_hardening import Host
from tests.test_library import FIX, make_env

SECRET = "c4nary-p4ssword"
LOGIN = "Basic " + base64.b64encode(f"member:{SECRET}".encode()).decode()
FEED = ("<?xml version='1.0'?><rss version='2.0' xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'>"
        "<channel><title>Members only</title>{extra}"
        "<item><title>One</title><guid>one</guid><enclosure url='{base}/one.mp3' type='audio/mpeg' length='5'/>"
        "<itunes:duration>100</itunes:duration></item>"
        "<item><title>Two</title><guid>two</guid><enclosure url='{base}/two.mp3' type='audio/mpeg' length='5'/>"
        "<itunes:duration>100</itunes:duration></item></channel></rss>")


def with_login(url, login=f"member:{SECRET}"):
    return url.replace("://", f"://{login}@", 1)


class Recorded:
    """Everything a FakeServer is sent from now on: request lines, headers and bodies."""

    def __init__(self, server):
        self.entries = entries = []

        class Handler(server.httpd.RequestHandlerClass):
            def parse_request(self):
                parsed = super().parse_request()
                if parsed:
                    entries.append(f"{self.command} {self.path}\n{self.headers}")
                return parsed

            def _body(self):
                body = super()._body()
                entries.append(body.decode("utf-8", "replace"))
                return body

        server.httpd.RequestHandlerClass = Handler

    def text(self):
        return "\n".join(self.entries)


class Logins(unittest.TestCase):
    def setUp(self):
        self.tmp, self.settings, self.db, self.library = make_env()
        self.closers = []
        self.feeds = Host()  # the protected podcast's own server
        self.closers.append(self.feeds.close)
        self.base = self.feeds.base
        self.protect("/feed.xml", FEED.format(base=self.base, extra="").encode())
        self.protect("/one.mp3", b"audio", "audio/mpeg")
        self.protect("/two.mp3", b"audio", "audio/mpeg")

    def tearDown(self):
        for close in reversed(self.closers):
            close()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def protect(self, path, body, ctype="application/rss+xml", login=LOGIN, host=None):
        def route(handler):
            allowed = handler.headers.get("Authorization") == login
            answer = body if allowed else b""
            handler.send_response(200 if allowed else 401)
            if not allowed:
                handler.send_header("WWW-Authenticate", 'Basic realm="podcast"')
            handler.send_header("Content-Type", ctype)
            handler.send_header("Content-Length", str(len(answer)))
            handler.end_headers()
            handler.wfile.write(answer)

        (host or self.feeds).routes[path] = route

    def sync_server(self):
        server = FakeServer(str(FIX / "tone.mp3")).__enter__()
        self.closers.append(lambda: server.__exit__(None, None, None))
        return server, Recorded(server)

    def service(self, server, provider="nextcloud", name="creds.json"):
        from omapod.config import Credentials
        from omapod.sync.service import SyncService

        creds = Credentials(Path(self.tmp) / name)
        creds.save({"sync": {"provider": provider, "server": server.base, "username": "user", "password": "pass",
                             "device_id": "omapod-test"}})
        self.settings.set("sync_provider", provider)
        service = SyncService(self.library, self.settings, creds)
        self.closers.append(service.close)
        return service

    def subscribe(self):
        return self.library.subscribe_url(self.base + "/feed.xml", "member", SECRET)

    def as_an_earlier_version_stored_it(self, feed_id):
        """Up to 0.1.2 the address stayed as it was typed, and the login was read out of it for each request."""
        self.db.execute("UPDATE feeds SET url = ?, username = '', password = '' WHERE id = ?",
                        (with_login(self.base + "/feed.xml"), feed_id))

    def reopen(self):
        """What a start of omaPOD does (the app's and the background refresh's): secrets leave the settings, and
        the library opens."""
        from omapod import config
        from omapod.core.library import Library
        from omapod.db import Database

        config.move_secrets(self.settings, config.Credentials(Path(self.tmp) / "secrets.json"))
        self.db.close()
        self.db = Database(config.paths.db)
        self.library = Library(self.db, self.settings)

    def stored_text(self):
        """Every value in the library except the one place a password belongs, and the settings file."""
        from omapod import config

        settings = Path(config.paths.settings_file)
        texts = [settings.read_text("utf-8") if settings.exists() else ""]
        conn = sqlite3.connect(self.db.path)
        try:
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                for row in conn.execute(f"SELECT * FROM {table}"):
                    texts += [f"{table}.{name}={value}" for name, value in zip(columns, row, strict=True)
                              if (table, name) != ("feeds", "password")]
        finally:
            conn.close()
        return "\n".join(texts)

    def exports(self):
        from omapod.feeds.opml import build_html_export, build_opml

        feeds = self.db.get_feeds(with_counts=False)
        return build_opml(feeds) + build_html_export(feeds)


class WhatCountsAsALogin(unittest.TestCase):
    def test_a_login_is_taken_out_whatever_it_looks_like(self):
        from omapod.util import plain_address, scrub_logins, split_login

        for address, expected in (
                ("https://user:pass@host/feed", ("https://host/feed", "user", "pass")),
                ("https://:token@host/feed", ("https://host/feed", "", "token")),  # a password without a name
                ("https://user@host/feed", ("https://host/feed", "user", "")),
                ("https://us%20er:p%40ss@host/feed", ("https://host/feed", "us er", "p@ss")),
                ("https://user:p@ss@host:8443/f?x=1#y", ("https://host:8443/f?x=1#y", "user", "p@ss")),
                ("https://other:secret@[::1]:8443/feed.xml", ("https://[::1]:8443/feed.xml", "other", "secret")),
                ("HTTPS://User:Pass@Host/feed", ("HTTPS://Host/feed", "User", "Pass")),
                ("  https://user:pass@host/feed\n", ("https://host/feed", "user", "pass")),
                ("https://user:pass@[not-an-address/feed", ("https://[not-an-address/feed", "user", "pass")),
                # No login: an @ further on, a folder, a mail address.
                ("https://host/@user/feed?mail=a@b.c", ("https://host/@user/feed?mail=a@b.c", "", "")),
                ("local:/home/me/a@b", ("local:/home/me/a@b", "", "")),
                ("file:///home/me/a@b/cover.jpg", ("file:///home/me/a@b/cover.jpg", "", "")),
                ("mailto:someone@example.org", ("mailto:someone@example.org", "", ""))):
            self.assertEqual(split_login(address), expected, address)
            self.assertNotIn("pass@", plain_address(address))
        self.assertEqual(scrub_logins({"add": ["https://u:p@h/x", "https://h/y"], "n": [1, {"e": "http://:k@h/z"}],
                                       "title": "mail a@b.c"}),
                         {"add": ["https://h/x", "https://h/y"], "n": [1, {"e": "http://h/z"}], "title": "mail a@b.c"})

    def test_only_addresses_every_parser_reads_alike_go_to_a_sync_server(self):
        """A server and the apps behind it read addresses with parsers of their own. What a browser-like parser
        takes for a login (extra or missing slashes, a backslash, white space inside the host) is not sent, even
        though Python sees no login in it."""
        from omapod.util import sync_address

        import re

        for address in ("https://host/feed", "http://host:8080/feed?x=1#y", "https://[::1]:8443/feed.xml",
                        "https://host/a b.mp3", "https://host/@user/feed"):
            self.assertEqual(sync_address(address), address)
        self.assertEqual(sync_address("https://user:pass@host/feed"), "https://host/feed")
        odd = ("https:///user:pass@host/feed", "https:user:pass@host/feed", "https:/user:pass@host/feed",
               "https://ho st/feed", "https://host:8a/feed",
               "https:\\\\user:pass@host/feed", "https://", "https://:80/x", "https://user:pass@/feed",
               "local:/home/me/music", "file:///etc/passwd", "ftp://host/feed", "httpx://host/feed",
               "https://host/fe\x00ed", "https://host\\evil.example/feed", "", None, 5)
        for address in odd:
            self.assertEqual(sync_address(address), "", repr(address))
        # Whatever goes in: what comes out is nothing, or a host followed by a path, with no "@" or "\\" before it.
        plain = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+([/?#].*)?$", re.S | re.I)
        for address in odd + ("https://host\\@evil.example/feed", "https://a:b@c:d@host/x", "https://a@b@c/x",
                              "https://\tuser:pass@host/feed", "https://user:pass@ho\tst/feed",
                              "HTTPS://U:P@Host:443/x", "https://user:pass@host", "https://@host/x"):
            result = sync_address(address)
            self.assertTrue(result == "" or plain.match(result), (address, result))
            self.assertNotIn("pass", result)


class OlderLibraries(Logins):
    """What an earlier version left in the library is cleaned whenever the library opens, and whatever is still
    there does not get past the last check before the sync server."""

    def waiting_uploads(self, address):
        for kind, payload in (
                ("sub", {"url": address, "action": "add"}),
                ("sub", {"url": with_login(self.base + "/gone.xml"), "action": "remove"}),
                ("action", {"podcast": address, "episode": with_login(self.base + "/one.mp3"), "action": "play",
                            "guid": "one", "timestamp": "2026-11-01T10:00:00", "started": 0, "position": 5,
                            "total": 100})):
            self.db.execute("INSERT INTO sync_queue(kind, payload, created) VALUES(?, ?, 1)",
                            (kind, json.dumps(payload)))

    def upgraded_library(self):
        feed_id = self.subscribe()
        episodes = {e.guid: e for e in self.db.get_episodes(feed_id=feed_id)}
        self.library.mark_played([episodes["one"].id])
        self.library.add_to_queue([episodes["two"].id])
        self.library.set_favorite([episodes["two"].id])
        self.as_an_earlier_version_stored_it(feed_id)
        address = with_login(self.base + "/feed.xml")
        self.db.execute("UPDATE episodes SET media_url = ? WHERE guid = 'two'", (with_login(self.base + "/two.mp3"),))
        self.db.execute("INSERT INTO download_log(ts, kind, ref_id, title, url, success) VALUES(1, 'feed', ?, 't', ?, 0)",
                        (feed_id, address))
        self.db.execute("INSERT INTO sync_seen(key, ts) VALUES(?, 5)", ("url:" + with_login(self.base + "/one.mp3"),))
        self.db.execute("INSERT INTO remote_downloads(episode, downloaded, ts) VALUES(?, 1, 5)",
                        (with_login(self.base + "/one.mp3"),))
        self.waiting_uploads(address)
        self.settings.update({"favorites_sync_base": [[address, with_login(self.base + "/two.mp3")]],
                              "favorites_sync_local": [[address, with_login(self.base + "/two.mp3")]]})
        return feed_id

    def first_sync(self, provider, reopen):
        server, recorded = self.sync_server()
        service = self.service(server, provider)
        self.settings.set("sync_queue_favorites", True)
        feed_id = self.upgraded_library()
        if reopen:
            self.reopen()
            service.library, service.db = self.library, self.db
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertNotIn(SECRET, recorded.text())
        self.assertIn(self.base + "/feed.xml", server.sync.subscriptions)  # the podcast did go, by its plain address
        return feed_id, server, recorded

    def test_the_first_sync_after_an_upgrade_sends_no_login_to_nextcloud(self):
        feed_id, server, _recorded = self.first_sync("nextcloud", reopen=True)
        feed = self.db.get_feed(feed_id)
        self.assertEqual((feed.url, feed.username, feed.password), (self.base + "/feed.xml", "member", SECRET))
        self.assertNotIn(SECRET, self.stored_text())
        self.assertNotIn(SECRET, self.exports())
        self.assertNotIn(SECRET, json.dumps([body.decode() for body, _etag in server.httpd.dav.values()]))
        # The podcast still works: its login goes to its own server.
        self.feeds.seen.clear()
        self.assertEqual(self.library.refresh_feed(feed_id, force=True).error, "")
        self.assertEqual(self.feeds.credentials("/feed.xml"), [LOGIN])

    def test_the_first_sync_after_an_upgrade_sends_no_login_to_gpodder_net(self):
        self.first_sync("gpodder", reopen=True)
        self.assertNotIn(SECRET, self.stored_text())

    def test_what_is_still_in_the_library_does_not_get_past_the_last_check(self):
        """Without the cleaning at the start: every address is checked again where it leaves."""
        for provider in ("nextcloud", "gpodder"):
            with self.subTest(provider=provider):
                self.first_sync(provider, reopen=False)
                self.db.execute("DELETE FROM feeds")
                self.db.sync_clear()
                self.settings.update({"sync_last_subscriptions": 0, "sync_last_actions": 0})

    def test_opening_the_library_takes_logins_out_of_what_is_stored(self):
        feed_id = self.upgraded_library()
        # The same podcast a second time under its plain address, and one whose own login differs from its address's.
        self.db.execute("INSERT INTO feeds(url, title) VALUES(?, 'twice')", (self.base + "/feed.xml",))
        self.db.execute("INSERT INTO feeds(url, title, username, password) VALUES(?, 'own', 'kept', 'too')",
                        (with_login(self.base + "/other.xml"),))
        self.assertIn(SECRET, self.stored_text())
        self.reopen()
        self.assertNotIn(SECRET, self.stored_text())
        feed = self.db.get_feed(feed_id)
        # Both stay, each with its episodes; the mark after "#" is never sent to a server.
        self.assertEqual((feed.url, feed.username, feed.password), (self.base + "/feed.xml#2", "member", SECRET))
        self.assertEqual(self.db.count_episodes(feed_id=feed_id), 2)
        own = self.db.get_feed_by_url(self.base + "/other.xml")
        self.assertEqual((own.username, own.password), ("kept", "too"))
        self.assertEqual([p.get("url") for _id, p in self.db.sync_pending("sub")],
                         [self.base + "/feed.xml", self.base + "/gone.xml"])
        self.assertEqual(self.db.remote_downloads(), [(self.base + "/one.mp3", "")])
        self.assertEqual(self.db.sync_seen(["url:" + self.base + "/one.mp3"]), {"url:" + self.base + "/one.mp3": 5})
        self.assertEqual(self.db.take_logins_out_of_addresses(), 0)  # nothing left to do
        # The queue and favorites differ from what the last sync saw only in how they are written: the next sync
        # combines both sides instead of taking that for changes made here.
        self.assertEqual((self.settings.get("favorites_sync_base"), self.settings.get("queue_sync_base")), ([], ""))
        # A folder's address is a path, whatever is in it.
        folder = "local:/srv/media/a@b"
        self.db.execute("INSERT INTO feeds(url, title) VALUES(?, 'folder')", (folder,))
        self.reopen()
        self.assertIsNotNone(self.db.get_feed_by_url(folder))

    def test_a_podcast_added_twice_is_one_podcast_for_the_sync_server(self):
        """Once with the login in its address and once without: both stay, and the server hears of one."""
        server, recorded = self.sync_server()
        service = self.service(server)
        feed_id = self.subscribe()
        self.db.execute("INSERT INTO feeds(url, title, state) VALUES(?, 'twice', 1)", (with_login(self.base + "/feed.xml"),))
        self.reopen()
        service.library, service.db = self.library, self.db
        urls = sorted(f.url for f in self.db.get_feeds(with_counts=False))
        self.assertEqual(urls, [self.base + "/feed.xml", self.base + "/feed.xml#2"])
        self.assertTrue(service.run(full=True), self.settings.get("sync_last_error"))
        self.assertEqual(set(server.sync.subscriptions), {self.base + "/feed.xml"})
        self.assertNotIn(SECRET, recorded.text())
        self.assertNotIn("#2", recorded.text())
        # The second one is read from the feed's own address (urllib leaves the mark out), with its login.
        twice = self.db.get_feed_by_url(self.base + "/feed.xml#2")
        self.assertEqual(self.library.refresh_feed(twice.id, force=True).error, "")
        self.assertEqual(self.db.count_episodes(feed_id=feed_id), 2)

    def test_an_imported_library_is_cleaned_like_an_older_one(self):
        """A backup from 0.1.2 holds addresses as they were typed. The import puts it in place, the start cleans it."""
        from omapod import __main__ as main
        from omapod.ui import preferences

        self.upgraded_library()
        backup = Path(self.tmp) / "from-0.1.2.db"
        with open(backup, "wb") as handle:
            self.db.write_snapshot(handle)
        self.db.execute("DELETE FROM feeds")
        self.assertIsNone(preferences.stage_import(str(backup)))
        self.db.close()
        main._apply_pending_import()
        self.reopen()
        self.assertEqual([f.url for f in self.db.get_feeds(with_counts=False)], [self.base + "/feed.xml"])
        self.assertNotIn(SECRET, self.stored_text())
        server, recorded = self.sync_server()
        self.assertTrue(self.service(server).run())
        self.assertNotIn(SECRET, recorded.text())


class EveryWayIn(Logins):
    def assert_kept_apart(self, feed_id, address=None):
        feed = self.db.get_feed(feed_id)
        self.assertEqual((feed.url, feed.username, feed.password),
                         (address or self.base + "/feed.xml", "member", SECRET))
        self.assertNotIn(SECRET, self.stored_text())
        self.assertNotIn(SECRET, self.exports())

    def test_typed_or_from_a_link(self):
        self.settings.set("sync_provider", "nextcloud")  # subscribing then queues the address for the server
        feed_id = self.library.subscribe_url(with_login(self.base + "/feed.xml"))
        self.assert_kept_apart(feed_id)
        self.assertEqual([p["url"] for _id, p in self.db.sync_pending("sub")], [self.base + "/feed.xml"])

    def test_a_password_without_a_name(self):
        """https://:token@host/feed: Python reports no user name for it, and the address was kept as it was."""
        token = "Basic " + base64.b64encode(f":{SECRET}".encode()).decode()
        for path, kind in (("/token.xml", "application/rss+xml"), ("/one.mp3", "audio/mpeg")):
            body = FEED.format(base=self.base, extra="").encode() if path.endswith(".xml") else b"audio"
            self.protect(path, body, kind, login=token)
        feed_id = self.library.subscribe_url(with_login(self.base + "/token.xml", f":{SECRET}"))
        feed = self.db.get_feed(feed_id)
        self.assertEqual((feed.url, feed.username, feed.password), (self.base + "/token.xml", "", SECRET))
        self.assertNotIn(SECRET, self.stored_text())
        # It is a login like any other: the feed is read again with it, and streams of its own server get it.
        self.assertEqual(self.library.refresh_feed(feed_id, force=True).error, "")
        from omapod import config
        from omapod.player.controller import PlayerController

        player = PlayerController(self.library, self.settings, config.paths)
        player.feed = feed
        player.episode = self.db.get_episodes(feed_id=feed_id)[0]
        self.assertEqual(player._source(), with_login(player.episode.media_url, f":{SECRET}"))

    def test_from_an_opml_file(self):
        from omapod.feeds.opml import parse_opml

        opml = (f"<opml version='2.0'><body><outline type='rss' text='Members' "
                f"xmlUrl='{with_login(self.base + '/feed.xml')}'/></body></opml>")
        (entry,) = parse_opml(opml)
        self.assert_kept_apart(self.library.subscribe_url(entry["url"]))

    def test_changing_the_address(self):
        server, recorded = self.sync_server()
        service = self.service(server)
        feed_id = self.subscribe()
        self.assertTrue(service.run())
        self.as_an_earlier_version_stored_it(feed_id)
        # Typing the address again, as 0.1.3 advised, must not send the old one along to have it removed.
        self.assertTrue(self.library.edit_feed_url(feed_id, with_login(self.base + "/feed.xml")))
        self.assert_kept_apart(feed_id)
        self.assertTrue(service.run())
        self.assertNotIn(SECRET, recorded.text())
        # The same address with another login changes the login and nothing else.
        self.assertTrue(self.library.edit_feed_url(feed_id, with_login(self.base + "/feed.xml", "other:word")))
        feed = self.db.get_feed(feed_id)
        self.assertEqual((feed.url, feed.username, feed.password), (self.base + "/feed.xml", "other", "word"))
        self.assertEqual(self.db.sync_pending("sub"), [])
        # Another podcast's address, and what is no web address.
        self.feeds.routes["/open.xml"] = (200, {"Content-Type": "application/rss+xml"},
                                          FEED.format(base=self.base, extra="").encode())
        self.library.subscribe_url(self.base + "/open.xml")
        self.assertFalse(self.library.edit_feed_url(feed_id, self.base + "/open.xml"))
        for address in ("file:///etc/passwd", "local:/home/me", "https:///user:pass@host/feed", "nonsense"):
            with self.assertRaises(ValueError):
                self.library.edit_feed_url(feed_id, address)
        self.assertEqual(self.db.get_feed(feed_id).url, self.base + "/feed.xml")

    def test_a_feed_cannot_put_a_login_into_an_address(self):
        """What a feed names (its episodes, pictures, links, further pages, a new address of its own) is taken
        without a login. The one login a podcast has is the one its user entered."""
        from omapod.feeds.chapters import parse_json_chapters
        from omapod.feeds.parser import parse_feed

        login = with_login(self.base, "feed:" + SECRET)
        extra = (f"<link>{login}/site</link><itunes:image href='{login}/cover.jpg'/>"
                 f"<itunes:new-feed-url>{login}/moved.xml</itunes:new-feed-url>"
                 f"<atom:link xmlns:atom='http://www.w3.org/2005/Atom' rel='next' href='{login}/page2'/>"
                 f"<podcast:funding xmlns:podcast='https://podcastindex.org/namespace/1.0' url='{login}/pay'>"
                 f"Pay</podcast:funding>")
        text = FEED.format(base=login, extra=extra)
        parsed = parse_feed(text.encode(), self.base + "/open.xml")
        addresses = [parsed.link, parsed.image_url, parsed.new_feed_url, parsed.next_page_url,
                     parsed.funding[0]["url"]] + [item.media_url for item in parsed.items]
        self.assertTrue(all(addresses))
        self.assertNotIn(SECRET, " ".join(addresses))
        self.assertEqual(parsed.items[0].media_url, self.base + "/one.mp3")
        chapters = parse_json_chapters(json.dumps({"chapters": [{"startTime": 1, "url": login + "/c",
                                                                 "img": login + "/c.jpg"}]}))
        self.assertEqual((chapters[0]["url"], chapters[0]["image"]), (self.base + "/c", self.base + "/c.jpg"))
        # A feed without a login that names a new address with one: the podcast moves, and gets no login from it.
        self.feeds.routes["/open.xml"] = (200, {"Content-Type": "application/rss+xml"}, text.encode())
        self.feeds.routes["/moved.xml"] = (200, {"Content-Type": "application/rss+xml"}, text.encode())
        self.settings.set("sync_provider", "nextcloud")
        feed_id = self.library.subscribe_url(self.base + "/open.xml")
        self.assertEqual(self.library.refresh_feed(feed_id, force=True).error, "")
        feed = self.db.get_feed(feed_id)
        self.assertEqual((feed.url, feed.username, feed.password), (self.base + "/moved.xml", "", ""))
        self.assertNotIn(SECRET, self.stored_text())
        self.assertNotIn(SECRET, json.dumps(self.db.sync_pending("sub")))

    def test_a_redirect_cannot_either(self):
        self.feeds.routes["/old.xml"] = (301, {"Location": with_login(self.base + "/open.xml", "redirect:" + SECRET)},
                                         b"")
        self.feeds.routes["/open.xml"] = (200, {"Content-Type": "application/rss+xml"},
                                          FEED.format(base=self.base, extra="").encode())
        feed_id = self.library.subscribe_url(self.base + "/old.xml")
        feed = self.db.get_feed(feed_id)
        self.assertEqual((feed.url, feed.username, feed.password), (self.base + "/open.xml", "", ""))
        self.assertEqual(self.feeds.credentials("/open.xml"), [None])  # and the login it named was not used


class SyncServers(Logins):
    def test_a_login_another_device_put_into_an_address_stays_on_this_computer(self):
        """AntennaPod keeps an address as it was typed and sends it to the sync server. From there it arrived
        here unchanged, went into exports and, after logging in to another server, to that one."""
        server, recorded = self.sync_server()
        service = self.service(server)
        named = with_login(self.base + "/feed.xml")
        server.sync.apply_subs([named], [])
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        feed = self.db.get_feed_by_url(self.base + "/feed.xml")
        self.assertEqual((feed.title, feed.username, feed.password), ("Members only", "member", SECRET))
        self.assertNotIn(SECRET, self.stored_text())
        self.assertNotIn(SECRET, self.exports())
        self.assertNotIn(SECRET, recorded.text())
        # It is the same podcast under both names: a full sync adds no second one, and uploads no second name.
        self.assertTrue(service.run(full=True))
        self.assertEqual(len(self.db.get_feeds(with_counts=False)), 1)
        self.assertEqual(set(server.sync.subscriptions), {named})
        # Another sync server: its first sync uploads every subscription.
        other, other_recorded = self.sync_server()
        second = self.service(other, name="creds2.json")
        self.settings.update({"sync_last_subscriptions": 0, "sync_last_actions": 0})
        self.db.sync_clear()
        self.assertTrue(second.run())
        self.assertEqual(list(other.sync.subscriptions), [self.base + "/feed.xml"])
        self.assertNotIn(SECRET, other_recorded.text())

    def test_unsubscribing_does_not_repeat_the_login_and_the_podcast_does_not_come_back(self):
        server, recorded = self.sync_server()
        service = self.service(server)
        named = with_login(self.base + "/feed.xml")
        server.sync.apply_subs([named], [])
        self.assertTrue(service.run())
        self.library.unsubscribe([self.db.get_feed_by_url(self.base + "/feed.xml").id])
        self.assertTrue(service.run())
        self.assertNotIn(SECRET, recorded.text())
        self.assertFalse(server.sync.subscriptions[self.base + "/feed.xml"][0])  # told under its plain address
        self.assertTrue(service.run(full=True))  # the server still lists the other device's name for it
        self.assertEqual(self.db.get_feeds(with_counts=False), [])

    def test_a_server_adds_and_removes_web_addresses_only(self):
        server, _recorded = self.sync_server()
        service = self.service(server)
        folder = "local:/srv/media"
        self.db.execute("INSERT INTO feeds(url, title, state) VALUES(?, 'A folder', 1)", (folder,))
        self.assertTrue(service.run())
        server.sync.apply_subs(["local:/etc", "httpx://host/feed", "https:///user:pass@host/feed", "file:///etc/passwd"],
                               [folder])
        self.assertTrue(service.run())
        self.assertEqual([f.url for f in self.db.get_feeds(with_counts=False)], [folder])

    def test_shared_queue_and_favorites_carry_no_login(self):
        server, recorded = self.sync_server()
        service = self.service(server)
        self.settings.set("sync_queue_favorites", True)
        feed_id = self.subscribe()
        episodes = {e.guid: e for e in self.db.get_episodes(feed_id=feed_id)}
        self.library.add_to_queue([episodes["one"].id])
        self.library.set_favorite([episodes["two"].id])
        # An omaPOD up to 0.1.4 wrote the files with addresses as it had them, and remembered them that way.
        address = with_login(self.base + "/feed.xml")
        old = {"version": 1, "modified": 5, "device": "omapod-old", "episodes": [
            {"podcast": address, "episode": self.base + "/two.mp3", "title": "Two", "guid": "two"}]}
        for name in ("queue.json", "favorites.json"):
            server.httpd.dav[DAV + ".omapod/" + name] = (json.dumps(old).encode(), '"1"')
        server.httpd.dav_folders.add(DAV + ".omapod/")
        server.httpd.dav_version = 1
        self.settings.update({"favorites_sync_base": [[address, self.base + "/two.mp3"]],
                              "favorites_sync_local": [[address, self.base + "/two.mp3"]]})
        self.assertTrue(service.run(), self.settings.get("sync_last_error"))
        self.assertNotIn(SECRET, recorded.text())
        files = {name: body.decode() for name, (body, _etag) in server.httpd.dav.items()}
        self.assertNotIn(SECRET, json.dumps(files))  # the files of the earlier version are written anew
        self.assertNotIn(SECRET, self.stored_text())
        self.assertTrue(self.db.get_episode(episodes["two"].id).favorite)  # and nothing was taken back
        self.assertEqual(set(self.library.queue_ids()), {episodes["one"].id, episodes["two"].id})

    def test_nothing_with_a_login_gets_past_a_provider(self):
        """Whatever a caller hands a provider: the last check before the network."""
        from omapod.sync.providers import GpodderNet, Nextcloud

        server, recorded = self.sync_server()
        dirty = with_login(self.base + "/feed.xml")
        action = {"podcast": dirty, "episode": with_login(self.base + "/one.mp3"), "action": "play",
                  "timestamp": "2026-11-01T10:00:00", "started": 0, "position": 5, "total": 9}
        for provider in (Nextcloud(server.base, "user", "pass"),
                         GpodderNet(server.base, "user", "pass", "omapod-test")):
            provider.upload_subscription_changes([dirty], [dirty])
            provider.upload_episode_actions([action])
        Nextcloud(server.base, "user", "pass").put_shared("queue.json", {"episodes": [{"podcast": dirty}]})
        self.assertNotIn(SECRET, recorded.text())
        self.assertEqual(set(server.sync.subscriptions), {self.base + "/feed.xml"})


class OtherPlaces(Logins):
    def test_the_download_log_and_the_bar_widget(self):
        from types import SimpleNamespace

        from omapod import config
        from omapod.player.controller import PlayerController
        from omapod.player.ipc import ControlServer

        feed_id = self.subscribe()
        self.as_an_earlier_version_stored_it(feed_id)
        self.db.execute("UPDATE episodes SET media_url = ? WHERE guid = 'one'", (with_login(self.base + "/one.mp3"),))
        feed = self.db.get_feed(feed_id)
        self.library._refresh_failed(feed, "reason", "detail")
        self.assertNotIn(SECRET, json.dumps([dict(row) for row in self.db.get_log()]))
        player = PlayerController(self.library, self.settings, config.paths)
        player.episode = self.db.get_episodes(feed_id=feed_id, extra="e.guid = 'one'")[0]
        player.feed = feed
        images = SimpleNamespace(thumb_if_cached=lambda *_a: None, request=lambda *_a: None)
        control = ControlServer(SimpleNamespace(), player, self.library, images, Path(self.tmp) / "control.sock")
        self.assertNotIn(SECRET, json.dumps(control.snapshot()))


if __name__ == "__main__":
    unittest.main()
