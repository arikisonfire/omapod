"""Queue, Inbox, Episodes, Favorites, Downloads and History."""

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ...events import bus
from ...i18n import _, ngettext
from ...util import fmt_datetime, fmt_human_duration, fmt_size
from ..widgets.common import EmptyState, clear_box, confirm, icon_button, label
from ..widgets.episode_row import EpisodeList
from .base import BasePage, EpisodeListPage

EPISODE_SORTS = [
    ("date_desc", _("Date, new to old")),
    ("date_asc", _("Date, old to new")),
    ("duration_asc", _("Duration, short to long")),
    ("duration_desc", _("Duration, long to short")),
    ("title_asc", _("Episode title, A to Z")),
    ("title_desc", _("Episode title, Z to A")),
    ("feed_asc", _("Podcast title, A to Z")),
    ("feed_desc", _("Podcast title, Z to A")),
    ("size_desc", _("Size, large to small")),
]


def _menu(page, entries):
    """entries: (label, callback) or None for a section break."""
    menu = Gio.Menu()
    section = Gio.Menu()
    group = getattr(page, "_cmd_group", None)
    if group is None:
        group = page._cmd_group = Gio.SimpleActionGroup()
        page._cmd_count = 0
        page.insert_action_group("pagecmd", group)
    for entry in entries:
        if entry is None:
            menu.append_section(None, section)
            section = Gio.Menu()
            continue
        text, callback = entry[0], entry[1]
        page._cmd_count += 1
        name = f"cmd{page._cmd_count}"
        if len(entry) > 2:  # stateful toggle
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", bool(entry[2]())))

            def toggled(act, _value, cb=callback, getter=entry[2]):
                cb()
                act.set_state(GLib.Variant("b", bool(getter())))

            action.connect("activate", toggled)
        else:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=callback: cb())
        group.add_action(action)
        section.append(text, f"pagecmd.{name}")
    menu.append_section(None, section)
    return menu


class QueuePage(EpisodeListPage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Queue"), "queue", reorderable=True,
                         empty=("view-list-ordered-symbolic", _("No queued episodes"),
                                _("Add an episode by downloading it, or right-click an episode and choose "
                                  "Add to queue.")))
        self.empty_inbox = Gtk.Button(label=_("Go to inbox"), css_classes=["omp-primary"], halign=Gtk.Align.CENTER)
        self.empty_inbox.connect("clicked", lambda _b: ctx.show_page("inbox"))
        self.empty.append(self.empty_inbox)
        sort_menu = Gio.Menu()
        for key, text in [("date_asc", _("Date, old to new")), ("date_desc", _("Date, new to old")),
                          ("duration_asc", _("Duration, short to long")),
                          ("duration_desc", _("Duration, long to short")), ("feed_asc", _("Podcast title")),
                          ("title_asc", _("Episode title")), ("random", _("Random")),
                          ("smart_shuffle_asc", _("Smart shuffle, old to new")),
                          ("smart_shuffle_desc", _("Smart shuffle, new to old"))]:
            sort_menu.append(text, f"queue.sort::{key}")
        group = Gio.SimpleActionGroup()
        sort_action = Gio.SimpleAction.new("sort", GLib.VariantType.new("s"))
        sort_action.connect("activate", lambda _a, v: self._sort(v.get_string()))
        group.add_action(sort_action)
        self.insert_action_group("queue", group)
        settings = ctx.settings
        menu = _menu(self, [
            (_("Keep sorted"), lambda: settings.set("queue_keep_sorted", not settings.get("queue_keep_sorted")),
             lambda: settings.get("queue_keep_sorted")),
            (_("Lock queue"), self._toggle_lock, lambda: settings.get("queue_locked")),
            None,
            (_("Select episodes"), lambda: self.set_selecting(True)),
            (_("Clear queue"), self._clear),
        ])
        menu.prepend_submenu(_("Sort"), sort_menu)
        self.add_menu(menu)
        self.add_action_button("media-playlist-shuffle-symbolic", _("Smart shuffle"),
                               lambda: self._sort("smart_shuffle_asc"))

    def query(self):
        return self.ctx.db.get_queue()

    def refresh(self):
        super().refresh()
        has_inbox = self.ctx.db.count_episodes(("new", "subscribed")) > 0
        self.empty_inbox.set_visible(has_inbox)
        if has_inbox:
            self.empty.update(text=_("No episodes lined up, but there are new episodes that await you!"))

    def update_subtitle(self, episodes):
        if not episodes:
            self.set_subtitle("")
            return
        text = ngettext("{n} episode", "{n} episodes", len(episodes)).format(n=len(episodes))
        remaining = self.ctx.library.queue_remaining_ms()
        if remaining:
            text += "  ·  " + _("{time} left").format(time=fmt_human_duration(remaining))
        if self.ctx.settings.get("queue_locked"):
            text += "  ·  " + _("locked")
        self.set_subtitle(text)

    def on_reorder(self, episode_id, index):
        if not self.ctx.library.move_in_queue(episode_id, index):
            if self.ctx.settings.get("queue_keep_sorted"):
                self.ctx.toast(_("The queue is kept sorted. Turn off Keep sorted to reorder it."))
            else:
                self.ctx.toast(_("The queue is locked."))

    def _sort(self, order):
        settings = self.ctx.settings
        settings.set("queue_sort", order)
        self.ctx.library.sort_queue(order)

    def _toggle_lock(self):
        settings = self.ctx.settings
        settings.set("queue_locked", not settings.get("queue_locked"))
        self.ctx.toast(_("Queue locked") if settings.get("queue_locked") else _("Queue unlocked"))
        self.refresh()

    def _clear(self):
        confirm(self.ctx.window, _("Clear queue?"),
                _("Please confirm that you want to remove ALL episodes from the queue."), _("Clear queue"),
                self.ctx.library.clear_queue)


class InboxPage(EpisodeListPage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Inbox"), "inbox",
                         empty=("omapod-inbox-symbolic", _("No episodes in the inbox"),
                                _("New episodes will show up here. You can then decide whether you are "
                                  "interested in them.")))
        self.add_menu(_menu(self, [
            (_("Select episodes"), lambda: self.set_selecting(True)),
            (_("Remove all from inbox"), self._remove_all),
        ]))
        self.add_action_button("view-refresh-symbolic", _("Refresh podcasts"), lambda: ctx.refresh_all(force=True))

    def query(self):
        return self.ctx.db.get_episodes(("new", "subscribed"), sort="date_desc")

    def _remove_all(self):
        def run():
            count = self.ctx.library.remove_all_from_inbox()
            self.ctx.toast(ngettext("{n} episode removed from inbox.", "{n} episodes removed from inbox.",
                                    count).format(n=count))

        confirm(self.ctx.window, _("Remove all from inbox?"),
                _("Please confirm that you want to remove all from the inbox."), _("Remove all"), run)


class AllEpisodesPage(EpisodeListPage):
    LIMIT = 2000

    def __init__(self, ctx):
        filters = [
            ("all", _("All")),
            ("unplayed", _("Unplayed")),
            ("paused", _("In progress")),
            ("downloaded", _("Downloaded")),
            ("not_downloaded", _("Not downloaded")),
            ("favorite", _("Favorites")),
            ("played", _("Played")),
            ("queued", _("Queued")),
        ]
        super().__init__(ctx, _("Episodes"), "episodes", filters=filters, sort_key="episodes_sort",
                         empty=("audio-x-generic-symbolic", _("No episodes"),
                                _("When you add a podcast, the episodes will be shown here.")))
        menu = self.sort_menu(EPISODE_SORTS)
        self.add_menu(_menu(self, [(_("Select episodes"), lambda: self.set_selecting(True))]))
        self.add_menu(menu, icon="view-sort-descending-symbolic", tooltip=_("Sort"))
        self.add_action_button("view-refresh-symbolic", _("Refresh podcasts"), lambda: ctx.refresh_all(force=True))

    def query(self):
        filters = ["subscribed"]
        if self.active_filter and self.active_filter != "all":
            filters.append(self.active_filter)
        if self.active_filter not in ("all", None):
            self.empty.update(_("No episodes"), _("Try clearing the filter to see more episodes."))
        else:
            self.empty.update(_("No episodes"), _("When you add a podcast, the episodes will be shown here."))
        return self.ctx.db.get_episodes(tuple(filters), sort=self.sort, limit=self.LIMIT)


class FavoritesPage(EpisodeListPage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Favorites"), "favorites", sort_key="favorites_sort",
                         empty=("starred-symbolic", _("No favorites"),
                                _("Mark episodes as favorites with the star on their page or in the menu.")))
        self.add_menu(_menu(self, [(_("Select episodes"), lambda: self.set_selecting(True))]))
        self.add_menu(self.sort_menu(EPISODE_SORTS), icon="view-sort-descending-symbolic", tooltip=_("Sort"))

    def query(self):
        return self.ctx.db.get_episodes(("favorite",), sort=self.sort)


class HistoryPage(EpisodeListPage):
    def __init__(self, ctx):
        super().__init__(ctx, _("History"), "history",
                         empty=("document-open-recent-symbolic", _("No history"),
                                _("After you listen to an episode, it will appear here.")))
        self.add_menu(_menu(self, [
            (_("Select episodes"), lambda: self.set_selecting(True)),
            (_("Clear history"), self._clear),
        ]))

    def query(self):
        return self.ctx.db.get_episodes(extra="e.completed_at > 0", sort="completed_desc", limit=500)

    def update_subtitle(self, episodes):
        self.set_subtitle(ngettext("{n} episode", "{n} episodes", len(episodes)).format(n=len(episodes))
                          if episodes else "")

    def _clear(self):
        confirm(self.ctx.window, _("Clear history?"),
                _("This will clear the entire playback history. Are you sure you want to proceed?"),
                _("Clear history"), self.ctx.library.clear_history)


class LogRow(Gtk.Box):
    def __init__(self, row):
        super().__init__(spacing=12, css_classes=["omp-row"])
        icon = "object-select-symbolic" if row["success"] else "dialog-error-symbolic"
        image = Gtk.Image(icon_name=icon, valign=Gtk.Align.CENTER)
        if not row["success"]:
            image.add_css_class("error")
        self.append(image)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.append(label(row["title"] or _("Unknown title"), ("omp-row-title",), wrap=True, lines=2))
        kind = _("Feed") if row["kind"] == "feed" else _("Media file")
        meta = f"{kind}  ·  {fmt_datetime(row['ts'])}"
        if not row["success"]:
            meta += f"  ·  {row['reason']}"
        text.append(label(meta, ("omp-row-meta",), wrap=True, lines=2))
        self.append(text)
        self.data = dict(row)


class DownloadsPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Downloads"), "downloads")
        self.stack = Adw.ViewStack()
        switcher = Adw.InlineViewSwitcher(stack=self.stack) if hasattr(Adw, "InlineViewSwitcher") else \
            Adw.ViewSwitcher(stack=self.stack)
        switcher.set_halign(Gtk.Align.START)
        switcher.set_margin_start(24)
        switcher.set_margin_bottom(8)
        self.sort = ctx.settings.get("downloads_sort") or "downloaded_desc"
        self.list = EpisodeList(ctx, on_activate=lambda e: ctx.show_episode(e.id))
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(self.list.view)
        self.downloads_stack = Gtk.Stack()
        self.downloads_stack.add_named(scrolled, "list")
        self.downloads_stack.add_named(EmptyState("folder-download-symbolic", _("No downloaded episodes"),
                                                  _("You can download episodes on the podcast page or with the "
                                                    "download button next to each episode.")), "empty")
        self.stack.add_titled_with_icon(self.downloads_stack, "downloaded", _("Downloaded"),
                                        "folder-download-symbolic")
        # What AntennaPod or another omaPOD downloaded (sync), with a download button in each row.
        self.elsewhere = EpisodeList(ctx, on_activate=lambda e: ctx.show_episode(e.id))
        elsewhere_scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        elsewhere_scrolled.set_child(self.elsewhere.view)
        self.elsewhere_empty = EmptyState("omapod-sync-symbolic", _("Nothing from other devices"), "")
        self.elsewhere_stack = Gtk.Stack()
        self.elsewhere_stack.add_named(elsewhere_scrolled, "list")
        self.elsewhere_stack.add_named(self.elsewhere_empty, "empty")
        self.stack.add_titled_with_icon(self.elsewhere_stack, "elsewhere", _("On other devices"),
                                        "omapod-sync-symbolic")
        self.log_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.log_list.add_css_class("omp-log")
        self.log_list.connect("row-activated", self._on_log_row)
        log_scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        log_scrolled.set_child(self.log_list)
        self.log_stack = Gtk.Stack()
        self.log_stack.add_named(log_scrolled, "list")
        self.log_stack.add_named(EmptyState("x-office-document-symbolic", _("No download log"),
                                            _("Download logs will appear here when available.")), "empty")
        self.stack.add_titled_with_icon(self.log_stack, "log", _("Download log"), "x-office-document-symbolic")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(switcher)
        box.append(self.stack)
        self.set_content(box)
        sorts = Gio.Menu()
        for key, text in [("downloaded_desc", _("Download date, new to old")), ("date_desc", _("Date, new to old")),
                          ("date_asc", _("Date, old to new")), ("size_desc", _("Size, large to small")),
                          ("feed_asc", _("Podcast title")), ("title_asc", _("Episode title"))]:
            sorts.append(text, f"dl.sort::{key}")
        group = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new("sort", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, v: self._set_sort(v.get_string()))
        group.add_action(action)
        self.insert_action_group("dl", group)
        menu = _menu(self, [
            (_("Delete played downloads"), self._delete_played),
            (_("Clear download log"), self._clear_log),
        ])
        menu.prepend_submenu(_("Sort"), sorts)
        self.add_menu(menu)
        self.download_all = self.add_action_button("folder-download-symbolic", _("Download all"),
                                                   self._download_elsewhere)
        self._downloaded_subtitle = ""
        self._log_shown = None
        self.stack.connect("notify::visible-child-name", lambda *_a: self._update_header())
        self.watch(200, "downloads-changed", "episodes-changed")

    def fill(self):
        return self.list.grow(self.ctx.db.get_episodes(("downloaded",), sort=self.sort))

    def _set_sort(self, sort):
        self.sort = sort
        self.ctx.settings.set("downloads_sort", sort)
        self.refresh()

    def refresh(self):
        db = self.ctx.db
        active = self.ctx.downloads.active_ids()
        running = db.get_episodes_by_ids(sorted(active))
        done = db.get_episodes(("downloaded",), sort=self.sort)
        episodes = running + [e for e in done if e.id not in active]
        self.list.set_episodes(episodes)
        self.downloads_stack.set_visible_child_name("list" if episodes else "empty")
        size = sum(e.media_size for e in done)
        parts = [ngettext("{n} episode", "{n} episodes", len(done)).format(n=len(done))]
        if size:
            parts.append(fmt_size(size))
        if running:
            parts.append(ngettext("{n} download running", "{n} downloads running", len(running)).format(
                n=len(running)))
        self._downloaded_subtitle = "  ·  ".join(parts)
        self._refresh_elsewhere()
        rows = db.get_log(200)
        shown = [row["id"] for row in rows]
        if shown != self._log_shown:
            self._log_shown = shown
            clear_box(self.log_list)
            for row in rows:
                self.log_list.append(LogRow(row))
        self.log_stack.set_visible_child_name("list" if rows else "empty")

    def _refresh_elsewhere(self):
        episodes = self.ctx.library.downloads_elsewhere()
        self.elsewhere.set_episodes(episodes)
        self.elsewhere_stack.set_visible_child_name("list" if episodes else "empty")
        settings = self.ctx.settings
        if not settings.get("sync_provider"):
            text = _("With synchronization, episodes you downloaded on your phone or another computer show up here.")
        elif settings.get("sync_provider") == "nextcloud" and not settings.get("remote_downloads_seen"):
            text = _("GPodder Sync on your Nextcloud does not pass on download reports yet: version 3.18 and older "
                     "discard them.")
        else:
            text = _("Episodes downloaded with AntennaPod or another omaPOD that are not downloaded here show up "
                     "here.")
        self.elsewhere_empty.update(text=text)
        self._update_header()

    def _update_header(self):
        count = len(self.elsewhere.episodes())
        elsewhere = self.stack.get_visible_child_name() == "elsewhere"
        self.download_all.set_visible(elsewhere and count > 0)
        if elsewhere:
            self.set_subtitle(ngettext("{n} episode on other devices", "{n} episodes on other devices",
                                       count).format(n=count) if count else "")
        else:
            self.set_subtitle(self._downloaded_subtitle)

    def _download_elsewhere(self):
        count = self.ctx.library.download([e.id for e in self.elsewhere.episodes()])
        if count:
            self.ctx.toast(ngettext("Downloading {n} episode from your other devices",
                                    "Downloading {n} episodes from your other devices", count).format(n=count))

    def _on_log_row(self, _box, row):
        data = row.get_child().data
        if data["success"]:
            return
        from ..dialogs import log_details

        log_details(self.ctx, data)

    def _delete_played(self):
        def run():
            count = self.ctx.library.delete_played_downloads()
            if not count:
                self.ctx.toast(_("There are no played downloads."))

        confirm(self.ctx.window, _("Delete played downloads?"),
                _("Please confirm that you want to delete all played downloads."), _("Delete"), run)

    def _clear_log(self):
        self.ctx.db.clear_log()
        self.refresh()
