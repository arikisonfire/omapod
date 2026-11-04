"""Episode details: show notes, chapters and every action on one episode."""

from gi.repository import Adw, Gio, GLib, Gtk

from ...i18n import _
from ...util import fmt_clock, fmt_date, fmt_human_duration, fmt_size
from ..widgets.common import Cover, Debouncer, label, wrap_box
from ..widgets.shownotes import ShowNotes
from .base import BasePage


class EpisodePage(BasePage):
    def __init__(self, ctx, episode_id):
        super().__init__(ctx, "", f"episode-{episode_id}")
        self.episode_id = episode_id
        self.episode = ctx.db.get_episode(episode_id)
        self.title_label.set_visible(False)

        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=920, tightening_threshold=700)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_start(24)
        body.set_margin_end(24)
        body.set_margin_top(8)
        body.set_margin_bottom(32)
        clamp.set_child(body)
        scrolled.set_child(clamp)
        self.set_content(scrolled)

        hero = Gtk.Box(spacing=22)
        self.cover = Cover(ctx.textures, 168)
        hero.append(self.cover)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        self.ep_title = label("", ("omp-display",), wrap=True, selectable=True)
        info.append(self.ep_title)
        self.podcast = Gtk.Button(css_classes=["flat"], halign=Gtk.Align.START)
        self.podcast_label = label("", ("omp-feed-author",))
        self.podcast.set_child(self.podcast_label)
        self.podcast.connect("clicked", lambda _b: ctx.show_feed(self.episode.feed_id))
        info.append(self.podcast)
        self.meta = label("", ("omp-row-meta", "omp-dim"), wrap=True)
        info.append(self.meta)
        self.progress = Gtk.ProgressBar(css_classes=["omp-row-progress"])
        self.progress.set_margin_top(4)
        info.append(self.progress)
        self.progress_label = label("", ("caption", "omp-dim"))
        info.append(self.progress_label)
        hero.append(info)
        body.append(hero)

        actions = wrap_box()
        self.play = Gtk.Button(css_classes=["omp-primary"])
        self.play_content = Adw.ButtonContent(icon_name="media-playback-start-symbolic", label=_("Play"))
        self.play.set_child(self.play_content)
        self.play.connect("clicked", self._on_play)
        actions.append(self.play)
        self.download = Gtk.Button()
        self.download_content = Adw.ButtonContent(icon_name="folder-download-symbolic", label=_("Download"))
        self.download.set_child(self.download_content)
        self.download.connect("clicked", self._on_download)
        actions.append(self.download)
        self.queue = Gtk.Button()
        self.queue_content = Adw.ButtonContent(icon_name="view-list-ordered-symbolic", label=_("Add to queue"))
        self.queue.set_child(self.queue_content)
        self.queue.connect("clicked", self._on_queue)
        actions.append(self.queue)
        self.played = Gtk.Button(css_classes=["flat"])
        self.played.connect("clicked", self._on_played)
        actions.append(self.played)
        self.favorite = Gtk.ToggleButton(icon_name="starred-symbolic", css_classes=["flat"],
                                         tooltip_text=_("Favorite"))
        self._fav_handler = self.favorite.connect("toggled", self._on_favorite)
        actions.append(self.favorite)
        self.more = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["flat"], tooltip_text=_("More"))
        actions.append(self.more)
        body.append(actions)

        self.chapters_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.chapters_title = label(_("Chapters"), ("omp-heading",))
        body.append(self.chapters_title)
        body.append(self.chapters_box)

        body.append(label(_("Show notes"), ("omp-heading",)))
        self.notes = ShowNotes(ctx, on_seek=self._seek_to)
        body.append(self.notes)

        self._reload = Debouncer(120, self.refresh)
        self.listen("episodes-changed", self._on_episodes_changed)
        self.listen("player-state", lambda _s: self._update_buttons())
        self.listen("downloads-changed", lambda: self._update_buttons())
        self.listen("download-progress", lambda eid, *_a: self._update_buttons() if eid == episode_id else None)
        self.refresh()

    def initial_focus(self):
        return next((b for b in (self.play, self.download, self.queue, self.played) if b.get_visible()), None)

    def destroy_page(self):
        super().destroy_page()
        self._reload.cancel()

    def _on_episodes_changed(self, ids=None):
        if ids is None or self.episode_id in ids:
            self._reload()

    def refresh(self):
        episode = self.ctx.db.get_episode(self.episode_id)
        if episode is None:
            return
        self.episode = episode
        e = episode
        self.set_title(e.title)
        self.cover.set_source(e.cover_url(True), e.feed_title)
        self.ep_title.set_text(e.title or _("Untitled episode"))
        self.podcast_label.set_text(e.feed_title)
        parts = []
        if e.pub_date:
            parts.append(fmt_date(e.pub_date, relative=False))
        if e.duration:
            parts.append(fmt_human_duration(e.duration))
        if e.media_size:
            parts.append(fmt_size(e.media_size))
        if e.season or e.episode_number:
            numbers = []
            if e.season:
                numbers.append(_("Season {n}").format(n=e.season))
            if e.episode_number:
                numbers.append(_("Episode {n}").format(n=e.episode_number))
            parts.append(", ".join(numbers))
        if e.episode_type in ("trailer", "bonus"):
            parts.append(_("Trailer") if e.episode_type == "trailer" else _("Bonus"))
        if e.is_new:
            parts.append(_("New"))
        if e.is_played:
            parts.append(_("Played"))
        self.meta.set_text("  ·  ".join(parts))
        in_progress = e.is_in_progress and e.duration > 0
        self.progress.set_visible(in_progress)
        self.progress_label.set_visible(in_progress)
        if in_progress:
            self.progress.set_fraction(e.position / e.duration)
            self.progress_label.set_text(_("{pos} of {total}  ·  {left} left").format(
                pos=fmt_clock(e.position), total=fmt_clock(e.duration), left=fmt_human_duration(e.remaining)))
        self.notes.set_notes(e.description, _("This episode has no show notes."))
        self._update_chapters()
        self._update_buttons()

    def _update_chapters(self):
        rows = self.ctx.db.get_chapters(self.episode_id)
        chapters = [dict(start=r["start_ms"], title=r["title"]) for r in rows] or \
            [dict(start=c["start"], title=c.get("title", "")) for c in (self.episode.psc_chapters or [])]
        child = self.chapters_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.chapters_box.remove(child)
            child = following
        self.chapters_title.set_visible(bool(chapters))
        for index, chapter in enumerate(chapters):
            button = Gtk.Button(css_classes=["flat"])
            row = Gtk.Box(spacing=12, css_classes=["omp-chapter-row"])
            row.append(label(f"{index + 1:>2}", ("omp-numeric", "omp-dim"), ellipsize=False))
            title = label(chapter["title"] or _("Chapter {n}").format(n=index + 1), ("omp-chapter-title",))
            title.set_hexpand(True)
            row.append(title)
            row.append(label(fmt_clock(chapter["start"]), ("omp-numeric", "omp-dim"), ellipsize=False))
            button.set_child(row)
            button.connect("clicked", lambda _b, ms=chapter["start"]: self._seek_to(ms))
            self.chapters_box.append(button)

    def _update_buttons(self):
        e = self.episode
        ctx = self.ctx
        current = ctx.player.current_id() == e.id
        playing = current and ctx.player.status in ("playing", "loading")
        self.cover.set_playing(current)
        self.play.set_visible(e.has_media)
        if playing:
            self.play_content.set_icon_name("media-playback-pause-symbolic")
            self.play_content.set_label(_("Pause"))
        else:
            self.play_content.set_icon_name("media-playback-start-symbolic")
            self.play_content.set_label(_("Play") if e.is_downloaded or e.is_local else _("Stream"))
        progress = ctx.downloads.progress(e.id)
        self.download.set_visible(e.has_media and not e.is_local)
        if progress is not None:
            received, total, _state = progress
            percent = f" {int(received * 100 / total)}%" if total else ""
            self.download_content.set_icon_name("process-stop-symbolic")
            self.download_content.set_label(_("Cancel download") + percent)
        elif e.is_downloaded:
            self.download_content.set_icon_name("user-trash-symbolic")
            self.download_content.set_label(_("Delete"))
        else:
            self.download_content.set_icon_name("folder-download-symbolic")
            self.download_content.set_label(_("Download"))
        self.queue.set_visible(e.has_media)
        if e.in_queue:
            self.queue_content.set_icon_name("list-remove-symbolic")
            self.queue_content.set_label(_("Remove from queue"))
        else:
            self.queue_content.set_icon_name("view-list-ordered-symbolic")
            self.queue_content.set_label(_("Add to queue"))
        self.played.set_label(_("Mark as unplayed") if e.is_played else _("Mark as played"))
        self.favorite.handler_block(self._fav_handler)
        self.favorite.set_active(e.is_favorite)
        self.favorite.handler_unblock(self._fav_handler)
        self.favorite.set_tooltip_text(_("Remove from favorites") if e.is_favorite else _("Add to favorites"))
        self.more.set_menu_model(ctx.episode_menu(e))

    def _on_play(self, _button):
        player = self.ctx.player
        if player.current_id() == self.episode.id and player.status in ("playing", "loading"):
            player.pause()
        else:
            self.ctx.play_episode(self.episode.id)

    def _on_download(self, _button):
        e = self.episode
        if self.ctx.downloads.is_active(e.id):
            self.ctx.downloads.cancel(e.id)
        elif e.is_downloaded:
            self.ctx.delete_downloads([e.id])
        else:
            self.ctx.library.download([e.id])
        self._update_buttons()

    def _on_queue(self, _button):
        if self.episode.in_queue:
            self.ctx.library.remove_from_queue([self.episode.id])
        else:
            self.ctx.library.add_to_queue([self.episode.id])

    def _on_played(self, _button):
        self.ctx.library.mark_played([self.episode.id], played=not self.episode.is_played)

    def _on_favorite(self, button):
        self.ctx.library.set_favorite([self.episode.id], button.get_active())

    def _seek_to(self, ms):
        player = self.ctx.player
        if player.current_id() != self.episode.id:
            player.play_episode(self.episode.id, start_at=ms)
        else:
            player.seek(ms)
            if player.status != "playing":
                player.play()
