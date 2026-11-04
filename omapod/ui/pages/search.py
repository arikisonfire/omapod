"""Search in your own podcasts and episodes, with a jump to the online search."""

from gi.repository import GLib, Gtk

from ...i18n import _, ngettext
from ..widgets.common import Cover, Debouncer, EmptyState, label
from ..widgets.episode_row import EpisodeList
from .base import BasePage


class SearchPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Search"), "search")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, css_classes=["omp-toolbar"])
        self.entry = Gtk.SearchEntry(placeholder_text=_("Search your podcasts and episodes"), hexpand=True)
        self.entry.connect("search-changed", lambda _e: self._debounce())
        bar.append(self.entry)
        online = Gtk.Button(label=_("Search online"), css_classes=["flat"])
        online.connect("clicked", lambda _b: self._online())
        bar.append(online)
        box.append(bar)
        self.feeds = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=12, row_spacing=6,
                                 column_spacing=6)
        self.feeds.set_margin_start(18)
        self.feeds.set_margin_end(18)
        self.feeds.connect("child-activated", lambda _f, c: ctx.show_feed(c.get_child().feed_id))
        box.append(self.feeds)
        self.list = EpisodeList(ctx, on_activate=lambda e: ctx.show_episode(e.id))
        self.stack = Gtk.Stack(vexpand=True)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(self.list.view)
        self.stack.add_named(scrolled, "list")
        self.empty = EmptyState("system-search-symbolic", _("Search"), _("Type a query to search"))
        self.stack.add_named(self.empty, "empty")
        self.stack.set_visible_child_name("empty")
        box.append(self.stack)
        self.set_content(box)
        self._debounce = Debouncer(250, self._run)
        self.connect("shown", lambda _p: self.entry.grab_focus())

    def _run(self):
        text = self.entry.get_text().strip()
        child = self.feeds.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.feeds.remove(child)
            child = following
        if len(text) < 2:
            self.list.set_episodes([])
            self.empty.update(_("Search"), _("Type a query to search"))
            self.stack.set_visible_child_name("empty")
            self.set_subtitle("")
            return
        db = self.ctx.db
        feeds = db.search_feeds(text)
        for feed in feeds[:12]:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["omp-tile"])
            tile.feed_id = feed.id
            cover = Cover(self.ctx.textures, 72)
            cover.set_source(feed.image_url, feed.display_title)
            tile.append(cover)
            name = label(feed.display_title, ("caption",), wrap=True, lines=2)
            name.set_size_request(72, -1)
            name.set_max_width_chars(1)
            tile.append(name)
            self.feeds.append(tile)
        episodes = db.search_episodes(text)
        self.list.set_episodes(episodes)
        self.stack.set_visible_child_name("list" if episodes else "empty")
        if not episodes:
            self.empty.update(_("No results"), _("No results were found for “{query}”").format(query=text))
        self.set_subtitle(ngettext("{n} episode", "{n} episodes", len(episodes)).format(n=len(episodes)))

    def _online(self):
        self.ctx.show_page("add")
        page = self.ctx.pages.get("add")
        if page is not None and self.entry.get_text().strip():
            page.set_query(self.entry.get_text().strip())
