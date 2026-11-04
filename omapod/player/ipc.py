"""Local control socket for the Omarchy bar plugin and `omapod ctl`.

Newline-delimited JSON over a Unix socket in $XDG_RUNTIME_DIR/omapod, with
app.json next to it while omaPOD runs. Every client gets the full player state
on connect and whenever it changes; positions are pushed once per second while
playing. Clients send commands like {"cmd": "toggle"} or {"cmd": "seek",
"position": 120000}. Before omaPOD quits, clients get {"type": "bye"}.
"""

import json
import os
import socket
import time

from gi.repository import GLib

from .. import APP_NAME, VERSION
from ..events import bus
from ..util import fmt_speed

PROTOCOL = 1


class _Client:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.watch = 0


class ControlServer:
    def __init__(self, app, player, library, images, path):
        self.app = app
        self.player = player
        self.library = library
        self.images = images
        self.path = str(path)
        self.server = None
        self.watch = 0
        self.clients = []
        self._last_position_push = 0.0
        self._state_pending = False

    def start(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            if os.path.exists(self.path):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.connect(self.path)
                    probe.close()
                    return False  # another omaPOD owns it
                except OSError:
                    os.unlink(self.path)
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(self.path)
            os.chmod(self.path, 0o600)
            self.server.listen(8)
            self.server.setblocking(False)
        except OSError:
            self.server = None
            return False
        self.watch = GLib.io_add_watch(self.server.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._accept)
        self._write_presence()
        bus.subscribe("player-state", self._on_state)
        bus.subscribe("player-position", self._on_position)
        bus.subscribe("queue-changed", lambda: self._schedule_state())
        bus.subscribe("episodes-changed", lambda _ids=None: self._schedule_state())
        return True

    @property
    def presence_path(self):
        return os.path.join(os.path.dirname(self.path), "app.json")

    def _write_presence(self):
        """The bar widget watches this file and only connects while it exists."""
        try:
            tmp = self.presence_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "socket": self.path, "version": VERSION}, handle)
            os.replace(tmp, self.presence_path)
        except OSError:
            pass

    def stop(self):
        try:
            os.unlink(self.presence_path)
        except OSError:
            pass
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = 0
        if self.server is not None:
            self.server.close()
            self.server = None
            try:
                os.unlink(self.path)
            except OSError:
                pass
        # Say goodbye first, so the bar widget does not try to reconnect.
        for client in list(self.clients):
            self._send(client, {"type": "bye"})
            self._drop(client)

    # ------------------------------------------------------------ connections

    def _accept(self, _fd, _condition):
        try:
            sock, _addr = self.server.accept()
        except OSError:
            return True
        sock.setblocking(False)
        client = _Client(sock)
        client.watch = GLib.io_add_watch(sock.fileno(), GLib.PRIORITY_DEFAULT,
                                         GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read, client)
        self.clients.append(client)
        self._send(client, {"type": "hello", "app": APP_NAME, "version": VERSION, "protocol": PROTOCOL})
        self._send(client, self.snapshot())
        return True

    def _read(self, _fd, condition, client):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self._drop(client)
            return False
        try:
            data = client.sock.recv(65536)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data:
            self._drop(client)
            return False
        client.buffer += data
        while b"\n" in client.buffer:
            line, client.buffer = client.buffer.split(b"\n", 1)
            if line.strip():
                self._handle_line(client, line)
        if len(client.buffer) > 1 << 20:
            self._drop(client)
            return False
        return True

    def _drop(self, client):
        if client.watch:
            GLib.source_remove(client.watch)
            client.watch = 0
        try:
            client.sock.close()
        except OSError:
            pass
        if client in self.clients:
            self.clients.remove(client)

    def _send(self, client, message):
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            client.sock.sendall(data)
        except (BlockingIOError, OSError):
            self._drop(client)

    def broadcast(self, message):
        for client in list(self.clients):
            self._send(client, message)

    # ------------------------------------------------------------ state

    def _cover(self, url):
        if not url:
            return ""
        path = self.images.thumb_if_cached(url, 256)
        if path:
            return GLib.filename_to_uri(path, None)
        self.images.request(url, 256, lambda _p: GLib.idle_add(self._schedule_state))
        return ""

    def snapshot(self):
        state = self.player.state()
        episode = state.get("episode")
        if episode:
            episode["cover"] = self._cover(episode.get("image") or episode.get("feed_image"))
        current = self.player.current_id()
        queue_ids = self.library.queue_ids()
        upcoming = queue_ids[queue_ids.index(current) + 1:] if current in queue_ids else queue_ids
        up_next = []
        for item in self.library.db.get_episodes_by_ids(upcoming[:5]):
            up_next.append({"id": item.id, "title": item.title, "podcast": item.feed_title,
                            "duration": item.duration, "position": item.position,
                            "cover": self._cover(item.cover_url(True))})
        state.update({
            "type": "state",
            "speed_label": fmt_speed(state["speed"]),
            "speed_presets": self.player.settings.get("speed_presets"),
            "queue": up_next,
            "queue_count": len(queue_ids),
            "window_visible": self.app.window_visible() if hasattr(self.app, "window_visible") else False,
            "sent_at": int(time.time() * 1000),
        })
        chapters = state.pop("chapters", None) or []
        state["chapter_count"] = len(chapters)
        state["chapter_starts"] = [int(c.get("start", 0)) for c in chapters][:200]
        return state

    def _schedule_state(self):
        if self._state_pending or not self.clients:
            return False
        self._state_pending = True
        GLib.timeout_add(120, self._flush_state)
        return False

    def _flush_state(self):
        self._state_pending = False
        if self.clients:
            self.broadcast(self.snapshot())
        return False

    def _on_state(self, _state):
        self._schedule_state()

    def _on_position(self, position, duration):
        now = time.monotonic()
        if now - self._last_position_push < 0.9 and self.player.status == "playing":
            return
        self._last_position_push = now
        self.broadcast({"type": "position", "position": position, "duration": duration,
                        "sent_at": int(time.time() * 1000), "speed": self.player.speed,
                        "chapter": self.player.state()["chapter"],
                        "sleep": self.player.state()["sleep"]})

    # ------------------------------------------------------------ commands

    def _handle_line(self, client, line):
        try:
            message = json.loads(line)
            command = message.get("cmd")
        except (ValueError, AttributeError):
            self._send(client, {"type": "error", "error": "bad json"})
            return
        ok, error = True, ""
        try:
            ok = self.execute(command, message)
        except Exception as exc:  # report, never crash the app
            ok, error = False, str(exc)
        if message.get("id") is not None or not ok:
            self._send(client, {"type": "result", "id": message.get("id"), "ok": bool(ok), "error": error})

    def execute(self, command, args):
        player = self.player
        simple = {
            "toggle": player.toggle, "play": player.play, "pause": player.pause, "stop": player.stop,
            "rewind": player.rewind, "forward": player.fast_forward, "skip": player.skip,
            "chapter_next": player.next_chapter, "chapter_prev": player.previous_chapter,
            "cycle_speed": player.cycle_speed, "sleep_cancel": player.cancel_sleep_timer,
            "restart": player.restart,
        }
        if command in simple:
            result = simple[command]()
            return result is not False
        if command == "seek":
            player.seek(int(args.get("position", 0)))
        elif command == "seek_relative":
            player.seek_relative(int(args.get("delta", 0)))
        elif command == "speed":
            player.set_speed(float(args.get("value", 1.0)))
        elif command == "volume":
            player.set_volume(int(args.get("value", 100)))
        elif command == "sleep":
            if args.get("episodes"):
                player.set_sleep_timer(episodes=int(args["episodes"]))
            else:
                player.set_sleep_timer(minutes=float(args.get("minutes", 15)))
        elif command == "sleep_extend":
            player.extend_sleep_timer(float(args.get("minutes", 5)))
        elif command == "skip_silence":
            player.set_skip_silence(bool(args.get("enabled", not player.skip_silence_enabled())))
        elif command == "play_episode":
            return player.play_episode(int(args["id"]))
        elif command == "chapter":
            player.seek_chapter(int(args.get("index", 0)))
        elif command == "favorite":
            if player.episode is not None:
                value = not bool(player.episode.favorite)
                self.library.set_favorite([player.episode.id], value)
                player.episode.favorite = int(value)
                player._emit_state()
        elif command == "show":
            self.app.activate()
            if args.get("page"):
                self.app.show_page(args["page"])
        elif command == "now_playing":
            self.app.activate()
            self.app.show_page("now-playing")
        elif command == "refresh":
            self.app.refresh_all()
        elif command == "state":
            return True
        elif command == "quit":
            self.app.quit()
        else:
            raise ValueError(f"unknown command {command!r}")
        return True


def send_command(path, command, **args):
    """Client side, used by `omapod ctl`. Returns the server's reply (or raises OSError)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(path))
    payload = dict(args, cmd=command, id=1)
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    buffer = b""
    state = None
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("type") == "state" and state is None:
                    state = message
                if message.get("type") == "result" and message.get("id") == 1:
                    sock.close()
                    return message, state
    finally:
        sock.close()
    return {"ok": False, "error": "no reply"}, state
