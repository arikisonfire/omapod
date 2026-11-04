"""The signature progress bar: one segment per chapter, like the bar's workspaces.

Played time is drawn in accent, the chapter that is playing gets a lit
background segment, and gaps separate the chapters. Click or drag to seek;
hovering shows the time and chapter under the pointer.
"""

from gi.repository import Gdk, Graphene, Gtk

from ...feeds.chapters import index_at
from ...util import fmt_clock
from ..theme import parse_hex

GAP = 3


def _rgba(hex_value, alpha=1.0):
    rgb = parse_hex(hex_value) or (0.5, 0.5, 0.5)
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = rgb[0], rgb[1], rgb[2], alpha
    return color


class ChapterBar(Gtk.Widget):
    __gtype_name__ = "OmaPodChapterBar"

    def __init__(self, theme, on_seek, thickness=4):
        super().__init__()
        self.theme = theme
        self.on_seek = on_seek
        self.thickness = thickness
        self.position = 0
        self.duration = 0
        self.chapters = []
        self._hover_x = None
        self._drag_ms = None
        self.set_size_request(80, 18)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self.set_focusable(True)
        self.set_cursor_from_name("pointer")
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_tooltip)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        drag.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Playback position"])

    def set_state(self, position, duration, chapters=None):
        changed = (int(position) // 250 != int(self.position) // 250 or duration != self.duration
                   or (chapters is not None and chapters != self.chapters))
        self.position = max(0, int(position))
        self.duration = max(0, int(duration))
        if chapters is not None:
            self.chapters = chapters
        if changed and self._drag_ms is None:
            self.queue_draw()

    # -- geometry ---------------------------------------------------------

    def _segments(self, width):
        if self.duration <= 0:
            return [(0.0, float(width), 0)]
        starts = [c["start"] for c in self.chapters if 0 <= c["start"] < self.duration] if self.chapters else []
        if not starts or starts[0] > 0:
            starts = [0] + starts
        segments = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else self.duration
            x0 = start / self.duration * width
            x1 = end / self.duration * width
            if index > 0:
                x0 += GAP / 2
            if index + 1 < len(starts):
                x1 -= GAP / 2
            segments.append((x0, max(x0 + 1, x1), start))
        return segments

    def _ms_at(self, x):
        width = self.get_width()
        if width <= 0 or self.duration <= 0:
            return 0
        return int(max(0.0, min(1.0, x / width)) * self.duration)

    # -- drawing ------------------------------------------------------------

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if width <= 0:
            return
        values = self.theme.values
        track = _rgba(values.get("fg", "#888888"), 0.18)
        lit = _rgba(values.get("accent", "#888888"), 0.28)
        played = _rgba(values.get("accent", "#888888"))
        hover_mark = _rgba(values.get("fg", "#888888"), 0.6)
        hovering = self._hover_x is not None or self._drag_ms is not None or self.has_focus()
        thick = self.thickness + (2 if hovering else 0)
        y = (height - thick) / 2
        position = self._drag_ms if self._drag_ms is not None else self.position
        played_x = position / self.duration * width if self.duration > 0 else 0
        current = index_at(self.chapters, position) if self.chapters else 0
        segments = self._segments(width)
        for index, (x0, x1, _start) in enumerate(segments):
            is_current = len(segments) > 1 and (index == max(current, 0))
            rect = Graphene.Rect().init(x0, y, x1 - x0, thick)
            snapshot.append_color(lit if is_current else track, rect)
            if played_x > x0:
                fill = Graphene.Rect().init(x0, y, min(x1, played_x) - x0, thick)
                snapshot.append_color(played, fill)
        if self._hover_x is not None and self.duration > 0:
            mark = Graphene.Rect().init(max(0, self._hover_x - 1), y - 3, 2, thick + 6)
            snapshot.append_color(hover_mark, mark)
        if self.duration > 0:
            knob = Graphene.Rect().init(max(0, min(width - 3, played_x - 1.5)), y - 3, 3, thick + 6)
            snapshot.append_color(played, knob)

    # -- input ----------------------------------------------------------------

    def _on_pressed(self, gesture, _n, x, _y):
        self.grab_focus()

    def _on_drag_begin(self, _gesture, x, _y):
        self._drag_ms = self._ms_at(x)
        self.queue_draw()

    def _on_drag_update(self, gesture, dx, _dy):
        ok, start_x, _start_y = gesture.get_start_point()
        if ok:
            self._drag_ms = self._ms_at(start_x + dx)
            self.queue_draw()

    def _on_drag_end(self, gesture, dx, _dy):
        ok, start_x, _start_y = gesture.get_start_point()
        target = self._ms_at(start_x + dx) if ok else self._drag_ms
        self._drag_ms = None
        if self.duration > 0 and target is not None:
            self.position = target
            self.on_seek(target)
        self.queue_draw()

    def _on_motion(self, _controller, x, _y):
        self._hover_x = x
        self.queue_draw()

    def _on_leave(self, _controller):
        self._hover_x = None
        self.queue_draw()

    def _on_key(self, _controller, keyval, _code, _state):
        step = 5000
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Down):
            self.on_seek(max(0, self.position - step))
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Up):
            self.on_seek(min(self.duration, self.position + step))
            return True
        return False

    def _on_tooltip(self, _widget, x, _y, _keyboard, tooltip):
        if self.duration <= 0:
            return False
        ms = self._ms_at(x)
        text = fmt_clock(ms, force_hours=self.duration >= 3600000)
        index = index_at(self.chapters, ms) if self.chapters else -1
        if index >= 0 and self.chapters[index].get("title"):
            text = f"{text}  {index + 1}. {self.chapters[index]['title']}"
        tooltip.set_text(text)
        return True
