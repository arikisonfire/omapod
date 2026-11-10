"""A podcast: header, actions and its episode list (also used as the
preview before subscribing)."""

from gi.repository import Adw, Gio, GLib, Gtk

from ...i18n import _, ngettext
from ...models import FEED_ARCHIVED, FEED_PREVIEW, FEED_SUBSCRIBED
from ...util import fmt_date, html_to_text
from ..widgets.common import Cover, confirm, label, wrap_box
from .base import EpisodeListPage
from .lists import EPISODE_SORTS


class FeedPage(EpisodeListPage):
    def __init__(self, ctx, feed_id):
        self.feed_id = feed_id
        self.feed = ctx.db.get_feed(feed_id, with_counts=True)
        filters = [("all", _("All")), ("unplayed", _("Unplayed")), ("paused", _("In progress")),
                   ("downloaded", _("Downloaded")), ("played", _("Played")), ("favorite", _("Favorites"))]
        super().__init__(ctx, self.feed.display_title if self.feed else "", f"feed-{feed_id}",
                         show_podcast=False, filters=filters,
                         empty=("audio-x-generic-symbolic", _("No episodes"),
                                _("This podcast has no episodes yet.")))
        self.sort = (self.feed.sort_order if self.feed and self.feed.sort_order else None) or \
            ctx.settings.get("default_sort") or "date_desc"
        self.title_label.set_visible(False)
        self._build_header()
        self.add_menu(self._feed_menu())
        self.add_menu(self.sort_menu(EPISODE_SORTS), icon="view-sort-descending-symbolic", tooltip=_("Sort"))
        self.refresh_button = self.add_action_button("view-refresh-symbolic", _("Refresh"), self._refresh)
        self.update_header()

    # -- header ----------------------------------------------------------------------

    def _build_header(self):
        ctx = self.ctx
        header = Gtk.Box(spacing=20, css_classes=["omp-feed-header"])
        self.cover = Cover(ctx.textures, 120)
        header.append(self.cover)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self.feed_title = label("", ("omp-feed-title",), wrap=True, lines=2)
        info.append(self.feed_title)
        self.author = label("", ("omp-feed-author",))
        info.append(self.author)
        self.meta = label("", ("omp-row-meta", "omp-dim"))
        info.append(self.meta)
        self.description = label("", ("omp-dim", "omp-small"), wrap=True, lines=2)
        info.append(self.description)
        buttons = wrap_box(margin_top=8)
        self.subscribe = Gtk.Button(label=_("Subscribe"), css_classes=["omp-primary"])
        self.subscribe.connect("clicked", self._on_subscribe)
        buttons.append(self.subscribe)
        self.settings_button = Gtk.Button()
        self.settings_button.set_child(Adw.ButtonContent(icon_name="emblem-system-symbolic", label=_("Settings")))
        self.settings_button.connect("clicked", lambda _b: self._open_settings())
        buttons.append(self.settings_button)
        self.info_button = Gtk.Button()
        self.info_button.set_child(Adw.ButtonContent(icon_name="help-about-symbolic", label=_("Info")))
        self.info_button.connect("clicked", lambda _b: self._open_info())
        buttons.append(self.info_button)
        self.support = Gtk.Button(css_classes=["flat"])
        self.support.set_child(Adw.ButtonContent(icon_name="starred-symbolic", label=_("Support")))
        self.support.connect("clicked", lambda _b: self._open_funding())
        buttons.append(self.support)
        info.append(buttons)
        header.append(info)
        self.top.append(header)
        self.preview_banner = Adw.Banner(title=_("You are not subscribed to this podcast yet. Subscribe to keep its "
                                                 "episodes up to date and see it in your library."))
        self.top.prepend(self.preview_banner)
        # Plain text: the reason can name a host that a redirect chose.
        self.error_banner = Adw.Banner(button_label=_("Details"), use_markup=False)
        self.error_banner.connect("button-clicked", lambda _b: self._show_error())
        self.top.prepend(self.error_banner)

    def update_header(self):
        feed = self.ctx.db.get_feed(self.feed_id, with_counts=True)
        if feed is None:
            return
        self.feed = feed
        self.set_title(feed.display_title)
        self.cover.set_source(feed.image_url, feed.display_title)
        self.feed_title.set_text(feed.display_title)
        self.author.set_text(feed.author)
        self.author.set_visible(bool(feed.author))
        self.description.set_text(html_to_text(feed.description, 400))
        self.subscribe.set_visible(feed.state != FEED_SUBSCRIBED)
        self.subscribe.set_label(_("Restore") if feed.state == FEED_ARCHIVED else _("Subscribe"))
        self.settings_button.set_visible(feed.state != FEED_PREVIEW)
        self.support.set_visible(bool(feed.funding))
        self.preview_banner.set_revealed(feed.state == FEED_PREVIEW)
        self.error_banner.set_title(_("Last refresh failed: {reason}").format(reason=feed.refresh_error)
                                    if feed.refresh_error else "")
        self.error_banner.set_revealed(bool(feed.refresh_error))
        parts = [ngettext("{n} episode", "{n} episodes", feed.count_total).format(n=feed.count_total)]
        if feed.last_pub:
            parts.append(_("latest {date}").format(date=fmt_date(feed.last_pub)))
        if feed.is_archived:
            parts.append(_("archived"))
        if not feed.keep_updated and feed.state == FEED_SUBSCRIBED:
            parts.append(_("updates disabled"))
        self.meta.set_text("  ·  ".join(parts))

    def refresh(self):
        super().refresh()
        if hasattr(self, "feed_title"):
            self.update_header()

    def update_subtitle(self, episodes):
        pass

    def query(self):
        filters = []
        if self.active_filter and self.active_filter != "all":
            filters.append(self.active_filter)
        return self.ctx.db.get_episodes(tuple(filters), sort=self.sort, feed_id=self.feed_id)

    def set_sort(self, sort):
        self.sort = sort
        self.ctx.library.update_feed_settings(self.feed_id, sort_order=sort)
        self.refresh()

    # -- actions ---------------------------------------------------------------------------

    def _feed_menu(self):
        from .lists import _menu

        lib = self.ctx.library
        return _menu(self, [
            (_("Mark all as played"), self._mark_all_played),
            (_("Remove all from inbox"), lambda: lib.remove_all_from_inbox(self.feed_id)),
            (_("Select episodes"), lambda: self.set_selecting(True)),
            None,
            (_("Refresh complete podcast"), lambda: self._refresh(full=True)),
            (_("Rename"), self._rename),
            (_("Edit tags"), self._tags),
            (_("Visit website"), lambda: self.ctx.open_uri(self.feed.link)),
            (_("Copy feed address"), lambda: self.ctx.copy_text(self.feed.url)),
            (_("Edit feed URL"), self._edit_url),
            None,
            (_("Archive"), self._archive),
            (_("Unsubscribe"), self._unsubscribe),
        ])

    def _on_subscribe(self, _button):
        settings = self.ctx.settings
        if settings.get("parental_enabled") and settings.get("parental_require_subscribe"):
            from ..dialogs import check_parental

            check_parental(self.ctx, lambda: self._do_subscribe())
            return
        self._do_subscribe()

    def _do_subscribe(self):
        if self.feed.state == FEED_ARCHIVED:
            self.ctx.library.archive([self.feed_id], archived=False)
        else:
            self.ctx.library.subscribe(self.feed_id)
        self.update_header()

    def _refresh(self, full=False):
        from ..app import run_in_thread

        self.refresh_button.set_sensitive(False)

        def done(result):
            self.refresh_button.set_sensitive(True)
            if result.error:
                self.ctx.toast(result.error, timeout=6)
            elif result.new_ids:
                self.ctx.toast(ngettext("{n} new episode", "{n} new episodes", len(result.new_ids)).format(
                    n=len(result.new_ids)))
            self.refresh()

        run_in_thread(lambda: self.ctx.library.refresh_feed(self.feed_id, force=True, full=full), done,
                      lambda error: (self.refresh_button.set_sensitive(True), self.ctx.toast(str(error))))

    def _mark_all_played(self):
        confirm(self.ctx.window, _("Mark all as played?"),
                _("Please confirm that you want to mark all episodes of this podcast as played."),
                _("Mark as played"), lambda: self.ctx.library.mark_feed_played(self.feed_id), destructive=False)

    def _rename(self):
        from ..dialogs import text_prompt

        text_prompt(self.ctx.window, _("Rename podcast"), self.feed.display_title, _("Rename"),
                    lambda value: self.ctx.library.rename_feed(self.feed_id, value), reset_label=_("Reset"),
                    on_reset=lambda: self.ctx.library.rename_feed(self.feed_id, ""))

    def _tags(self):
        from ..dialogs import edit_tags

        edit_tags(self.ctx, [self.feed_id])

    def _edit_url(self):
        from ..dialogs import text_prompt

        def apply(value):
            if not self.ctx.library.edit_feed_url(self.feed_id, value.strip()):
                self.ctx.toast(_("That address is already used by another podcast."))

        text_prompt(self.ctx.window, _("Edit feed URL"), self.feed.url, _("Change"), apply,
                    body=_("Changing the RSS address can break the playback state and episode list of this "
                           "podcast. Only do this if the podcast moved and the old address no longer works."))

    def _archive(self):
        body = _("Archiving hides the subscription from the list and stops looking for new episodes. Downloads, "
                 "statistics and playback state are kept.")
        if self.ctx.library.sync_enabled():
            body += " " + _("Your other devices keep the subscription.")
        confirm(self.ctx.window, _("Archive podcast?"), body, _("Archive"),
                lambda: (self.ctx.library.archive([self.feed_id]), self.ctx.show_page("subscriptions")))

    def _unsubscribe(self):
        confirm(self.ctx.window, _("Unsubscribe?"),
                _("Unsubscribing removes the subscription including all its episodes, downloads, playback history "
                  "and statistics."), _("Unsubscribe"),
                lambda: (self.ctx.library.unsubscribe([self.feed_id]), self.ctx.show_page("subscriptions")))

    def _open_settings(self):
        from ..dialogs import FeedSettingsDialog

        FeedSettingsDialog(self.ctx, self.feed_id).present(self.ctx.window)

    def _open_info(self):
        from ..dialogs import feed_info

        feed_info(self.ctx, self.feed_id)

    def _open_funding(self):
        funding = self.feed.funding or []
        if len(funding) == 1:
            self.ctx.open_uri(funding[0]["url"])
        else:
            self._open_info()

    def _show_error(self):
        from ..dialogs import log_details

        rows = self.ctx.db.query("SELECT * FROM download_log WHERE kind = 'feed' AND ref_id = ? "
                                 "ORDER BY ts DESC LIMIT 1", (self.feed_id,))
        if rows:
            log_details(self.ctx, dict(rows[0]))
