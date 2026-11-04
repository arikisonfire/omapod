"""Shared page scaffolding: header with display title, actions and the
episode list page that most screens are built from."""

from gi.repository import Adw, Gio, GLib, Gtk

from ...events import bus
from ...i18n import _, ngettext
from ...util import fmt_human_duration
from ..widgets.common import Debouncer, EmptyState, icon_button, label
from ..widgets.episode_row import EpisodeList


STYLED_AFTER_MS = 100  # GTK styles what is new in its next frame


class BasePage(Adw.NavigationPage):
    def __init__(self, ctx, title, tag):
        super().__init__(title=title, tag=tag)
        self.ctx = ctx
        self._subs = []
        self._outdated = True
        self._seen = None
        self.view = Adw.ToolbarView()
        self.header = Adw.HeaderBar(show_title=False)
        self.header.add_css_class("omp-page-headerbar")
        self.sidebar_button = Gtk.Button(icon_name="sidebar-show-symbolic", css_classes=["flat"],
                                         tooltip_text=_("Show navigation"))
        self.sidebar_button.connect("clicked", lambda _b: ctx.window.toggle_sidebar())
        self.sidebar_button.set_visible(ctx.window.sidebar_collapsed())
        self.header.pack_start(self.sidebar_button)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, valign=Gtk.Align.CENTER)
        titles.set_margin_start(6)
        self.title_label = label(title, ("omp-display",))
        titles.append(self.title_label)
        self.subtitle = label("", ("omp-page-sub",))
        self.subtitle.set_visible(False)
        titles.append(self.subtitle)
        self.header.pack_start(titles)
        self.view.add_top_bar(self.header)
        self.set_child(self.view)
        self.listen("sidebar-collapsed", lambda collapsed: self.sidebar_button.set_visible(collapsed))

    def listen(self, topic, callback):
        self._subs.append(bus.subscribe(topic, callback))

    def watch(self, delay_ms, *topics):
        """Keeps the page up to date. On screen it follows the topics, `delay_ms` after the last change. Out of
        sight it costs nothing until it opens again, and opens without loading anything if nothing changed."""
        self._reload = Debouncer(delay_ms, self._catch_up)
        for topic in topics:
            self.listen(topic, self._on_change)
        self.connect("showing", self._on_showing)

    def _on_change(self, *_args):
        self._outdated = True
        if self.get_mapped():
            self._reload()

    def _on_showing(self, _page):
        if self._seen is None:
            self.update()
        elif self._behind():
            # A page opens with what it has; what is new follows right after. While something plays that is every
            # time, and rarely more than the progress of one episode.
            self.ctx.window.after_frame(self._catch_up)

    def _behind(self):
        return self._outdated or self._seen != self.ctx.revision()

    def _catch_up(self):
        if self.get_mapped() and self._behind():
            self.update()

    def update(self):
        self._outdated = False
        self._seen = self.ctx.revision()
        self.refresh()

    def keep_in_tree(self):
        """For a page that stays in the widget tree: its content is hidden while it is not on screen, because every
        change of the keyboard focus makes GTK check the style of each visible widget in the window. The content,
        not the page: showing or hiding a widget also touches the styles of its siblings, here the other pages."""
        self.view.set_visible(False)
        self.connect("showing", lambda _p: self.view.set_visible(True))
        self.connect("hidden", lambda _p: self.view.set_visible(False))

    def prepare(self):
        """Gets the page ready before it first opens, in steps for the idle loop: its data, and GTK's work on its
        widgets, which needs them visible. The first time, that takes far longer than opening the page."""
        self.view.set_visible(True)
        for _step in self.fill():
            self._lay_out()
            yield
        self.update()
        self._lay_out()
        yield
        GLib.timeout_add(STYLED_AFTER_MS, self._hide_unless_shown)

    def _hide_unless_shown(self):
        self.view.set_visible(self.get_mapped())
        return False

    def fill(self):
        """Steps that put the page's data into widgets that take long to build."""
        return ()

    def _lay_out(self):
        nav = self.ctx.window.nav
        if nav.get_width() > 0 and not self.get_mapped():
            width = max(nav.get_width(), self.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
            height = max(nav.get_height(), self.measure(Gtk.Orientation.VERTICAL, width)[0])
            self.allocate(width, height, -1, None)

    def destroy_page(self):
        """The page left the navigation for good: stop listening."""
        for token in self._subs:
            bus.unsubscribe(token)
        self._subs = []

    def initial_focus(self):
        """The widget that takes the focus when the page opens; None keeps GTK's order."""
        return None

    def do_focus(self, direction):
        # Opening a page focuses its main control. GTK would take the first focusable widget,
        # often the selectable title, and select all of its text.
        if direction == Gtk.DirectionType.TAB_FORWARD and self.get_focus_child() is None:
            target = self.initial_focus()
            if target is not None and target.grab_focus():
                return True
        return Adw.NavigationPage.do_focus(self, direction)

    def set_title_text(self, text):
        self.title_label.set_text(text)
        self.set_title(text)

    def set_subtitle(self, text):
        self.subtitle.set_text(text or "")
        self.subtitle.set_visible(bool(text))

    def add_action_button(self, icon, tooltip, callback):
        button = icon_button(icon, tooltip, callback)
        self.header.pack_end(button)
        return button

    def add_menu(self, menu, icon="view-more-symbolic", tooltip=None):
        button = Gtk.MenuButton(icon_name=icon, menu_model=menu, css_classes=["flat"],
                                tooltip_text=tooltip or _("More"))
        self.header.pack_end(button)
        return button

    def set_content(self, widget):
        self.view.set_content(widget)

    def refresh(self):
        pass


class EpisodeListPage(BasePage):
    """A page with filter chips, an episode list, an empty state and selection mode."""

    def __init__(self, ctx, title, tag, show_podcast=True, reorderable=False, empty=None, filters=None,
                 sort_key=None, default_sort="date_desc"):
        super().__init__(ctx, title, tag)
        self.sort_key = sort_key
        self.default_sort = default_sort
        self.sort = (ctx.settings.get(sort_key) if sort_key else None) or default_sort
        self.filter_options = filters or []
        self.active_filter = self.filter_options[0][0] if self.filter_options else None
        self.list = EpisodeList(ctx, show_podcast=show_podcast, reorderable=reorderable,
                                on_activate=lambda e: ctx.show_episode(e.id), on_reorder=self.on_reorder)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.append(self.top)
        if self.filter_options:
            chips = Gtk.Box(spacing=6, css_classes=["omp-toolbar"])
            group = None
            for key, text in self.filter_options:
                chip = Gtk.ToggleButton(label=text, css_classes=["omp-chip"], focus_on_click=False)
                if group is None:
                    group = chip
                else:
                    chip.set_group(group)
                chip.set_active(key == self.active_filter)
                chip.connect("toggled", self._on_chip, key)
                chips.append(chip)
            scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                          hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(chips)
            self.box.append(scroller)
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=150)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(self.list.view)
        self.scrolled = scrolled
        self.stack.add_named(scrolled, "list")
        icon, empty_title, empty_text = empty or ("folder-symbolic", _("Nothing here"), "")
        self.empty = EmptyState(icon, empty_title, empty_text)
        self.stack.add_named(self.empty, "empty")
        self.box.append(self.stack)
        self.actionbar = self._build_actionbar()
        self.box.append(self.actionbar)
        self.set_content(self.box)
        self.watch(150, "episodes-changed", "queue-changed", "feeds-changed")

    def fill(self):
        return self.list.grow(self.query())

    def destroy_page(self):
        super().destroy_page()
        self._reload.cancel()
        self.list.destroy()

    # -- data -------------------------------------------------------------------

    def query(self):
        return []

    def refresh(self):
        episodes = self.query()
        self.list.set_episodes(episodes)
        self.stack.set_visible_child_name("list" if episodes else "empty")
        self.update_subtitle(episodes)

    def update_subtitle(self, episodes):
        if not episodes:
            self.set_subtitle("")
            return
        total = sum(e.remaining if e.duration else 0 for e in episodes)
        text = ngettext("{n} episode", "{n} episodes", len(episodes)).format(n=len(episodes))
        if total:
            text += "  ·  " + _("{time} left").format(time=fmt_human_duration(total))
        self.set_subtitle(text)

    def on_reorder(self, episode_id, index):
        pass

    def _on_chip(self, chip, key):
        if chip.get_active():
            self.active_filter = key
            self.refresh()

    def set_sort(self, sort):
        self.sort = sort
        if self.sort_key:
            self.ctx.settings.set(self.sort_key, sort)
        self.refresh()

    def sort_menu(self, options):
        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new_stateful("sort", GLib.VariantType.new("s"), GLib.Variant("s", self.sort))
        action.connect("activate", lambda a, v: (a.set_state(v), self.set_sort(v.get_string())))
        group.add_action(action)
        self.insert_action_group("page", group)
        for key, text in options:
            menu.append(text, f"page.sort::{key}")
        return menu

    # -- selection mode -----------------------------------------------------------

    def _build_actionbar(self):
        bar = Gtk.Box(spacing=6, css_classes=["omp-actionbar"])
        bar.set_visible(False)
        self.selected_label = label("", ("omp-numeric",))
        bar.append(self.selected_label)
        bar.append(Gtk.Box(hexpand=True))
        for icon, tooltip, handler in (
            ("view-list-ordered-symbolic", _("Add to queue"), lambda ids: self.ctx.library.add_to_queue(ids)),
            ("list-remove-symbolic", _("Remove from queue"), lambda ids: self.ctx.library.remove_from_queue(ids)),
            ("folder-download-symbolic", _("Download"), lambda ids: self.ctx.library.download(ids)),
            ("user-trash-symbolic", _("Delete download"), lambda ids: self.ctx.delete_downloads(ids)),
            ("object-select-symbolic", _("Mark as played"), lambda ids: self.ctx.library.mark_played(ids)),
            ("edit-undo-symbolic", _("Mark as unplayed"), lambda ids: self.ctx.library.mark_played(ids, False)),
            ("mail-read-symbolic", _("Remove from inbox"), lambda ids: self.ctx.library.remove_from_inbox(ids)),
            ("starred-symbolic", _("Add to favorites"), lambda ids: self.ctx.library.set_favorite(ids, True)),
        ):
            bar.append(icon_button(icon, tooltip, self._bulk, ("flat", "omp-icon"), handler))
        select_all = Gtk.Button(label=_("Select all"), css_classes=["flat"])
        select_all.connect("clicked", lambda _b: self.list.selection.select_all())
        bar.append(select_all)
        done = Gtk.Button(label=_("Done"), css_classes=["omp-primary"])
        done.connect("clicked", lambda _b: self.set_selecting(False))
        bar.append(done)
        self.list.selection.connect("selection-changed", self._on_selection_changed)
        return bar

    def _on_selection_changed(self, *_args):
        count = len(self.list.selected_ids())
        self.selected_label.set_text(ngettext("{n} selected", "{n} selected", count).format(n=count))

    def _bulk(self, handler):
        ids = self.list.selected_ids()
        if ids:
            handler(ids)
        self.set_selecting(False)

    def set_selecting(self, selecting):
        self.list.set_selecting(selecting)
        self.actionbar.set_visible(selecting)
        self._on_selection_changed()
