"""Episode rows for every episode list, and the list view that hosts them."""

from datetime import date

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from ...events import bus
from ...i18n import _
from ...util import fmt_date, fmt_human_duration, fmt_size
from .common import Cover, EpisodeItem, label


# The small icons after the date, in this order: key, icon, tooltip, style class.
STATE_ICONS = (
    ("downloaded", "folder-download-symbolic", "Downloaded", None),
    ("queued", "view-list-ordered-symbolic", "In the queue", None),
    ("favorite", "starred-symbolic", "Favorite", "omp-favorite"),
    ("video", "camera-video-symbolic", "Video", None),
    ("played", "object-select-symbolic", "Played", None),
)


class EpisodeRow(Gtk.Box):
    """A list shows up to 200 of these on every page, on screen or not. What only some rows need is built when a
    row needs it: fewer widgets make a row quicker to build, and every change of the keyboard focus quicker too.
    Tooltips go on hidden widgets: on a visible one, GTK looks for the widget under the pointer each time."""

    def __init__(self, ctx, show_podcast=True, show_handle=False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.ctx = ctx
        self.show_podcast = show_podcast
        self.item = None
        self._item_handler = 0
        self.add_css_class("omp-row")

        if show_handle:
            self.handle = Gtk.Image(visible=False, icon_name="list-drag-handle-symbolic",
                                    css_classes=["omp-state-icon"], valign=Gtk.Align.CENTER,
                                    tooltip_text=_("Drag to reorder"))
            self.handle.set_cursor_from_name("grab")
            self.handle.set_visible(True)
            self.append(self.handle)
        else:
            self.handle = None

        self.cover = Cover(ctx.textures, 52)
        self.cover.set_valign(Gtk.Align.CENTER)
        self.append(self.cover)

        self.middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        self.append(self.middle)
        self.top = Gtk.Box(spacing=7)
        self.new_mark = None
        self.title = label("", ("omp-row-title",), wrap=True, lines=2)
        self.title.set_hexpand(True)
        self.top.append(self.title)
        self.middle.append(self.top)

        meta_row = Gtk.Box(spacing=6)
        self.meta = label("", ("omp-row-meta",))
        self.meta.set_hexpand(False)
        meta_row.append(self.meta)
        self.icons = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.state_icons = {}
        meta_row.append(self.icons)
        self.middle.append(meta_row)
        self.progress = None

        self.actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.download_label = None
        self.secondary = Gtk.Button(visible=False, css_classes=["flat", "omp-action"], valign=Gtk.Align.CENTER,
                                    focus_on_click=False)
        self.secondary.connect("clicked", self._on_secondary)
        self.actions.append(self.secondary)
        self.primary = Gtk.Button(visible=False, css_classes=["omp-action"], valign=Gtk.Align.CENTER,
                                  focus_on_click=False)
        self.primary.connect("clicked", self._on_primary)
        self.actions.append(self.primary)
        self.append(self.actions)

        # The list's own widget around a row would take the focus from every click, on the row's buttons too. Not
        # any earlier than this: while a list builds it, it has no parent yet, and touching it from Python crashes.
        self.connect("realize", lambda row: row.get_parent().set_focus_on_click(False))
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", self._on_context)
        self.add_controller(click)
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", lambda _g, x, y: self._open_menu(x, y))
        self.add_controller(long_press)

    def _set_state_icon(self, key, shown):
        icon = self.state_icons.get(key)
        if icon is None and shown:
            icon = self.state_icons[key] = self._state_icon(key)
        if icon is not None:
            icon.set_visible(shown)

    def _state_icon(self, key):
        after = None
        for name, icon_name, tooltip, extra in STATE_ICONS:
            if name == key:
                icon = Gtk.Image(visible=False, icon_name=icon_name, pixel_size=12, css_classes=["omp-state-icon"],
                                 tooltip_text=_(tooltip))
                if extra:
                    icon.add_css_class(extra)
                self.icons.insert_child_after(icon, after)
                return icon
            after = self.state_icons.get(name, after)

    def _set_new_mark(self, shown):
        if self.new_mark is None and shown:
            self.new_mark = Gtk.Box(visible=False, css_classes=["omp-new-mark"], valign=Gtk.Align.CENTER,
                                    tooltip_text=_("New episode in the inbox"))
            self.new_mark.set_size_request(7, 7)
            self.top.prepend(self.new_mark)
        if self.new_mark is not None:
            self.new_mark.set_visible(shown)

    def _set_progress(self, fraction):
        if self.progress is None and fraction is not None:
            self.progress = Gtk.ProgressBar(css_classes=["omp-row-progress"], valign=Gtk.Align.CENTER)
            self.progress.set_margin_top(3)
            self.middle.append(self.progress)
        if self.progress is not None:
            self.progress.set_visible(fraction is not None)
            if fraction is not None:
                self.progress.set_fraction(fraction)

    def _set_download_status(self, status):
        if self.download_label is None and status is not None:
            self.download_label = label("", ("omp-download-label",), xalign=1.0)
            self.actions.prepend(self.download_label)
        if self.download_label is not None:
            if status is not None:
                self.download_label.set_text(status)
            self.download_label.set_visible(status is not None)

    # -- binding ---------------------------------------------------------------

    def bind(self, item):
        self.unbind()
        self.item = item
        self._item_handler = item.connect("changed", lambda _i: self.refresh())
        self.refresh()

    def unbind(self):
        if self.item is not None and self._item_handler:
            self.item.disconnect(self._item_handler)
        self.item = None
        self._item_handler = 0

    def refresh(self):
        if self.item is None:
            return
        e = self.item.episode
        ctx = self.ctx
        settings = ctx.settings
        self.cover.set_source(e.cover_url(settings.get("episode_cover")), e.feed_title)
        playing_id = ctx.player.current_id()
        is_current = playing_id == e.id
        is_playing = is_current and ctx.player.status in ("playing", "loading")
        self.cover.set_playing(is_current)
        self.title.set_text(e.title or _("Untitled episode"))
        self._set_new_mark(e.is_new)
        parts = []
        if self.show_podcast and e.feed_title:
            parts.append(e.feed_title)
        if e.pub_date:
            parts.append(fmt_date(e.pub_date))
        if e.duration:
            if e.is_in_progress or (settings.get("show_remaining_time") and not e.is_played):
                parts.append(_("{time} left").format(time=fmt_human_duration(e.remaining)))
            else:
                parts.append(fmt_human_duration(e.duration))
        if e.media_size and not e.is_local:
            parts.append(fmt_size(e.media_size))
        self.meta.set_text("  ·  ".join(parts))
        self._set_state_icon("downloaded", e.is_downloaded)
        self._set_state_icon("queued", e.in_queue)
        self._set_state_icon("favorite", e.is_favorite)
        self._set_state_icon("video", e.is_video)
        self._set_state_icon("played", e.is_played)
        if e.is_played:
            self.add_css_class("omp-played")
        else:
            self.remove_css_class("omp-played")
        if is_current:
            self.add_css_class("omp-now")
        else:
            self.remove_css_class("omp-now")
        self._set_progress(min(1.0, e.position / e.duration) if e.is_in_progress and e.duration > 0 else None)
        self._update_actions(e, is_playing)

    def _update_actions(self, e, is_playing):
        # What changes is worked out first: a button hidden and shown again would make GTK lay the row out anew.
        progress = self.ctx.downloads.progress(e.id) if self.ctx.downloads and e.has_media else None
        status = secondary = None
        if progress is not None:
            received, total, _state = progress
            status = f"{min(99, int(received * 100 / total))}%" if total else fmt_size(received) or _("Queued")
            secondary = "process-stop-symbolic", _("Cancel download"), "cancel"
        elif e.has_media and not e.is_downloaded and not e.is_local:
            secondary = "folder-download-symbolic", _("Download"), "download"
        self._set_download_status(status)
        if secondary is not None:
            icon, tooltip, self._secondary_action = secondary
            self.secondary.set_icon_name(icon)
            self.secondary.set_tooltip_text(tooltip)
        self.secondary.set_visible(secondary is not None)

        if not e.has_media:
            icon, tooltip, self._primary_action = "web-browser-symbolic", _("Visit website"), "website"
        elif is_playing:
            icon, tooltip, self._primary_action = "media-playback-pause-symbolic", _("Pause"), "pause"
        else:
            icon, tooltip, self._primary_action = ("media-playback-start-symbolic",
                                                   _("Play") if e.is_downloaded else _("Stream"), "play")
        if is_playing and e.has_media:
            self.primary.add_css_class("omp-action-play")
        else:
            self.primary.remove_css_class("omp-action-play")
        self.primary.set_icon_name(icon)
        self.primary.set_tooltip_text(tooltip)
        self.primary.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        self.primary.set_visible(e.has_media or bool(e.link))

    def _on_primary(self, _button):
        if self.item is None:
            return
        e = self.item.episode
        if self._primary_action == "pause":
            self.ctx.player.pause()
        elif self._primary_action == "play":
            self.ctx.play_episode(e.id)
        elif self._primary_action == "website" and e.link:
            self.ctx.open_uri(e.link)

    def _on_secondary(self, _button):
        if self.item is None:
            return
        e = self.item.episode
        if self._secondary_action == "download":
            self.ctx.library.download([e.id])
        elif self._secondary_action == "cancel":
            self.ctx.downloads.cancel(e.id)

    def _on_context(self, gesture, _n, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._open_menu(x, y)

    def _open_menu(self, x, y):
        if self.item is None:
            return
        menu = self.ctx.episode_menu(self.item.episode)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()


# A ListView builds a row for about this many items around what is on screen, however long the list is.
ROWS_BUILT = 240


class EpisodeList:
    """A ListView of EpisodeItems with live updates for downloads and playback."""

    def __init__(self, ctx, show_podcast=True, reorderable=False, on_activate=None, on_reorder=None):
        self.ctx = ctx
        self.show_podcast = show_podcast
        self.reorderable = reorderable
        self.on_activate = on_activate
        self.on_reorder = on_reorder
        self.store = Gio.ListStore(item_type=EpisodeItem)
        self.selection = Gtk.MultiSelection(model=self.store)
        self.single = Gtk.NoSelection(model=self.store)
        self.selecting = False
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        self.view = Gtk.ListView(model=self.single, factory=factory, single_click_activate=True)
        self.view.add_css_class("omp-episode-list")
        self.view.connect("activate", self._on_activate)
        self.view.connect("map", self._on_map)
        self._rows = {}
        self._rows_outdated = False
        self._look = None  # see _current_look
        self._subs = [
            bus.subscribe("download-progress", self._on_download_progress),
            bus.subscribe("player-state", self._on_player_state),
            bus.subscribe("downloads-changed", self._on_downloads_changed),
        ]
        self._last_player = None

    def destroy(self):
        for token in self._subs:
            bus.unsubscribe(token)

    def _setup(self, _factory, list_item):
        row = EpisodeRow(self.ctx, self.show_podcast, show_handle=self.reorderable)
        list_item.set_child(row)
        if self.reorderable:
            self._enable_dnd(row, list_item)

    def _bind(self, _factory, list_item):
        row = list_item.get_child()
        item = list_item.get_item()
        row.bind(item)
        self._rows[item.id] = row

    def _unbind(self, _factory, list_item):
        row = list_item.get_child()
        item = list_item.get_item()
        if item is not None and self._rows.get(item.id) is row:
            del self._rows[item.id]
        row.unbind()

    def _on_activate(self, _view, position):
        if self.selecting:
            return
        item = self.store.get_item(position)
        if item is not None and self.on_activate:
            self.on_activate(item.episode)

    # -- live updates ------------------------------------------------------------

    def _on_download_progress(self, episode_id, _received, _total):
        row = self._rows.get(episode_id)
        if row is not None:
            row.refresh()

    def _on_downloads_changed(self):
        if self.view.get_mapped():
            self._refresh_rows()
        else:
            self._rows_outdated = True

    def _on_map(self, _view):
        if self._rows_outdated:
            self._refresh_rows()

    def _refresh_rows(self):
        self._rows_outdated = False
        for row in list(self._rows.values()):
            row.refresh()

    def _on_player_state(self, state):
        key = (state.get("episode") or {}).get("id"), state.get("status")
        if key == self._last_player:
            return
        previous = self._last_player
        self._last_player = key
        for episode_id in {key[0], previous[0] if previous else None}:
            row = self._rows.get(episode_id)
            if row is not None:
                row.refresh()

    # -- data ------------------------------------------------------------------

    def _current_look(self):
        """What the rows show besides their episode: settings, and the day ('Today', 'Yesterday')."""
        return self.ctx.settings.revision, date.today()

    def set_episodes(self, episodes):
        """Replace the content, keeping items (and scroll) when only the data changed."""
        look, previous_look = self._current_look(), self._look
        self._look = look
        items = [self.store.get_item(i) for i in range(self.store.get_n_items())]
        if [item.id for item in items] == [e.id for e in episodes]:
            for item, episode in zip(items, episodes):
                if look != previous_look or item.episode != episode:
                    item.episode = episode
                    item.emit("changed")
            return
        self.store.splice(0, len(items), [EpisodeItem(e) for e in episodes])

    def grow(self, episodes, batch=20):
        """Fills an empty list in steps for the idle loop: each row is built as its item arrives."""
        self._look = self._current_look()
        items = [EpisodeItem(e) for e in episodes]
        start = 0
        while start < len(items) and self.store.get_n_items() == start:
            end = start + batch if start < ROWS_BUILT else len(items)
            self.store.splice(start, 0, items[start:end])
            start = end
            yield

    def update_episodes(self, episodes):
        by_id = {e.id: e for e in episodes}
        for index in range(self.store.get_n_items()):
            item = self.store.get_item(index)
            if item.id in by_id:
                item.episode = by_id[item.id]
                item.emit("changed")

    def episodes(self):
        return [self.store.get_item(i).episode for i in range(self.store.get_n_items())]

    def set_selecting(self, selecting):
        self.selecting = selecting
        if selecting:
            self.selection.unselect_all()
            self.view.set_model(self.selection)
            self.view.set_single_click_activate(False)
        else:
            self.view.set_model(self.single)
            self.view.set_single_click_activate(True)

    def selected_ids(self):
        ids = []
        bitset = self.selection.get_selection()
        for index in range(self.store.get_n_items()):
            if bitset.contains(index):
                ids.append(self.store.get_item(index).id)
        return ids

    # -- drag and drop (queue) ---------------------------------------------------

    def _enable_dnd(self, row, list_item):
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)

        def prepare(_source, _x, _y):
            item = list_item.get_item()
            if item is None or self.ctx.settings.get("queue_locked"):
                return None
            return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_INT64, item.id))

        def begin(source, _drag):
            paintable = Gtk.WidgetPaintable(widget=row)
            source.set_icon(paintable, 20, 20)

        source.connect("prepare", prepare)
        source.connect("drag-begin", begin)
        (row.handle or row).add_controller(source)

        target = Gtk.DropTarget.new(GObject.TYPE_INT64, Gdk.DragAction.MOVE)

        def drop(_target, value, _x, y):
            item = list_item.get_item()
            if item is None or self.on_reorder is None:
                return False
            ids = [self.store.get_item(i).id for i in range(self.store.get_n_items())]
            if value not in ids:
                return False
            index = ids.index(item.id)
            if y > row.get_height() / 2:
                index += 1
            if ids.index(value) < index:
                index -= 1
            self.on_reorder(int(value), index)
            return True

        target.connect("drop", drop)
        row.add_controller(target)
