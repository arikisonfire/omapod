"""Sync servers: gpodder.net (API v2, also self-hosted) and the Nextcloud
"GPodder Sync" app. Request formats match AntennaPod's GpodnetService and
NextcloudSyncService, so both apps can share one account."""

import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ..i18n import _
from ..net import http

UPLOAD_BULK_SIZE = 30


class SyncError(Exception):
    pass


class SyncAuthError(SyncError):
    pass


@dataclass
class SubscriptionChanges:
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    timestamp: int = 0


@dataclass
class ActionChanges:
    actions: list = field(default_factory=list)
    timestamp: int = 0


def normalize_host(host, default="gpodder.net"):
    """AntennaPod's HostnameParser: 'host', 'host:port/sub', 'http(s)://...' -> base URL without slash."""
    host = (host or default).strip()
    match = re.match(r"^(?:(https?)://)?([^:/]+)(?::(\d+))?(.*)?$", host)
    if not match:
        return "https://" + host.rstrip("/")
    scheme, name, port, sub = match.groups()
    if scheme is None:
        scheme = "http" if port == "80" else "https"
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = name + (f":{port}" if port and not ((scheme == "https" and port == "443") or
                                                (scheme == "http" and port == "80")) else "")
    return f"{scheme}://{netloc}{(sub or '').rstrip('/')}"


def _explain(error):
    """An HttpError's reason plus its technical detail and address, so that problems can be told apart."""
    lines = [str(error)]
    if error.detail and error.detail != str(error):
        lines.append(error.detail)
    if error.url:
        lines.append(error.url)
    return "\n".join(lines)


def _check(error):
    if isinstance(error, http.HttpError):
        if error.status == 401:
            return SyncAuthError(_("Wrong username or password"))
        if error.status >= 500:
            return SyncError(_("The sync server is currently unavailable (code {code}).").format(code=error.status))
        if error.status:
            return SyncError(_("Unable to connect to the sync server (code {code}).").format(code=error.status))
        return SyncError(_explain(error))
    return SyncError(str(error))


def _fix_urls(urls):
    # gpodder escapes colons unnecessarily
    return [u.replace("%3A", ":") for u in urls if isinstance(u, str)] if isinstance(urls, list) else []


def _object(response):
    """A JSON object from the server; a broken or hostile one gets a clear error instead of a crash."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise SyncError(_("The sync server sent an answer omaPOD does not understand."))
    return data


def _stamp(value):
    """The server's timestamp: 0 (which means 'from the start' next time) when it is not a usable number."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if 0 <= number < 2 ** 53 else 0


def _actions(data):
    """Episode actions as the API describes them. Anything else is left out instead of stopping the sync."""
    actions = data.get("actions")
    if not isinstance(actions, list):
        return []
    return [a for a in actions if isinstance(a, dict) and isinstance(a.get("episode"), str)
            and isinstance(a.get("podcast", ""), str) and isinstance(a.get("guid") or "", str)]


class GpodderNet:
    name = "gpodder"
    title = "gpodder.net"
    shares_files = False
    # Episode changes are asked for again this far back: 'since' is compared with the time an action happened, and
    # a phone without Wi-Fi uploads its actions later. Kept short on this shared public service.
    actions_overlap = 86400

    def __init__(self, host, username, password, device_id):
        self.base = normalize_host(host or "gpodder.net")
        self.username = username
        self.password = password
        self.device_id = device_id
        self._auth = (username, password)
        self.timeout = 60

    def _url(self, path, **query):
        url = self.base + path
        if query:
            url += "?" + urlencode(query)
        return url

    def _request(self, url, method="GET", payload=None):
        data = b"" if method == "POST" else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = http.request(url, method=method, data=data, headers=headers, auth=self._auth,
                                    timeout=self.timeout)
        except http.HttpError as error:
            raise _check(error) from None
        return response

    def login(self):
        user = quote(self.username, safe="")
        self._request(self._url(f"/api/2/auth/{user}/login.json"), method="POST", payload=None)

    def logout(self):
        pass

    def devices(self):
        user = quote(self.username, safe="")
        data = self._request(self._url(f"/api/2/devices/{user}.json")).json()
        return [{"id": d.get("id"), "caption": d.get("caption") or d.get("id"), "type": d.get("type"),
                 "subscriptions": d.get("subscriptions", 0)} for d in (data if isinstance(data, list) else [])
                if isinstance(d, dict)]

    def configure_device(self, device_id, caption, kind="laptop"):
        user = quote(self.username, safe="")
        self._request(self._url(f"/api/2/devices/{user}/{quote(device_id, safe='')}.json"), method="POST",
                      payload={"caption": caption, "type": kind})

    def get_subscription_changes(self, since):
        user = quote(self.username, safe="")
        device = quote(self.device_id, safe="")
        data = _object(self._request(self._url(f"/api/2/subscriptions/{user}/{device}.json", since=int(since))))
        return SubscriptionChanges(_fix_urls(data.get("add", [])), _fix_urls(data.get("remove", [])),
                                   _stamp(data.get("timestamp")))

    def upload_subscription_changes(self, added, removed):
        user = quote(self.username, safe="")
        device = quote(self.device_id, safe="")
        data = _object(self._request(self._url(f"/api/2/subscriptions/{user}/{device}.json"), method="POST",
                                     payload={"add": list(added), "remove": list(removed)}))
        return _stamp(data.get("timestamp")) or int(time.time())

    def get_episode_actions(self, since):
        user = quote(self.username, safe="")
        data = _object(self._request(self._url(f"/api/2/episodes/{user}.json", since=int(since))))
        return ActionChanges(_actions(data), _stamp(data.get("timestamp")))

    def upload_episode_actions(self, actions):
        user = quote(self.username, safe="")
        timestamp = int(time.time())
        for start in range(0, len(actions), UPLOAD_BULK_SIZE):
            batch = []
            for action in actions[start:start + UPLOAD_BULK_SIZE]:
                entry = dict(action)
                entry["device"] = self.device_id
                batch.append(entry)
            data = _object(self._request(self._url(f"/api/2/episodes/{user}.json"), method="POST", payload=batch))
            timestamp = _stamp(data.get("timestamp")) or timestamp
        return timestamp


class Nextcloud:
    name = "nextcloud"
    title = "Nextcloud"
    # Queue and favorites are not part of the gpodder API; omaPOD computers share them as files in the
    # user's Nextcloud. The folder is hidden, so the desktop client does not sync it by default.
    shares_files = True
    SHARED_FOLDER = ".omapod"
    # GPodder Sync keeps one action per episode (up to 3.18 only play actions: download and delete are dropped since
    # 3.13.3): the last one uploaded. It answers 'since' by the time an action happened, so a phone that uploads late
    # (it waits for Wi-Fi) would never be seen; asking a week back is cheap with so few actions.
    actions_overlap = 7 * 86400

    def __init__(self, server, username, password, cert_sha256=""):
        self.base = normalize_host(server, default="")
        self.username = username
        self.password = password
        self.cert_sha256 = cert_sha256
        self._auth = (username, password)
        self._user_id = None
        self.timeout = 60

    def _url(self, path, **query):
        url = f"{self.base}/index.php/apps/gpoddersync{path}"
        if query:
            url += "?" + urlencode(query)
        return url

    def _request(self, url, method="GET", payload=None):
        data = None
        headers = {"Accept": "application/json", "OCS-APIRequest": "true"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            return http.request(url, method=method, data=data, headers=headers, auth=self._auth,
                                timeout=self.timeout, cert_sha256=self.cert_sha256)
        except http.HttpError as error:
            if error.status == 404:
                raise SyncError(_("The GPodder Sync app is not installed on this Nextcloud.")) from None
            raise _check(error) from None

    def _dav_request(self, url, method="GET", data=None, headers=None):
        """WebDAV and OCS calls; HttpError comes through so callers can react to the status."""
        return http.request(url, method=method, data=data, headers={"OCS-APIRequest": "true", **(headers or {})},
                            auth=self._auth, timeout=self.timeout, cert_sha256=self.cert_sha256)

    def _file_url(self, name=""):
        if self._user_id is None:
            # Files live under the user id, which can differ from the login name (e-mail logins, LDAP).
            try:
                data = self._dav_request(f"{self.base}/ocs/v2.php/cloud/user?format=json",
                                         headers={"Accept": "application/json"}).json()
                self._user_id = str(data["ocs"]["data"]["id"])
            except http.HttpError as error:
                raise _check(error) from None
            except (ValueError, KeyError, TypeError):
                raise SyncError(_("This Nextcloud did not say which account the app password belongs to.")) from None
        return f"{self.base}/remote.php/dav/files/{quote(self._user_id, safe='')}/{self.SHARED_FOLDER}/{name}"

    def get_shared(self, name):
        """A file shared by omaPOD computers: (data, etag), or (None, "") when there is none yet."""
        try:
            response = self._dav_request(self._file_url(name))
        except http.HttpError as error:
            if error.status == 404:
                return None, ""
            raise _check(error) from None
        try:
            return response.json(), response.etag
        except ValueError:
            return None, response.etag

    def put_shared(self, name, data, etag=""):
        """Writes a shared file unless another computer changed it since `etag` was read.
        Returns the new ETag ("" if the server sent none), or None when the file had changed."""
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        headers = {"Content-Type": "application/json", **({"If-Match": etag} if etag else {"If-None-Match": "*"})}
        for attempt in (1, 2):
            try:
                return self._dav_request(self._file_url(name), method="PUT", data=body, headers=headers).etag
            except http.HttpError as error:
                if error.status == 412:
                    return None
                if error.status not in (404, 409) or attempt == 2:
                    raise _check(error) from None
            try:
                self._dav_request(self._file_url(), method="MKCOL")  # the folder did not exist yet
            except http.HttpError as error:
                if error.status != 405:  # 405: it exists after all
                    raise _check(error) from None

    def login(self):
        pass

    def logout(self):
        pass

    def get_subscription_changes(self, since):
        data = _object(self._request(self._url("/subscriptions", since=int(since))))
        return SubscriptionChanges(_fix_urls(data.get("add", [])), _fix_urls(data.get("remove", [])),
                                   _stamp(data.get("timestamp")))

    def upload_subscription_changes(self, added, removed):
        self._request(self._url("/subscription_change/create"), method="POST",
                      payload={"add": list(added), "remove": list(removed)})
        return int(time.time())

    def get_episode_actions(self, since):
        # Without allActions GPodder Sync answers with play actions only; versions that keep no others ignore it.
        data = _object(self._request(self._url("/episode_action", since=int(since), allActions="true")))
        return ActionChanges(_actions(data), _stamp(data.get("timestamp")))

    def upload_episode_actions(self, actions):
        for start in range(0, len(actions), UPLOAD_BULK_SIZE):
            self._request(self._url("/episode_action/create"), method="POST",
                          payload=list(actions[start:start + UPLOAD_BULK_SIZE]))
        return int(time.time())


class UntrustedCertificate(SyncError):
    """The server's certificate is not trusted by the system; the user may decide to trust it. Normal for a server
    in the local network or a VPN; on the internet it can also mean that someone intercepts the connection."""

    def __init__(self, server, certificate):
        super().__init__(_("The server's certificate is not trusted."))
        self.server = server
        self.certificate = certificate
        self.private = is_private_host(urlsplit(server).hostname)


class PlainHttpOnly(SyncError):
    """The server answers only without encryption. Logging in that way is the user's explicit choice, offered
    only for addresses in the local network or a VPN."""

    def __init__(self, server):
        super().__init__(_("{host} answers only without encryption (http).").format(host=host_of(server)))
        self.server = server
        self.private = is_private_host(urlsplit(server).hostname)


def is_private_host(host):
    """Whether every address of host stays in a local network or VPN (private ranges, loopback, link-local, the
    100.64/10 range Tailscale uses), where plain http can be acceptable."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)} if host else set()
        return bool(addresses) and not any(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)
    except (OSError, UnicodeError, ValueError):
        return False


class NextcloudLoginFlow:
    """Nextcloud Login Flow v2: the user grants access in the browser, we poll for an app password."""

    def __init__(self, server, cert_sha256=""):
        typed = (server or "").strip()
        self.server = normalize_host(typed, default="")
        # A bare address gets https:// first. When the server does not speak TLS there at all (never after a
        # certificate problem) and answers over http://, the user decides (PlainHttpOnly).
        self.http_fallback = not re.match(r"(?i)https?://", typed) and self.server.startswith("https://")
        self.cert_sha256 = cert_sha256
        self.token = ""
        self.endpoint = ""
        self.login_url = ""

    def start(self):
        try:
            return self._start(self.server)
        except http.CertificateError as error:
            raise self._untrusted(error) from None
        except http.HttpError as error:
            if error.status or not self.http_fallback:
                raise SyncError(_explain(error)) from None
            plain = "http://" + self.server.removeprefix("https://")
            try:
                self._start(plain)
            except http.HttpError as plain_error:
                raise SyncError(_explain(error) + "\n\n" + _explain(plain_error)) from None
            # Never silently: the app password would then travel in plain text, and an attacker in the network
            # can make TLS fail on purpose.
            raise PlainHttpOnly(plain) from None

    def _start(self, server):
        try:
            response = http.request(f"{server}/index.php/login/v2", method="POST", data=b"",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15,
                                    cert_sha256=self.cert_sha256)
            data = response.json()
            login, token, endpoint = data["login"], data["poll"]["token"], data["poll"]["endpoint"]
        except http.HttpError as error:
            if error.status == 404:
                raise SyncError(_not_nextcloud()) from None
            raise
        except (ValueError, KeyError, TypeError):
            raise SyncError(_not_nextcloud()) from None
        # The login page is opened in the browser: never hand other schemes to the desktop.
        if not all(isinstance(u, str) and u.startswith(("https://", "http://")) for u in (login, endpoint)):
            raise SyncError(_not_nextcloud())
        self.login_url, self.token, self.endpoint = on_server(server, login), str(token), on_server(server, endpoint)
        return self.login_url

    def _untrusted(self, error):
        try:
            certificate = http.describe_certificate(http.peer_certificate(error.url or self.server))
        except (OSError, ValueError):
            return SyncError(_explain(error))
        return UntrustedCertificate(self.server, certificate)

    def poll(self):
        """Returns (server, login_name, app_password) once granted, None while waiting. The server is the one
        Nextcloud names if the app password may go there (auth_allowed: the same origin as the login, or its https
        upgrade), otherwise the one the login ran on: behind a proxy, Nextcloud often does not know it is reached
        with https, or under which name."""
        try:
            response = http.request(self.endpoint, method="POST", data=urlencode({"token": self.token}).encode(),
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30,
                                    cert_sha256=self.cert_sha256)
        except http.HttpError as error:
            if error.status == 404:
                return None
            raise SyncError(_explain(error)) from None
        try:
            data = response.json()
            server = normalize_host(str(data["server"]), default="")
            login, password = data["loginName"], data["appPassword"]
        except (ValueError, KeyError, TypeError):
            raise SyncError(_not_nextcloud()) from None
        if not http.auth_allowed(self.server, server):
            server = self.server  # the app password goes where the login went (see on_server)
        return server, login, password


def on_server(server, url):
    """An address the Login Flow names (login page, poll endpoint), on the server the login started on.

    The poll token is all it takes to collect the app password once the user grants access, so it goes only
    where the login went: the server the user entered, over https, or over http only after asking
    (PlainHttpOnly). Behind a proxy that ends TLS, Nextcloud often names its addresses with http://, its inner
    port or another host name; then the address keeps its path but moves to the login's own origin. The https
    upgrade of the same server (auth_allowed) stays as Nextcloud names it."""
    if http.auth_allowed(server, url):
        return url
    own, named = urlsplit(server), urlsplit(url)
    return urlunsplit((own.scheme, own.netloc, named.path, named.query, ""))


def _not_nextcloud():
    return _("This address does not answer like a Nextcloud. Enter the address your browser shows for it, "
             "with the port if there is one.")


def host_of(url):
    return urlsplit(url).hostname or url
