"""Subscriptions: cover grid (or list) with counters, tags, sorting and the archive."""

from gi.repository import Gdk, Gio, GLib, Gtk

from ...i18n import _, ngettext
from ...models import FEED_ARCHIVED, FEED_SUBSCRIBED
from ...util import fmt_date
from ..widgets.common import Cover, EmptyState, clear_box, confirm, label
from ..window import sort_feeds
from .base import BasePage

COUNTERS = [("inbox", _("Number of episodes in the inbox")), ("unplayed", _("Number of unplayed episodes")),
            ("downloaded", _("Number of downloaded episodes")),
            ("downloaded_unplayed", _("Number of downloaded and unplayed episodes")), ("none", _("None"))]
ORDERS = [("alpha", _("Sort alphabetically")), ("counter", _("Sort by counter")),
          ("date", _("Sort by publication date")), ("most_played", _("Sort by number of played episodes"))]


def counter_value(feed, counter):
    return {"inbox": feed.count_new, "unplayed": feed.count_unplayed, "downloaded": feed.count_downloaded,
            "downloaded_unplayed": feed.count_downloaded_unplayed}.get(counter, 0)


class FeedTile(Gtk.Box):
    def __init__(self, ctx, size, show_title, as_list):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL if as_list else Gtk.Orientation.VERTICAL,
                         spacing=12 if as_list else 6, css_classes=["omp-tile"])
        self.feed = None
        self.title = self.meta = None
        overlay = Gtk.Overlay(halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.cover = Cover(ctx.textures, 48 if as_list else size)
        overlay.set_child(self.cover)
        self.append(overlay)
        self.badge = label("", ("omp-badge", "omp-numeric"), xalign=0.5, ellipsize=False)
        if as_list:
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
            self.title = label("", ("omp-row-title",))
            text.append(self.title)
            self.meta = label("", ("omp-row-meta",))
            text.append(self.meta)
            self.append(text)
            self.append(self.badge)
        else:
            # As wide as the cover, not as the cell: a flow box measures a line at the narrower width, so a title
            # that fits on one line of the wider cell would leave its line one text line too tall. It takes both
            # alignments: with one of them left to fill, GTK asks a wrapping label how wide it wants to be.
            self.set_halign(Gtk.Align.START)
            self.set_valign(Gtk.Align.START)
            self.badge.set_halign(Gtk.Align.END)
            self.badge.set_valign(Gtk.Align.START)
            self.badge.set_margin_top(4)
            self.badge.set_margin_end(4)
            overlay.add_overlay(self.badge)
            if show_title:
                self.title = label("", ("omp-tile-title",), wrap=True, lines=2)
                self.title.set_size_request(size, -1)
                self.title.set_max_width_chars(1)
                self.append(self.title)

    def update(self, feed, counter):
        self.feed = feed
        self.cover.set_source(feed.image_url, feed.display_title)
        value = counter_value(feed, counter)
        self.badge.set_text(str(value))
        self.badge.set_visible(bool(value))
        if self.title is not None:
            self.title.set_text(feed.display_title)
        if self.meta is not None:
            meta = [feed.author] if feed.author else []
            meta.append(ngettext("{n} episode", "{n} episodes", feed.count_total).format(n=feed.count_total))
            if feed.last_pub:
                meta.append(fmt_date(feed.last_pub))
            self.meta.set_text("  ·  ".join(meta))
        tooltip = feed.display_title
        if feed.refresh_error:
            tooltip += "\n" + _("Last refresh failed: {reason}").format(reason=feed.refresh_error)
        self.set_tooltip_text(tooltip)


class SubscriptionsPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Subscriptions"), "subscriptions")
        self.tag = ctx.settings.get("subscriptions_tag") or ""
        self.show_archive = False
        self.filter_text = ""
        self._tiles = {}
        self._tiles_shown = None
        self._tags_shown = None
        settings = ctx.settings

        group = Gio.SimpleActionGroup()

        def stateful(name, key, values):
            action = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new("s"),
                                                   GLib.Variant("s", str(settings.get(key))))
            action.connect("activate", lambda a, v: (a.set_state(v), settings.set(key, v.get_string()),
                                                     self._reload()))
            group.add_action(action)

        stateful("order", "subscriptions_sort", ORDERS)
        stateful("counter", "subscriptions_counter", COUNTERS)
        stateful("view", "subscriptions_view", None)
        titles = Gio.SimpleAction.new_stateful("titles", None, GLib.Variant("b", bool(settings.get(
            "subscriptions_show_titles"))))
        titles.connect("activate", lambda a, _v: (a.set_state(GLib.Variant("b", not a.get_state().get_boolean())),
                                                   settings.set("subscriptions_show_titles",
                                                                a.get_state().get_boolean()), self._reload()))
        group.add_action(titles)
        self._feed_actions(group)
        self.insert_action_group("subs", group)

        menu = Gio.Menu()
        order = Gio.Menu()
        for key, text in ORDERS:
            order.append(text, f"subs.order::{key}")
        menu.append_submenu(_("Sort"), order)
        counter = Gio.Menu()
        for key, text in COUNTERS:
            counter.append(text, f"subs.counter::{key}")
        menu.append_submenu(_("Counter"), counter)
        view = Gio.Menu()
        view.append(_("Grid"), "subs.view::grid")
        view.append(_("List"), "subs.view::list")
        view.append(_("Show titles"), "subs.titles")
        menu.append_section(None, view)
        self.add_menu(menu)
        self.add_action_button("view-refresh-symbolic", _("Refresh podcasts"), lambda: ctx.refresh_all(force=True))
        self.add_action_button("list-add-symbolic", _("Add podcast"), lambda: ctx.show_page("add"))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(spacing=8, css_classes=["omp-toolbar"])
        self.search = Gtk.SearchEntry(placeholder_text=_("Filter podcasts"))
        self.search.set_size_request(220, -1)
        self.search.connect("search-changed", lambda e: (setattr(self, "filter_text", e.get_text().strip().casefold()),
                                                          self.refresh()))
        toolbar.append(self.search)
        self.tags_box = Gtk.Box(spacing=6)
        tag_scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True)
        tag_scroller.set_child(self.tags_box)
        toolbar.append(tag_scroller)
        box.append(toolbar)
        self.stack = Gtk.Stack(vexpand=True)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, row_spacing=10,
                                column_spacing=10, valign=Gtk.Align.START)
        self.flow.set_margin_start(18)
        self.flow.set_margin_end(18)
        self.flow.set_margin_bottom(24)
        self.flow.connect("child-activated", lambda _f, child: ctx.show_feed(child.get_child().feed.id))
        scrolled.set_child(self.flow)
        self.stack.add_named(scrolled, "grid")
        self.empty = EmptyState("view-grid-symbolic", _("No subscriptions"),
                                _("To subscribe to a podcast, use Add podcast."), _("Add podcast"),
                                lambda: ctx.show_page("add"))
        self.stack.add_named(self.empty, "empty")
        box.append(self.stack)
        self.set_content(box)
        self.watch(200, "feeds-changed", "episodes-changed")

    # -- data -----------------------------------------------------------------------

    def refresh(self):
        ctx = self.ctx
        settings = ctx.settings
        states = (FEED_ARCHIVED,) if self.show_archive else (FEED_SUBSCRIBED,)
        feeds = ctx.db.get_feeds(states=states)
        all_count = len(feeds)
        tags = ctx.db.all_tags()
        self._build_tags(tags, archived=len(ctx.db.get_feeds(states=(FEED_ARCHIVED,), with_counts=False)))
        if self.tag == "#untagged":
            feeds = [f for f in feeds if not f.tags]
        elif self.tag:
            feeds = [f for f in feeds if self.tag in f.tags]
        if self.filter_text:
            feeds = [f for f in feeds if self.filter_text in f.display_title.casefold()
                     or self.filter_text in (f.author or "").casefold()]
        feeds = sort_feeds(feeds, settings.get("subscriptions_sort"))
        as_list = settings.get("subscriptions_view") == "list"
        show_titles = settings.get("subscriptions_show_titles")
        counter = settings.get("subscriptions_counter")
        # The tiles stay when the same podcasts are shown the same way; only what they say is updated.
        shown = (as_list, show_titles, [feed.id for feed in feeds])
        if shown != self._tiles_shown:
            self._tiles_shown = shown
            clear_box(self.flow)
            self._tiles = {}
            self.flow.set_max_children_per_line(1 if as_list else 30)
            self.flow.set_min_children_per_line(1)
            for feed in feeds:
                tile = FeedTile(ctx, 132, show_titles, as_list)
                click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
                click.connect("pressed", self._on_context, tile)
                tile.add_controller(click)
                self._tiles[feed.id] = tile
                self.flow.append(tile)
        for feed in feeds:
            self._tiles[feed.id].update(feed, counter)
        self.stack.set_visible_child_name("grid" if feeds else "empty")
        if not feeds and (self.tag or self.filter_text):
            self.empty.update(_("No subscriptions"), _("Try clearing the filter to see more subscriptions."))
        elif not feeds and self.show_archive:
            self.empty.update(_("Nothing archived"), _("You can archive podcasts from their menu."))
        else:
            self.empty.update(_("No subscriptions"), _("To subscribe to a podcast, use Add podcast."))
        self.set_subtitle(ngettext("{n} subscription", "{n} subscriptions", all_count).format(n=all_count)
                          + ("  ·  " + _("archive") if self.show_archive else ""))

    def _build_tags(self, tags, archived):
        shown = (tags, archived, self.tag, self.show_archive)
        if shown == self._tags_shown:
            return
        self._tags_shown = shown
        clear_box(self.tags_box)
        choices = [("", _("All"))] + [(t, t) for t in tags]
        if tags:
            choices.append(("#untagged", _("Untagged")))
        group = None
        for key, text in choices:
            chip = Gtk.ToggleButton(label=text, css_classes=["omp-chip"], focus_on_click=False,
                                    active=key == self.tag and not self.show_archive)
            if group is None:
                group = chip
            else:
                chip.set_group(group)
            chip.connect("toggled", self._on_tag, key)
            self.tags_box.append(chip)
        if archived:
            chip = Gtk.ToggleButton(label=_("Archive ({n})").format(n=archived), css_classes=["omp-chip"],
                                    active=self.show_archive, focus_on_click=False)
            chip.set_group(group)
            chip.connect("toggled", self._on_archive)
            self.tags_box.append(chip)

    def _on_tag(self, chip, key):
        if chip.get_active():
            self.tag = key
            self.show_archive = False
            self.ctx.settings.set("subscriptions_tag", key)
            GLib.idle_add(lambda: (self.refresh(), False)[1])

    def _on_archive(self, chip):
        if chip.get_active():
            self.show_archive = True
            GLib.idle_add(lambda: (self.refresh(), False)[1])

    # -- context menu ------------------------------------------------------------------

    def _feed_actions(self, group):
        lib = self.ctx.library

        def add(name, handler):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("x"))
            action.connect("activate", lambda _a, v: handler(v.get_int64()))
            group.add_action(action)

        add("open", self.ctx.show_feed)
        add("settings", lambda fid: self._settings(fid))
        add("refresh", lambda fid: self._refresh(fid))
        add("played", lambda fid: confirm(self.ctx.window, _("Mark all as played?"),
                                          _("Please confirm that you want to mark all episodes of this podcast as "
                                            "played."), _("Mark as played"), lambda: lib.mark_feed_played(fid),
                                          destructive=False))
        add("inbox", lambda fid: lib.remove_all_from_inbox(fid))
        add("tags", lambda fid: self._tags(fid))
        add("archive", lambda fid: lib.archive([fid]))
        add("restore", lambda fid: lib.archive([fid], archived=False))
        add("unsubscribe", lambda fid: confirm(self.ctx.window, _("Unsubscribe?"),
                                               _("Unsubscribing removes the subscription including all its episodes, "
                                                 "downloads, playback history and statistics."), _("Unsubscribe"),
                                               lambda: lib.unsubscribe([fid])))

    def _on_context(self, gesture, _n, x, y, tile):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        feed = tile.feed
        target = GLib.Variant("x", feed.id)
        menu = Gio.Menu()

        def item(section, text, action):
            entry = Gio.MenuItem.new(text, None)
            entry.set_action_and_target_value(f"subs.{action}", target)
            section.append_item(entry)

        first = Gio.Menu()
        item(first, _("Open podcast"), "open")
        item(first, _("Podcast settings"), "settings")
        item(first, _("Refresh"), "refresh")
        menu.append_section(None, first)
        second = Gio.Menu()
        item(second, _("Mark all as played"), "played")
        item(second, _("Remove all from inbox"), "inbox")
        item(second, _("Edit tags"), "tags")
        menu.append_section(None, second)
        third = Gio.Menu()
        if feed.is_archived:
            item(third, _("Restore"), "restore")
        else:
            item(third, _("Archive"), "archive")
        item(third, _("Unsubscribe"), "unsubscribe")
        menu.append_section(None, third)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(tile)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _settings(self, feed_id):
        from ..dialogs import FeedSettingsDialog

        FeedSettingsDialog(self.ctx, feed_id).present(self.ctx.window)

    def _tags(self, feed_id):
        from ..dialogs import edit_tags

        edit_tags(self.ctx, [feed_id])

    def _refresh(self, feed_id):
        from ..app import run_in_thread

        run_in_thread(lambda: self.ctx.library.refresh_feed(feed_id, force=True),
                      lambda r: self.ctx.toast(r.error) if r.error else None)
