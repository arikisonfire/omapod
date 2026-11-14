"""HTTP client on top of urllib.

Handles conditional GETs (ETag / Last-Modified), gzip, basic auth for
password-protected feeds (sent to their own server only), permanent-redirect
tracking (feeds that moved), the optional proxy from the settings and
human-readable error reasons (the same categories AntennaPod shows in its
download log).
"""

import base64
import hashlib
import http.client
import os
import socket
import ssl
import threading
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from .. import USER_AGENT
from ..i18n import _
from ..util import split_login

TIMEOUT = 30
PROXY_AUTH = "Proxy-authorization"  # as urllib's ProxyHandler names the header (Request capitalizes header names)
# What a request may take along when a redirect leads to another server. urllib copies every header of the
# request to wherever a redirect points; a header a caller added for one server (an API key, say) is not for the
# next one. Credentials have their own rule (auth_allowed).
HEADERS_FOR_ANY_SERVER = frozenset({"accept", "accept-encoding", "accept-language", "user-agent", "range",
                                    "if-none-match", "if-modified-since"})


class HttpError(Exception):
    def __init__(self, message, status=0, detail="", url="", final_url=""):
        super().__init__(message)
        self.status = status
        self.detail = detail or message
        self.url = url
        self.final_url = final_url or url  # the address that gave the answer, at the end of any redirects


class CertificateError(HttpError):
    """The server's TLS certificate is not trusted: self-signed, from a private authority or for another name."""


class NotModified(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes = b""
    permanent_url: str = ""
    redirects: list = field(default_factory=list)

    @property
    def etag(self):
        return self.headers.get("etag", "")

    @property
    def last_modified(self):
        return self.headers.get("last-modified", "")

    @property
    def content_type(self):
        return self.headers.get("content-type", "")

    def text(self):
        charset = "utf-8"
        for part in self.content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"'")
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")

    def json(self):
        import json

        return json.loads(self.text())


def _origin(url):
    """(scheme, host, port) of an address; the host is "" for an address that has none or whose port is not a
    number, and such an address is no origin of anything (auth_allowed)."""
    try:
        parts = urlsplit(url)
        scheme, port = parts.scheme.lower(), parts.port
    except ValueError:
        # Not read as "the default port": another program (ffmpeg, for streams) may read a port out of it.
        return "", "", None
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def auth_allowed(owner, target):
    """May credentials entered for the address `owner` (a feed, a sync server) go along to `target`?
    Only to the same origin, or to its https upgrade on the default ports, as curl and requests decide:
    never to another host, not even one that asks for them (401), and never back to plain http."""
    owner, target = _origin(owner), _origin(target)
    if not owner[1] or owner[1] != target[1]:
        return False
    return owner == target or (owner[0], owner[2], target[0], target[2]) == ("http", 80, "https", 443)


def withheld(owner, asked):
    """Why the 401 answer of `asked` cannot be helped: credentials for `owner` never go there (auth_allowed).
    "" when they may go there, so they were sent along and are wrong, or missing."""
    if auth_allowed(owner, asked):
        return ""
    return _("{host} asks for a password, but omaPOD sends this podcast's password only to {own}.").format(
        host=urlsplit(asked).hostname or asked, own=urlsplit(owner).hostname or owner)


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    """Remembers redirects so callers can tell permanent (301/308) from temporary ones, and takes
    credentials along only where auth_allowed lets them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A login a server writes into the address it redirects to is not used (and urllib would look the whole
        # of "user:password@host" up as a host name).
        new = super().redirect_request(req, fp, code, msg, headers, split_login(newurl)[0])
        if new is not None:
            # urllib copies the proxy login too, and a host the proxy is bypassed for (no_proxy) would receive it.
            # ProxyHandler adds it again to every request that goes through the proxy.
            new.remove_header(PROXY_AUTH)
            if not auth_allowed(req.full_url, new.full_url):
                for name in [name for name in new.headers if name.lower() not in HEADERS_FOR_ANY_SERVER]:
                    new.remove_header(name)
            chain = getattr(req, "redirect_chain", None)
            if chain is None:
                chain = []
            chain.append((code, new.full_url))  # without a login written into it (see above)
            new.redirect_chain = chain
            # Credentials are unredirected headers, which urllib leaves behind (it copies every other header,
            # even from https to http), so they are added again only where they belong.
            new.auth_url = getattr(req, "auth_url", req.full_url)
            new.authorization = getattr(req, "authorization", "")
            if new.authorization and auth_allowed(new.auth_url, new.full_url):
                new.add_unredirected_header("Authorization", new.authorization)
        return new

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


def _configured_proxy():
    """The proxy from the settings as http://[user:password@]host:port, or "" without one (then urllib uses
    the environment's, like http_proxy)."""
    from ..config import credentials, settings

    try:
        proxy = settings().get("proxy") or {}
    except Exception:
        proxy = {}
    kind = (proxy.get("type") or "direct").lower()
    host = (proxy.get("host") or "").strip()
    if kind != "http" or not host:
        return ""
    port = int(proxy.get("port") or 8080)
    auth = ""
    if proxy.get("username"):
        password = credentials().get("proxy").get("password") or ""
        auth = f"{quote(proxy['username'], safe='')}:{quote(password, safe='')}@"
    return f"http://{auth}{host}:{port}"


def _proxy_handler():
    url = _configured_proxy()
    return urllib.request.ProxyHandler({"http": url, "https": url}) if url else None


def certificate_sha256(der):
    return hashlib.sha256(der).hexdigest()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Accepts exactly the certificate the user chose to trust, identified by its SHA-256 fingerprint,
    instead of checking it against the system's certificate authorities."""

    def __init__(self, host, cert_sha256="", **kwargs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, context=context, **kwargs)
        self._cert_sha256 = cert_sha256

    def connect(self):
        super().connect()
        if certificate_sha256(self.sock.getpeercert(binary_form=True) or b"") != self._cert_sha256:
            self.close()
            raise ssl.SSLCertVerificationError(_("The server's certificate has changed since you trusted it."))


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, cert_sha256, deadline=None):
        super().__init__()
        self._cert_sha256 = cert_sha256
        self._deadline = deadline

    def https_open(self, req):
        return self.do_open(_watched(_PinnedHTTPSConnection, self._deadline, cert_sha256=self._cert_sha256), req)


class Deadline:
    """The time a whole request may take. The timeout of a connection only limits each wait for the next bytes: a
    server that sends one byte every few seconds would keep a request, and with it the refresh of all podcasts,
    going for days. When the time is up, the connections of the request are shut down, whatever they are waiting
    for (the TLS handshake, the headers, the body); the pending read then fails. close() ends the watch."""

    def __init__(self, seconds):
        self.expired = False
        self._lock = threading.Lock()
        self._sockets = []  # duplicates of the request's sockets; shutting one down ends the connection itself
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock):
        duplicate = socket.socket(fileno=os.dup(sock.fileno()))
        with self._lock:
            self._sockets.append(duplicate)
            expired = self.expired
        if expired:
            self._shut(duplicate)

    @staticmethod
    def _shut(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _expire(self):
        with self._lock:
            self.expired = True
            for sock in self._sockets:
                self._shut(sock)

    def close(self):
        self._timer.cancel()
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()


def total_time(timeout):
    """How long a whole request may take when each wait for the server may take `timeout` seconds: long enough
    for a large feed over a slow line (five minutes at the usual 30 s), short next to a server that never
    finishes."""
    return max(10 * timeout, 30)


def _watched(connection_class, deadline, **fixed):
    """connection_class whose socket the deadline watches from the moment it is connected."""

    def connect(host, **kwargs):
        connection = connection_class(host, **fixed, **kwargs)
        create = getattr(connection, "_create_connection", None)
        if deadline is not None and create is not None:
            def create_watched(*args, **kwargs):
                sock = create(*args, **kwargs)
                deadline.watch(sock)
                return sock

            connection._create_connection = create_watched
        return connection

    return connect


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline=None):
        super().__init__()
        self._deadline = deadline

    def http_open(self, req):
        return self.do_open(_watched(http.client.HTTPConnection, self._deadline), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline=None):
        self._tls = ssl.create_default_context()
        super().__init__(context=self._tls)
        self._deadline = deadline

    def https_open(self, req):
        return self.do_open(_watched(http.client.HTTPSConnection, self._deadline), req, context=self._tls)


def build_opener(cert_sha256="", deadline=None):
    """An opener for http and https only. Addresses come from feeds, and urllib's default openers would
    read file:, ftp: and data: ones as well. With a deadline, its connections end when the time is up."""
    if cert_sha256:
        https = _PinnedHTTPSHandler(cert_sha256, deadline)
    else:
        https = _HTTPSHandler(deadline)
    opener = urllib.request.OpenerDirector()
    for handler in (_proxy_handler() or urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    _HTTPHandler(deadline), https, urllib.request.HTTPDefaultErrorHandler(),
                    _RedirectRecorder(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def peer_certificate(url, timeout=15):
    """The certificate (DER) a TLS server presents, fetched without checking it, so the user can decide. Through
    the proxy the requests take, as urllib picks it: past the proxy the certificate could be another one, and the
    connection would leave out a proxy the user chose."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    proxy = _configured_proxy() or urllib.request.getproxies().get("https", "")
    if proxy and not urllib.request.proxy_bypass(host):
        proxy = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        connection = http.client.HTTPSConnection(proxy.hostname, proxy.port, timeout=timeout, context=context)
        headers = {}
        if proxy.username:
            headers["Proxy-Authorization"] = basic_auth(unquote(proxy.username), unquote(proxy.password or ""))
        connection.set_tunnel(host, port, headers=headers)
    else:
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        connection.connect()
        return connection.sock.getpeercert(binary_form=True)
    finally:
        connection.close()


def describe_certificate(der):
    """Fingerprint and, where Python can decode the certificate, its names and expiry."""
    digest = certificate_sha256(der)
    info = {"sha256": digest, "fingerprint": ":".join(digest[i:i + 2] for i in range(0, 64, 2)).upper(),
            "subject": "", "issuer": "", "expires": 0}
    try:
        # ssl only decodes certificates it has verified; this helper reads any PEM file, by its name. The file is
        # made in omaPOD's own cache folder, not in the shared /tmp: the names shown next to the fingerprint must
        # be those of this certificate, whoever else can write the folder for temporary files.
        import _ssl
        import os
        import tempfile

        from ..config import paths

        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, dir=paths.cache) as handle:
            handle.write(ssl.DER_cert_to_PEM_cert(der))
        try:
            decoded = _ssl._test_decode_cert(handle.name)
        finally:
            os.unlink(handle.name)
    except Exception:
        return info

    def name(rdns):
        fields = dict(pair for rdn in rdns for pair in rdn)
        return fields.get("commonName") or fields.get("organizationName") or ""

    alt_names = [value for kind, value in decoded.get("subjectAltName", ()) if kind in ("DNS", "IP Address")]
    info["subject"] = ", ".join(dict.fromkeys(n for n in [name(decoded.get("subject", ()))] + alt_names if n))
    info["issuer"] = name(decoded.get("issuer", ()))
    try:
        info["expires"] = int(ssl.cert_time_to_seconds(decoded.get("notAfter", "")))
    except ValueError:
        pass
    return info


def basic_auth(username, password):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def split_userinfo(url):
    """https://user:pass@host/feed -> (https://host/feed, user, pass), see util.split_login."""
    return split_login(url)


def has_login(auth):
    """Whether (username, password) is a login to send: a name, or a password without one (an access token)."""
    return bool(auth) and bool(auth[0] or auth[1])


def describe_error(error, url=""):
    """Map an exception to (reason, detail) like AntennaPod's DownloadError messages."""
    if isinstance(error, HttpError):
        return str(error), error.detail
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        if code in (401, 407):
            reason = _("Authentication error. Make sure that username and password are correct.")
        elif code == 403:
            reason = _("The podcast host's server refuses to respond.")
        elif code in (404, 410):
            reason = _("The podcast host's server does not know where to find the file. It may have been deleted.")
        elif code >= 500:
            reason = _("The server is currently unavailable (HTTP {code}).").format(code=code)
        else:
            reason = _("HTTP data error")
        return reason, f"HTTP {code} {error.reason}"
    if isinstance(error, urllib.error.URLError):
        inner = error.reason
        if isinstance(inner, socket.gaierror):
            return _("Cannot find the server. Check if the address is typed correctly and if you have a working "
                     "network connection."), str(inner)
        if isinstance(inner, ssl.SSLError):
            return _("Unable to establish a secure connection. Something may be wrong with the server "
                     "certificates."), str(inner)
        if isinstance(inner, (socket.timeout, TimeoutError)):
            return _("Connection error"), _("The connection timed out.")
        if isinstance(inner, ConnectionRefusedError):
            return _("Connection error"), str(inner)
        return _("Connection error"), str(inner)
    if isinstance(error, (socket.timeout, TimeoutError)):
        return _("Connection error"), _("The connection timed out.")
    if isinstance(error, http.client.HTTPException):
        # Broken or cut-off answers (IncompleteRead, BadStatusLine) are not OSErrors.
        return _("Connection error"), str(error) or error.__class__.__name__
    if isinstance(error, OSError):
        return _("IO error"), str(error)
    return _("Unknown error"), repr(error)


def _decompress(body, encoding, max_bytes):
    """Unpacks gzip and deflate answers; None when they unpack to more than max_bytes (a few hundred KB of
    zeros unpack to gigabytes)."""
    encoding = (encoding or "").lower()
    if encoding in ("gzip", "x-gzip") or body[:2] == b"\x1f\x8b":
        formats = (16 + zlib.MAX_WBITS,)
    elif encoding == "deflate":
        formats = (zlib.MAX_WBITS, -zlib.MAX_WBITS)  # zlib-wrapped as the RFC says, raw as some servers send it
    else:
        return body
    for wbits in formats:
        out, rest = bytearray(), body
        try:
            while rest:
                unpacker = zlib.decompressobj(wbits)
                out += unpacker.decompress(rest, max_bytes + 1 - len(out))
                if len(out) > max_bytes:
                    return None
                if not unpacker.eof:
                    raise zlib.error("the compressed data ends early")
                # A gzip answer may consist of several members, like gzip.decompress reads them.
                rest = unpacker.unused_data.lstrip(b"\0") if wbits > zlib.MAX_WBITS else b""
            return bytes(out)
        except zlib.error:
            continue
    return body


def _prepare(url, method, headers, data, auth, auth_url):
    """A request whose credentials (auth, user:password in the address or an Authorization header) go only to
    the address they belong to: auth_url, by default the address itself (see auth_allowed)."""
    url, user, password = split_userinfo(url)
    if not has_login(auth) and (user or password):
        auth, auth_url = (user, password), url
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.authorization = basic_auth(auth[0], auth[1]) if has_login(auth) else ""
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            req.authorization = value
        else:
            req.add_header(key, value)
    req.auth_url = auth_url or url
    req.redirect_chain = []
    if req.authorization and auth_allowed(req.auth_url, url):
        req.add_unredirected_header("Authorization", req.authorization)
    return req


def open_url(url, method="GET", headers=None, data=None, auth=None, timeout=TIMEOUT, auth_url=""):
    """Open a streaming response. The caller must close it. Returns (response, redirect_chain).
    Credentials work as in request(); an HTTPError's geturl() is the address that answered. Getting to the
    answer's headers, through every redirect, has a time limit as a whole (Deadline); how long the body may take
    is up to the caller, who reads it."""
    req = _prepare(url, method, {"Accept-Encoding": "identity", **(headers or {})}, data, auth, auth_url)
    deadline = Deadline(total_time(timeout))
    try:
        response = build_opener(deadline=deadline).open(req, timeout=timeout)
    finally:
        deadline.close()
    return response, req.redirect_chain


def request(url, method="GET", headers=None, data=None, auth=None, timeout=TIMEOUT, etag="", last_modified="",
            max_bytes=64 * 1024 * 1024, cert_sha256="", auth_url="", total=None):
    """Fetch a whole resource into memory. Raises NotModified for 304, HttpError otherwise.
    With cert_sha256, HTTPS accepts only that certificate (one the user trusted explicitly).
    Credentials go only to the origin of auth_url (by default url), also along redirects, and never to
    another host, even one that asks for them; HttpError.final_url names the address that answered.
    timeout limits each wait for the server, total (by default total_time(timeout)) the whole request."""
    defaults = {"Accept-Encoding": "gzip, deflate"}
    if etag:
        defaults["If-None-Match"] = etag
    if last_modified:
        defaults["If-Modified-Since"] = last_modified
    req = _prepare(url, method, {**defaults, **(headers or {})}, data, auth, auth_url)
    url = req.full_url
    deadline = Deadline(total or total_time(timeout))
    try:
        return _request(req, url, timeout, max_bytes, cert_sha256, deadline)
    except HttpError as error:
        if deadline.expired and not error.status:
            raise HttpError(_("Connection error"), detail=_("The server took too long to answer."), url=url) from None
        raise
    finally:
        deadline.close()


def _request(req, url, timeout, max_bytes, cert_sha256, deadline):
    try:
        with build_opener(cert_sha256, deadline).open(req, timeout=timeout) as handle:
            body = handle.read(max_bytes + 1)
            if deadline.expired:
                # A body without a stated length ends where the connection ends: cut off, it would pass for whole.
                raise TimeoutError()
            if len(body) > max_bytes:
                raise HttpError(_("The response is too large."), url=url)
            headers_out = {k.lower(): v for k, v in handle.headers.items()}
            body = _decompress(body, headers_out.get("content-encoding"), max_bytes)
            if body is None:
                raise HttpError(_("The response is too large."), url=url)
            final_url = handle.geturl()
            status = handle.status
    except urllib.error.HTTPError as error:
        error.close()  # its connection; status, reason and address stay readable
        if error.code == 304:
            raise NotModified() from None
        reason, detail = describe_error(error, url)
        raise HttpError(reason, status=error.code, detail=detail, url=url, final_url=error.geturl()) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason, detail = describe_error(error, url)
        inner = error.reason if isinstance(error, urllib.error.URLError) else error
        kind = CertificateError if isinstance(inner, ssl.SSLCertVerificationError) else HttpError
        raise kind(reason, detail=detail, url=url) from None
    chain = getattr(req, "redirect_chain", [])
    permanent = ""
    if chain and all(code in (301, 308) for code, _url in chain):
        permanent = chain[-1][1]
    return Response(url=final_url, status=status, headers=headers_out, body=body, permanent_url=permanent,
                    redirects=chain)


def get_json(url, headers=None, auth=None, timeout=TIMEOUT):
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    return request(url, headers=hdrs, auth=auth, timeout=timeout).json()
