"""The player dock at the bottom of the window, plus the speed and sleep popovers
that the dock and the Now Playing page share."""

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ...events import bus
from ...i18n import _, ngettext
from ...util import fmt_clock, fmt_speed
from .chapterbar import ChapterBar
from .common import Cover, icon_button, label

SPEED_STEPS = [0.5, 0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.75, 1.8, 2.0, 2.25, 2.5, 3.0, 3.5]


def time_label(ms, duration, remaining=False):
    hours = duration >= 3600000
    if remaining:
        return "−" + fmt_clock(max(0, duration - ms), force_hours=hours)
    return fmt_clock(ms, force_hours=hours)


def total_label(ms, duration, remaining=False):
    """Right-hand time: the remaining time or the total length."""
    if not duration:
        return "–"
    if remaining:
        return time_label(ms, duration, remaining=True)
    return fmt_clock(duration, force_hours=duration >= 3600000)


class SpeedPopover(Gtk.Popover):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        header = Gtk.Box(spacing=8)
        header.append(label(_("Playback speed"), ("heading",)))
        self.value = label("", ("omp-numeric", "heading"), xalign=1.0)
        self.value.set_hexpand(True)
        header.append(self.value)
        box.append(header)
        row = Gtk.Box(spacing=6)
        row.append(icon_button("list-remove-symbolic", _("Decrease speed"), self._step, ("omp-icon",), -1))
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 3.5, 0.05)
        self.scale.set_hexpand(True)
        self.scale.set_size_request(220, -1)
        self.scale.set_draw_value(False)
        for mark in (1.0, 1.5, 2.0, 2.5, 3.0):
            self.scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        self._block = False
        self.scale.connect("value-changed", self._on_scale)
        row.append(self.scale)
        row.append(icon_button("list-add-symbolic", _("Increase speed"), self._step, ("omp-icon",), 1))
        box.append(row)
        box.append(label(_("Presets"), ("caption", "omp-dim")))
        self.presets = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                   column_spacing=6, row_spacing=6, homogeneous=True)
        box.append(self.presets)
        preset_row = Gtk.Box(spacing=6)
        add = Gtk.Button(label=_("Save as preset"), css_classes=["flat"])
        add.connect("clicked", self._add_preset)
        preset_row.append(add)
        remove = Gtk.Button(label=_("Remove preset"), css_classes=["flat"])
        remove.connect("clicked", self._remove_preset)
        preset_row.append(remove)
        box.append(preset_row)
        box.append(Gtk.Separator())
        self.silence = Gtk.Switch(valign=Gtk.Align.CENTER)
        srow = Gtk.Box(spacing=12)
        srow.append(label(_("Skip silence"), ()))
        spacer = Gtk.Box(hexpand=True)
        srow.append(spacer)
        srow.append(self.silence)
        self.silence.connect("notify::active", self._on_silence)
        box.append(srow)
        self.set_child(box)
        self.connect("show", lambda _p: self.sync())

    def sync(self):
        player = self.ctx.player
        self._block = True
        self.scale.set_value(player.speed)
        self.value.set_text(fmt_speed(player.speed))
        self.silence.set_active(player.skip_silence_enabled())
        self._block = False
        child = self.presets.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.presets.remove(child)
            child = following
        for speed in sorted({float(s) for s in self.ctx.settings.get("speed_presets") or [1.0]}):
            button = Gtk.ToggleButton(label=fmt_speed(speed), active=abs(speed - player.speed) < 0.001)
            button.add_css_class("omp-chip")
            button.connect("clicked", lambda _b, s=speed: self._set(s))
            self.presets.append(button)

    def _set(self, speed):
        self.ctx.player.set_speed(speed)
        self.sync()

    def _step(self, direction):
        current = self.ctx.player.speed
        if direction > 0:
            later = [s for s in SPEED_STEPS if s > current + 0.001]
            self._set(later[0] if later else SPEED_STEPS[-1])
        else:
            earlier = [s for s in SPEED_STEPS if s < current - 0.001]
            self._set(earlier[-1] if earlier else SPEED_STEPS[0])

    def _on_scale(self, scale):
        if self._block:
            return
        value = round(scale.get_value() * 20) / 20
        self.ctx.player.set_speed(value)
        self.value.set_text(fmt_speed(value))

    def _add_preset(self, _button):
        presets = sorted({float(s) for s in self.ctx.settings.get("speed_presets") or []} | {self.ctx.player.speed})
        self.ctx.settings.set("speed_presets", presets)
        self.sync()

    def _remove_preset(self, _button):
        presets = [s for s in self.ctx.settings.get("speed_presets") or [] if abs(float(s) - self.ctx.player.speed) > 0.001]
        self.ctx.settings.set("speed_presets", presets or [1.0])
        self.sync()

    def _on_silence(self, switch, _pspec):
        if not self._block:
            self.ctx.player.set_skip_silence(switch.get_active())


class SleepPopover(Gtk.Popover):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.append(label(_("Sleep timer"), ("heading",)))
        self.status = label("", ("omp-dim",), wrap=True)
        box.append(self.status)
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, column_spacing=6,
                           row_spacing=6, homogeneous=True)
        for minutes in (5, 10, 15, 20, 30, 45, 60, 90):
            button = Gtk.Button(label=_("{n} min").format(n=minutes), css_classes=["omp-chip"])
            button.connect("clicked", lambda _b, m=minutes: self._start(m))
            grid.append(button)
        box.append(grid)
        custom = Gtk.Box(spacing=6)
        self.spin = Gtk.SpinButton.new_with_range(1, 600, 1)
        self.spin.set_value(ctx.settings.get("sleep_minutes") or 15)
        custom.append(self.spin)
        custom.append(label(_("minutes"), ("omp-dim",)))
        start = Gtk.Button(label=_("Start"), css_classes=["omp-primary"])
        start.set_hexpand(True)
        start.connect("clicked", lambda _b: self._start(int(self.spin.get_value())))
        custom.append(start)
        box.append(custom)
        episode = Gtk.Button(label=_("At the end of this episode"))
        episode.connect("clicked", lambda _b: self._episodes(1))
        box.append(episode)
        box.append(Gtk.Separator())
        self.extend_row = Gtk.Box(spacing=6, homogeneous=True)
        for minutes in (5, 10, 15):
            button = Gtk.Button(label=f"+{minutes} min", css_classes=["flat"])
            button.connect("clicked", lambda _b, m=minutes: self._extend(m))
            self.extend_row.append(button)
        box.append(self.extend_row)
        self.cancel = Gtk.Button(label=_("Disable sleep timer"), css_classes=["destructive-action"])
        self.cancel.connect("clicked", lambda _b: self._cancel())
        box.append(self.cancel)
        auto = Gtk.Box(spacing=12)
        auto.append(label(_("Turn on automatically at night"), ()))
        auto.append(Gtk.Box(hexpand=True))
        self.auto = Gtk.Switch(active=bool(ctx.settings.get("sleep_auto_enable")), valign=Gtk.Align.CENTER)
        self.auto.connect("notify::active", lambda s, _p: ctx.settings.set("sleep_auto_enable", s.get_active()))
        auto.append(self.auto)
        box.append(auto)
        hours = label(_("Between {start}:00 and {end}:00 when you press play (change in Settings)").format(
            start=ctx.settings.get("sleep_auto_from"), end=ctx.settings.get("sleep_auto_to")), ("caption", "omp-dim"),
            wrap=True)
        box.append(hours)
        self.set_child(box)
        self.connect("show", lambda _p: self.sync())

    def sync(self):
        sleep = self.ctx.player.sleep
        active = sleep is not None
        self.extend_row.set_visible(active)
        self.cancel.set_visible(active)
        if not active:
            self.status.set_text(_("Pause playback after a while."))
        elif sleep["mode"] == "episodes":
            self.status.set_text(ngettext("Stops after {n} episode.", "Stops after {n} episodes.",
                                          sleep["episodes"]).format(n=sleep["episodes"]))
        else:
            self.status.set_text(_("Pauses in {time}.").format(time=fmt_clock(sleep["remaining"])))

    def _start(self, minutes):
        self.ctx.player.set_sleep_timer(minutes=minutes)
        self.popdown()

    def _episodes(self, count):
        self.ctx.player.set_sleep_timer(episodes=count)
        self.popdown()

    def _extend(self, minutes):
        self.ctx.player.extend_sleep_timer(minutes)
        self.sync()

    def _cancel(self):
        self.ctx.player.cancel_sleep_timer()
        self.popdown()


class VolumePopover(Gtk.Popover):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        self.value = label("", ("heading", "omp-numeric"), xalign=0.5)
        box.append(self.value)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, 0, 150, 5)
        self.scale.set_inverted(True)
        self.scale.set_draw_value(False)
        self.scale.set_size_request(-1, 160)
        self.scale.add_mark(100, Gtk.PositionType.RIGHT, None)
        self.scale.connect("value-changed", self._changed)
        box.append(self.scale)
        self.set_child(box)
        self.connect("show", lambda _p: self.scale.set_value(ctx.player.volume))

    def _changed(self, scale):
        value = int(scale.get_value())
        self.value.set_text(f"{value}%")
        if value != self.ctx.player.volume:
            self.ctx.player.set_volume(value)


class PlayerDock(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.ctx = ctx
        self.add_css_class("omp-dock")
        self.set_visible(False)
        self._remaining = bool(ctx.settings.get("show_remaining_time"))

        # Episode
        episode = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
        self.cover = Cover(ctx.textures, 56)
        self.cover.set_valign(Gtk.Align.CENTER)
        self.cover.set_cursor_from_name("pointer")
        episode.append(self.cover)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.text_box = text
        self.title = label("", ("omp-dock-title",))
        self.title.set_max_width_chars(34)
        self.title.set_width_chars(10)
        text.append(self.title)
        self.sub = label("", ("omp-dock-sub",))
        self.sub.set_max_width_chars(34)
        text.append(self.sub)
        self.chapter = label("", ("omp-dock-chapter",))
        self.chapter.set_max_width_chars(34)
        text.append(self.chapter)
        episode.append(text)
        open_np = Gtk.GestureClick()
        open_np.connect("released", lambda *_a: ctx.show_now_playing())
        episode.add_controller(open_np)
        episode.set_cursor_from_name("pointer")
        episode.set_tooltip_text(_("Open Now Playing"))
        self.append(episode)

        # Transport + progress
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        transport = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.rewind = icon_button("media-seek-backward-symbolic", "", lambda: ctx.player.rewind())
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["omp-play"],
                               focus_on_click=False,
                               tooltip_text=_("Play"))
        self.play.connect("clicked", lambda _b: ctx.player.toggle())
        self.forward = icon_button("media-seek-forward-symbolic", "", lambda: ctx.player.fast_forward())
        self.skip = icon_button("media-skip-forward-symbolic", _("Skip episode"), lambda: ctx.player.skip())
        for widget in (self.rewind, self.play, self.forward, self.skip):
            transport.append(widget)
        center.append(transport)
        progress = Gtk.Box(spacing=10)
        self.elapsed = label("0:00", ("omp-time",), xalign=1.0, ellipsize=False)
        self.elapsed.set_width_chars(7)
        progress.append(self.elapsed)
        self.bar = ChapterBar(ctx.theme, lambda ms: ctx.player.seek(ms))
        progress.append(self.bar)
        self.total = Gtk.Button(css_classes=["flat", "omp-time"], tooltip_text=_("Show remaining time"),
                                focus_on_click=False)
        self.total_label = label("0:00", ("omp-time",), xalign=0.0, ellipsize=False)
        self.total_label.set_width_chars(7)
        self.total.set_child(self.total_label)
        self.total.connect("clicked", self._toggle_remaining)
        progress.append(self.total)
        center.append(progress)
        self.append(center)

        # Extras
        extras = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.extras = extras
        self.speed = Gtk.MenuButton(css_classes=["flat", "omp-speed"], tooltip_text=_("Playback speed"))
        self.speed.set_popover(SpeedPopover(ctx))
        extras.append(self.speed)
        self.sleep = Gtk.MenuButton(icon_name="weather-clear-night-symbolic", css_classes=["flat"],
                                    tooltip_text=_("Sleep timer"))
        self.sleep_popover = SleepPopover(ctx)
        self.sleep.set_popover(self.sleep_popover)
        extras.append(self.sleep)
        self.volume = Gtk.MenuButton(icon_name="audio-volume-high-symbolic", css_classes=["flat"],
                                     tooltip_text=_("Volume"))
        self.volume.set_popover(VolumePopover(ctx))
        extras.append(self.volume)
        self.queue_button = icon_button("view-list-ordered-symbolic", _("Show the queue"),
                                        lambda: ctx.show_page("queue"))
        extras.append(self.queue_button)
        self.append(extras)

        bus.subscribe("player-state", self._on_state)
        bus.subscribe("player-position", self._on_position)
        ctx.settings.connect(self._on_setting)

    def _on_setting(self, key, _value):
        if key in ("rewind_secs", "fast_forward_secs"):
            self._on_state(self.ctx.player.state())

    def _toggle_remaining(self, _button):
        self._remaining = not self._remaining
        self.ctx.settings.set("show_remaining_time", self._remaining)
        self._on_position(self.ctx.player.position, self.ctx.player.duration)

    def _on_state(self, state):
        episode = state.get("episode")
        self.set_visible(episode is not None)
        if episode is None:
            return
        self.cover.set_source(episode.get("image") or episode.get("feed_image"), episode.get("podcast"))
        self.cover.set_playing(state["playing"])
        self.title.set_text(episode.get("title") or "")
        self.title.set_tooltip_text(episode.get("title") or "")
        self.sub.set_text(episode.get("podcast") or "")
        chapter = state.get("chapter") or ""
        index = state.get("chapter_index", -1)
        self.chapter.set_visible(bool(chapter))
        if chapter:
            self.chapter.set_text(f"{index + 1}/{len(state.get('chapters') or [])}  {chapter}")
        playing = state["playing"]
        self.play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self.play.set_tooltip_text(_("Pause") if playing else _("Play"))
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [self.play.get_tooltip_text()])
        self.rewind.set_tooltip_text(_("Rewind {n} s").format(n=state.get("rewind_secs", 10)))
        self.forward.set_tooltip_text(_("Forward {n} s").format(n=state.get("forward_secs", 30)))
        self.speed.set_label(fmt_speed(state.get("speed", 1.0)))
        sleep = state.get("sleep")
        if sleep:
            self.sleep.add_css_class("omp-sleep-active")
            # The moon stays and turns to the accent colour. (A label, even an empty one, would replace the
            # menu button's icon with a dropdown arrow.)
            if sleep["mode"] == "time":
                self.sleep.set_tooltip_text(_("Sleep timer: {time} left").format(time=fmt_clock(sleep["remaining"])))
            else:
                self.sleep.set_tooltip_text(_("Sleep timer: end of episode"))
        else:
            self.sleep.remove_css_class("omp-sleep-active")
            self.sleep.set_tooltip_text(_("Sleep timer"))
        volume = state.get("volume", 100)
        self.volume.set_icon_name("audio-volume-muted-symbolic" if volume == 0 else
                                  "audio-volume-low-symbolic" if volume < 40 else
                                  "audio-volume-medium-symbolic" if volume < 80 else "audio-volume-high-symbolic")
        self.bar.set_state(state["position"], state["duration"], state.get("chapters") or [])
        self._on_position(state["position"], state["duration"])

    def _on_position(self, position, duration):
        self.elapsed.set_text(time_label(position, duration))
        self.total_label.set_text(total_label(position, duration, self._remaining))
        self.bar.set_state(position, duration)
