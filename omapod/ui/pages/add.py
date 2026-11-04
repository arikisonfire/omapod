"""Add podcast: directory search, discover, RSS address, OPML import and local folders."""

import locale
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from ...feeds.opml import parse_opml
from ...i18n import _, ngettext
from ...net import http, search
from ..widgets.common import Cover, Debouncer, label
from .base import BasePage

COUNTRIES = ["us", "gb", "de", "at", "ch", "fr", "nl", "es", "it", "se", "dk", "no", "fi", "pl", "ie", "ca", "au",
             "nz", "br", "mx", "jp", "in"]


def default_country():
    code = (locale.getlocale()[0] or "en_US").split("_")[-1].lower()
    return code if code in COUNTRIES else "us"


class ResultRow(Gtk.Box):
    def __init__(self, ctx, result):
        super().__init__(spacing=12, css_classes=["omp-row"])
        self.result = result
        cover = Cover(ctx.textures, 56)
        cover.set_source(result.image_url, result.title)
        self.append(cover)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label(result.title, ("omp-row-title",), wrap=True, lines=2))
        meta = [result.author] if result.author else []
        if result.episodes:
            meta.append(ngettext("{n} episode", "{n} episodes", result.episodes).format(n=result.episodes))
        if meta:
            text.append(label("  ·  ".join(meta), ("omp-row-meta",)))
        self.append(text)
        self.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["omp-state-icon"]))


class AddPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx, _("Add podcast"), "add")
        self._search_token = 0
        settings = ctx.settings
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(24)
        body.set_margin_end(24)
        body.set_margin_bottom(28)
        scrolled.set_child(body)
        self.set_content(scrolled)

        search_row = Gtk.Box(spacing=8)
        self.entry = Gtk.SearchEntry(placeholder_text=_("Search podcast… or paste an RSS address"), hexpand=True)
        self.entry.connect("activate", lambda _e: self._search())
        search_row.append(self.entry)
        self.provider = Gtk.DropDown.new_from_strings(list(search.PROVIDERS.values()))
        keys = list(search.PROVIDERS)
        current = settings.get("search_provider")
        self.provider.set_selected(keys.index(current) if current in keys else 0)
        self.provider.connect("notify::selected", lambda d, _p: (settings.set("search_provider",
                                                                              keys[d.get_selected()]), self._search()))
        self.provider.set_tooltip_text(_("Podcast directory"))
        search_row.append(self.provider)
        go = Gtk.Button(label=_("Search"), css_classes=["omp-primary"])
        go.connect("clicked", lambda _b: self._search())
        search_row.append(go)
        body.append(search_row)

        self.status = label("", ("omp-dim",), wrap=True)
        self.status.set_visible(False)
        body.append(self.status)
        self.spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        self.spinner.set_visible(False)
        self.spinner.set_size_request(24, 24)
        self.spinner.set_halign(Gtk.Align.START)
        body.append(self.spinner)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results.connect("row-activated", lambda _l, row: self._open(row.get_child().result))
        body.append(self.results)

        options = Adw.PreferencesGroup(title=_("Other ways to add"))
        url_row = Adw.EntryRow(title=_("Add podcast by RSS address"))
        url_row.set_show_apply_button(True)
        url_row.connect("apply", lambda r: (ctx.preview_url(r.get_text().strip()), r.set_text("")))
        options.add(url_row)
        opml = Adw.ActionRow(title=_("Import podcast list (OPML)"),
                             subtitle=_("Bring your subscriptions over from another podcast app"), activatable=True)
        opml.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
        opml.connect("activated", lambda _r: self._choose_opml())
        options.add(opml)
        folder = Adw.ActionRow(title=_("Add local folder"),
                               subtitle=_("Play audio files from a folder like a podcast"), activatable=True)
        folder.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        folder.connect("activated", lambda _r: self._choose_folder())
        options.add(folder)
        sync = Adw.ActionRow(title=_("Synchronize with AntennaPod"),
                             subtitle=_("Nextcloud GPodder Sync or gpodder.net"), activatable=True)
        sync.add_suffix(Gtk.Image(icon_name="omapod-sync-symbolic"))
        sync.connect("activated", lambda _r: ctx.show_preferences("sync"))
        options.add(sync)
        body.append(options)

        discover_header = Gtk.Box(spacing=8)
        discover_header.append(label(_("Discover"), ("omp-heading",), xalign=0))
        discover_header.append(Gtk.Box(hexpand=True))
        self.country = Gtk.DropDown.new_from_strings([c.upper() for c in COUNTRIES])
        country = settings.get("discover_country") or default_country()
        self.country.set_selected(COUNTRIES.index(country) if country in COUNTRIES else 0)
        self.country.connect("notify::selected", lambda d, _p: (settings.set("discover_country",
                                                                             COUNTRIES[d.get_selected()]),
                                                                self._load_discover()))
        self.country.set_tooltip_text(_("Select country"))
        discover_header.append(self.country)
        self.hide_discover = Gtk.ToggleButton(label=_("Hide"), css_classes=["flat"],
                                              active=bool(settings.get("discover_hidden")))
        self.hide_discover.connect("toggled", lambda b: (settings.set("discover_hidden", b.get_active()),
                                                         self._load_discover()))
        discover_header.append(self.hide_discover)
        body.append(discover_header)
        self.discover = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, row_spacing=10, column_spacing=10,
                                    max_children_per_line=12)
        self.discover.connect("child-activated", lambda _f, child: self._open(child.get_child().result))
        body.append(self.discover)
        self.discover_note = label(_("Suggestions by Apple Podcasts"), ("caption", "omp-dim"))
        body.append(self.discover_note)
        self._discover_loaded = False
        self.connect("showing", lambda _p: self._load_discover() if not self._discover_loaded else None)

    # ------------------------------------------------------------ search

    def set_query(self, text):
        self.entry.set_text(text)
        self._search()

    def _search(self):
        query = self.entry.get_text().strip()
        if not query:
            return
        if query.lower().startswith(("http://", "https://", "feed://", "podcast://", "itpc://")):
            self.ctx.subscribe_from_uri(query)
            return
        provider = list(search.PROVIDERS)[self.provider.get_selected()]
        self._search_token += 1
        token = self._search_token
        self.spinner.set_visible(True)
        self.status.set_visible(False)

        def work():
            try:
                results = search.search(provider, query)
                error = None
            except (http.HttpError, ValueError, KeyError) as exc:
                results, error = [], str(exc)
            GLib.idle_add(self._show_results, token, results, error, provider)

        threading.Thread(target=work, daemon=True).start()

    def _show_results(self, token, results, error, provider):
        if token != self._search_token:
            return False
        self.spinner.set_visible(False)
        child = self.results.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.results.remove(child)
            child = following
        if error:
            self.status.set_text(error)
            self.status.set_visible(True)
        elif not results:
            self.status.set_text(_("No results were found for “{query}”").format(query=self.entry.get_text()))
            self.status.set_visible(True)
        else:
            self.status.set_text(_("Results by {provider}").format(provider=search.PROVIDERS[provider]))
            self.status.set_visible(True)
        for result in results:
            self.results.append(ResultRow(self.ctx, result))
        return False

    def _open(self, result):
        self.ctx.preview_url(result.feed_url)

    # ------------------------------------------------------------ discover

    def _load_discover(self):
        self._discover_loaded = True
        child = self.discover.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.discover.remove(child)
            child = following
        hidden = self.ctx.settings.get("discover_hidden")
        self.discover_note.set_text(_("You selected to hide suggestions.") if hidden
                                    else _("Suggestions by Apple Podcasts"))
        self.country.set_sensitive(not hidden)
        if hidden:
            return
        country = COUNTRIES[self.country.get_selected()]
        subscribed = [f.title for f in self.ctx.db.get_feeds(with_counts=False)]

        def work():
            try:
                results = search.top_podcasts(country, 24, subscribed)
            except (http.HttpError, ValueError, KeyError) as exc:
                GLib.idle_add(lambda message=str(exc): (self.discover_note.set_text(message), False)[1])
                return
            GLib.idle_add(self._show_discover, results)

        threading.Thread(target=work, daemon=True).start()

    def _show_discover(self, results):
        for result in results:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["omp-tile"])
            tile.result = result
            cover = Cover(self.ctx.textures, 112)
            cover.set_source(result.image_url, result.title)
            tile.append(cover)
            title = label(result.title, ("omp-tile-title",), wrap=True, lines=2)
            title.set_size_request(112, -1)
            title.set_max_width_chars(1)
            tile.append(title)
            tile.set_tooltip_text(f"{result.title}\n{result.author}")
            self.discover.append(tile)
        return False

    # ------------------------------------------------------------ files

    def _choose_opml(self):
        dialog = Gtk.FileDialog(title=_("Import podcast list (OPML)"))
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        opml_filter = Gtk.FileFilter(name=_("OPML files"))
        for pattern in ("*.opml", "*.xml", "*.OPML"):
            opml_filter.add_pattern(pattern)
        filters.append(opml_filter)
        dialog.set_filters(filters)

        def done(dlg, result):
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            if file is not None:
                import_opml_file(self.ctx, file.get_path())

        dialog.open(self.ctx.window, None, done)

    def _choose_folder(self):
        dialog = Gtk.FileDialog(title=_("Add local folder"))

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            from ..app import run_in_thread

            run_in_thread(lambda: self.ctx.library.add_local_folder(folder.get_path()),
                          lambda feed_id: self.ctx.show_feed(feed_id),
                          lambda error: self.ctx.toast(str(error)))

        dialog.select_folder(self.ctx.window, None, done)


def import_opml_file(ctx, path):
    try:
        with open(path, "rb") as handle:
            entries = parse_opml(handle.read())
    except (OSError, ValueError, Exception):
        ctx.toast(_("An error has occurred while reading the file. Make sure that it is a valid OPML file."), 6)
        return
    known = {f.url for f in ctx.db.get_feeds(with_counts=False)}
    entries = [e for e in entries if e["url"] not in known]
    if not entries:
        ctx.toast(_("You are already subscribed to every podcast in this file."))
        return
    dialog = Adw.AlertDialog(heading=_("OPML import"),
                             body=ngettext("Subscribe to {n} podcast?", "Subscribe to {n} podcasts?",
                                           len(entries)).format(n=len(entries)))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    checks = []
    for entry in entries:
        check = Gtk.CheckButton(label=entry["title"], active=True)
        checks.append((check, entry))
        box.append(check)
    scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=min(360, 28 * len(entries)))
    scrolled.set_child(box)
    dialog.set_extra_child(scrolled)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("Subscribe"))
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_d, response):
        if response != "ok":
            return
        chosen = [entry for check, entry in checks if check.get_active()]
        ctx.toast(ngettext("Importing {n} podcast…", "Importing {n} podcasts…", len(chosen)).format(n=len(chosen)))

        def work():
            failed = []
            for entry in chosen:
                try:
                    ctx.library.subscribe_url(entry["url"])
                except Exception:
                    failed.append(entry["title"])
            return failed

        from ..app import run_in_thread

        run_in_thread(work, lambda failed: ctx.toast(
            _("Import done.") if not failed else ngettext("{n} podcast could not be added.",
                                                          "{n} podcasts could not be added.",
                                                          len(failed)).format(n=len(failed)), 6))

    dialog.connect("response", on_response)
    dialog.present(ctx.window)
