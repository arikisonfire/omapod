"""mpv as a subprocess, controlled over its JSON IPC socket.

A reader thread parses mpv's messages; property changes and events are
handed to `on_message` (from that thread; the controller hops to the main
loop itself). Requests that need an answer block briefly with a timeout.
"""

import itertools
import json
import os
import shutil
import socket
import subprocess
import threading
import time

from .. import USER_AGENT


class MpvError(Exception):
    pass


class Mpv:
    def __init__(self, socket_path, on_message, log_path=None, extra_args=()):
        self.socket_path = str(socket_path)  # short enough for a Unix socket: see Paths.mpv_socket
        self.on_message = on_message
        self.log_path = log_path
        self.extra_args = list(extra_args)
        self.process = None
        self.sock = None
        self._send_lock = threading.Lock()
        self._pending = {}
        self._ids = itertools.count(1)
        self._observe_ids = itertools.count(1)
        self._reader = None
        self.alive = False

    @staticmethod
    def available():
        return shutil.which("mpv") is not None

    def start(self, volume=100):
        if self.alive:
            return
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        # --tls-verify: mpv does not check certificates by default, and a stream of a protected podcast carries
        # its password, which ffmpeg hands to whoever answers 401. With it, ffmpeg checks the certificate and the
        # host name against the system's certificate authorities before anything is sent, on redirects, retries
        # and HLS segments too, like the rest of omaPOD.
        args = [
            "mpv", "--idle=yes", "--no-config", "--no-terminal", f"--input-ipc-server={self.socket_path}",
            "--tls-verify=yes",
            "--audio-display=no", "--vid=no", "--force-window=no", "--keep-open=no", "--ytdl=no",
            "--cache=yes", "--demuxer-max-bytes=64MiB", "--demuxer-readahead-secs=60",
            "--audio-client-name=omaPOD", f"--user-agent={USER_AGENT}", f"--volume={int(volume)}",
            "--volume-max=150", "--input-default-bindings=yes", "--hwdec=auto-safe", "--title=omaPOD",
            "--msg-level=all=warn", "--save-position-on-quit=no", "--resume-playback=no",
        ]
        ao = os.environ.get("OMAPOD_MPV_AO")
        if ao:
            args.append(f"--ao={ao}")
        args += self.extra_args
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True)
        if log is not subprocess.DEVNULL:
            log.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise MpvError(f"mpv exited with code {self.process.returncode}")
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.socket_path)
                self.sock = sock
                break
            except OSError:
                sock.close()
                time.sleep(0.05)
        if self.sock is None:
            self.kill()
            raise MpvError("mpv did not open its IPC socket")
        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, name="mpv-reader", daemon=True)
        self._reader.start()

    def _read_loop(self):
        buffer = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                request_id = message.get("request_id")
                if request_id and request_id in self._pending and "event" not in message:
                    slot = self._pending.pop(request_id)
                    slot["response"] = message
                    slot["event"].set()
                    continue
                try:
                    self.on_message(message)
                except Exception:
                    import traceback

                    traceback.print_exc()
        self.alive = False
        for slot in list(self._pending.values()):
            slot["event"].set()
        self._pending.clear()
        try:
            self.on_message({"event": "omapod-mpv-exit"})
        except Exception:
            pass

    def _send(self, payload, wait=False, timeout=2.0):
        if not self.alive:
            raise MpvError("mpv is not running")
        request_id = next(self._ids)
        payload["request_id"] = request_id
        slot = None
        if wait:
            slot = {"event": threading.Event(), "response": None}
            self._pending[request_id] = slot
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                self.sock.sendall(data)
            except OSError as error:
                self._pending.pop(request_id, None)
                raise MpvError(str(error)) from None
        if not wait:
            return None
        if not slot["event"].wait(timeout):
            self._pending.pop(request_id, None)
            raise MpvError("mpv did not answer in time")
        response = slot["response"] or {}
        if response.get("error") not in (None, "success"):
            raise MpvError(response.get("error"))
        return response.get("data")

    def command(self, *args, wait=False):
        return self._send({"command": list(args)}, wait=wait)

    def command_named(self, name, wait=False, **kwargs):
        return self._send({"command": dict(name=name, **kwargs)}, wait=wait)

    def set(self, name, value):
        try:
            self._send({"command": ["set_property", name, value]})
        except MpvError:
            pass

    def get(self, name, default=None, timeout=1.0):
        try:
            return self._send({"command": ["get_property", name]}, wait=True, timeout=timeout)
        except MpvError:
            return default

    def observe(self, name):
        observe_id = next(self._observe_ids)
        self._send({"command": ["observe_property", observe_id, name]})
        return observe_id

    def loadfile(self, url, options=None):
        """Replace the current file. Options are per-file mpv options (start, http-header-fields...)."""
        self.command_named("loadfile", url=url, flags="replace", options=options or {})

    def quit(self):
        if self.alive:
            try:
                self.command("quit")
            except MpvError:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.kill()
        self._cleanup()

    def kill(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._cleanup()

    def _cleanup(self):
        self.alive = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
