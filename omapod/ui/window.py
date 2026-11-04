"""Main window: navigation rail, page stack, player dock, toasts."""

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..events import bus
from ..i18n import _
from ..models import FEED_SUBSCRIBED
from .widgets.common import Cover, Debouncer, label
from .widgets.dock import PlayerDock

NAV = [
    ("home", "go-home-symbolic", "Home"),
    ("queue", "view-list-ordered-symbolic", "Queue"),
    ("inbox", "omapod-inbox-symbolic", "Inbox"),
    ("episodes", "audio-x-generic-symbolic", "Episodes"),
    ("favorites", "starred-symbolic", "Favorites"),
    ("subscriptions", "view-grid-symbolic", "Subscriptions"),
    ("downloads", "folder-download-symbolic", "Downloads"),
    ("history", "document-open-recent-symbolic", "History"),
    ("statistics", "omapod-statistics-symbolic", "Statistics"),
]


class NavRow(Gtk.ListBoxRow):
    def __init__(self, key, icon, text):
        # focus_on_click, here and in FeedRow: every change of the keyboard focus makes GTK check the style of each
        # visible widget in the window, and a click on a page's name should show that page, nothing else.
        super().__init__(focus_on_click=False)
        self.key = key
        box = Gtk.Box(spacing=10, css_classes=["omp-nav-row"])
        box.append(Gtk.Image(icon_name=icon, css_classes=["omp-nav-icon"]))
        self.text = label(_(text), ("omp-nav-label",))
        self.text.set_hexpand(True)
        box.append(self.text)
        self.count = label("", ("omp-count",), xalign=1.0, ellipsize=False)
        box.append(self.count)
        self.set_child(box)

    def set_count(self, value, highlight=False):
        self.count.set_text(str(value) if value else "")
        if highlight and value:
            self.count.add_css_class("omp-count-new")
        else:
            self.count.remove_css_class("omp-count-new")


class FeedRow(Gtk.ListBoxRow):
    def __init__(self, ctx, feed):
        super().__init__(focus_on_click=False)
        self.feed_id = feed.id
        box = Gtk.Box(spacing=10, css_classes=["omp-nav-row"])
        self.cover = Cover(ctx.textures, 24)
        self.cover.set_valign(Gtk.Align.CENTER)
        box.append(self.cover)
        self.text = label("", ("omp-nav-label",))
        self.text.set_hexpand(True)
        box.append(self.text)
        self.count = label("", ("omp-count",), xalign=1.0, ellipsize=False)
        box.append(self.count)
        self.set_child(box)
        self.update(ctx, feed)

    def update(self, ctx, feed):
        self.cover.set_source(feed.image_url, feed.display_title)
        self.text.set_text(feed.display_title)
        self.set_tooltip_text(feed.display_title)
        counter = ctx.settings.get("subscriptions_counter")
        value = {"inbox": feed.count_new, "unplayed": feed.count_unplayed, "downloaded": feed.count_downloaded,
                 "downloaded_unplayed": feed.count_downloaded_unplayed}.get(counter, 0)
        self.count.set_text(str(value) if value else "")
        if feed.refresh_error:
            self.text.add_css_class("omp-dim")


def sort_feeds(feeds, order):
    if order == "counter":
        return sorted(feeds, key=lambda f: (-f.count_new, -f.count_unplayed, f.display_title.casefold()))
    if order == "date":
        return sorted(feeds, key=lambda f: -(f.last_pub or 0))
    if order == "most_played":
        return sorted(feeds, key=lambda f: (-f.count_played, f.display_title.casefold()))
    return sorted(feeds, key=lambda f: f.display_title.casefold())


class Rail(Gtk.Box):
    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.add_css_class("omp-rail")
        self.set_size_request(232, -1)
        wordmark = Gtk.Box(spacing=0, css_classes=["omp-wordmark"])
        wordmark.append(Gtk.Label(label="oma"))
        wordmark.append(Gtk.Label(label="POD", css_classes=["omp-wordmark-accent"]))
        self.append(wordmark)

        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.rows = {}
        for key, icon, text in NAV:
            row = NavRow(key, icon, text)
            self.rows[key] = row
            self.nav.append(row)
        self.nav.connect("row-activated", lambda _l, row: ctx.show_page(row.key))
        content.append(self.nav)
        self.feeds_label = label(_("Podcasts"), ("omp-section-label",))
        content.append(self.feeds_label)
        self.feeds = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.feeds.connect("row-activated", lambda _l, row: ctx.show_feed(row.feed_id))
        content.append(self.feeds)
        scrolled.set_child(content)
        self.append(scrolled)

        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        foot.set_margin_start(6)
        foot.set_margin_end(6)
        foot.set_margin_bottom(8)
        foot.set_margin_top(6)
        self.status = label("", ("caption", "omp-dim"))
        self.status.set_margin_start(10)
        self.status.set_margin_bottom(4)
        self.status.set_visible(False)
        foot.append(self.status)
        foot.append(self._foot_button("list-add-symbolic", _("Add podcast"), lambda: ctx.show_page("add")))
        foot.append(self._foot_button("emblem-system-symbolic", _("Settings"), lambda: ctx.show_preferences()))
        self.append(foot)

        self._feed_rows = {}
        self._reload = Debouncer(200, self.refresh)
        bus.subscribe("feeds-changed", lambda _ids=None: self._reload())
        bus.subscribe("episodes-changed", lambda _ids=None: self._reload())
        bus.subscribe("queue-changed", lambda: self._reload())
        bus.subscribe("downloads-changed", lambda: self._reload())
        bus.subscribe("refresh-state", self._on_refresh_state)
        bus.subscribe("sync-state", self._on_sync_state)
        ctx.settings.connect(lambda key, _v: self._reload() if key.startswith("subscriptions_") else None)
        self.refresh()

    def _foot_button(self, icon, text, callback):
        button = Gtk.Button(css_classes=["flat"])
        box = Gtk.Box(spacing=10)
        box.append(Gtk.Image(icon_name=icon, css_classes=["omp-nav-icon"]))
        box.append(label(text))
        button.set_child(box)
        button.connect("clicked", lambda _b: callback())
        return button

    def select(self, key):
        row = self.rows.get(key)
        if row is not None:
            self.nav.select_row(row)
            self.feeds.unselect_all()
        else:
            self.nav.unselect_all()

    def select_feed(self, feed_id):
        self.nav.unselect_all()
        row = self._feed_rows.get(feed_id)
        if row is not None:
            self.feeds.select_row(row)
        else:
            self.feeds.unselect_all()

    def refresh(self):
        db = self.ctx.db
        self.rows["queue"].set_count(len(db.queue_ids()))
        self.rows["inbox"].set_count(db.count_episodes(("new", "subscribed")), highlight=True)
        self.rows["downloads"].set_count(db.count_episodes(("downloaded",)))
        feeds = sort_feeds(db.get_feeds(states=(FEED_SUBSCRIBED,)), self.ctx.settings.get("subscriptions_sort"))
        self.feeds_label.set_visible(bool(feeds) and self.ctx.settings.get("show_sidebar_feeds"))
        self.feeds.set_visible(self.ctx.settings.get("show_sidebar_feeds"))
        wanted = [f.id for f in feeds]
        existing = list(self._feed_rows)
        if wanted != existing:
            selected = self.feeds.get_selected_row()
            selected_id = selected.feed_id if selected is not None else None
            child = self.feeds.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                self.feeds.remove(child)
                child = following
            self._feed_rows = {}
            for feed in feeds:
                row = FeedRow(self.ctx, feed)
                self._feed_rows[feed.id] = row
                self.feeds.append(row)
            if selected_id in self._feed_rows:
                self.feeds.select_row(self._feed_rows[selected_id])
        else:
            for feed in feeds:
                self._feed_rows[feed.id].update(self.ctx, feed)

    def _on_refresh_state(self, running, done, total):
        if running and total:
            self.status.set_text(_("Refreshing podcasts… {done}/{total}").format(done=done, total=total))
            self.status.set_visible(True)
        else:
            self.status.set_visible(False)

    def _on_sync_state(self, running, text):
        if running:
            self.status.set_text(text)
            self.status.set_visible(True)
        elif not self.ctx.library.refreshing:
            self.status.set_visible(False)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="omaPOD")
        self.ctx = app
        settings = app.settings
        self.set_default_size(int(settings.get("window_width")), int(settings.get("window_height")))
        if settings.get("window_maximized"):
            self.maximize()
        self.set_size_request(360, 420)
        self.toasts = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split = Adw.OverlaySplitView(min_sidebar_width=220, max_sidebar_width=272,
                                          sidebar_width_fraction=0.24, vexpand=True)
        self.rail = Rail(app)
        self.split.set_sidebar(self.rail)
        # The pages of the rail stay built, in a stack: unlike a navigation view, which moves a page to the end of its
        # children each time, a stack changes nothing about the styles when it switches. What opens from them (a
        # podcast, an episode) is pushed on top of the page that holds the stack.
        self.roots = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.root_page = Adw.NavigationPage(child=self.roots, title="omaPOD", tag="root")
        self.root_shown = None
        for signal in ("showing", "shown", "hidden"):
            self.root_page.connect(signal, self._forward_to_root, signal)
        self.nav = Adw.NavigationView()
        self.nav.replace([self.root_page])
        self.nav.connect("popped", self._on_popped)
        self.split.set_content(self.nav)
        root.append(self.split)
        self.dock = PlayerDock(app)
        # Now Playing has the big controls: the dock steps aside while they are on screen.
        self.dock_revealer = Gtk.Revealer(child=self.dock, reveal_child=True,
                                          transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        root.append(self.dock_revealer)
        self.nav.connect("notify::visible-page", lambda *_a: self.update_dock())
        self.toasts.set_child(root)
        self.set_content(self.toasts)
        # Tiled side by side on a small screen: the dock keeps cover, transport and progress, at the very end not even
        # the speed (Now Playing has it too). Only the narrowest step counts, so each one names all it hides.
        dock = self.dock
        for width, hidden in ((760, (dock.volume, dock.queue_button)),
                              (540, (dock.volume, dock.queue_button, dock.text_box, dock.sleep, dock.skip)),
                              (400, (dock.extras, dock.text_box, dock.skip))):
            step = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {width}sp"))
            step.add_setter(self.split, "collapsed", True)
            for widget in hidden:
                step.add_setter(widget, "visible", False)
            self.add_breakpoint(step)
        self.split.connect("notify::collapsed", lambda *_a: bus.emit("sidebar-collapsed", self.split.get_collapsed()))
        self.pages = {}
        self.current = None
        self.connect("close-request", self._on_close)
        self._install_actions()
        self._install_shortcuts()

    # -- navigation ---------------------------------------------------------------

    def sidebar_collapsed(self):
        return self.split.get_collapsed()

    def toggle_sidebar(self):
        self.split.set_show_sidebar(not self.split.get_show_sidebar())

    def add_root(self, page):
        self.roots.add_child(page)

    def show_root(self, key, page):
        self.current = key
        self.rail.select(key)
        # The focus leaves the pages and stays in the rail: inside a page it would follow every page that opens, and
        # each move makes GTK check the style of every visible widget.
        focus, row = self.get_focus(), self.rail.rows.get(key)
        if focus is not None and row is not None and focus.is_ancestor(self.nav):
            row.grab_focus()
        previous, self.root_shown = self.root_shown, page
        if page is not previous:
            page.emit("showing")
            self.roots.set_visible_child(page)
            if previous is not None:
                previous.emit("hidden")
            page.emit("shown")
        leaving = self._stack()[1:]
        if leaving:
            self.nav.replace([self.root_page])
            for old in leaving:
                self._destroy(old)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _forward_to_root(self, _page, signal):
        # The navigation view knows only the page around the stack.
        if self.root_shown is not None:
            self.root_shown.emit(signal)

    def after_frame(self, callback):
        """Calls back when the window has drawn its next picture, and input had its turn."""
        clock = self.get_frame_clock()
        if clock is None:
            callback()
            return

        def painted(_clock):
            clock.disconnect(handler)
            GLib.idle_add(lambda: (callback(), False)[1])

        handler = clock.connect("after-paint", painted)

    def push(self, page):
        visible = self.nav.get_visible_page()
        if visible is not None and visible.get_tag() == page.get_tag() and page.get_tag():
            return
        self.nav.push(page)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def remove_page(self, page):
        """Take a pushed page out of the navigation, wherever it is in the stack."""
        pages = self._stack()
        if page not in pages[1:]:
            return
        if pages[-1] is page:
            self.nav.pop()
        else:
            self.nav.replace([p for p in pages if p is not page])
            self._destroy(page)

    def _stack(self):
        stack = self.nav.get_navigation_stack()
        return [stack.get_item(i) for i in range(stack.get_n_items())]

    def _destroy(self, page):
        # Pushed pages are built anew every time.
        if hasattr(page, "destroy_page"):
            page.destroy_page()

    def _on_popped(self, _nav, page):
        self._destroy(page)
        if self.nav.get_visible_page() is self.root_page:
            self.rail.select(self.current)

    def update_dock(self):
        page = self.nav.get_visible_page()
        covered = hasattr(page, "shows_controls") and page.shows_controls(self.dock_revealer.get_height())
        self.dock_revealer.set_reveal_child(not covered)

    # -- actions --------------------------------------------------------------------

    def _install_actions(self):
        ctx = self.ctx
        lib = ctx.library

        def ep_action(name, handler):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("x"))
            action.connect("activate", lambda _a, value: handler(value.get_int64()))
            self.add_action(action)

        def episode(eid):
            return ctx.db.get_episode(eid, full=False)

        ep_action("ep-play", ctx.play_episode)
        ep_action("ep-details", ctx.show_episode)
        ep_action("ep-feed", lambda eid: ctx.show_feed(episode(eid).feed_id) if episode(eid) else None)
        ep_action("ep-download", lambda eid: lib.download([eid]))
        ep_action("ep-cancel", lambda eid: ctx.downloads.cancel(eid))
        ep_action("ep-delete", lambda eid: ctx.delete_downloads([eid]))
        ep_action("ep-queue", lambda eid: (lib.add_to_queue([eid]), ctx.toast(_("Added to the queue"))))
        ep_action("ep-queue-front", lambda eid: lib.add_to_queue([eid], location="front"))
        ep_action("ep-unqueue", lambda eid: lib.remove_from_queue([eid]))
        ep_action("ep-top", lambda eid: lib.move_to_top([eid]))
        ep_action("ep-bottom", lambda eid: lib.move_to_bottom([eid]))
        ep_action("ep-played", lambda eid: lib.mark_played([eid]))
        ep_action("ep-unplayed", lambda eid: lib.mark_played([eid], played=False))
        ep_action("ep-inbox-remove", lambda eid: lib.remove_from_inbox([eid]))
        ep_action("ep-favorite", lambda eid: lib.set_favorite([eid], True))
        ep_action("ep-unfavorite", lambda eid: lib.set_favorite([eid], False))
        ep_action("ep-reset", lambda eid: lib.reset_position([eid]))
        ep_action("ep-history-remove", lambda eid: lib.remove_from_history([eid]))
        ep_action("ep-website", lambda eid: ctx.open_uri(episode(eid).link) if episode(eid) and episode(eid).link
                  else None)
        ep_action("ep-comments", lambda eid: ctx.open_uri(episode(eid).social_url) if episode(eid) else None)
        ep_action("ep-copy-link", lambda eid: ctx.copy_text(episode(eid).link or episode(eid).media_url))
        ep_action("ep-copy-media", lambda eid: ctx.copy_text(episode(eid).media_url))
        ep_action("ep-skip", lambda eid: ctx.player.skip() if ctx.player.current_id() == eid else None)

    def _install_shortcuts(self):
        app = self.ctx
        controller = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        self.add_controller(controller)
        # GTK uses the player keys itself (Ctrl+arrows move the focus or a cursor, list rows take
        # Ctrl+Space), so they also run before the focused widget sees them.
        player_keys = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        self.add_controller(player_keys)

        def add(trigger, callback):
            shortcut = Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                                    action=Gtk.CallbackAction.new(lambda *_a: (callback(), True)[1]))
            controller.add_shortcut(shortcut)

        def player(trigger, callback):
            # A text field goes first (Ctrl+← jumps back a word, input methods may use Ctrl+Space);
            # a key it does not use still reaches the window afterwards.
            add(trigger, callback)

            def first(*_args):
                if self._typing():
                    return False
                callback()
                return True

            player_keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                                                  action=Gtk.CallbackAction.new(first)))

        player("<Control>space", lambda: app.player.toggle())
        player("<Control>Left", lambda: app.player.rewind())
        player("<Control>Right", lambda: app.player.fast_forward())
        player("<Control><Shift>Right", lambda: app.player.skip())
        player("<Control>Up", lambda: app.player.set_speed(app.player.speed + 0.1))
        player("<Control>Down", lambda: app.player.set_speed(app.player.speed - 0.1))
        add("<Control>r", lambda: app.refresh_all())
        add("<Control>f", lambda: app.show_page("search"))
        add("<Control>n", lambda: app.show_page("add"))
        add("<Control>comma", lambda: app.show_preferences())
        add("<Control>p", lambda: app.show_now_playing())
        add("<Alt>1", lambda: app.show_page("home"))
        add("<Alt>2", lambda: app.show_page("queue"))
        add("<Alt>3", lambda: app.show_page("inbox"))
        add("<Alt>4", lambda: app.show_page("episodes"))
        add("<Alt>5", lambda: app.show_page("subscriptions"))
        add("<Alt>6", lambda: app.show_page("downloads"))
        add("F9", lambda: self.toggle_sidebar())
        # Plain space toggles playback unless a text field or button has the focus.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _typing(self):
        return isinstance(self.get_focus(), (Gtk.Text, Gtk.Entry, Gtk.SearchEntry, Gtk.TextView, Gtk.SpinButton))

    def _on_key(self, _controller, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if self._typing():
            return False
        if keyval == Gdk.KEY_space:
            self.ctx.player.toggle()
            return True
        if keyval in (Gdk.KEY_j,):
            self.ctx.player.rewind()
            return True
        if keyval in (Gdk.KEY_l,):
            self.ctx.player.fast_forward()
            return True
        if keyval == Gdk.KEY_Escape and self.nav.get_navigation_stack().get_n_items() > 1:
            self.nav.pop()
            return True
        return False

    # -- closing --------------------------------------------------------------------

    def _on_close(self, _window):
        settings = self.ctx.settings
        if not self.is_maximized():
            width, height = self.get_default_size()
            settings.update({"window_width": width, "window_height": height, "window_maximized": False})
        else:
            settings.set("window_maximized", True)
        return self.ctx.on_window_close()
