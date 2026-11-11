"""Credentials, size limits, local files and slow input: what a feed, a media host or a sync server could abuse.
Everything runs against local servers; a second host name for the same machine is "localhost"."""

import base64
import gzip
import html
import json
import re
import shutil
import ssl
import threading
import time
import unittest
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.test_library import FIX, make_env

AUTH = "Basic " + base64.b64encode(b"user:pass").decode()
FEED = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>Private</title><item><title>One</title>"
        b"<guid>one</guid><enclosure url='MEDIA' type='audio/mpeg' length='1000'/></item></channel></rss>")


class Host:
    """A local web server under one host name. Answers from `routes` {path: (status, headers, body)} and
    records which requests carried credentials; paths in `private` answer 401 without user:pass.
    A callable route writes the whole answer itself."""

    def __init__(self, name="127.0.0.1", tls=None):
        self.name, self.seen, self.routes, self.private = name, [], {}, set()
        self.requests = []  # (path, all headers)
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                auth = self.headers.get("Authorization")
                host.seen.append((self.path, auth))
                host.requests.append((self.path, self.headers))
                route = host.routes.get(self.path, (404, {}, b""))
                if callable(route):
                    return route(self)
                status, headers, body = route
                if self.path in host.private and auth != AUTH:
                    status, headers, body = 401, {"WWW-Authenticate": 'Basic realm="podcast"'}, b""
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        scheme = "http"
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
            self.httpd.handle_error = lambda *_args: None  # clients that reject the certificate hang up
            scheme = "https"
        self.port = self.httpd.server_address[1]
        self.base = f"{scheme}://{name}:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def endless(handler, chunk=b"\0" * 65536):
        """Audio without a Content-Length that never ends (until the client hangs up)."""
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        try:
            while True:
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        except OSError:
            pass

    def credentials(self, path=None):
        """The Authorization headers this host received (for one path)."""
        return [auth for seen, auth in self.seen if path is None or seen == path]

    def header(self, name):
        """One header of every request this host received, None where it was missing."""
        return [headers.get(name) for _path, headers in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Servers(unittest.TestCase):
    def setUp(self):
        self.tmp, self.settings, self.db, self.library = make_env()
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.close()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def host(self, name="127.0.0.1", tls=None):
        host = Host(name, tls)
        self.hosts.append(host)
        return host

    def trusted_tls(self):
        """A certificate for both host names, trusted for the duration of the test."""
        from omapod.net import http
        from tests.fakeserver import self_signed_certificate

        tls = self_signed_certificate(self.tmp, "DNS:localhost,IP:127.0.0.1")
        if tls is None:
            self.skipTest("openssl is not installed")
        original = ssl.create_default_context
        patch = mock.patch.object(http.ssl, "create_default_context", lambda: original(cafile=str(tls[0])))
        patch.start()
        self.addCleanup(patch.stop)
        return tls


class CredentialsStayWithTheirHost(Servers):
    def test_which_addresses_may_get_credentials(self):
        from omapod.net.http import auth_allowed

        self.assertTrue(auth_allowed("https://a.example/feed", "https://a.example/other?x=1"))
        self.assertTrue(auth_allowed("https://a.example:443/feed", "https://A.example/feed"))
        self.assertTrue(auth_allowed("http://a.example/feed", "https://a.example/feed"))  # upgrade to TLS
        self.assertFalse(auth_allowed("https://a.example/feed", "http://a.example/feed"))  # back to plain text
        self.assertFalse(auth_allowed("https://a.example/feed", "https://b.example/feed"))
        self.assertFalse(auth_allowed("https://a.example/feed", "https://a.example.evil/feed"))
        self.assertFalse(auth_allowed("https://a.example/feed", "https://a.example:8443/feed"))
        self.assertFalse(auth_allowed("http://a.example:8080/feed", "https://a.example/feed"))
        self.assertFalse(auth_allowed("", "https://a.example/feed"))

    def test_redirect_to_another_host_leaves_credentials_behind(self):
        from omapod.net import http

        first, other = self.host(), self.host("localhost")
        other.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, b"<rss/>")
        first.routes["/feed.xml"] = (302, {"Location": other.base + "/feed.xml"}, b"")
        http.request(first.base + "/feed.xml", auth=("user", "pass"))
        response, _chain = http.open_url(first.base + "/feed.xml", auth=("user", "pass"))
        response.read()
        response.close()
        http.request(first.base.replace("://", "://user:pass@") + "/feed.xml")
        http.request(first.base + "/feed.xml", headers={"Authorization": AUTH})
        self.assertEqual(first.credentials(), [AUTH] * 4)
        self.assertEqual(other.credentials(), [None] * 4)

    def test_redirect_on_the_same_host_keeps_credentials(self):
        from omapod.net import http

        server = self.host()
        server.routes["/old.xml"] = (301, {"Location": server.base + "/new.xml"}, b"")
        server.routes["/new.xml"] = (200, {}, b"<rss/>")
        server.private.add("/new.xml")
        response = http.request(server.base + "/old.xml", auth=("user", "pass"))
        self.assertEqual(response.body, b"<rss/>")
        self.assertEqual(response.permanent_url, server.base + "/new.xml")

    def test_no_other_host_gets_the_podcast_password_even_when_it_asks(self):
        """Marketplace review of 0.1.0 (Medium): another https host that answered 401 got the podcast's username
        and password, directly or at the end of a redirect."""
        from omapod.net import http

        tls = self.trusted_tls()
        cdn, plain, stats = self.host("localhost", tls), self.host("localhost"), self.host()
        for host in (cdn, plain):
            host.routes["/ep.mp3"] = (200, {"Content-Type": "audio/mpeg"}, b"audio")
            host.private.add("/ep.mp3")
        stats.routes["/r/cdn.mp3"] = (302, {"Location": cdn.base + "/ep.mp3"}, b"")
        stats.routes["/r/plain.mp3"] = (302, {"Location": plain.base + "/ep.mp3"}, b"")
        login = {"auth": ("user", "pass"), "auth_url": "https://podcast.example/feed.xml"}
        for address, asks in ((cdn.base + "/ep.mp3", cdn), (stats.base + "/r/cdn.mp3", cdn),
                              (stats.base + "/r/plain.mp3", plain)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                http.open_url(address, **login)
            caught.exception.close()
            self.assertEqual((caught.exception.code, caught.exception.geturl()), (401, asks.base + "/ep.mp3"))
            with self.assertRaises(http.HttpError) as caught:
                http.request(address, **login)
            self.assertEqual((caught.exception.status, caught.exception.final_url), (401, asks.base + "/ep.mp3"))
        self.assertEqual(cdn.credentials(), [None] * 4)
        self.assertEqual(plain.credentials(), [None] * 2)
        self.assertEqual(stats.credentials(), [None] * 4)
        self.assertEqual(http.withheld(login["auth_url"], cdn.base + "/ep.mp3"),
                         "localhost asks for a password, but omaPOD sends this podcast's password only to "
                         "podcast.example.")
        self.assertEqual(http.withheld(login["auth_url"], "https://podcast.example/ep.mp3"), "")
        self.assertEqual(http.withheld("http://podcast.example/feed.xml", "https://podcast.example/ep.mp3"), "")

    def test_downloads_send_the_podcast_password_only_to_its_host(self):
        from omapod.models import FEED_SUBSCRIBED
        from omapod.net.downloads import DownloadManager

        podcast, media = self.host(), self.host("localhost")
        podcast.routes["/own.mp3"] = media.routes["/elsewhere.mp3"] = (200, {"Content-Type": "audio/mpeg"},
                                                                        b"audio")
        podcast.private.add("/own.mp3")
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": podcast.base + "/feed.xml", "title": "Private",
                                                 "state": FEED_SUBSCRIBED, "username": "user", "password": "pass"})
            ids = [self.db.insert_episode(conn, {"feed_id": feed_id, "guid": name, "title": name,
                                                 "media_url": base + f"/{name}.mp3", "media_type": "audio/mpeg"})
                   for name, base in (("own", podcast.base), ("elsewhere", media.base))]
        manager = DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        manager.start()
        self.addCleanup(manager.stop)
        self.library.download(ids)
        deadline = time.monotonic() + 15
        while manager.active_ids() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(all(e.is_downloaded for e in self.db.get_episodes_by_ids(ids)), self.db.get_log())
        self.assertEqual(podcast.credentials("/own.mp3"), [AUTH])
        self.assertEqual(media.credentials("/elsewhere.mp3"), [None])

    def test_downloads_never_answer_another_host_that_asks_for_the_password(self):
        """Marketplace review of 0.1.0 (Medium): a media host linked by a protected feed answered 401 and
        received the podcast's username and password."""
        from omapod.models import FEED_SUBSCRIBED
        from omapod.net.downloads import DownloadManager

        tls = self.trusted_tls()
        podcast, media = self.host(), self.host("localhost", tls)
        media.routes["/ep.mp3"] = (200, {"Content-Type": "audio/mpeg"}, b"audio")
        media.private.add("/ep.mp3")
        podcast.routes["/stats/ep.mp3"] = (302, {"Location": media.base + "/ep.mp3"}, b"")
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": podcast.base + "/feed.xml", "title": "Private",
                                                 "state": FEED_SUBSCRIBED, "username": "user", "password": "pass"})
            ids = [self.db.insert_episode(conn, {"feed_id": feed_id, "guid": url, "title": url, "media_url": url,
                                                 "media_type": "audio/mpeg"})
                   for url in (media.base + "/ep.mp3", podcast.base + "/stats/ep.mp3")]
        manager = DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        manager.start()
        self.addCleanup(manager.stop)
        self.library.download(ids)
        deadline = time.monotonic() + 15
        while manager.active_ids() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(any(e.is_downloaded for e in self.db.get_episodes_by_ids(ids)))
        self.assertEqual(media.credentials(), [None, None])
        self.assertEqual(podcast.credentials(), [AUTH])  # its own server, where the statistics prefix starts
        self.assertEqual({row["reason"] for row in self.db.get_log()},
                         {"localhost asks for a password, but omaPOD sends this podcast's password only to "
                          "127.0.0.1."})

    def test_stream_address_carries_the_password_only_to_the_podcasts_own_server(self):
        from omapod import config
        from omapod.models import Episode, Feed
        from omapod.player.controller import PlayerController

        player = PlayerController(self.library, self.settings, config.paths)
        player.feed = Feed(url="https://podcast.example/feed.xml", username="us er", password="p@ss:word")
        for media, expected in (
                ("https://podcast.example/ep.mp3", "https://us%20er:p%40ss%3Aword@podcast.example/ep.mp3"),
                ("http://podcast.example/ep.mp3", "http://podcast.example/ep.mp3"),  # https feed: not in plain text
                ("https://cdn.example/ep.mp3", "https://cdn.example/ep.mp3"),  # marketplace review of 0.1.0
                ("http://cdn.example/ep.mp3", "http://cdn.example/ep.mp3"),
                ("https://podcast.example:8443/ep.mp3", "https://podcast.example:8443/ep.mp3"),
                ("https://own:login@cdn.example/ep.mp3", "https://own:login@cdn.example/ep.mp3")):
            player.episode = Episode(media_url=media)
            self.assertEqual(player._source(), expected)
        player.feed = Feed(url="http://podcast.example/feed.xml", username="user", password="pass")
        player.episode = Episode(media_url="https://podcast.example/ep.mp3")  # the same server over TLS
        self.assertEqual(player._source(), "https://user:pass@podcast.example/ep.mp3")

    @unittest.skipUnless(shutil.which("mpv"), "mpv not installed")
    def test_stream_redirected_to_another_host_does_not_take_the_password_along(self):
        from omapod import config
        from omapod.events import bus
        from omapod.models import FEED_SUBSCRIBED
        from omapod.player.controller import PlayerController
        from tests.test_player import run_until

        bus.attach_main_loop()
        podcast, other = self.host(), self.host("localhost")
        tone = (FIX / "tone.mp3").read_bytes()
        podcast.routes["/own.mp3"] = other.routes["/elsewhere.mp3"] = (200, {"Content-Type": "audio/mpeg"}, tone)
        podcast.private.add("/own.mp3")
        podcast.routes["/stats.mp3"] = (302, {"Location": other.base + "/elsewhere.mp3"}, b"")
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": podcast.base + "/feed.xml", "title": "Private",
                                                 "state": FEED_SUBSCRIBED, "username": "user", "password": "pass"})
            redirected, own = (self.db.insert_episode(conn, {"feed_id": feed_id, "guid": name, "title": name,
                                                             "media_url": podcast.base + f"/{name}.mp3",
                                                             "media_type": "audio/mpeg"})
                               for name in ("stats", "own"))
        player = PlayerController(self.library, self.settings, config.paths)
        self.library.player = player
        try:
            for episode_id in (redirected, own):
                self.assertTrue(player.play_episode(episode_id, checked=True))
                self.assertTrue(run_until(lambda: player.status == "playing" and player.position > 500),
                                player.error)
        finally:
            player.shutdown()  # before tearDown closes the database
        self.assertEqual(other.credentials(), [None])
        self.assertNotIn(AUTH, podcast.credentials("/stats.mp3"))
        self.assertIn(AUTH, podcast.credentials("/own.mp3"))

    def test_password_protected_podcast_does_not_move_to_another_host(self):
        podcast, elsewhere = self.host(), self.host("localhost")
        body = FEED.replace(b"MEDIA", podcast.base.encode() + b"/one.mp3")
        elsewhere.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, body)
        podcast.routes["/feed.xml"] = (301, {"Location": elsewhere.base + "/feed.xml"}, b"")
        protected = self.library.preview(podcast.base + "/feed.xml", "user", "pass")
        self.assertEqual(self.db.get_feed(protected).url, podcast.base + "/feed.xml")
        self.library.unsubscribe([protected])
        public = self.library.preview(podcast.base + "/feed.xml")
        self.assertEqual(self.db.get_feed(public).url, elsewhere.base + "/feed.xml")
        # Moving announced by the feed itself (itunes:new-feed-url) follows the same rule.
        self.library.unsubscribe([public])
        podcast.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, body.replace(
            b"<channel>", b"<channel><itunes:new-feed-url xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'>"
            + elsewhere.base.encode() + b"/feed.xml</itunes:new-feed-url>"))
        protected = self.library.preview(podcast.base + "/feed.xml", "user", "pass")
        self.assertFalse(self.library.refresh_feed(protected, force=True).error)
        self.assertEqual(self.db.get_feed(protected).url, podcast.base + "/feed.xml")

    def test_feed_redirected_to_another_host_that_asks_never_sends_it_the_password(self):
        """The marketplace finding for feeds: subscribing names the host that asks, and the password entered
        there belongs to that host; a subscribed podcast's password stays with its own server."""
        from omapod.core.library import AuthRequired
        from omapod.events import bus
        from omapod.models import FEED_SUBSCRIBED

        tls = self.trusted_tls()
        podcast, other = self.host(), self.host("localhost", tls)
        other.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"},
                                     FEED.replace(b"MEDIA", other.base.encode() + b"/one.mp3"))
        other.private.add("/feed.xml")
        podcast.routes["/feed.xml"] = (302, {"Location": other.base + "/feed.xml"}, b"")
        with self.assertRaises(AuthRequired) as caught:
            self.library.preview(podcast.base + "/feed.xml")
        self.assertEqual(caught.exception.url, other.base + "/feed.xml")
        feed_id = self.library.preview(caught.exception.url, "user", "pass")
        self.assertEqual(self.db.get_feed(feed_id).url, other.base + "/feed.xml")
        self.assertEqual(other.credentials(), [None, AUTH])
        # A subscribed podcast whose server now redirects there: the other host gets nothing, and no login
        # dialog opens, as a password entered for this podcast would not go there either.
        with self.db.tx() as conn:
            moved = self.db.insert_feed(conn, {"url": podcast.base + "/feed.xml", "title": "Private",
                                               "state": FEED_SUBSCRIBED, "username": "user", "password": "pass"})
        dialogs = []
        self.addCleanup(bus.unsubscribe, bus.subscribe("auth-required", dialogs.append))
        result = self.library.refresh_feed(moved, force=True)
        self.assertEqual(result.error, "localhost asks for a password, but omaPOD sends this podcast's password "
                                       "only to 127.0.0.1.")
        self.assertEqual(dialogs, [])
        self.assertEqual(other.credentials(), [None, AUTH, None])
        self.assertEqual(podcast.credentials(), [None, AUTH])
        # Its own server asking is a wrong password: that one the dialog can fix.
        podcast.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, b"<rss/>")
        podcast.private.add("/feed.xml")
        self.library.update_feed_settings(moved, password="wrong")
        result = self.library.refresh_feed(moved, force=True)
        self.assertEqual(result.error, "Authentication error. Make sure that username and password are correct.")
        self.assertEqual(dialogs, [moved])


class StreamCertificates(Servers):
    """Marketplace review of 0.1.1: mpv checks no certificates unless told to, so whoever intercepts a stream of a
    protected podcast with a certificate of its own could answer 401 and receive the password."""

    def setUp(self):
        super().setUp()
        if not shutil.which("mpv"):
            self.skipTest("mpv not installed")
        from omapod.events import bus
        from tests.fakeserver import self_signed_certificate

        bus.attach_main_loop()
        self.tls = self_signed_certificate(self.tmp, "DNS:localhost,IP:127.0.0.1")
        if self.tls is None:
            self.skipTest("openssl is not installed")

    def play(self, mpv_class=None):
        """Streams an episode of a protected podcast from a server with a certificate the system does not trust.
        Returns the player's error and the credentials the server received."""
        from omapod import config
        from omapod.models import FEED_SUBSCRIBED
        from omapod.player import controller
        from tests.test_player import run_until

        podcast = self.host(tls=self.tls)
        podcast.routes["/own.mp3"] = (200, {"Content-Type": "audio/mpeg"}, (FIX / "tone.mp3").read_bytes())
        podcast.private.add("/own.mp3")
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": podcast.base + "/feed.xml", "title": "Private",
                                                 "state": FEED_SUBSCRIBED, "username": "user", "password": "pass"})
            episode_id = self.db.insert_episode(conn, {"feed_id": feed_id, "guid": "own", "title": "own",
                                                       "media_url": podcast.base + "/own.mp3",
                                                       "media_type": "audio/mpeg"})
        with mock.patch.object(controller, "Mpv", mpv_class or controller.Mpv):
            player = controller.PlayerController(self.library, self.settings, config.paths)
            self.library.player = player
            try:
                self.assertTrue(player.play_episode(episode_id, checked=True))
                run_until(lambda: player.error or (player.status == "playing" and player.position > 500))
            finally:
                player.shutdown()  # before tearDown closes the database
        return player.error, podcast.credentials()

    def test_a_server_the_system_does_not_trust_gets_nothing(self):
        error, credentials = self.play()
        self.assertTrue(error)
        self.assertEqual(credentials, [])  # not even a request, so no 401 could ask for the password

    def test_a_trusted_server_gets_the_stream_with_the_password(self):
        from omapod.player.mpv import Mpv

        authority = str(self.tls[0])

        class Trusting(Mpv):  # as if the system trusted the server's certificate
            def __init__(self, *args, **kwargs):
                super().__init__(*args, extra_args=[f"--tls-ca-file={authority}"], **kwargs)

        error, credentials = self.play(Trusting)
        self.assertEqual(error, "")
        self.assertIn(AUTH, credentials)


class TunnelProxy:
    """An HTTP proxy that only tunnels (CONNECT), as https requests use one; records what it was asked."""

    def __init__(self):
        import socket

        self.seen = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                proxy.seen.append((self.path, self.headers.get("Proxy-Authorization")))
                host, port = self.path.rsplit(":", 1)
                upstream = socket.create_connection((host, int(port)), timeout=5)
                self.send_response(200, "Connection established")
                self.end_headers()

                def pipe(source, target):
                    try:
                        while data := source.recv(65536):
                            target.sendall(data)
                    except OSError:
                        pass
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

                back = threading.Thread(target=pipe, args=(upstream, self.connection), daemon=True)
                back.start()
                pipe(self.connection, upstream)
                back.join(5)
                upstream.close()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class CertificateTrust(Servers):
    """The certificate the user is asked to trust at the Nextcloud login."""

    def test_certificate_comes_through_the_proxy_the_requests_take(self):
        import os

        from omapod import config
        from omapod.net import http
        from tests.fakeserver import self_signed_certificate

        tls = self_signed_certificate(self.tmp)
        if tls is None:
            self.skipTest("openssl is not installed")
        server, proxy = self.host(tls=tls), TunnelProxy()
        self.addCleanup(proxy.close)
        config.settings().set("proxy", {"type": "http", "host": "127.0.0.1", "port": proxy.port, "username": "me"})
        config.credentials().set("proxy", {"password": "pw"})
        with mock.patch.dict(os.environ, {"no_proxy": "", "NO_PROXY": ""}):  # the sandbox exempts 127.0.0.1
            der = http.peer_certificate(server.base)
        self.assertEqual(der, ssl.PEM_cert_to_DER_cert(Path(tls[0]).read_text()))
        self.assertEqual(proxy.seen, [(server.base.removeprefix("https://"),
                                       "Basic " + base64.b64encode(b"me:pw").decode())])

    def test_servers_on_the_internet_get_the_stronger_warning(self):
        from omapod.sync import providers

        with mock.patch.object(providers, "is_private_host", lambda host: host == "nas.lan"):
            self.assertTrue(providers.UntrustedCertificate("https://nas.lan", {}).private)
            self.assertFalse(providers.UntrustedCertificate("https://cloud.example", {}).private)


class NextcloudLoginAddresses(Servers):
    """Marketplace review of 0.1.1: behind a proxy that ends TLS, Nextcloud names http:// addresses in the Login
    Flow, and the poll token, all it takes to collect the app password, went there in plain text although the
    login ran over https."""

    def test_addresses_keep_their_path_on_the_login_origin(self):
        from omapod.sync.providers import on_server

        for server, named, expected in (
                ("https://cloud.example", "http://cloud.example/login/v2/poll", "https://cloud.example/login/v2/poll"),
                ("https://cloud.example/nc", "http://cloud.example:8080/nc/index.php/login/v2/poll?x=1",
                 "https://cloud.example/nc/index.php/login/v2/poll?x=1"),  # the proxy's inner port
                ("https://192.168.1.5", "https://cloud.home.lan/login/v2/flow/abc",
                 "https://192.168.1.5/login/v2/flow/abc"),  # another name for the server
                ("https://cloud.example:8443", "https://u:p@cloud.example/login/v2/poll#x",
                 "https://cloud.example:8443/login/v2/poll"),
                ("https://cloud.example", "https://cloud.example/login/v2/poll", "https://cloud.example/login/v2/poll"),
                ("http://cloud.example", "https://cloud.example/login/v2/poll",
                 "https://cloud.example/login/v2/poll")):  # an https upgrade stays
            self.assertEqual(on_server(server, named), expected, named)

    def test_token_and_app_password_stay_with_the_server_the_login_ran_on(self):
        from omapod.net import http
        from omapod.sync.providers import NextcloudLoginFlow
        from tests.fakeserver import FakeServer, self_signed_certificate

        tls = self_signed_certificate(self.tmp, "DNS:localhost,IP:127.0.0.1")
        if tls is None:
            self.skipTest("openssl is not installed")
        pin = http.certificate_sha256(ssl.PEM_cert_to_DER_cert(Path(tls[0]).read_text()))
        with FakeServer(str(FIX / "tone.mp3"), tls=tls) as cloud, FakeServer(str(FIX / "tone.mp3")) as plain:
            port = cloud.base.rsplit(":", 1)[1]
            for named in (plain.base,  # http:// and the proxy's inner port
                          f"http://127.0.0.1:{port}",  # http:// on the server's own port
                          f"https://localhost:{port}"):  # another name for the server
                with self.subTest(named=named):
                    cloud.sync.login_base = cloud.sync.login_server = named
                    cloud.sync.login_polls = 0
                    flow = NextcloudLoginFlow(cloud.base, pin)
                    self.assertEqual(flow.start(), cloud.base + "/login/v2/flow/abc")
                    self.assertIsNone(flow.poll())
                    self.assertEqual(flow.poll(), (cloud.base, "user", "pass"))
            self.assertEqual(plain.httpd.requests, [])
            self.assertEqual(cloud.httpd.requests.count(("POST", "/login/v2/poll")), 6)


class SizeLimits(Servers):
    def test_compressed_answers_unpack_to_at_most_the_limit(self):
        from omapod.net import http

        server = self.host()
        bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), 9)  # 64 KB on the wire
        server.routes["/bomb"] = (200, {"Content-Encoding": "gzip"}, bomb)
        server.routes["/sniffed"] = (200, {}, bomb)  # gzip without saying so is unpacked as well
        with self.assertRaises(http.HttpError):
            http.request(server.base + "/bomb", max_bytes=1024 * 1024)
        with self.assertRaises(http.HttpError):
            http.request(server.base + "/sniffed", max_bytes=1024 * 1024)
        text = b"<rss>" + b"x" * 5000 + b"</rss>"
        raw = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        members = gzip.compress(text[:100]) + gzip.compress(text[100:])  # two gzip members, as some servers send
        server.routes["/gzip"] = (200, {"Content-Encoding": "gzip"}, members)
        server.routes["/deflate"] = (200, {"Content-Encoding": "deflate"}, zlib.compress(text))
        server.routes["/raw-deflate"] = (200, {"Content-Encoding": "deflate"}, raw.compress(text) + raw.flush())
        for path in ("/gzip", "/deflate", "/raw-deflate"):
            with self.subTest(path=path):
                self.assertEqual(http.request(server.base + path, max_bytes=len(text)).body, text)


class Downloads(Servers):
    def episode(self, server, path, title="Episode", size=1000):
        from omapod.models import FEED_SUBSCRIBED

        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": server.base + "/feed.xml", "title": "Podcast",
                                                 "state": FEED_SUBSCRIBED})
            return self.db.insert_episode(conn, {"feed_id": feed_id, "guid": path, "title": title,
                                                 "media_url": server.base + path, "media_type": "audio/mpeg",
                                                 "media_size": size})

    def download(self, episode_id):
        from omapod.net.downloads import DownloadManager

        manager = DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        manager.start()
        self.library.download([episode_id])
        deadline = time.monotonic() + 30
        while manager.active_ids() and time.monotonic() < deadline:
            time.sleep(0.05)
        manager.stop()
        episode = self.db.get_episode(episode_id)
        folder = self.library.feed_folder(self.db.get_feed(episode.feed_id))
        return episode, sorted(p.name for p in folder.iterdir()), self.db.get_log(1)[0]

    def test_a_stream_without_end_stops_at_the_limit(self):
        server = self.host()
        server.routes["/live.mp3"] = Host.endless
        episode_id = self.episode(server, "/live.mp3", size=100_000)
        with mock.patch("omapod.net.downloads.UNKNOWN_SIZE_LIMIT", 4 * 1024 * 1024):
            episode, files, log = self.download(episode_id)
        self.assertFalse(episode.is_downloaded)
        self.assertEqual(files, [])  # no .part file left behind
        self.assertIn("4194304 bytes", log["detail"])

    def test_a_download_stops_before_the_disk_is_full(self):
        from collections import namedtuple

        server = self.host()
        server.routes["/live.mp3"] = Host.endless
        episode_id = self.episode(server, "/live.mp3")
        usage = namedtuple("usage", "total used free")
        with mock.patch("omapod.net.downloads.SPACE_CHECK_EVERY", 1024 * 1024), \
                mock.patch("omapod.net.downloads.shutil.disk_usage", lambda _path: usage(1, 1, 1024)):
            episode, files, log = self.download(episode_id)
        self.assertFalse(episode.is_downloaded)
        self.assertEqual(files, [])
        self.assertEqual(log["reason"], "There is not enough space left on your device.")

    def test_long_japanese_title_fits_the_file_system(self):
        server = self.host()
        server.routes["/ep.mp3"] = (200, {"Content-Type": "audio/mpeg"}, (FIX / "tone.mp3").read_bytes())
        episode, files, log = self.download(self.episode(server, "/ep.mp3", title="日本語のタイトル" * 12))
        self.assertTrue(episode.is_downloaded, log)
        self.assertEqual(files, [Path(episode.file_path).name])
        self.assertLessEqual(len(files[0].encode()), 255)
        self.assertTrue(files[0].startswith("日本語のタイトル"))


class Covers(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="omapod-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def png(self, width, height):
        """A grey PNG; a huge one stays small on disk because its rows compress so well."""
        import struct

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        packer, row = zlib.compressobj(9), b"\0" + b"\x80" * width
        rows = b"".join([packer.compress(row) for _ in range(height)] + [packer.flush()])
        path = self.tmp / f"{width}x{height}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                         + chunk(b"IDAT", rows) + chunk(b"IEND", b""))
        return path

    def test_sizes_come_from_the_file_header(self):
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        from omapod.core.images import image_size

        jpeg = self.tmp / "cover.jpg"
        GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 300, 200).savev(str(jpeg), "jpeg", [], [])
        files = {
            "cover.png": (self.png(640, 480).read_bytes(), (640, 480)),
            "cover.jpg": (jpeg.read_bytes(), (300, 200)),
            "cover.gif": (b"GIF89a\x20\x4e\x20\x4e" + b"\0" * 20, (20000, 20000)),
            "vp8x.webp": (b"RIFF\0\0\0\0WEBPVP8X\0\0\0\0\0\0\0\0" + (19999).to_bytes(3, "little") * 2 + b"\0" * 4,
                          (20000, 20000)),
            "vp8l.webp": (b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x2f" + ((1499 << 14) | 1999).to_bytes(4, "little")
                          + b"\0" * 8, (2000, 1500)),
            "vp8.webp": (b"RIFF\0\0\0\0WEBPVP8 \0\0\0\0\0\0\0\x9d\x01\x2a" + (3000).to_bytes(2, "little")
                         + (2000).to_bytes(2, "little") + b"\0" * 4, (3000, 2000)),
            "cut.jpg": (jpeg.read_bytes()[:26], None),  # ends inside the frame header
            "cover.svg": (b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>", None),
        }
        for name, (data, expected) in files.items():
            with self.subTest(name=name):
                (self.tmp / name).write_bytes(data)
                self.assertEqual(image_size(self.tmp / name), expected)

    def test_huge_cover_is_refused_before_it_is_decoded(self):
        import resource

        from omapod.core.images import ImageCache

        cache = ImageCache(self.tmp / "cache")
        bomb = self.png(20000, 20000)
        self.assertLess(bomb.stat().st_size, 1024 * 1024)
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        done = threading.Event()
        result = []
        cache.request(bomb.as_uri(), 256, lambda path: (result.append(path), done.set()))
        self.assertTrue(done.wait(30))
        self.assertEqual(result, [None])
        grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) // 1024
        self.assertLess(grown_mb, 200, "the picture was decoded after all")

    def test_normal_cover_becomes_a_square_thumbnail(self):
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        from omapod.core.images import ImageCache

        cache = ImageCache(self.tmp / "cache")
        for width, height, expected in ((3000, 2000, 256), (100, 80, 80)):
            source = self.png(width, height)
            thumb = GdkPixbuf.Pixbuf.new_from_file(cache._render(str(source), source.as_uri(), 256))
            self.assertEqual((thumb.get_width(), thumb.get_height()), (expected, expected))

    def test_cache_keeps_to_its_limit_and_keeps_what_is_shown(self):
        import os

        from omapod.core.images import ImageCache

        folder = self.tmp / "cache"
        folder.mkdir()
        old = time.time() - 86400
        for number in range(10):  # 10 originals of 100 KB from yesterday, the oldest first
            path = folder / f"{number}.img"
            path.write_bytes(b"\0" * 100_000)
            os.utime(path, (old + number, old + number))
        shown = folder / "shown-256.png"
        shown.write_bytes(b"\0" * 100_000)
        os.utime(shown, (old - 1000, old - 1000))
        cache = ImageCache(folder, limit=500_000)  # trims once at the start, in the background
        cache._pool.shutdown(wait=True)
        left = sorted(path.name for path in folder.iterdir())
        self.assertEqual(left, ["6.img", "7.img", "8.img", "9.img"])  # 400 KB: 80 % of the limit
        # A thumbnail in use counts as recent, so trimming passes it by.
        shown.write_bytes(b"\0" * 100_000)
        os.utime(shown, (old - 1000, old - 1000))
        with mock.patch.object(cache, "thumb_path", lambda _url, _size: shown):
            self.assertEqual(cache.thumb_if_cached("https://podcast.example/cover.jpg", 256), str(shown))
        cache.limit = 250_000
        cache.trim()
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["9.img", "shown-256.png"])


class LocalFolders(Servers):
    """Episodes of an added folder are the user's own files, not downloads."""

    def add_folder(self, *names):
        folder = Path(self.tmp) / "audiobooks"
        folder.mkdir(exist_ok=True)
        for name in names:
            shutil.copy(FIX / "tone.mp3", folder / name)
        feed_id = self.library.add_local_folder(str(folder))
        return folder, {e.title: e.id for e in self.db.get_episodes(feed_id=feed_id)}

    def test_files_stay_unless_the_user_confirms_and_then_go_to_the_trash(self):
        from omapod.models import PLAYED

        folder, ids = self.add_folder("chapter-01.mp3", "chapter-02.mp3")
        self.db.update_episodes(list(ids.values()), state=PLAYED)
        trashed = []
        with mock.patch("omapod.core.library.move_to_trash", trashed.append):
            self.assertEqual(self.library.delete_played_downloads(), 0)
            self.assertEqual(self.library.delete_downloads(list(ids.values())), 0)
            self.settings.set("auto_delete", True)
            self.library.mark_played(list(ids.values()))  # automatic deletion leaves them alone by default
            self.assertEqual(trashed, [])
            self.assertEqual(self.db.count_episodes(feed_id=self.db.get_episode(ids["chapter-01"]).feed_id), 2)
            self.assertEqual(self.library.delete_downloads([ids["chapter-01"]], local_files=True), 1)
            self.settings.set("auto_delete_local", True)
            self.library.mark_played([ids["chapter-02"]])
        self.assertEqual(trashed, [str(folder / "chapter-01.mp3"), str(folder / "chapter-02.mp3")])
        self.assertTrue((folder / "chapter-01.mp3").exists())  # only move_to_trash may touch them
        self.assertIsNone(self.db.get_episode(ids["chapter-01"]))

    def test_a_file_that_cannot_go_to_the_trash_stays(self):
        folder, ids = self.add_folder("chapter-01.mp3")

        def refuse(path):
            raise OSError("Trashing on system internal mounts is not supported")

        with mock.patch("omapod.core.library.move_to_trash", refuse):
            self.assertEqual(self.library.delete_downloads(list(ids.values()), local_files=True), 0)
        self.assertTrue((folder / "chapter-01.mp3").exists())
        self.assertIsNotNone(self.db.get_episode(ids["chapter-01"]))

    def test_move_to_trash_uses_the_desktop_trash(self):
        import os
        import subprocess
        import sys

        home = Path(self.tmp) / "home"
        (home / "music").mkdir(parents=True)
        song = home / "music" / "song.mp3"
        song.write_bytes(b"audio")
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local" / "share"))
        subprocess.run([sys.executable, "-c", "import sys; from omapod.core.library import move_to_trash; "
                        "move_to_trash(sys.argv[1])", str(song)], env=env, check=True,
                       cwd=Path(__file__).resolve().parents[1])
        self.assertFalse(song.exists())
        self.assertEqual((home / ".local/share/Trash/files/song.mp3").read_bytes(), b"audio")


class SlowInput(unittest.TestCase):
    """Inputs that took seconds to minutes before (time grew with the square of their length)."""

    def assertQuick(self, work, seconds=1.0):
        start = time.perf_counter()
        result = work()
        self.assertLess(time.perf_counter() - start, seconds)
        return result

    def test_feed_values(self):
        from omapod.feeds.parser import parse_date, parse_duration

        self.assertEqual(self.assertQuick(lambda: parse_date("Mon, 01 Jan 2024" + " " * 20000 + "x")), 0)
        self.assertEqual(self.assertQuick(lambda: parse_duration("1" + " " * 20000 + "x")), 0)
        self.assertEqual(parse_date("  Wed, 08 Oct 2026\n   08:00:00 GMT  "), 1791446400)

    def test_transcripts(self):
        from omapod.feeds.transcripts import parse_transcript

        self.assertQuick(lambda: parse_transcript("<time>" * 20000, "text/html"))
        self.assertQuick(lambda: parse_transcript("1" * 40000, "application/x-subrip"))
        self.assertQuick(lambda: parse_transcript("00:00:00.000 --> 00:00:01.000\n" + "<v x" * 20000, "text/vtt"))
        cues = "\n\n".join("00:00:00,000 --> 00:00:00,000\nword" for _ in range(20000))
        self.assertLessEqual(max(len(s.text) for s in self.assertQuick(lambda: parse_transcript(cues, "text/vtt"))),
                             2100)
        html_text = ("<cite>Bob:</cite>\n<time>0:05</time>\n<p>Yes.</p><time datetime='x'>0:07</time><p>Next.</p>"
                     "<cite>Ann:</cite> <TIME>1:09</TIME> <p>No <b>way</b>.</p>")
        self.assertEqual([(s.start, s.speaker, s.text) for s in parse_transcript(html_text, "text/html")],
                         [(5000, "Bob", "Yes."), (7000, "", "Next."), (69000, "Ann", "No way.")])

    def test_show_notes(self):
        import gi

        gi.require_version("Gtk", "4.0")
        from omapod.ui.widgets.shownotes import to_markup
        from omapod.util import html_to_text

        self.assertQuick(lambda: to_markup("<p>x</p>" * 20000))
        self.assertQuick(lambda: to_markup("<ul>" * 10000 + "<li>deep</li>"))
        self.assertQuick(lambda: html_to_text("<" * 100000))
        self.assertEqual(to_markup("<ul><li>a</li><li>b<ul><li>c</li></ul></li></ul><p>12:34 end</p>"),
                         '• a\n• b\n\n    • c\n\n<a href="omapod-seek:12:34">12:34</a> end')

    # GTK lays out labels on the main thread, in quadratic time for long paragraphs and words and in time that
    # grows with links × text: a show note of 100,000 characters without a space took five minutes.

    def test_show_notes_stay_light_for_the_label(self):
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Pango", "1.0")
        from gi.repository import Pango

        from omapod.ui.widgets import shownotes

        cases = {
            "one word": "x" * 300_000,
            "one paragraph": "<p>" + "word &amp; word " * 20_000 + "</p>",
            "entities only": "&amp;" * 30_000,
            "many links": "\n".join(f"https://example.com/{i} at 1:{i % 60:02d}" for i in range(8000)),
        }
        for name, value in cases.items():
            with self.subTest(name):
                markup = self.assertQuick(lambda value=value: shownotes.to_markup(value))
                Pango.parse_markup(re.sub(r"</?a\b[^>]*>", "", markup), -1, "\0")  # still valid
                text = re.sub(r"<[^>]*>", "", markup)  # with entities like &amp;, as the label gets it
                shown = html.unescape(text).replace("\n", "")
                self.assertLessEqual(len(shown), shownotes.MAX_CHARS + len("…"))
                self.assertLessEqual(max(len(line) for line in text.split("\n")), shownotes.MAX_PARAGRAPH)
                links = markup.count("<a ")
                self.assertEqual(links, markup.count("</a>"))
                self.assertLessEqual(links * len(markup), shownotes.LINK_BUDGET)
        typical = "<p>Notes</p><ul>" + "".join(f"<li>{m}:00 <a href='https://e.x/{m}'>Topic</a></li>"
                                               for m in range(60)) + "</ul>"
        self.assertEqual(shownotes.to_markup(typical).count("<a "), 120)  # every link and timestamp

    def test_show_notes_lay_out_quickly(self):
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        if Gdk.Display.get_default() is None:
            self.skipTest("needs a display, e.g. xvfb-run")
        from omapod.ui.widgets.shownotes import ShowNotes

        label = ShowNotes(None)
        Gtk.Window(child=label)
        label.set_notes("x" * 300_000)
        self.assertQuick(lambda: label.measure(Gtk.Orientation.VERTICAL, 600), 10)

    def test_names_and_transcripts_stay_light_for_their_labels(self):
        from omapod.feeds.chapters import from_mpv, parse_json_chapters
        from omapod.feeds.parser import parse_feed
        from omapod.feeds.transcripts import parse_transcript
        from omapod.ui.pages.nowplaying import TRANSCRIPT_MAX_CHARS, _shown_part
        from omapod.util import MAX_NAME

        long = "x" * 100_000
        feed = parse_feed(f"<rss><channel><title>{long}</title><item><title>{long}</title><guid>g</guid>"
                          "</item></channel></rss>".encode())
        self.assertEqual((len(feed.title), len(feed.items[0].title)), (MAX_NAME, MAX_NAME))
        self.assertTrue(feed.items[0].title.endswith("x…"))
        self.assertEqual(len(parse_json_chapters(json.dumps({"chapters": [{"startTime": 0, "title": long}]}))[0]
                             ["title"]), MAX_NAME)
        self.assertEqual(len(from_mpv([{"time": 0, "title": long}])[0]["title"]), MAX_NAME)
        for data, mime in ((("word " * 400_000).encode(), "text/plain"), (b"x" * 2_000_000, "text/plain"),
                           (json.dumps({"segments": [{"startTime": 0, "body": long}]}).encode(), "application/json")):
            with self.subTest(mime=mime, size=len(data)):
                segments = _shown_part(self.assertQuick(lambda data=data, mime=mime: parse_transcript(data, mime)))
                self.assertLessEqual(sum(len(s.text) for s in segments), TRANSCRIPT_MAX_CHARS)
                self.assertLessEqual(max(len(s.text) for s in segments), 2100)
                self.assertLessEqual(max(len(word) for s in segments for word in re.split(r"[\s​]", s.text)),
                                     100)
        self.assertEqual(parse_transcript("1\n00:00:01,000 --> 00:00:02,000\nShort and sweet.\n", "text/srt")[0]
                         .text, "Short and sweet.")


class OddNumbers(unittest.TestCase):
    """Numbers that are infinite, not a number or too large for SQLite: skipped, never the whole feed or list."""

    def test_feed_with_odd_numbers_is_read_and_stored(self):
        from omapod.feeds.parser import parse_feed
        from omapod.models import FEED_SUBSCRIBED

        item = ("<item><title>{n}</title><guid>{n}</guid><enclosure url='https://p.example/{n}.mp3' "
                "type='audio/mpeg' length='{length}'/>{extra}</item>")
        items = "".join(item.format(n=n, length=length, extra=extra) for n, length, extra in (
            ("a", "1e999", "<itunes:duration>inf</itunes:duration><itunes:season>1e999</itunes:season>"),
            ("b", "99999999999999999999", "<itunes:episode>99999999999999999999</itunes:episode>"),
            ("c", "-5", "<itunes:duration>nan</itunes:duration><itunes:episode>3</itunes:episode>"),
            ("d", "50000", "<itunes:duration>99999999999999999999h</itunes:duration>")))
        xml = ("<rss version='2.0' xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'><channel><title>t"
               f"</title>{items}</channel></rss>")
        feed = parse_feed(xml.encode(), "https://p.example/feed.xml")
        self.assertEqual([(i.media_size, i.duration, i.season, i.episode_number) for i in feed.items],
                         [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 3), (50000, 0, 0, 0)])
        tmp, _settings, db, library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(db.close)
        with db.tx() as conn:
            feed_id = db.insert_feed(conn, {"url": "https://p.example/feed.xml", "title": "t",
                                            "state": FEED_SUBSCRIBED})
            library._merge(conn, feed_id, feed, first_fetch=True)
        self.assertEqual(db.count_episodes(feed_id=feed_id), 4)

    def test_chapters_and_transcripts_skip_what_they_cannot_place(self):
        from omapod.feeds.chapters import from_mpv, parse_json_chapters
        from omapod.feeds.transcripts import parse_transcript

        chapters = parse_json_chapters('{"chapters": [{"startTime": NaN}, {"startTime": Infinity}, '
                                       '{"startTime": 1e400}, {"startTime": -3}, {"startTime": 5, "title": "ok"}]}')
        self.assertEqual([c["title"] for c in chapters], ["ok"])
        self.assertEqual(len(from_mpv([{"time": float("nan")}, {"time": float("inf")}, {"time": 1.5}])), 1)
        segments = parse_transcript('[{"startTime": 1e400, "body": "a"}, {"startTime": 2, "body": "b"}]',
                                    "application/json")
        self.assertEqual([s.text for s in segments], ["b"])


class SyncMatching(unittest.TestCase):
    def test_a_guid_counts_with_its_podcast(self):
        from omapod.models import FEED_SUBSCRIBED

        tmp, _settings, db, _library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(db.close)
        ids = {}
        with db.tx() as conn:
            for name in ("a", "b"):
                feed_id = db.insert_feed(conn, {"url": f"https://{name}.example/feed", "title": name,
                                                "state": FEED_SUBSCRIBED})
                ids[name] = db.insert_episode(conn, {"feed_id": feed_id, "guid": "1", "title": name,
                                                     "media_url": f"https://{name}.example/1.mp3"})
            ids["unique"] = db.insert_episode(conn, {"feed_id": feed_id, "guid": "only-here", "title": "u",
                                                     "media_url": "https://b.example/u.mp3"})

        def found(*args):
            episode = db.find_episode_for_sync(*args)
            return episode.id if episode else None

        self.assertEqual(found("1", "https://b.example/1.mp3", "https://b.example/feed"), ids["b"])
        self.assertEqual(found("1", "", "https://b.example/feed"), ids["b"])
        self.assertEqual(found("1", "https://b.example/1.mp3", "https://moved.example/feed"), ids["b"])
        self.assertIsNone(found("1", "", "https://moved.example/feed"))  # two podcasts have it: no guessing
        self.assertEqual(found("only-here", "", None), ids["unique"])


class DatabaseImport(unittest.TestCase):
    def test_copy_of_the_replaced_database_includes_the_latest_changes(self):
        import glob
        import sqlite3

        from omapod.__main__ import _apply_pending_import

        tmp, _settings, db, _library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        from omapod import config

        path = str(config.paths.db)
        with db.tx() as conn:
            conn.execute("INSERT INTO feeds(url, title, state) VALUES('https://recent.example/', 'Recent', 1)")
        self.assertTrue(Path(path + "-wal").stat().st_size)  # still in the write-ahead log
        source, imported = sqlite3.connect(path), sqlite3.connect(path + ".import")
        source.backup(imported)
        imported.execute("UPDATE feeds SET title = 'Imported'")
        imported.commit()
        imported.close()
        source.close()
        _apply_pending_import()  # with the database still open, as a worker thread's connection may keep it
        db.close()
        titles = lambda file: [r[0] for r in sqlite3.connect(file).execute("SELECT title FROM feeds")]  # noqa: E731
        self.assertEqual(titles(path), ["Imported"])
        backup = glob.glob(path + ".before-import-*")[0]
        self.assertEqual(titles(backup), ["Recent"])
        self.assertEqual(Path(backup).stat().st_mode & 0o777, 0o600)


class ImportedBackups(Servers):
    """Marketplace review of 0.1.1: the database names the files of downloads, and an imported backup can name any
    file of the user. omaPOD deletes and moves only files in its download folder, and an import forgets the rest."""

    def setUp(self):
        super().setUp()
        from omapod import config

        config._settings = self.settings  # one settings object, as in the app (downloads_dir reads it)
        self.documents = Path(self.tmp) / "home" / "Documents"
        self.documents.mkdir(parents=True)
        self.victim = self.documents / "thesis.pdf"
        self.victim.write_bytes(b"years of work")
        self.feeds = 0

    def downloaded(self, path, **episode):
        """An unplayed episode of an https podcast (auto delete on) that claims `path` as its download."""
        from omapod.models import FEED_SUBSCRIBED, UNPLAYED

        self.feeds += 1
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": f"https://p{self.feeds}.example/feed.xml", "title": "Imported",
                                                 "state": FEED_SUBSCRIBED, "auto_delete": 1})
            episode_id = self.db.insert_episode(conn, {"feed_id": feed_id, "guid": "g", "title": "Episode",
                                                       "media_url": f"https://p{self.feeds}.example/1.mp3",
                                                       "file_path": str(path), "downloaded_at": 1, "state": UNPLAYED,
                                                       **episode})
        return feed_id, episode_id

    def test_files_outside_the_download_folder_stay_whatever_removes_a_download(self):
        from omapod.config import downloads_dir
        from omapod.events import bus

        messages = []
        self.addCleanup(bus.unsubscribe, bus.subscribe("message", lambda text, kind: messages.append(text)))
        # A podcast folder in the download folder that is a link to the documents.
        downloads_dir().mkdir(parents=True, exist_ok=True)
        (downloads_dir() / "Linked").symlink_to(self.documents)
        self.settings.update({"episode_cache": 1, "episode_cleanup": -3})
        for name, path, remove in (
                ("mark all as played", self.victim, lambda feed, episode: self.library.mark_feed_played(feed)),
                ("delete download", self.victim, lambda feed, episode: self.library.delete_downloads([episode])),
                ("remove podcast", self.victim, lambda feed, episode: self.library.unsubscribe([feed])),
                ("episode cleanup", self.victim, lambda feed, episode: self.library.cleanup(1)),
                ("linked folder", downloads_dir() / "Linked" / "thesis.pdf",
                 lambda feed, episode: self.library.delete_downloads([episode]))):
            with self.subTest(name):
                feed_id, episode_id = self.downloaded(path)
                remove(feed_id, episode_id)
                self.assertEqual(self.victim.read_bytes(), b"years of work")
                episode = self.db.get_episode(episode_id)
                self.assertTrue(episode is None or not episode.is_downloaded)  # forgotten, though
        self.assertIn("thesis.pdf is not in the download folder, so omaPOD left it where it is.", messages)
        # Changing the download folder moves only downloads.
        _feed, episode_id = self.downloaded(self.victim)
        self.library.move_downloads(str(Path(self.tmp) / "new-downloads"))
        self.assertEqual(self.victim.read_bytes(), b"years of work")
        self.assertEqual(self.db.get_episode(episode_id).file_path, str(self.victim))

    def test_downloads_in_the_download_folder_still_go(self):
        feed_id, episode_id = self.downloaded("")
        folder = self.library.feed_folder(self.db.get_feed(feed_id))
        folder.mkdir(parents=True)
        (folder / "episode.mp3").write_bytes(b"audio")
        self.db.update_episode(episode_id, file_path=str(folder / "episode.mp3"))
        moved = Path(self.tmp) / "new-downloads"
        self.assertEqual(self.library.move_downloads(str(moved)), 1)
        self.assertEqual(self.db.get_episode(episode_id).file_path, str(moved / folder.name / "episode.mp3"))
        self.library.mark_feed_played(feed_id)
        self.assertFalse((moved / folder.name / "episode.mp3").exists())
        self.assertFalse(self.db.get_episode(episode_id).is_downloaded)

    def backup(self, *statements):
        """A database file as someone could hand it over: this library, plus `statements`."""
        import sqlite3

        path = Path(self.tmp) / f"backup-{len(statements)}-{time.monotonic_ns()}.db"
        copy = sqlite3.connect(path)
        self.db.conn().backup(copy)
        for statement in statements:
            copy.execute(statement)
        copy.commit()
        copy.close()
        return str(path)

    def test_import_takes_only_plain_tables_and_forgets_foreign_files(self):
        import sqlite3

        from omapod.config import downloads_dir
        from omapod.ui.preferences import check_import, prepare_import

        _feed, foreign = self.downloaded(self.victim)
        feed_id, own = self.downloaded("")
        own_file = self.library.feed_folder(self.db.get_feed(feed_id)) / "episode.mp3"
        self.db.update_episode(own, file_path=str(own_file))
        folder = Path(self.tmp) / "audiobooks"
        local_id = self.library.add_local_folder(str(folder))
        with self.db.tx() as conn:
            inside, outside = (self.db.insert_episode(conn, {"feed_id": local_id, "guid": name, "title": name,
                                                             "file_path": str(path), "downloaded_at": 1})
                               for name, path in (("inside", folder / "a.mp3"), ("outside", self.victim)))
        # Triggers, views and virtual tables would run their own SQL whenever omaPOD changes its database.
        for extra in ("CREATE TRIGGER t AFTER UPDATE ON episodes BEGIN UPDATE episodes SET file_path = "
                      f"'{self.victim}', downloaded_at = 1; END",
                      "CREATE VIEW v AS SELECT * FROM episodes",
                      "CREATE VIRTUAL TABLE r USING rtree(id, a, b)"):
            with self.subTest(extra.split(" AS ")[0][:24]):
                self.assertEqual(check_import(self.backup(extra)), "This file is not an omaPOD database.")
        path = self.backup()
        self.assertIsNone(check_import(path))
        self.assertIsNone(prepare_import(path))
        rows = dict(sqlite3.connect(path).execute("SELECT id, file_path FROM episodes"))
        self.assertEqual((rows[foreign], rows[own], rows[inside], rows[outside]),
                         ("", str(own_file), str(folder / "a.mp3"), ""))
        self.assertTrue(str(own_file).startswith(str(downloads_dir())))
        damaged = Path(self.backup())
        conn = sqlite3.connect(damaged)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'episodes'").fetchone()[0]
        conn.close()
        data = bytearray(damaged.read_bytes())
        data[(root - 1) * page_size] = 0x0f  # no valid page type for the episodes table's first page
        damaged.write_bytes(bytes(data))
        self.assertRegex(check_import(str(damaged)) or "", r"^This file is not an omaPOD database \(.+\)\.$")


class TwoProcesses(Servers):
    """The app and the hourly background refresh share settings, database and download folder."""

    def test_settings_written_by_both_keep_each_others_changes(self):
        from omapod.config import Settings

        app, refresh = Settings(Path(self.tmp) / "shared.json"), Settings(Path(self.tmp) / "shared.json")
        app.update({"queue_sync_base": "abc", "speed": 1.5})
        refresh.set("sync_last_actions", 1111)  # does not know about the app's changes
        app.set("speed", 2.0)  # and the app's copy does not know the refresh's
        app.reset("queue_sync_base")
        on_disk = json.loads((Path(self.tmp) / "shared.json").read_text())
        self.assertEqual(on_disk, {"sync_last_actions": 1111, "speed": 2.0})
        self.assertEqual(app.get("sync_last_actions"), 1111)

    def test_background_refresh_makes_way_for_the_app(self):
        import os
        import subprocess
        import sys

        from omapod.config import instance_lock

        holder = subprocess.Popen(
            [sys.executable, "-c", "import signal, sys, time; from omapod.config import instance_lock\n"
             "lock = instance_lock('refresh'); print('held', flush=True)\n"
             "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(30)"],
            cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertIsNone(instance_lock("refresh"))  # a second refresh does not start
        self.assertIsNone(instance_lock("app", stop_refresh=True))  # asks the refresh to stop
        self.assertEqual(holder.wait(10), 0)
        lock = instance_lock("app")
        self.assertIsNotNone(lock)
        self.assertIsNone(instance_lock("refresh"))
        lock.close()

    def test_a_download_running_in_the_other_process_is_left_alone(self):
        import fcntl

        from omapod.models import FEED_SUBSCRIBED
        from omapod.net.downloads import DownloadManager

        server = self.host()
        server.routes["/ep.mp3"] = (200, {"Content-Type": "audio/mpeg"}, (FIX / "tone.mp3").read_bytes())
        with self.db.tx() as conn:
            feed_id = self.db.insert_feed(conn, {"url": server.base + "/feed.xml", "title": "P",
                                                 "state": FEED_SUBSCRIBED})
            episode_id = self.db.insert_episode(conn, {"feed_id": feed_id, "guid": "e", "title": "E",
                                                       "media_url": server.base + "/ep.mp3"})
        part = self.library.feed_folder(self.db.get_feed(feed_id)) / f".{episode_id}.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(b"written by the other process")
        manager = DownloadManager(self.library, self.settings)
        self.library.downloads = manager
        manager.start()
        self.addCleanup(manager.stop)

        def download():
            self.library.download([episode_id])
            deadline = time.monotonic() + 15
            while manager.active_ids() and time.monotonic() < deadline:
                time.sleep(0.05)

        with open(part, "ab") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            download()
            self.assertEqual(part.read_bytes(), b"written by the other process")
            self.assertEqual(server.seen, [])
        part.unlink()
        download()
        self.assertTrue(self.db.get_episode(episode_id).is_downloaded)


class PrivateFiles(unittest.TestCase):
    def test_only_the_owner_can_read_what_omapod_keeps(self):
        import os
        import stat

        tmp, settings, db, _library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(db.close)
        from omapod import config

        settings.set("proxy", {"type": "http", "host": "proxy", "port": 3128, "username": "u", "password": "secret"})
        with db.tx() as conn:
            conn.execute("INSERT INTO feeds(url, title, state, username, password) "
                         "VALUES('https://p.example/', 'p', 1, 'u', 'secret')")

        def mode(path):
            return stat.S_IMODE(os.stat(path).st_mode)

        for folder in (config.paths.config, config.paths.data, config.paths.cache, config.paths.runtime):
            self.assertEqual(mode(folder), 0o700, folder)
        for path in (config.paths.settings_file, config.paths.db, str(config.paths.db) + "-wal"):
            self.assertEqual(mode(path), 0o600, path)
        from types import SimpleNamespace

        from omapod.ui.preferences import export_database

        export = Path(tmp) / "export.db"
        export_database(SimpleNamespace(db=db), str(export))
        self.assertEqual(mode(export), 0o600)

    def test_exports_are_private_from_their_first_byte(self):
        """Marketplace review of 0.1.1: SQLite created the database export readable for everyone (umask 022) and
        filled it before the chmod; another user could open it meanwhile and keep reading the passwords."""
        import os
        import sqlite3
        import stat
        from types import SimpleNamespace

        from omapod.ui.preferences import export_database, write_export

        tmp, _settings, db, _library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(db.close)
        with db.tx() as conn:
            conn.execute("INSERT INTO feeds(url, title, state, username, password) "
                         "VALUES('https://p.example/', 'p', 1, 'u', 'secret')")
        self.addCleanup(os.umask, os.umask(0o022))
        shared = Path(tmp) / "shared"
        shared.mkdir(mode=0o755)
        target = shared / "backup.db"
        seen = []

        def look():
            seen.extend((p.name, stat.S_IMODE(p.stat().st_mode)) for p in shared.iterdir())

        class Library:  # the library's connection, watching the folder while the copy is written
            def backup(self, destination, **kwargs):
                look()
                db.conn().backup(destination, **kwargs)
                look()

        app = SimpleNamespace(db=SimpleNamespace(conn=Library))
        export_database(app, str(target))
        self.assertTrue(seen)
        self.assertEqual([(name, mode) for name, mode in seen if mode & 0o077], [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in shared.iterdir()], ["backup.db"])  # nothing left behind
        check = sqlite3.connect(target)
        self.assertEqual(check.execute("SELECT password FROM feeds").fetchall(), [("secret",)])
        check.close()
        # An older export others can read is replaced, not written into: whoever opened it keeps the old file.
        target.chmod(0o644)
        with open(target, "rb") as held:
            export_database(app, str(target))
            self.assertNotEqual(os.fstat(held.fileno()).st_ino, target.stat().st_ino)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        for name in ("subscriptions.opml", "favorites.html"):
            (shared / name).write_text("old")
            (shared / name).chmod(0o644)
            write_export(str(shared / name), "<opml/>")
            self.assertEqual(((shared / name).read_text(), stat.S_IMODE((shared / name).stat().st_mode)),
                             ("<opml/>", 0o600))
        self.assertEqual(sorted(p.name for p in shared.iterdir()),
                         ["backup.db", "favorites.html", "subscriptions.opml"])

    def test_secrets_are_kept_apart_from_the_settings(self):
        import os
        import stat

        tmp, settings, db, _library = make_env()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(db.close)
        from omapod import config
        from omapod.net import http, search

        # Where 0.1.0 kept them.
        settings.update({"proxy": {"type": "http", "host": "proxy.lan", "port": 3128, "username": "me",
                                   "password": "proxy-pw"},
                         "podcastindex_key": "pi-key", "podcastindex_secret": "pi-secret"})
        secrets = config.credentials()
        config.move_secrets(config.settings(), secrets)
        stored = config.paths.settings_file.read_text()
        for value in ("proxy-pw", "pi-key", "pi-secret"):
            self.assertNotIn(value, stored)
        self.assertEqual(json.loads(stored)["proxy"], {"type": "http", "host": "proxy.lan", "port": 3128,
                                                       "username": "me"})
        self.assertEqual(stat.S_IMODE(os.stat(config.paths.credentials_file).st_mode), 0o600)
        self.assertEqual(secrets.get("proxy"), {"password": "proxy-pw"})
        self.assertEqual(http._configured_proxy(), "http://me:proxy-pw@proxy.lan:3128")
        with mock.patch.object(search.http, "get_json", return_value={"feeds": []}) as get_json:
            search.search("podcastindex", "news")
        self.assertEqual(get_json.call_args.kwargs["headers"]["X-Auth-Key"], "pi-key")
        config.move_secrets(config.settings(), secrets)  # nothing left to move: unchanged
        self.assertEqual(secrets.get("podcastindex"), {"key": "pi-key", "secret": "pi-secret"})

        # Logging in and out of sync leaves the other secrets alone; the file goes with the last one.
        secrets.set("sync", {"provider": "nextcloud", "username": "me", "password": "app-pw"})
        secrets.set("sync", None)
        self.assertEqual(set(secrets.load()), {"proxy", "podcastindex"})
        secrets.set("proxy", None)
        secrets.set("podcastindex", None)
        self.assertFalse(config.paths.credentials_file.exists())
        self.assertEqual(http._configured_proxy(), "http://me:@proxy.lan:3128")

    def test_parental_password_is_salted_and_old_hashes_still_work(self):
        import hashlib

        from omapod.ui.dialogs import password_hash, password_matches

        first, second = password_hash("1234"), password_hash("1234")
        self.assertTrue(first.startswith("scrypt$"))
        self.assertNotEqual(first, second)  # salted
        self.assertTrue(password_matches("1234", first))
        self.assertFalse(password_matches("1235", first))
        legacy = hashlib.sha256(b"omapod:1234").hexdigest()
        self.assertTrue(password_matches("1234", legacy))
        self.assertFalse(password_matches("", ""))
        self.assertFalse(password_matches("1234", "scrypt$broken"))


class FeedAddresses(unittest.TestCase):
    """Addresses from feeds go to mpv, the image loader, the desktop and exported pages: only web ones may."""

    FEED = """<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
        xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:psc="http://podlove.org/simple-chapters">
      <channel><title>t</title><link>javascript:alert(1)</link><itunes:image href="file:///etc/passwd"/>
        <podcast:funding url="smb://host/share">Give</podcast:funding>
        <item><title>a</title><guid>a</guid><link>/episodes/a</link><itunes:image href="file:///home/me/x.png"/>
          <enclosure url="av://lavfi:sine=frequency=440" type="audio/mpeg"/>
          <podcast:chapters url="file:///etc/hostname" type="application/json+chapters"/>
          <psc:chapters><psc:chapter start="0" title="c" href="javascript:x" image="ftp://h/i.png"/></psc:chapters>
        </item>
        <item><title>b</title><guid>b</guid><enclosure url="media/b.mp3" type="audio/mpeg"/></item>
      </channel></rss>"""

    def test_parser_keeps_only_web_addresses(self):
        from omapod.feeds.parser import parse_feed

        feed = parse_feed(self.FEED.encode(), "https://podcast.example/feed.xml")
        self.assertEqual((feed.link, feed.image_url, feed.funding), ("", "", []))
        first, second = feed.items
        self.assertEqual((first.media_url, first.image_url, first.chapters_url), ("", "", ""))
        self.assertEqual(first.link, "https://podcast.example/episodes/a")
        self.assertEqual((first.psc_chapters[0]["url"], first.psc_chapters[0]["image"]), ("", ""))
        self.assertEqual(second.media_url, "https://podcast.example/media/b.mp3")

    def test_player_opens_only_its_own_files_and_web_addresses(self):
        from omapod.models import Episode
        from omapod.player.controller import PlayerController

        player = PlayerController.__new__(PlayerController)  # only _source is needed
        player.feed = None
        for url in ("av://lavfi:sine", "file:///etc/hostname", "FILE:/etc/hostname", "memory://data", ""):
            player.episode = Episode(media_url=url)
            self.assertIsNone(player._source(), url)
        player.episode = Episode(media_url="HTTPS://podcast.example/a.mp3")
        self.assertEqual(player._source(), "HTTPS://podcast.example/a.mp3")

    def test_other_programs_see_no_stream_address(self):
        """Over MPRIS every program on the session bus reads along; a private feed's episode addresses often carry
        its access token."""
        from types import SimpleNamespace

        from omapod.models import Episode
        from omapod.player.mpris import Mpris

        mpris = Mpris.__new__(Mpris)  # only _metadata is needed
        mpris.images = SimpleNamespace(thumb_if_cached=lambda *_a: None, request=lambda *_a: None)
        stream = Episode(id=1, title="t", media_url="https://cdn.example/e.mp3?token=private")
        mpris.player = SimpleNamespace(episode=stream, duration=0)
        self.assertNotIn("xesam:url", mpris._metadata())
        self.assertNotIn("token", str(mpris._metadata()))
        downloaded = Episode(id=2, title="t", media_url=stream.media_url, file_path=__file__, downloaded_at=1)
        mpris.player = SimpleNamespace(episode=downloaded, duration=0)
        self.assertEqual(mpris._metadata()["xesam:url"].unpack(), Path(__file__).as_uri())

    def test_desktop_gets_only_web_and_mail_links(self):
        from types import SimpleNamespace

        from omapod.ui.app import OmaPodApp

        app = SimpleNamespace(window=None)
        for uri in ("file:///etc/passwd", "smb://host/share", "apt:package", " javascript:alert(1)", "/etc"):
            self.assertFalse(OmaPodApp.open_uri(app, uri), uri)

    def test_exported_pages_link_only_web_addresses(self):
        from omapod.feeds.opml import build_html_export
        from omapod.models import Feed

        page = build_html_export([Feed(url="https://podcast.example/feed.xml", title="<b>T</b>",
                                       link="javascript:alert(1)", image_url="file:///home/me/cover.png")])
        self.assertNotIn("javascript:", page)
        self.assertNotIn("file:", page)
        self.assertIn('<a href="https://podcast.example/feed.xml">&lt;b&gt;T&lt;/b&gt;</a>', page)


class LocalFiles(unittest.TestCase):
    def test_feed_addresses_cannot_read_local_files(self):
        from omapod.net import http

        for url in (f"file://{Path(__file__).resolve()}", "data:text/plain,hello", "ftp://127.0.0.1/x"):
            with self.subTest(url=url), self.assertRaises(http.HttpError):
                http.request(url, timeout=5)


class SocketFolder(unittest.TestCase):
    """Sockets of a deep development folder go to /tmp, which other users share."""

    def setUp(self):
        import os
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="omapod-sockets-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (mock.patch.object(tempfile, "tempdir", str(self.tmp)),
                        mock.patch.dict(os.environ, {"OMAPOD_RUNTIME_DIR": str(self.tmp / ("deep" * 30))})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.own = self.tmp / f"omapod-{os.getuid()}"

    def mode(self, path):
        import os
        import stat

        return stat.S_IMODE(os.stat(path).st_mode)

    def test_own_folder_is_used_and_closed_to_others(self):
        from omapod.config import Paths

        self.own.mkdir()
        self.own.chmod(0o777)
        paths = Paths()
        self.assertEqual(paths.control_socket, self.own / "control.sock")
        self.assertEqual(paths.mpv_socket.parent, self.own)
        self.assertEqual(self.mode(self.own), 0o700)

    def test_a_link_or_a_folder_of_someone_else_is_not_used(self):
        import os

        from omapod.config import Paths

        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o777)
        self.own.symlink_to(elsewhere)
        paths = Paths()
        folder = paths.control_socket.parent
        self.assertNotIn(folder, (self.own, elsewhere))
        self.assertEqual((folder.parent, self.mode(folder)), (self.tmp, 0o700))
        self.assertEqual(paths.mpv_socket.parent, folder)  # one folder for the whole run
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):  # as if another user made it
            folder = Paths().control_socket.parent
        self.assertNotEqual(folder.name, f"omapod-{os.getuid() + 1}")
        self.assertEqual(self.mode(folder), 0o700)


class ProxyLogin(Servers):
    """The password of the proxy in the settings goes to the proxy only (audit of 2026-11-03, B-21)."""

    def setUp(self):
        super().setUp()
        from omapod import config

        self.proxy = self.host()  # answers the absolute addresses a proxy is asked for
        config.settings().set("proxy", {"type": "http", "host": "127.0.0.1", "port": self.proxy.port,
                                        "username": "me"})
        config.credentials().set("proxy", {"password": "pw"})
        self.login = "Basic " + base64.b64encode(b"me:pw").decode()

    def test_redirect_to_a_host_without_the_proxy_leaves_its_password_behind(self):
        import os

        from omapod.net import http

        target = self.host("localhost")  # in no_proxy, so reached directly
        target.routes["/episode.mp3"] = (200, {}, b"ok")
        self.proxy.routes["http://feed.example/podcast.xml"] = (302, {"Location": target.base + "/episode.mp3"},
                                                                b"")
        with mock.patch.dict(os.environ, {"no_proxy": "localhost", "NO_PROXY": "localhost"}):
            response = http.request("http://feed.example/podcast.xml")
        self.assertEqual(response.body, b"ok")
        self.assertEqual(self.proxy.header("Proxy-Authorization"), [self.login])
        self.assertEqual(target.header("Proxy-Authorization"), [None])


class SearchResults(Servers):
    """Directories list feeds anyone can publish, so their results keep to the rules feeds keep to (audit of
    2026-11-03, B-20)."""

    def test_results_keep_to_the_rules_for_feeds(self):
        from omapod.net import search
        from omapod.util import MAX_NAME

        directory = self.host()
        picture = Path(self.tmp) / "Pictures" / "private.png"  # one of the user's own
        entries = [
            {"url": "https://one.example/rss", "title": "x" * 100_000, "author": "y" * 100_000,
             "logo_url": picture.as_uri(), "description": "z" * 100_000},
            {"url": "https://two.example/rss", "title": None, "logo_url": "https://cdn.example/two.png"},
            {"url": "https://three.example/rss", "title": "Three", "scaled_logo_url": "/home/someone/picture.png"},
            "not an object", {"title": "without an address"}, {"url": ["not", "a", "string"]},
        ]
        directory.routes["/search.json?q=anything"] = (200, {"Content-Type": "application/json"},
                                                       json.dumps(entries).encode())
        results = search._gpodder("anything", host=directory.base)
        self.assertEqual([r.feed_url for r in results],
                         ["https://one.example/rss", "https://two.example/rss", "https://three.example/rss"])
        one, two, three = results
        self.assertEqual((one.image_url, len(one.title), len(one.author), len(one.description)),
                         ("", MAX_NAME, MAX_NAME, search.MAX_DESCRIPTION))
        self.assertEqual((two.title, two.image_url), ("https://two.example/rss", "https://cdn.example/two.png"))
        self.assertEqual(three.image_url, "")

    def test_answers_of_another_shape_find_nothing_instead_of_failing(self):
        from omapod.net import search

        directory = self.host()
        directory.routes["/search.json?q=anything"] = (200, {}, b'{"error": "not a list"}')
        self.assertEqual(search._gpodder("anything", host=directory.base), [])
        self.assertEqual(search._entries({"results": {"not": "a list"}}, "results"), [])
        self.assertEqual(search._entries(["text", 3, {"url": "u"}]), [{"url": "u"}])
        self.assertEqual([search._result("t", "u", episodes=n).episodes for n in (12, "12", "12a", "1e999",
                                                                                   10 ** 12, -3, None)],
                         [12, 12, 0, 0, 0, 0, 0])


NAMED = ("<?xml version='1.0'?><rss version='2.0' xmlns:podcast='https://podcastindex.org/namespace/1.0'>"
         "<channel><title>Q&amp;A &lt;i&gt;Show&lt;/i&gt;</title><link>https://show.example/?a=1&amp;b=2</link>"
         "<podcast:funding url='https://pay.example/?a=1&amp;b=2'>&lt;b&gt;Support&lt;/b&gt; &amp; more"
         "</podcast:funding><item><title>One</title><guid>one</guid></item></channel></rss>").encode()


class RowsShowTextAsItIs(Servers):
    """libadwaita reads row titles and subtitles, group titles too, as markup: an "&" left them empty and a feed
    could format them (audit of 2026-11-03, B-19)."""

    def test_podcast_info_and_settings_show_names_and_addresses_as_they_are(self):
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gtk

        if Gdk.Display.get_default() is None:
            self.skipTest("needs a display, e.g. xvfb-run")
        Adw.init()
        from types import SimpleNamespace

        from omapod.ui import dialogs

        host = self.host()
        host.routes["/feed.xml?id=1&format=rss"] = (200, {"Content-Type": "application/rss+xml"}, NAMED)
        feed_id = self.library.subscribe_url(host.base + "/feed.xml?id=1&format=rss")
        feed = self.db.get_feed(feed_id)
        self.assertIn("&", feed.url)
        ctx = SimpleNamespace(db=self.db, library=self.library, settings=self.settings, window=Gtk.Window(),
                              player=None, open_uri=lambda _uri: True, copy_text=lambda _text: None,
                              toast=lambda *_args, **_kwargs: None)

        def shown(widget):
            child, texts = widget.get_first_child(), []
            while child is not None:
                if isinstance(child, Gtk.Label):
                    texts.append(child.get_text())
                texts += shown(child)
                child = child.get_next_sibling()
            return texts

        info = shown(dialogs.feed_info(ctx, feed_id))
        funding = feed.funding[0]
        for text in (feed.url, feed.link, funding["url"], funding["title"]):
            self.assertIn(text, info)
        settings = dialogs.FeedSettingsDialog(ctx, feed_id)
        settings.present(ctx.window)  # its pages are put in place when it is shown
        self.assertIn(feed.display_title, shown(settings))


if __name__ == "__main__":
    unittest.main()
