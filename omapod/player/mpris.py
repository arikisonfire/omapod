"""MPRIS 2 on the session bus, so media keys, Omarchy's media widget and
other desktop tools can see and control omaPOD."""

import os

from gi.repository import Gio, GLib

from .. import APP_ID, APP_NAME
from ..events import bus
from ..util import html_to_text

BUS_NAME = "org.mpris.MediaPlayer2.omapod"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class Mpris:
    def __init__(self, app, player, images):
        self.app = app
        self.player = player
        self.images = images
        self.connection = None
        self.owner_id = 0
        self.registrations = []
        self._last = {}
        self._last_position = 0
        self._node = Gio.DBusNodeInfo.new_for_xml(XML)

    def start(self):
        try:
            self.owner_id = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                             self._on_bus, None, None)
        except GLib.Error:
            self.owner_id = 0
        bus.subscribe("player-state", self._on_state)
        bus.subscribe("player-position", self._on_position)

    def stop(self):
        for registration in self.registrations:
            try:
                self.connection.unregister_object(registration)
            except Exception:
                pass
        self.registrations = []
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)
            self.owner_id = 0

    def _on_bus(self, connection, _name):
        self.connection = connection
        for interface in self._node.interfaces:
            registration = connection.register_object(OBJECT_PATH, interface, self._method, self._get, self._set)
            self.registrations.append(registration)

    # ------------------------------------------------------------ properties

    def _track_id(self):
        episode = self.player.episode
        if episode is None:
            return "/org/mpris/MediaPlayer2/TrackList/NoTrack"
        return f"/io/github/arikisonfire/OmaPod/episode/{episode.id}"

    def _metadata(self):
        episode = self.player.episode
        if episode is None:
            return {"mpris:trackid": GLib.Variant("o", self._track_id())}
        meta = {
            "mpris:trackid": GLib.Variant("o", self._track_id()),
            "xesam:title": GLib.Variant("s", episode.title or ""),
            "xesam:artist": GLib.Variant("as", [episode.feed_title or ""]),
            "xesam:album": GLib.Variant("s", episode.feed_title or ""),
        }
        if self.player.duration > 0:
            meta["mpris:length"] = GLib.Variant("x", int(self.player.duration) * 1000)
        art = episode.cover_url(True)
        if art:
            cached = self.images.thumb_if_cached(art, 512)
            if cached:
                meta["mpris:artUrl"] = GLib.Variant("s", GLib.filename_to_uri(cached, None))
            else:
                self.images.request(art, 512, lambda _path: GLib.idle_add(self._refresh_metadata))
                # Only thumbnails omaPOD rendered (and checked) go to other programs, never the feed's original.
                meta["mpris:artUrl"] = GLib.Variant("s", "")
        # Only the local file: the address of a private feed's episode often carries its access token, and every
        # program on the session bus can read this.
        if episode.is_downloaded and os.path.exists(episode.file_path):
            meta["xesam:url"] = GLib.Variant("s", GLib.filename_to_uri(episode.file_path, None))
        if episode.description:
            meta["xesam:comment"] = GLib.Variant("as", [html_to_text(episode.description, 300)])
        return meta

    def _status(self):
        if self.player.status in ("playing", "loading"):
            return "Playing"
        if self.player.episode is not None:
            return "Paused"
        return "Stopped"

    def _properties(self):
        has_episode = self.player.episode is not None
        return {
            "PlaybackStatus": GLib.Variant("s", self._status()),
            "Rate": GLib.Variant("d", float(self.player.speed)),
            "Metadata": GLib.Variant("a{sv}", self._metadata()),
            "Volume": GLib.Variant("d", self.player.volume / 100.0),
            "Position": GLib.Variant("x", int(self.player.position) * 1000),
            "MinimumRate": GLib.Variant("d", 0.5),
            "MaximumRate": GLib.Variant("d", 3.5),
            "CanGoNext": GLib.Variant("b", has_episode),
            "CanGoPrevious": GLib.Variant("b", has_episode),
            "CanPlay": GLib.Variant("b", has_episode or bool(self.player.library.queue_ids())),
            "CanPause": GLib.Variant("b", has_episode),
            "CanSeek": GLib.Variant("b", has_episode),
            "CanControl": GLib.Variant("b", True),
        }

    def _root_properties(self):
        return {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", APP_NAME),
            "DesktopEntry": GLib.Variant("s", APP_ID),
            "SupportedUriSchemes": GLib.Variant("as", ["http", "https", "file"]),
            "SupportedMimeTypes": GLib.Variant("as", ["audio/mpeg", "audio/mp4", "audio/ogg", "audio/opus",
                                                      "audio/flac", "video/mp4"]),
        }

    def _get(self, _connection, _sender, _path, interface, name):
        if interface == "org.mpris.MediaPlayer2":
            return self._root_properties().get(name)
        return self._properties().get(name)

    def _set(self, _connection, _sender, _path, _interface, name, value):
        if name == "Rate":
            rate = value.unpack()
            if rate <= 0:
                self.player.pause()
            else:
                self.player.set_speed(rate)
        elif name == "Volume":
            self.player.set_volume(int(round(max(0.0, min(1.5, value.unpack())) * 100)))
        return True

    def _method(self, _connection, _sender, _path, _interface, method, params, invocation):
        player = self.player
        if method == "Raise":
            self.app.activate()
        elif method == "Quit":
            self.app.quit()
        elif method == "Next":
            player.hardware_button("next")
        elif method == "Previous":
            player.hardware_button("previous")
        elif method == "Pause":
            player.pause()
        elif method == "PlayPause":
            player.toggle()
        elif method == "Stop":
            player.pause()
        elif method == "Play":
            player.play()
        elif method == "Seek":
            player.seek_relative(params.unpack()[0] // 1000)
        elif method == "SetPosition":
            track, position = params.unpack()
            if track == self._track_id():
                player.seek(position // 1000)
        elif method == "OpenUri":
            self.app.open_uri(params.unpack()[0])
        invocation.return_value(None)

    # ------------------------------------------------------------ change signals

    def _emit_changed(self, changed, interface="org.mpris.MediaPlayer2.Player"):
        if self.connection is None or not changed:
            return
        try:
            self.connection.emit_signal(None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                        GLib.Variant("(sa{sv}as)", (interface, changed, [])))
        except GLib.Error:
            pass

    def _refresh_metadata(self):
        self._emit_changed({"Metadata": GLib.Variant("a{sv}", self._metadata())})
        return False

    def _on_state(self, _state):
        props = self._properties()
        props.pop("Position")
        changed = {}
        for key, value in props.items():
            if self._last.get(key) != value.print_(False):
                changed[key] = value
                self._last[key] = value.print_(False)
        self._emit_changed(changed)

    def _on_position(self, position, _duration):
        # Only jumps are announced (Seeked); clients extrapolate normal playback.
        expected = self._last_position + 500 * self.player.speed
        if abs(position - expected) > 1500 and self.connection is not None:
            try:
                self.connection.emit_signal(None, OBJECT_PATH, "org.mpris.MediaPlayer2.Player", "Seeked",
                                            GLib.Variant("(x)", (int(position) * 1000,)))
            except GLib.Error:
                pass
        self._last_position = position
