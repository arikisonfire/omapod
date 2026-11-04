"""Now Playing: big cover, chapters as workspaces, show notes, chapters and transcript."""

import threading

from gi.repository import Adw, GLib, Graphene, Gtk, Pango

from ...events import bus
from ...feeds.chapters import index_at
from ...feeds.transcripts import parse_transcript
from ...i18n import _
from ...net import http
from ...util import fmt_clock, fmt_speed
from ..widgets.chapterbar import ChapterBar
from ..widgets.common import Cover, Debouncer, icon_button, label
from ..widgets.dock import SleepPopover, SpeedPopover, VolumePopover, time_label, total_label
from ..widgets.shownotes import ShowNotes
from .base import BasePage

# Every paragraph is a widget, laid out at once: a three-hour episode has about a thousand and 200,000 characters,
# which take a second. The rest of a longer one is left out.
TRANSCRIPT_MAX_PARAGRAPHS = 3000
TRANSCRIPT_MAX_CHARS = 300_000
COLUMN = 312  # the window's minimum width less the page's padding


def _shown_part(segments):
    total = 0
    for count, segment in enumerate(segments[:TRANSCRIPT_MAX_PARAGRAPHS]):
        total += len(segment.text)
        if total > TRANSCRIPT_MAX_CHARS:
            return segments[:count]
    return segments[:TRANSCRIPT_MAX_PARAGRAPHS]


class NowPlayingPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Now playing"), "now-playing")
        self.title_label.set_visible(False)
        self._episode_id = None
        self._chapters_key = None
        self._transcript = []
        self._transcript_for = None
        self._transcript_rows = []
        self._follow = True
        self._remaining = bool(ctx.settings.get("show_remaining_time"))

        bin_ = Adw.BreakpointBin()
        bin_.set_size_request(360, 200)
        self.layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=32, css_classes=["omp-np"])
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 880sp"))
        breakpoint.add_setter(self.layout, "orientation", Gtk.Orientation.VERTICAL)
        bin_.add_breakpoint(breakpoint)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scrolled.set_child(self.layout)
        bin_.set_child(scrolled)
        self.set_content(bin_)

        # Left: the player itself
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True)
        left.set_size_request(COLUMN, -1)
        self.cover = Cover(ctx.textures, COLUMN)
        self.cover.set_halign(Gtk.Align.CENTER)
        left.append(self.cover)
        self.title = label("", ("omp-np-title",), wrap=True, selectable=True)
        self.title.set_justify(Gtk.Justification.LEFT)
        left.append(self.title)
        podcast = Gtk.Button(css_classes=["flat"], halign=Gtk.Align.START)
        self.podcast = label("", ("omp-np-podcast",))
        podcast.set_child(self.podcast)
        podcast.connect("clicked", lambda _b: ctx.player.episode and ctx.show_feed(ctx.player.episode.feed_id))
        left.append(podcast)
        self.chapter = label("", ("omp-np-chapter",), wrap=True)
        left.append(self.chapter)
        self.workspaces = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=24,
                                      column_spacing=4, row_spacing=4, homogeneous=False)
        left.append(self.workspaces)
        self.bar = ChapterBar(ctx.theme, lambda ms: ctx.player.seek(ms), thickness=6)
        self.bar.set_size_request(-1, 24)
        left.append(self.bar)
        times = Gtk.Box()
        self.elapsed = label("0:00", ("omp-time",), ellipsize=False)
        times.append(self.elapsed)
        times.append(Gtk.Box(hexpand=True))
        remaining = Gtk.Button(css_classes=["flat", "omp-time"])
        self.total = label("0:00", ("omp-time",), ellipsize=False)
        remaining.set_child(self.total)
        remaining.connect("clicked", self._toggle_remaining)
        times.append(remaining)
        left.append(times)

        transport = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        transport.append(icon_button("media-skip-backward-symbolic", _("Previous chapter"),
                                     lambda: ctx.player.previous_chapter()))
        self.rewind = icon_button("media-seek-backward-symbolic", "", lambda: ctx.player.rewind())
        transport.append(self.rewind)
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["omp-play"])
        self.play.set_size_request(56, 56)
        self.play.connect("clicked", lambda _b: ctx.player.toggle())
        transport.append(self.play)
        self.forward = icon_button("media-seek-forward-symbolic", "", lambda: ctx.player.fast_forward())
        transport.append(self.forward)
        transport.append(icon_button("media-skip-forward-symbolic", _("Next chapter or episode"),
                                     lambda: ctx.player.next_chapter()))
        left.append(transport)

        extras = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.speed = Gtk.MenuButton(css_classes=["flat", "omp-speed"], tooltip_text=_("Playback speed"))
        self.speed.set_popover(SpeedPopover(ctx))
        extras.append(self.speed)
        self.sleep = Gtk.MenuButton(icon_name="weather-clear-night-symbolic", css_classes=["flat"],
                                    tooltip_text=_("Sleep timer"))
        self.sleep.set_popover(SleepPopover(ctx))
        extras.append(self.sleep)
        self.sleep_label = label("", ("omp-time", "omp-sleep-active"), ellipsize=False)
        extras.append(self.sleep_label)
        volume = Gtk.MenuButton(icon_name="audio-volume-high-symbolic", css_classes=["flat"],
                                tooltip_text=_("Volume"))
        volume.set_popover(VolumePopover(ctx))
        extras.append(volume)
        self.favorite = Gtk.ToggleButton(icon_name="starred-symbolic", css_classes=["flat"],
                                         tooltip_text=_("Favorite"))
        self._fav_handler = self.favorite.connect("toggled", self._on_favorite)
        extras.append(self.favorite)
        extras.append(icon_button("x-office-document-symbolic", _("Episode details"),
                                  lambda: ctx.player.episode and ctx.show_episode(ctx.player.episode.id)))
        extras.append(icon_button("media-skip-forward-symbolic", _("Skip episode"), lambda: ctx.player.skip()))
        left.append(extras)
        self.layout.append(left)

        # Right: show notes, chapters, transcript
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        right.set_size_request(COLUMN, -1)
        # As tall as the open tab: a stack is as tall as its tallest page, so a long transcript or chapter list
        # would leave screens of empty space to scroll through below short show notes.
        self.tabs = Adw.ViewStack(vexpand=True, vhomogeneous=False)
        switcher_type = getattr(Adw, "InlineViewSwitcher", None)
        switcher = switcher_type(stack=self.tabs) if switcher_type else Adw.ViewSwitcher(stack=self.tabs)
        switcher.set_halign(Gtk.Align.START)
        self.switcher = switcher
        right.append(switcher)
        self.notes = ShowNotes(ctx, on_seek=lambda ms: ctx.player.seek(ms))
        notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        notes_box.append(self.notes)
        self.tabs.add_titled_with_icon(notes_box, "notes", _("Show notes"), "x-office-document-symbolic")
        self.chapter_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.chapters_page = self.tabs.add_titled_with_icon(self.chapter_list, "chapters", _("Chapters"),
                                                            "view-list-bullet-symbolic")
        transcript_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        follow = Gtk.CheckButton(label=_("Follow audio"), active=True)
        follow.connect("toggled", lambda b: setattr(self, "_follow", b.get_active()))
        transcript_box.append(follow)
        self.transcript_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        transcript_box.append(self.transcript_list)
        self.transcript_page = self.tabs.add_titled_with_icon(transcript_box, "transcript", _("Transcript"),
                                                              "chat-bubble-text-symbolic")
        right.append(self.tabs)
        self.layout.append(right)
        self.scrolled = scrolled

        # The window hides the dock while the play button is on screen and brings it back
        # once the button scrolls away, e.g. while reading long show notes.
        dock_check = Debouncer(80, ctx.window.update_dock)
        adjustment = scrolled.get_vadjustment()
        adjustment.connect("value-changed", dock_check)
        adjustment.connect("changed", dock_check)
        self.connect("showing", dock_check)
        self.connect("shown", dock_check)

        self.listen("player-state", self._on_state)
        self.listen("player-position", self._on_position)
        self._on_state(ctx.player.state())

    # ------------------------------------------------------------ state

    def _on_state(self, state):
        episode = state.get("episode")
        if episode is None:
            # Nothing is loaded any more (end of the queue, sleep timer, stop): the player page goes.
            GLib.idle_add(self._close_if_unloaded)
            return
        ctx = self.ctx
        if episode["id"] != self._episode_id:
            self._episode_id = episode["id"]
            full = ctx.db.get_episode(episode["id"])
            self.notes.set_notes(full.description if full else "", _("This episode has no show notes."))
            self._load_transcript(full)
        self.cover.set_source(episode.get("image") or episode.get("feed_image"), episode.get("podcast"))
        self.cover.set_playing(state["playing"])
        self.title.set_text(episode.get("title") or "")
        self.podcast.set_text(episode.get("podcast") or "")
        chapters = state.get("chapters") or []
        index = state.get("chapter_index", -1)
        self.chapter.set_visible(bool(state.get("chapter")))
        self.chapter.set_text(state.get("chapter") or "")
        key = (episode["id"], len(chapters), tuple(c["start"] for c in chapters))
        if key != self._chapters_key:
            self._chapters_key = key
            self._build_chapters(chapters)
        self._mark_chapter(index)
        playing = state["playing"]
        self.play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self.play.set_tooltip_text(_("Pause") if playing else _("Play"))
        self.rewind.set_tooltip_text(_("Rewind {n} s").format(n=state.get("rewind_secs", 10)))
        self.forward.set_tooltip_text(_("Forward {n} s").format(n=state.get("forward_secs", 30)))
        self.speed.set_label(fmt_speed(state.get("speed", 1.0)))
        sleep = state.get("sleep")
        if sleep and sleep["mode"] == "time":
            self.sleep_label.set_text(fmt_clock(sleep["remaining"]))
        elif sleep:
            self.sleep_label.set_text(_("end of episode"))
        else:
            self.sleep_label.set_text("")
        self.favorite.handler_block(self._fav_handler)
        self.favorite.set_active(bool(episode.get("favorite")))
        self.favorite.handler_unblock(self._fav_handler)
        self.bar.set_state(state["position"], state["duration"], chapters)
        self._on_position(state["position"], state["duration"])

    def _on_position(self, position, duration):
        self.elapsed.set_text(time_label(position, duration))
        self.total.set_text(total_label(position, duration, self._remaining))
        self.bar.set_state(position, duration)
        self._mark_transcript(position)

    def _toggle_remaining(self, _button):
        self._remaining = not self._remaining
        self.ctx.settings.set("show_remaining_time", self._remaining)
        self._on_position(self.ctx.player.position, self.ctx.player.duration)

    def _on_favorite(self, button):
        episode = self.ctx.player.episode
        if episode is not None:
            self.ctx.library.set_favorite([episode.id], button.get_active())
            episode.favorite = int(button.get_active())

    def _close_if_unloaded(self):
        if self.ctx.player.episode is None:
            self.ctx.window.remove_page(self)
        return False

    def initial_focus(self):
        return self.play

    def shows_controls(self, dock_height):
        """True if the play button is on screen once the dock (now dock_height tall) is gone.

        Judging the view without the dock keeps the answer the same while the dock slides."""
        height = self.scrolled.get_height()
        if not self.play.get_mapped() or not height:
            return False
        ok, point = self.play.compute_point(self.scrolled, Graphene.Point().init(0, self.play.get_height() / 2))
        if not ok:
            return False
        height += dock_height
        adjustment = self.scrolled.get_vadjustment()
        value = adjustment.get_value()
        # A taller view can scroll back when it was at the end.
        settled = min(value, max(0, adjustment.get_upper() - height))
        return 0 <= point.y + value - settled <= height

    def _update_switcher(self):
        if hasattr(self, "switcher"):
            pages = [self.chapters_page.get_visible(), self.transcript_page.get_visible()] \
                if hasattr(self, "transcript_page") else [self.chapters_page.get_visible()]
            self.switcher.set_visible(any(pages))

    # ------------------------------------------------------------ chapters

    def _build_chapters(self, chapters):
        for box in (self.workspaces, self.chapter_list):
            child = box.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                box.remove(child)
                child = following
        self.chapters_page.set_visible(bool(chapters))
        self.workspaces.set_visible(len(chapters) > 1)
        self._update_switcher()
        self._ws_buttons = []
        self._chapter_rows = []
        for index, chapter in enumerate(chapters):
            button = Gtk.Button(label=str(index + 1), css_classes=["omp-ws"])
            button.set_tooltip_text(f"{fmt_clock(chapter['start'])}  {chapter.get('title') or ''}")
            button.connect("clicked", lambda _b, i=index: self.ctx.player.seek_chapter(i))
            self.workspaces.append(button)
            self._ws_buttons.append(button)
            row_button = Gtk.Button(css_classes=["flat"])
            row = Gtk.Box(spacing=12, css_classes=["omp-chapter-row"])
            row.append(label(f"{index + 1:>2}", ("omp-numeric", "omp-dim"), ellipsize=False))
            title = label(chapter.get("title") or _("Chapter {n}").format(n=index + 1), ("omp-chapter-title",),
                          wrap=True)
            title.set_hexpand(True)
            row.append(title)
            if chapter.get("url"):
                link = icon_button("web-browser-symbolic", _("Open chapter link"),
                                   lambda u=chapter["url"]: self.ctx.open_uri(u))
                row.append(link)
            row.append(label(fmt_clock(chapter["start"]), ("omp-numeric", "omp-dim"), ellipsize=False))
            row_button.set_child(row)
            row_button.connect("clicked", lambda _b, i=index: self.ctx.player.seek_chapter(i))
            self.chapter_list.append(row_button)
            self._chapter_rows.append(row)

    def _mark_chapter(self, current):
        for index, button in enumerate(getattr(self, "_ws_buttons", [])):
            button.remove_css_class("omp-ws-current")
            button.remove_css_class("omp-ws-done")
            if index == current:
                button.add_css_class("omp-ws-current")
            elif index < current:
                button.add_css_class("omp-ws-done")
        for index, row in enumerate(getattr(self, "_chapter_rows", [])):
            if index == current:
                row.add_css_class("omp-current")
            else:
                row.remove_css_class("omp-current")

    # ------------------------------------------------------------ transcript

    def _load_transcript(self, episode):
        child = self.transcript_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.transcript_list.remove(child)
            child = following
        self._transcript = []
        self._transcript_rows = []
        has = bool(episode and episode.transcript_url)
        self.transcript_page.set_visible(has)
        self._update_switcher()
        if not has:
            return
        self._transcript_for = episode.id
        self.transcript_list.append(label(_("Loading transcript…"), ("omp-dim",)))
        url, mime, episode_id = episode.transcript_url, episode.transcript_type, episode.id

        def work():
            try:
                response = http.request(url, timeout=20, max_bytes=8 * 1024 * 1024)
                segments = _shown_part(parse_transcript(response.body, mime or response.content_type))
            except Exception:
                segments = None
            GLib.idle_add(self._show_transcript, episode_id, segments)

        threading.Thread(target=work, daemon=True).start()

    def _show_transcript(self, episode_id, segments):
        if episode_id != self._transcript_for:
            return False
        child = self.transcript_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.transcript_list.remove(child)
            child = following
        if not segments:
            self.transcript_list.append(label(_("No transcript"), ("omp-dim",)))
            return False
        self._transcript = segments
        for segment in segments:
            button = Gtk.Button(css_classes=["flat"])
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            if segment.speaker:
                box.append(label(f"{segment.speaker}  ·  {fmt_clock(segment.start)}", ("omp-transcript-speaker",)))
            text = label(segment.text, ("omp-transcript-text",), wrap=True, selectable=False)
            box.append(text)
            button.set_child(box)
            button.connect("clicked", lambda _b, ms=segment.start: self.ctx.player.seek(ms))
            self.transcript_list.append(button)
            self._transcript_rows.append((segment, text, button))
        return False

    def _mark_transcript(self, position):
        if not self._transcript_rows:
            return
        current = None
        for segment, text, button in self._transcript_rows:
            if segment.start <= position:
                current = (segment, text, button)
            text.remove_css_class("omp-current")
        if current is None:
            return
        current[1].add_css_class("omp-current")
        if not self._follow or self.tabs.get_visible_child_name() != "transcript":
            return
        if getattr(self, "_followed", None) is current[2]:
            return
        self._followed = current[2]
        ok, point = current[2].compute_point(self.layout, Graphene.Point().init(0, 0))
        if ok:
            adjustment = self.scrolled.get_vadjustment()
            target = point.y - adjustment.get_page_size() / 3
            adjustment.set_value(max(0, min(target, adjustment.get_upper() - adjustment.get_page_size())))
