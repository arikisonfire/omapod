"""A local HTTP server for the tests: fixture feeds, media files with Range
support, HTTP auth, redirects, and in-memory gpodder.net / Nextcloud
GPodder Sync endpoints that behave like the real ones."""

import base64
import json
import shutil
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).with_name("fixtures")
# The account's files over WebDAV; the user id differs from the login name on purpose.
DAV = "/remote.php/dav/files/user-id/"


def _epoch(text):
    stamp = datetime.fromisoformat(str(text))
    return int((stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp())


class SyncState:
    def __init__(self):
        self.subscriptions = {}  # url -> (added: bool, timestamp)
        self.actions = []  # (timestamp, action dict)
        # Nextcloud's GPodder Sync (3.18): only play actions, one row per episode (matched by guid, then by
        # address), the last one uploaded wins whatever its time; 'since' compares the action's own time.
        # With keeps_other_actions (the proposed fix), each action type has its own row per episode and the list
        # leaves out all but play actions unless a client sends allActions.
        self.nextcloud_actions = []
        self.nextcloud_keeps_other_actions = False
        self.devices = {}
        self.clock = int(time.time())
        self.login_polls = 0
        # Addresses a Nextcloud behind a proxy may name in the Login Flow (base of login page and poll endpoint,
        # server in the poll answer); None: its own.
        self.login_base = None
        self.login_server = None

    def tick(self):
        # Real servers use epoch seconds; run one second ahead so changes made in the same
        # second as a client's upload are still 'newer' (keeps the tests deterministic).
        self.clock = max(self.clock + 1, int(time.time()) + 1)
        return self.clock

    def sub_changes(self, since):
        add = [u for u, (on, ts) in self.subscriptions.items() if on and ts > since]
        remove = [u for u, (on, ts) in self.subscriptions.items() if not on and ts > since]
        return {"add": add, "remove": remove, "timestamp": self.clock}

    def apply_subs(self, add, remove):
        ts = self.tick()
        for url in add:
            self.subscriptions[url] = (True, ts)
        for url in remove:
            self.subscriptions[url] = (False, ts)
        return ts

    def action_changes(self, since):
        return {"actions": [a for ts, a in self.actions if ts > since], "timestamp": self.clock}

    def add_actions(self, actions):
        ts = self.tick()
        for action in actions:
            self.actions.append((ts, action))
        self._nextcloud_store(actions)
        return ts

    def _nextcloud_store(self, actions):
        for action in actions:
            kind = str(action.get("action", "")).upper()
            if kind != "PLAY" and not self.nextcloud_keeps_other_actions:
                continue  # EpisodeActionController::filterOnlyPlays
            row = {"podcast": action["podcast"], "episode": action["episode"], "guid": action.get("guid"),
                   "action": kind, "position": action.get("position", -1),
                   "started": action.get("started", -1), "total": action.get("total", -1),
                   "epoch": _epoch(action["timestamp"])}
            rows = [r for r in self.nextcloud_actions if r["action"] == kind]
            match = next((r for r in rows if row["guid"] and r["guid"] == row["guid"]), None) or \
                next((r for r in rows if r["episode"] == row["episode"]), None)
            if match is None:
                self.nextcloud_actions.append(row)
                continue
            if not row["guid"]:
                row["guid"] = match["guid"]  # ensureGuidDoesNotGetNulledWithOldData
            self.nextcloud_actions[:] = [r for r in self.nextcloud_actions if r is not match
                                         and not (r["action"] == kind and r["episode"] == row["episode"])]
            self.nextcloud_actions.append(row)

    def nextcloud_changes(self, since, all_actions=False):
        actions = [{"podcast": r["podcast"], "episode": r["episode"], "guid": r["guid"], "action": r["action"],
                    "position": r["position"], "started": r["started"], "total": r["total"],
                    "timestamp": datetime.fromtimestamp(r["epoch"], timezone.utc).isoformat()}
                   for r in self.nextcloud_actions if r["epoch"] > since and (all_actions or r["action"] == "PLAY")]
        return {"actions": actions, "timestamp": int(time.time())}


class Handler(BaseHTTPRequestHandler):
    server_version = "fake/1.0"

    def log_message(self, *args):
        pass

    @property
    def base(self):
        return f"{self.server.scheme}://127.0.0.1:{self.server.server_address[1]}"

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _auth_ok(self, user="user", password="pass"):
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def _feed(self, name, page=1):
        text = (FIXTURES / name).read_text("utf-8").replace("https://example.org", self.base)
        if page != 1:
            text = text.replace("<atom:link rel=\"next\" href=\"" + self.base + "/feed?page=2\"/>", "")
        return text

    def _media(self, path):
        data = Path(self.server.media_file).read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            if start >= len(data):
                return self._send(416, b"", "audio/mpeg")
            chunk = data[start:]
            return self._send(206, chunk, "audio/mpeg",
                              {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return self._send(200, data, "audio/mpeg")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        state = self.server.sync
        self.server.requests.append(("GET", self.path))
        if path == "/feed.xml":
            etag = '"v1"'
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, b"", "application/rss+xml")
            return self._send(200, self._feed(self.server.feed_name), "application/rss+xml", {"ETag": etag})
        if path == "/feed" and query.get("page") == ["2"]:
            text = (FIXTURES / "feed_atom.xml").read_text("utf-8")
            return self._send(200, text, "application/atom+xml")
        if path == "/broken.xml":
            return self._send(200, self._feed("feed_broken.xml"), "application/rss+xml")
        if path == "/atom.xml":
            return self._send(200, self._feed("feed_atom.xml"), "application/atom+xml")
        if path == "/page.html":
            return self._send(200, (FIXTURES / "page.html").read_text(), "text/html")
        if path == "/moved":
            return self._send(301, b"", "text/plain", {"Location": self.base + "/feed.xml"})
        if path == "/temp":
            return self._send(302, b"", "text/plain", {"Location": self.base + "/feed.xml"})
        if path == "/auth/feed.xml":
            if not self._auth_ok():
                return self._send(401, b"no", "text/plain", {"WWW-Authenticate": 'Basic realm="x"'})
            return self._send(200, self._feed("feed_rss.xml"), "application/rss+xml")
        if path.endswith((".mp3", ".m4a", ".ogg")):
            return self._media(path)
        if path == "/ep3.json":
            return self._send(200, (FIXTURES / "chapters.json").read_text())
        if path == "/ep3.srt":
            return self._send(200, (FIXTURES / "transcript.srt").read_text(), "application/x-subrip")
        # Nextcloud GPodder Sync
        if path == "/index.php/apps/gpoddersync/subscriptions":
            if not self._auth_ok():
                return self._send(401)
            return self._json(state.sub_changes(int(query.get("since", ["0"])[0])))
        if path == "/index.php/apps/gpoddersync/episode_action":
            if not self._auth_ok():
                return self._send(401)
            return self._json(state.nextcloud_changes(int(query.get("since", ["0"])[0]),
                                                      query.get("allActions", [""])[0] == "true"))
        # Nextcloud account and files (omaPOD's shared queue and favorites)
        if path == "/ocs/v2.php/cloud/user":
            if not self._auth_ok():
                return self._send(401)
            return self._json({"ocs": {"meta": {"status": "ok"}, "data": {"id": "user-id"}}})
        if path.startswith(DAV):
            if not self._auth_ok():
                return self._send(401)
            stored = self.server.dav.get(path)
            if stored is None:
                return self._send(404, b"", "text/plain")
            return self._send(200, stored[0], "application/json", {"ETag": stored[1]})
        # gpodder.net
        if path.startswith("/api/2/subscriptions/"):
            if not self._auth_ok():
                return self._send(401)
            return self._json(state.sub_changes(int(query.get("since", ["0"])[0])))
        if path.startswith("/api/2/episodes/"):
            if not self._auth_ok():
                return self._send(401)
            return self._json(state.action_changes(int(query.get("since", ["0"])[0])))
        if path.startswith("/api/2/devices/"):
            if not self._auth_ok():
                return self._send(401)
            return self._json([{"id": k, "caption": v, "type": "laptop", "subscriptions": 0}
                               for k, v in state.devices.items()])
        if path == "/search.json":
            return self._json([{"url": self.base + "/feed.xml", "title": "Test Cast", "description": "d",
                                "logo_url": ""}])
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parts = urlsplit(self.path)
        path = parts.path
        state = self.server.sync
        body = self._body()
        self.server.requests.append(("POST", self.path))
        if path == "/index.php/apps/gpoddersync/subscription_change/create":
            if not self._auth_ok():
                return self._send(401)
            data = json.loads(body)
            state.apply_subs(data.get("add", []), data.get("remove", []))
            return self._json({"timestamp": state.clock})
        if path == "/index.php/apps/gpoddersync/episode_action/create":
            if not self._auth_ok():
                return self._send(401)
            state.add_actions(json.loads(body))
            return self._json({"timestamp": int(time.time())})
        if path == "/index.php/login/v2":
            base = state.login_base or self.base
            return self._json({"poll": {"token": "tok", "endpoint": base + "/login/v2/poll"},
                               "login": base + "/login/v2/flow/abc"})
        # Servers that only look like a Nextcloud: an incomplete answer, a login page that is no web page
        if path == "/broken/index.php/login/v2":
            return self._json({"poll": {"token": "tok"}})
        if path == "/elsewhere/index.php/login/v2":
            return self._json({"poll": {"token": "tok", "endpoint": self.base + "/login/v2/poll"},
                               "login": "file:///etc/passwd"})
        if path == "/login/v2/poll":
            state.login_polls += 1
            if state.login_polls < 2:
                return self._send(404, b"", "text/plain")
            return self._json({"server": state.login_server or self.base, "loginName": "user", "appPassword": "pass"})
        if path.startswith("/api/2/auth/"):
            if not self._auth_ok():
                return self._send(401)
            return self._send(200, b"", "text/plain", {"Set-Cookie": "sessionid=abc; Path=/"})
        if path.startswith("/api/2/devices/"):
            if not self._auth_ok():
                return self._send(401)
            device = path.rsplit("/", 1)[1].removesuffix(".json")
            state.devices[device] = (json.loads(body or b"{}").get("caption") or device)
            return self._send(200, b"", "text/plain")
        if path.startswith("/api/2/subscriptions/"):
            if not self._auth_ok():
                return self._send(401)
            data = json.loads(body)
            ts = state.apply_subs(data.get("add", []), data.get("remove", []))
            return self._json({"timestamp": ts, "update_urls": []})
        if path.startswith("/api/2/episodes/"):
            if not self._auth_ok():
                return self._send(401)
            ts = state.add_actions(json.loads(body))
            return self._json({"timestamp": ts, "update_urls": []})
        return self._send(404, b"not found", "text/plain")

    # WebDAV, enough for files in a folder: ETags, If-Match / If-None-Match, a missing folder answers 409.

    def do_PUT(self):
        path = urlsplit(self.path).path
        body = self._body()
        self.server.requests.append(("PUT", self.path))
        if not path.startswith(DAV) or not self._auth_ok():
            return self._send(401 if path.startswith(DAV) else 404, b"", "text/plain")
        if path.rsplit("/", 1)[0] + "/" not in self.server.dav_folders | {DAV}:
            return self._send(409, b"", "text/plain")
        stored = self.server.dav.get(path)
        if_match, if_none = self.headers.get("If-Match"), self.headers.get("If-None-Match")
        if (if_match and (stored is None or stored[1] != if_match)) or (if_none == "*" and stored is not None):
            return self._send(412, b"", "text/plain")
        self.server.dav_version += 1
        etag = f'"{self.server.dav_version}"'
        self.server.dav[path] = (body, etag)
        return self._send(201 if stored is None else 204, b"", "text/plain", {"ETag": etag})

    def do_MKCOL(self):
        path = urlsplit(self.path).path.rstrip("/") + "/"
        self._body()
        if not path.startswith(DAV) or not self._auth_ok():
            return self._send(401 if path.startswith(DAV) else 404, b"", "text/plain")
        if path in self.server.dav_folders:
            return self._send(405, b"", "text/plain")
        self.server.dav_folders.add(path)
        return self._send(201, b"", "text/plain")


def self_signed_certificate(folder, names="IP:127.0.0.1"):
    """A throwaway certificate for 127.0.0.1 (cert, key), or None without the openssl tool."""
    if shutil.which("openssl") is None:
        return None
    cert, key = Path(folder) / "cert.pem", Path(folder) / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "30", "-subj", "/CN=omapod-test",
                    "-addext", f"subjectAltName={names}"], check=True, capture_output=True)
    return cert, key


class FakeServer:
    """tls=(cert, key) serves HTTPS, like a Nextcloud with a self-signed certificate."""

    def __init__(self, media_file, feed_name="feed_rss.xml", tls=None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.scheme = "http"
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
            self.httpd.scheme = "https"
            # Clients that reject the certificate hang up mid-request; that is expected here.
            self.httpd.handle_error = lambda *_args: None
        self.httpd.media_file = media_file
        self.httpd.feed_name = feed_name
        self.httpd.dav, self.httpd.dav_folders, self.httpd.dav_version = {}, set(), 0
        self.httpd.sync = SyncState()
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self):
        return f"{self.httpd.scheme}://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def sync(self):
        return self.httpd.sync

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
