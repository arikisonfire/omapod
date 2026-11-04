"""Cover art cache.

Originals are downloaded once into the cache folder; square thumbnails in
a few sizes are rendered next to them, so lists never decode 3000 px
artwork. Works without a GUI (MPRIS and the bar plugin only need files).
The folder keeps to a size limit by dropping what was used least recently,
mostly originals: thumbnails are what lists and the bar show.
"""

import os
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from ..net import http
from ..util import url_hash

SIZES = (64, 128, 256, 512)
# Larger covers are refused before decoding: a PNG of a few hundred KB can declare 20000×20000 pixels, which
# takes gigabytes once decoded. 6000×6000 artwork still fits.
MAX_PIXELS = 40_000_000
FAILED_RETRY_S = 600
CACHE_LIMIT = 300 * 1024 * 1024


def image_size(path):
    """(width, height) from the header of a PNG, JPEG, GIF or WebP file, without decoding it; None for other
    files. GdkPixbuf cannot tell without decoding: with glycin, even get_file_info decodes the whole picture."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return _webp_size(head)
            if head[:2] == b"\xff\xd8":
                handle.seek(2)
                return _jpeg_size(handle)
    except (OSError, struct.error):
        pass
    return None


def _webp_size(head):
    kind = head[12:16]
    if kind == b"VP8X":  # extended format: the canvas, 24 bits each, stored minus one
        return 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
    if kind == b"VP8L":  # lossless: 14 bits each, stored minus one, after the signature byte
        bits = int.from_bytes(head[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + (bits >> 14 & 0x3FFF)
    if kind == b"VP8 ":  # lossy: 14 bits each after the frame tag and start code
        return int.from_bytes(head[26:28], "little") & 0x3FFF, int.from_bytes(head[28:30], "little") & 0x3FFF
    return None


def _jpeg_size(handle):
    """Walks the segments up to the frame header (SOF), which holds the size."""
    for _segment in range(200):
        byte = handle.read(1)
        while byte and byte != b"\xff":
            byte = handle.read(1)
        while byte == b"\xff":
            byte = handle.read(1)
        if not byte or byte in (b"\xd9", b"\xda"):  # end of image, or the image data before any frame header
            return None
        marker = byte[0]
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
            continue
        (length,) = struct.unpack(">H", handle.read(2))
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">xHH", handle.read(5))
            return width, height
        handle.seek(length - 2, os.SEEK_CUR)
    return None


class ImageCache:
    def __init__(self, folder, limit=CACHE_LIMIT):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="images")
        self._lock = threading.Lock()
        self._inflight = {}
        self._failed = {}  # url -> when; tried again after FAILED_RETRY_S (the network may be back)
        self._used = set()  # files marked as used (newer modification time) in this run
        self._written = 0  # bytes since the last trim
        self._pool.submit(self.trim)

    def _original(self, url):
        return self.folder / f"{url_hash(url)}.img"

    def thumb_path(self, url, size):
        return self.folder / f"{url_hash(url)}-{size}.png"

    @staticmethod
    def _local_path(url):
        if url.startswith("file://"):
            return unquote(urlsplit(url).path)
        if url.startswith("/"):
            return url
        return None

    def original_if_cached(self, url):
        if not url:
            return None
        local = self._local_path(url)
        if local:
            return local if os.path.exists(local) else None
        path = self._original(url)
        return str(path) if path.exists() else None

    def thumb_if_cached(self, url, size):
        if not url:
            return None
        size = self._fit(size)
        path = self.thumb_path(url, size)
        if not path.exists():
            return None
        self._use(path)
        return str(path)

    def _use(self, path):
        """Marks a file as used, once a run: trim() drops what was used least recently."""
        if path not in self._used:
            self._used.add(path)
            try:
                os.utime(path)
            except OSError:
                pass

    def _wrote(self, size):
        with self._lock:
            self._written += size
            due = self._written > self.limit // 10
            if due:
                self._written = 0
        if due:
            self.trim()

    def trim(self):
        """Deletes the least recently used files once the folder is larger than the limit, down to 80 % of it."""
        files, total = [], 0
        try:
            with os.scandir(self.folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            info = entry.stat(follow_symlinks=False)
                            files.append((info.st_mtime, info.st_size, entry.path))
                            total += info.st_size
                    except OSError:
                        pass
        except OSError:
            return
        if total <= self.limit:
            return
        files.sort()
        for mtime, size, path in files:
            if total <= self.limit * 0.8:
                break
            if path.endswith((".part", ".tmp")) and time.time() - mtime < 3600:
                continue  # still being written
            try:
                os.unlink(path)
                total -= size
            except OSError:
                pass

    @staticmethod
    def _fit(size):
        for candidate in SIZES:
            if size <= candidate:
                return candidate
        return SIZES[-1]

    def request(self, url, size, callback=None):
        """Make sure a thumbnail of at least `size` px exists; callback(path or None) from a worker thread."""
        if not url:
            if callback:
                callback(None)
            return None
        size = self._fit(size)
        cached = self.thumb_if_cached(url, size)
        if cached:
            if callback:
                callback(cached)
            return cached
        key = (url, size)
        with self._lock:
            if time.monotonic() - self._failed.get(url, -FAILED_RETRY_S) < FAILED_RETRY_S:
                if callback:
                    callback(None)
                return None
            waiters = self._inflight.get(key)
            if waiters is not None:
                if callback:
                    waiters.append(callback)
                return None
            self._inflight[key] = [callback] if callback else []
        self._pool.submit(self._work, url, size)
        return None

    def _work(self, url, size):
        path = None
        try:
            source = self._local_path(url)
            if source is None:
                original = self._original(url)
                if original.exists():
                    self._use(original)
                else:
                    response = http.request(url, timeout=30, max_bytes=20 * 1024 * 1024)
                    tmp = original.with_suffix(".part")
                    tmp.write_bytes(response.body)
                    os.replace(tmp, original)
                    self._wrote(len(response.body))
                source = str(original)
            path = self._render(source, url, size)
            self._wrote(os.path.getsize(path))
        except Exception:
            with self._lock:
                self._failed[url] = time.monotonic()
            path = None
        with self._lock:
            waiters = self._inflight.pop((url, size), [])
        for waiter in waiters:
            if waiter:
                try:
                    waiter(path)
                except Exception:
                    pass

    def _render(self, source, url, size):
        target = self.thumb_path(url, size)
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        width, height = image_size(source) or (0, 0)
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            raise ValueError(f"not a PNG, JPEG, GIF or WebP picture, or too large ({width}×{height})")
        # Decoded at about the size needed, which spares memory with loaders that can (JPEG without glycin).
        shrink = min(1.0, size / min(width, height))
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(source, max(1, round(width * shrink)),
                                                         max(1, round(height * shrink)), False)
        width, height = pixbuf.get_width(), pixbuf.get_height()
        side = min(width, height)
        if width != height:
            pixbuf = pixbuf.new_subpixbuf((width - side) // 2, (height - side) // 2, side, side)
        if side > size:
            pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        tmp = target.with_suffix(".tmp")
        pixbuf.savev(str(tmp), "png", [], [])
        os.replace(tmp, target)
        return str(target)

    def clear(self):
        for path in self.folder.iterdir():
            try:
                path.unlink()
            except OSError:
                pass
        with self._lock:
            self._failed.clear()


class TextureCache:
    """GUI side: Gdk.Texture objects for thumbnail files, with an LRU. Files are decoded on worker threads
    (a large cover takes milliseconds, and a page shows dozens); GTK allows that for textures."""

    def __init__(self, images, limit=1000):
        self.images = images
        self.limit = limit
        self._textures = OrderedDict()
        self._waiting = {}  # key -> on_ready callbacks, main thread only
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="textures")

    def get(self, url, size, on_ready):
        """Return a texture now if there is one in memory, else call on_ready(texture) on the main loop later."""
        if not url:
            return None
        key = (url, ImageCache._fit(size))
        texture = self._textures.get(key)
        if texture is not None:
            self._textures.move_to_end(key)
            return texture
        waiting = self._waiting.get(key)
        if waiting is not None:
            waiting.append(on_ready)
            return None
        self._waiting[key] = [on_ready]
        self._pool.submit(self._load, key)
        return None

    def _load(self, key):
        url, size = key
        path = self.images.thumb_if_cached(url, size)
        if path:
            self._decode(key, path)
        else:
            self.images.request(url, size, lambda made: self._decode(key, made))

    def _decode(self, key, path):
        from gi.repository import Gdk, GLib

        texture = None
        if path:
            try:
                texture = Gdk.Texture.new_from_filename(path)
            except GLib.Error:
                pass
        GLib.idle_add(self._finish, key, texture)

    def _finish(self, key, texture):
        waiting = self._waiting.pop(key, [])
        if texture is not None:
            self._textures[key] = texture
            while len(self._textures) > self.limit:
                self._textures.popitem(last=False)
            for on_ready in waiting:
                on_ready(texture)
        return False
