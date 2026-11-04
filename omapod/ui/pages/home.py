"""Home: continue listening, what's new, surprises, classics and downloads,
like AntennaPod's home screen."""

import random

from gi.repository import Adw, Gio, GLib, Gtk

from ...i18n import _
from ...util import days_ago, fmt_human_duration, fmt_size
from ..widgets.common import Cover, EmptyState, EpisodeItem, clear_box, label
from ..widgets.episode_row import EpisodeRow
from .base import BasePage

SECTIONS = {
    "continue": "Continue listening",
    "new": "See what's new",
    "surprise": "Get surprised",
    "classics": "Check your classics",
    "downloads": "Manage downloads",
}


class EpisodeCard(Gtk.Button):
    def __init__(self, ctx, episode, size=148):
        super().__init__(css_classes=["flat", "omp-tile"])
        self.ctx = ctx
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        overlay = Gtk.Overlay()
        self.cover = Cover(ctx.textures, size)
        overlay.set_child(self.cover)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["omp-action", "omp-action-play"],
                          halign=Gtk.Align.END, valign=Gtk.Align.END, tooltip_text=_("Play"), focus_on_click=False)
        play.set_margin_end(6)
        play.set_margin_bottom(6)
        play.connect("clicked", lambda _b: ctx.play_episode(self.episode.id))
        overlay.add_overlay(play)
        box.append(overlay)
        self.title = label("", ("omp-tile-title",), wrap=True, lines=2)
        self.title.set_size_request(size, -1)
        self.title.set_max_width_chars(1)
        box.append(self.title)
        self.sub = label("", ("caption", "omp-dim"))
        self.sub.set_max_width_chars(1)
        box.append(self.sub)
        self.bar = Gtk.ProgressBar(css_classes=["omp-row-progress"])
        box.append(self.bar)
        self.set_child(box)
        self.connect("clicked", lambda _b: ctx.show_episode(self.episode.id))
        self.update(episode)

    def update(self, episode):
        self.episode = episode
        ctx = self.ctx
        self.cover.set_source(episode.cover_url(ctx.settings.get("episode_cover")), episode.feed_title)
        self.cover.set_playing(ctx.player.current_id() == episode.id)
        self.title.set_text(episode.title)
        in_progress = bool(episode.is_in_progress and episode.duration)
        self.sub.set_text(_("{time} left").format(time=fmt_human_duration(episode.remaining)) if in_progress
                          else episode.feed_title)
        self.bar.set_visible(in_progress)
        if in_progress:
            self.bar.set_fraction(episode.position / episode.duration)
        self.set_tooltip_text(f"{episode.title}\n{episode.feed_title}")


class FeedCard(Gtk.Button):
    def __init__(self, ctx, feed, size=120):
        super().__init__(css_classes=["flat", "omp-tile"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cover = Cover(ctx.textures, size)
        cover.set_source(feed["image_url"], feed["title"])
        box.append(cover)
        title = label(feed["title"], ("omp-tile-title",), wrap=True, lines=2)
        title.set_size_request(size, -1)
        title.set_max_width_chars(1)
        box.append(title)
        self.set_child(box)
        self.set_tooltip_text(feed["title"])
        self.connect("clicked", lambda _b: ctx.show_feed(feed["feed_id"]))


def carousel(widgets):
    scrolled = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
    row = Gtk.Box(spacing=6)
    row.set_margin_start(18)
    row.set_margin_end(18)
    for widget in widgets:
        row.append(widget)
    scrolled.set_child(row)
    return scrolled


class Section:
    """One part of the home screen. It is rebuilt only when what it lists changes; otherwise its cards and
    rows are updated in place, so the position saved every few seconds during playback leaves scrolling,
    focus and open menus alone."""

    def __init__(self):
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.signature = None
        self.updates = {}  # episode id -> shows a newer copy of the episode
        self.widgets = {}  # episode id -> its card or list row


class HomePage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Home"), "home")
        self.add_action_button("view-refresh-symbolic", _("Refresh podcasts"), lambda: ctx.refresh_all(force=True))
        menu = Gio.Menu()
        menu.append(_("Configure home screen"), "home.configure")
        group = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new("configure", None)
        action.connect("activate", lambda *_a: self._configure())
        group.add_action(action)
        self.insert_action_group("home", group)
        self.add_menu(menu)
        self.scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.body.set_margin_bottom(28)
        self.scrolled.set_child(self.body)
        self.set_content(self.scrolled)
        self._surprise_seed = random.random()
        self._layout = None
        self._sections = {}
        self.watch(250, "episodes-changed", "queue-changed", "feeds-changed", "downloads-changed")

    def refresh(self):
        db = self.ctx.db
        if not db.get_feeds(with_counts=False):
            if self._layout == "empty":
                return
            clear_box(self.body)
            self._sections = {}
            self._layout = "empty"
            empty = EmptyState("omapod-podcast-symbolic", _("Welcome to omaPOD!"),
                               _("You are not subscribed to any podcasts yet. Search for a podcast, paste a feed "
                                 "address, import an OPML file, or connect your sync account to bring your "
                                 "AntennaPod subscriptions over."),
                               _("Add podcast"), lambda: self.ctx.show_page("add"))
            sync = Gtk.Button(label=_("Set up synchronization"), halign=Gtk.Align.CENTER, css_classes=["flat"])
            sync.connect("clicked", lambda _b: self.ctx.show_preferences("sync"))
            empty.append(sync)
            empty.set_vexpand(True)
            self.body.append(empty)
            return
        hidden = set(self.ctx.settings.get("home_hidden") or [])
        keys = tuple(key for key in self.ctx.settings.get("home_sections") or list(SECTIONS)
                     if key not in hidden and key in SECTIONS)
        if keys != self._layout:
            clear_box(self.body)
            self._sections = {key: Section() for key in keys}
            for key in keys:
                self.body.append(self._sections[key].box)
            self._layout = keys
        for key in keys:
            getattr(self, f"_section_{key}")(self._sections[key])

    def _fill(self, section, signature, build, episodes=()):
        """Build the section when the episodes it lists, their order or its counts changed; otherwise only
        update what its cards and rows show."""
        if signature == section.signature:
            for episode in episodes:
                update = section.updates.get(episode.id)
                if update is not None:
                    update(episode)
            return
        # GTK would move the focus of a removed widget (and the scrolling with it) somewhere else: keep it on
        # the same episode, or at the start of the section when the episode is gone.
        focus = self.ctx.window.get_focus()
        if focus is not None and not focus.is_ancestor(section.box):
            focus = None
        kept = next((eid for eid, widget in section.widgets.items()
                     if focus is not None and (focus is widget or focus.is_ancestor(widget))), None)
        clear_box(section.box)
        section.updates, section.widgets = {}, {}
        section.signature = signature
        build()
        if focus is not None:
            # The view stays put: the new widget has no position yet to scroll to.
            viewport = self.scrolled.get_child()
            viewport.set_scroll_to_focus(False)
            target = section.widgets.get(kept)
            if target is None or not target.grab_focus():
                section.box.child_focus(Gtk.DirectionType.TAB_FORWARD)
            viewport.set_scroll_to_focus(True)

    def _header(self, section, key, action_label=None, action=None, extra=None):
        row = Gtk.Box(spacing=8)
        title = label(_(SECTIONS[key]), ("omp-section-title",))
        title.set_hexpand(True)
        row.append(title)
        if extra is not None:
            row.append(extra)
        if action_label:
            button = Gtk.Button(label=action_label, css_classes=["flat"], valign=Gtk.Align.END)
            button.set_margin_end(18)
            button.connect("clicked", lambda _b: action())
            row.append(button)
        section.box.append(row)

    def _rows(self, section, episodes):
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("omp-home-list")
        for episode in episodes:
            row = EpisodeRow(self.ctx)
            row.bind(EpisodeItem(episode))
            section.updates[episode.id] = lambda e, row=row: row.bind(EpisodeItem(e))
            box.append(row)
            section.widgets[episode.id] = row.get_parent()
        box.connect("row-activated", lambda _b, r: self.ctx.show_episode(r.get_child().item.episode.id))
        section.box.append(box)

    def _cards(self, section, episodes):
        cards = []
        for episode in episodes:
            card = EpisodeCard(self.ctx, episode)
            section.updates[episode.id] = card.update
            section.widgets[episode.id] = card
            cards.append(card)
        section.box.append(carousel(cards))

    def _hint(self, section, text):
        hint = label(text, ("omp-dim",), wrap=True)
        hint.set_margin_start(24)
        hint.set_margin_end(24)
        hint.set_margin_bottom(6)
        section.box.append(hint)

    def _section_continue(self, section):
        queue = self.ctx.db.get_queue()
        current = self.ctx.player.current_id()
        queue.sort(key=lambda e: (e.id != current, not e.is_in_progress))
        queue = queue[:12]

        def build():
            self._header(section, "continue", _("Open queue"), lambda: self.ctx.show_page("queue"))
            if not queue:
                self._hint(section, _("You can add episodes by downloading them or with Add to queue in their menu."))
                return
            self._cards(section, queue)

        self._fill(section, tuple(e.id for e in queue), build, queue)

    def _section_new(self, section):
        episodes = self.ctx.db.get_episodes(("new", "subscribed"), sort="date_desc", limit=6)
        count = self.ctx.db.count_episodes(("new", "subscribed"))

        def build():
            self._header(section, "new", _("Inbox ({n})").format(n=count) if count else None,
                         lambda: self.ctx.show_page("inbox"))
            if not episodes:
                self._hint(section, _("New episodes will show up here. You can then decide whether you are "
                                      "interested in them."))
                return
            self._rows(section, episodes)

        self._fill(section, (count, tuple(e.id for e in episodes)), build, episodes)

    def _section_surprise(self, section):
        db = self.ctx.db
        pool = db.get_episodes(("unplayed", "subscribed", "has_media"), sort="date_desc",
                               extra="e.pub_date >= ?", params=[days_ago(60)], limit=120)
        rng = random.Random(self._surprise_seed)
        rng.shuffle(pool)
        pool = pool[:8]

        def build():
            shuffle = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", css_classes=["flat"],
                                 tooltip_text=_("Shuffle suggestions"), valign=Gtk.Align.END)
            shuffle.set_margin_end(18)
            shuffle.connect("clicked", lambda _b: (setattr(self, "_surprise_seed", random.random()), self.refresh()))
            self._header(section, "surprise", extra=shuffle)
            if not pool:
                self._hint(section, _("You already played all recent episodes. Nothing to be surprised here."))
                return
            self._cards(section, pool)

        self._fill(section, tuple(e.id for e in pool), build, pool)

    def _section_classics(self, section):
        stats = [s for s in self.ctx.library.statistics(start=days_ago(365)) if s["state"] == 1 and s["played"] > 0]
        if not stats:
            stats = [s for s in self.ctx.library.statistics() if s["state"] == 1]
        stats = stats[:10]

        def build():
            self._header(section, "classics", _("All podcasts"), lambda: self.ctx.show_page("subscriptions"))
            if stats:
                section.box.append(carousel([FeedCard(self.ctx, s) for s in stats]))

        self._fill(section, tuple((s["feed_id"], s["title"], s["image_url"]) for s in stats), build)

    def _section_downloads(self, section):
        episodes = self.ctx.db.get_episodes(("downloaded",), sort="downloaded_desc", limit=4)
        total = self.ctx.db.count_episodes(("downloaded",))
        size = self.ctx.db.scalar("SELECT SUM(media_size) FROM episodes WHERE downloaded_at > 0", (), 0)

        def build():
            self._header(section, "downloads", _("Downloads"), lambda: self.ctx.show_page("downloads"))
            if not episodes:
                self._hint(section, _("You can download any episode to listen to it offline."))
                return
            if size:
                self._hint(section, _("{n} episodes, {size} on this computer").format(n=total, size=fmt_size(size)))
            self._rows(section, episodes)

        self._fill(section, (total, size, tuple(e.id for e in episodes)), build, episodes)

    def _configure(self):
        settings = self.ctx.settings
        dialog = Adw.AlertDialog(heading=_("Configure home screen"),
                                 body=_("Choose which sections the home screen shows."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hidden = set(settings.get("home_hidden") or [])
        checks = {}
        for key in settings.get("home_sections") or list(SECTIONS):
            check = Gtk.CheckButton(label=_(SECTIONS[key]), active=key not in hidden)
            checks[key] = check
            box.append(check)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Save"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response == "ok":
                settings.set("home_hidden", [k for k, c in checks.items() if not c.get_active()])
                self.refresh()

        dialog.connect("response", on_response)
        dialog.present(self.ctx.window)
