"""Settings, laid out like AntennaPod's preference screens, plus sync setup,
backup/restore and the keyboard shortcuts dialog."""

import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_NAME
from ..config import DEFAULTS, own_downloads_dir, owns_file, paths, private_file
from ..db import SCHEMA_VERSION
from ..i18n import _, ngettext
from ..models import LOCAL_PREFIX
from ..sync.providers import (GpodderNet, NextcloudLoginFlow, PlainHttpOnly, SyncError, UntrustedCertificate, host_of,
                              normalize_host)
from ..sync.service import default_device_id
from ..util import fmt_datetime, fmt_speed, web_url
from .dialogs import _combo_row, password_hash
from .widgets.common import label, text_row

REFRESH = [(0, _("Never")), (60, _("Every hour")), (120, _("Every 2 hours")), (240, _("Every 4 hours")),
           (480, _("Every 8 hours")), (720, _("Every 12 hours")), (1440, _("Every day")), (4320, _("Every 3 days"))]
CACHE = [(5, "5"), (10, "10"), (25, "25"), (50, "50"), (100, "100"), (500, "500"), (-1, _("Unlimited"))]
CLEANUP = [(-3, _("When not favorited")), (-1, _("When not in queue")), (0, _("After finishing")),
           (12, _("12 hours after finishing")), (24, _("1 day after finishing")), (72, _("3 days after finishing")),
           (120, _("5 days after finishing")), (168, _("7 days after finishing")), (-2, _("Never"))]
SMART = [(0, _("Disabled")), (15, _("15 seconds")), (30, _("30 seconds")), (60, _("1 minute")),
         (120, _("2 minutes")), (300, _("5 minutes")), (600, _("10 minutes"))]
BUTTONS = [("fast_forward", _("Fast-forward")), ("rewind", _("Rewind")), ("skip", _("Skip episode")),
           ("restart", _("Restart episode"))]
ENQUEUE = [("back", _("Back")), ("front", _("Front")), ("after_current", _("After current episode")),
           ("random", _("Random"))]
# Forget what the last sync of queue and favorites saw, so the next one combines both sides again.
SHARED_SYNC_RESET = {"queue_sync_base": "", "queue_sync_local": "", "queue_sync_changed": 0,
                     "favorites_sync_base": [], "favorites_sync_local": []}
PAGES = [("home", _("Home")), ("queue", _("Queue")), ("inbox", _("Inbox")), ("episodes", _("Episodes")),
         ("subscriptions", _("Subscriptions")), ("downloads", _("Downloads")), ("remember", _("Remember last page"))]


def export_database(app, target):
    """A copy of the library. It holds the passwords of protected podcasts, so it is private from its first byte
    (config.private_file): SQLite would create it readable for everyone under the usual umask 022."""
    with private_file(target) as path:
        destination = sqlite3.connect(path)
        try:
            app.db.conn().backup(destination)
        finally:
            destination.close()


def write_export(target, text):
    """OPML and HTML exports: private like the database export, as feed addresses can carry access tokens."""
    with private_file(target) as path, open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def pending_import_path():
    return str(paths.db) + ".import"  # swapped in at the next start (__main__._apply_pending_import)


def check_import(path):
    """None if `path` holds an omaPOD database that can be imported, otherwise why not. The file may come from
    anyone, so it is read with SQLite's precautions for untrusted files, must pass the integrity check and may
    hold only tables and indexes: no triggers, views or virtual tables, which would run their own SQL whenever
    omaPOD changes its database."""
    try:
        conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
        try:
            for pragma in ("trusted_schema = OFF", "cell_size_check = ON", "mmap_size = 0"):
                conn.execute(f"PRAGMA {pragma}")
            objects = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
            damage = conn.execute("PRAGMA integrity_check(1)").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as error:
        return _("This file is not an omaPOD database ({error}).").format(error=error)
    tables = {name for kind, name, _sql in objects if kind == "table"}
    extra = [name for kind, name, sql in objects
             if kind not in ("table", "index") or re.match(r"\s*CREATE\s+VIRTUAL\b", sql or "", re.I)]
    if not {"feeds", "episodes", "queue"} <= tables or extra:
        return _("This file is not an omaPOD database.")
    if damage != "ok":
        return _("This file is not an omaPOD database ({error}).").format(error=" ".join(str(damage).split()))
    if version > SCHEMA_VERSION:
        return _("This database was exported with a newer version of omaPOD. Update omaPOD first.")
    return None


def prepare_import(path, known=()):
    """Readies omaPOD's own copy of a database chosen for import, in place, before it replaces the library: it is
    checked again (the chosen file could have changed since), and downloads naming a file omaPOD may not touch are
    forgotten, so that deleting them later cannot reach the user's other files (config.owns_file): a file outside
    the download folder, or, for a podcast of a folder, a file outside a folder the user chose on this computer.
    omaPOD's own download folder holds nothing but its downloads; one the user chose may hold other files as well,
    so there the import keeps only `known`, the files of the downloads the current library has.
    Returns a problem text, or None."""
    problem = check_import(path)
    if problem:
        return problem
    own_folder = own_downloads_dir()
    known = set(known)

    def kept(file_path, url):
        if not owns_file(file_path, url):
            return False
        return own_folder or file_path in known or (isinstance(url, str) and url.startswith(LOCAL_PREFIX))

    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA trusted_schema = OFF")
            rows = conn.execute("SELECT e.id, e.file_path, f.url FROM episodes e LEFT JOIN feeds f ON f.id = e.feed_id "
                                "WHERE e.file_path IS NOT NULL AND e.file_path != ''").fetchall()
            foreign = [(episode_id,) for episode_id, file_path, url in rows if not kept(file_path, url)]
            conn.executemany("UPDATE episodes SET file_path = '', downloaded_at = 0 WHERE id = ?", foreign)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        return _("This file is not an omaPOD database ({error}).").format(error=error)
    return None


def restart_app(app):
    """Quit; __main__ starts omaPOD again once run() has returned."""
    app.restart_requested = True
    app.quit()


class Preferences(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title=_("Settings"), search_enabled=True)
        self.set_content_width(760)
        self.app = app
        self.settings = app.settings
        self.add(self._interface())
        self.add(self._playback())
        self.add(self._downloads())
        self.sync_page = Adw.PreferencesPage(name="sync", title=_("Synchronization"),
                                             icon_name="omapod-sync-symbolic")
        self.add(self.sync_page)
        self._build_sync()
        self.add(self._import_export())
        self.add(self._other())
        self._sync_token = app.settings.connect(self._on_setting)
        self.connect("closed", lambda _d: app.settings.disconnect(self._sync_token))

    # ------------------------------------------------------------ helpers

    def toast(self, text, timeout=3):
        # The main window's toasts would sit hidden behind this dialog.
        if not self.get_mapped():
            self.app.toast(text, timeout)
            return
        self.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    def _switch(self, group, key, title, subtitle=""):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.settings.get(key)))
        row.connect("notify::active", lambda r, _p: self.settings.set(key, r.get_active()))
        group.add(row)
        return row

    def _combo(self, group, key, title, options, subtitle=""):
        row = _combo_row(title, subtitle, options, self.settings.get(key), lambda v: self.settings.set(key, v))
        group.add(row)
        return row

    def _spin(self, group, key, title, low, high, step, subtitle=""):
        row = Adw.SpinRow.new_with_range(low, high, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_value(self.settings.get(key) or 0)
        row.connect("notify::value", lambda r, _p: self.settings.set(key, int(r.get_value())))
        group.add(row)
        return row

    # ------------------------------------------------------------ pages

    def _interface(self):
        page = Adw.PreferencesPage(name="interface", title=_("User interface"),
                                   icon_name="preferences-desktop-appearance-symbolic")
        look = Adw.PreferencesGroup(title=_("Appearance"),
                                    description=_("omaPOD takes its colours, font size and corners from your "
                                                  "Omarchy theme and follows theme changes live."))
        # text_row for names, paths and server answers: rows read markup, so an "&" would empty them.
        theme_row = text_row(_("Current theme"), self.app.theme.theme.name or _("unknown"))
        look.add(theme_row)
        self._combo(look, "language", _("Language"), [("auto", _("System default")), ("en", "English"),
                                                      ("de", "Deutsch")], _("Applies after a restart"))
        page.add(look)
        behavior = Adw.PreferencesGroup(title=_("Behavior"))
        self._combo(behavior, "default_page", _("Default page"), PAGES, _("Screen that is opened when starting omaPOD"))
        self._switch(behavior, "show_sidebar_feeds", _("Podcasts in the navigation"),
                     _("List your subscriptions below the screens"))
        page.add(behavior)
        episodes = Adw.PreferencesGroup(title=_("Episode information"))
        self._switch(episodes, "episode_cover", _("Use episode cover"),
                     _("Use the episode's own cover in lists when it has one"))
        self._switch(episodes, "show_remaining_time", _("Show remaining time"),
                     _("Display the remaining time of episodes instead of their length"))
        self._switch(episodes, "time_respects_speed", _("Adjust media info to playback speed"),
                     _("Displayed position and duration are adapted to playback speed"))
        self._combo(episodes, "default_sort", _("Default sort order"),
                    [("date_desc", _("Date, new to old")), ("date_asc", _("Date, old to new")),
                     ("title_asc", _("Episode title, A to Z")), ("duration_asc", _("Duration, short to long"))],
                    _("Order of episodes on a podcast page"))
        page.add(episodes)
        subs = Adw.PreferencesGroup(title=_("Subscriptions"))
        self._combo(subs, "subscriptions_counter", _("Counter"),
                    [("inbox", _("Number of episodes in the inbox")), ("unplayed", _("Number of unplayed episodes")),
                     ("downloaded", _("Number of downloaded episodes")),
                     ("downloaded_unplayed", _("Number of downloaded and unplayed episodes")), ("none", _("None"))])
        self._combo(subs, "subscriptions_sort", _("Order"),
                    [("alpha", _("Alphabetically")), ("counter", _("By counter")),
                     ("date", _("By publication date")), ("most_played", _("By number of played episodes"))])
        self._switch(subs, "subscriptions_show_titles", _("Show titles"), _("Show podcast titles below the covers"))
        page.add(subs)
        return page

    def _playback(self):
        page = Adw.PreferencesPage(name="playback", title=_("Playback"), icon_name="media-playback-start-symbolic")
        control = Adw.PreferencesGroup(title=_("Playback control"))
        self._spin(control, "fast_forward_secs", _("Fast-forward skip time"), 5, 300, 5, _("Seconds"))
        self._spin(control, "rewind_secs", _("Rewind skip time"), 5, 300, 5, _("Seconds"))
        presets = Adw.EntryRow(title=_("Playback speed presets"),
                               text=", ".join(fmt_speed(s).rstrip("×") for s in self.settings.get("speed_presets")))
        presets.set_show_apply_button(True)

        def apply_presets(row):
            values = []
            for part in row.get_text().replace(";", ",").split(","):
                try:
                    value = float(part.strip().rstrip("x×").replace(" ", ""))
                except ValueError:
                    continue
                if 0.25 <= value <= 4:
                    values.append(round(value, 2))
            if values:
                self.settings.set("speed_presets", sorted(set(values)))

        presets.connect("apply", apply_presets)
        control.add(presets)
        self._switch(control, "skip_silence", _("Skip silence"), _("Shorten pauses in the audio (can be set per podcast)"))
        page.add(control)
        keys = Adw.PreferencesGroup(title=_("Media keys"),
                                    description=_("What the next and previous keys (and the bar widget's MPRIS "
                                                  "controls) do"))
        self._combo(keys, "forward_button", _("Next key"), BUTTONS)
        self._combo(keys, "previous_button", _("Previous key"), BUTTONS)
        page.add(keys)
        queue = Adw.PreferencesGroup(title=_("Queue"))
        self._combo(queue, "enqueue_location", _("Enqueue location"), ENQUEUE)
        self._switch(queue, "enqueue_downloaded", _("Enqueue downloaded"), _("Add downloaded episodes to the queue"))
        self._switch(queue, "continuous_playback", _("Continuous playback"),
                     _("Jump to the next queue item when playback completes"))
        self._combo(queue, "smart_mark_played_secs", _("Smart mark as played"), SMART,
                    _("Mark episodes as played when less than this time is left"))
        self._switch(queue, "skip_keeps_episode", _("Keep skipped episodes"), _("Keep episodes in the queue when "
                                                                                 "they are skipped"))
        page.add(queue)
        sleep = Adw.PreferencesGroup(title=_("Sleep timer"))
        self._switch(sleep, "sleep_auto_enable", _("Turn on automatically"),
                     _("Start the sleep timer when you press play in the hours below"))
        self._spin(sleep, "sleep_auto_from", _("From hour"), 0, 23, 1)
        self._spin(sleep, "sleep_auto_to", _("Until hour"), 0, 23, 1)
        self._spin(sleep, "sleep_minutes", _("Default duration"), 1, 600, 1, _("Minutes"))
        page.add(sleep)
        return page

    def _downloads(self):
        page = Adw.PreferencesPage(name="downloads", title=_("Downloads"), icon_name="folder-download-symbolic")
        automation = Adw.PreferencesGroup(title=_("Automation"))
        self._combo(automation, "refresh_interval_min", _("Refresh podcasts"), REFRESH,
                    _("How often omaPOD looks for new episodes while it runs"))
        self._combo(automation, "new_episodes_action", _("New episodes action"),
                    [("inbox", _("Add to inbox")), ("queue", _("Add to queue")), ("nothing", _("Nothing"))])
        page.add(automation)
        auto = Adw.PreferencesGroup(title=_("Automatic download"))
        self._switch(auto, "auto_download", _("Automatic download"),
                     _("Automatically download episodes from the inbox. Can be overridden per podcast."))
        self._switch(auto, "auto_download_queue", _("Download queued"), _("Automatically download queued episodes"))
        self._combo(auto, "episode_cache", _("Episode limit"), CACHE,
                    _("Automatic download is stopped if this number is reached"))
        self._switch(auto, "auto_download_on_battery", _("Download when not charging"),
                     _("Allow automatic download on battery"))
        page.add(auto)
        deletion = Adw.PreferencesGroup(title=_("Automatic deletion"))
        self._switch(deletion, "auto_delete", _("Delete after playing"), _("Delete episode when playback completes"))
        self._switch(deletion, "auto_delete_local", _("Auto delete from local folders"),
                     _("Also files added through Add local folder. They go to the trash, as they cannot be "
                       "downloaded again."))
        self._switch(deletion, "favorite_keeps_episode", _("Keep favorite episodes"),
                     _("Keep episodes when they are marked favorite"))
        self._combo(deletion, "episode_cleanup", _("Delete before auto download"), CLEANUP,
                    _("Episodes that may be removed when automatic download needs space"))
        self._switch(deletion, "delete_removes_from_queue", _("Delete removes from queue"),
                     _("Remove an episode from the queue when its download is deleted"))
        page.add(deletion)
        storage = Adw.PreferencesGroup(title=_("Storage"))
        from ..config import downloads_dir

        self.folder_row = text_row(_("Download folder"), str(downloads_dir()), activatable=True)
        self.folder_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        self.folder_row.connect("activated", lambda _r: self._choose_folder())
        storage.add(self.folder_row)
        self._spin(storage, "parallel_downloads", _("Parallel downloads"), 1, 8, 1, _("Applies after a restart"))
        page.add(storage)
        proxy = Adw.PreferencesGroup(title=_("Proxy"), description=_("HTTP proxy for feeds and downloads; playback "
                                                                     "uses the system settings"))
        settings = self.settings
        current = dict(settings.get("proxy") or {})

        def store(key, value):
            data = dict(settings.get("proxy") or {})
            data[key] = value
            settings.set("proxy", data)

        kind = _combo_row(_("Type"), None, [("direct", _("Direct")), ("http", "HTTP")], current.get("type", "direct"),
                          lambda v: store("type", v))
        proxy.add(kind)
        for key, title in (("host", _("Host")), ("port", _("Port")), ("username", _("Username"))):
            row = Adw.EntryRow(title=title, text=str(current.get(key) or ""))
            row.set_show_apply_button(True)
            row.connect("apply", lambda r, k=key: store(k, int(r.get_text() or 0) if k == "port" else r.get_text()))
            proxy.add(row)
        secrets = self.app.credentials
        password = Adw.PasswordEntryRow(title=_("Password"), text=secrets.get("proxy").get("password") or "")
        password.set_show_apply_button(True)
        password.connect("apply", lambda r: secrets.set("proxy", {"password": r.get_text()} if r.get_text() else None))
        proxy.add(password)
        page.add(proxy)
        return page

    def _import_export(self):
        page = Adw.PreferencesPage(name="backup", title=_("Backup & restore"), icon_name="document-save-symbolic")
        database = Adw.PreferencesGroup(title=_("Database"),
                                        description=_("Subscriptions, played episodes, queue, statistics and the "
                                                      "passwords of protected podcasts"))
        rows = [
            (_("Database export"), _("Save a copy of the whole library"), self._export_db),
            (_("Database import"), _("Replace the library with an exported database"), self._import_db),
        ]
        for title, subtitle, handler in rows:
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, h=handler: h())
            database.add(row)
        self._switch(database, "auto_db_export", _("Automatic database export"),
                     _("Back up the database every 3 days and keep the 5 most recent backups in {path}").format(
                         path=str(paths.backups)))
        page.add(database)
        opml = Adw.PreferencesGroup(title="OPML")
        for title, subtitle, handler in (
            (_("OPML export"), _("Transfer your subscriptions to another podcast app"), self._export_opml),
            (_("OPML import"), _("Import your subscriptions from another podcast app"), self._import_opml),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, h=handler: h())
            opml.add(row)
        page.add(opml)
        html = Adw.PreferencesGroup(title="HTML")
        for title, subtitle, handler in (
            (_("HTML export"), _("Show your subscriptions to a friend"), self._export_html),
            (_("Favorites export"), _("Export saved favorites to a file"), self._export_favorites),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, h=handler: h())
            html.add(row)
        page.add(html)
        return page

    def _other(self):
        page = Adw.PreferencesPage(name="other", title=_("More"), icon_name="view-more-symbolic")
        notifications = Adw.PreferencesGroup(title=_("Notifications"),
                                             description=_("Notifications for new episodes are switched on per "
                                                           "podcast in its settings."))
        self._switch(notifications, "notify_download_errors", _("Download failed"),
                     _("Shown when a download or podcast refresh fails"))
        self._switch(notifications, "notify_sync_errors", _("Synchronization failed"),
                     _("Shown when synchronization fails"))
        page.add(notifications)
        search = Adw.PreferencesGroup(title=_("Search"),
                                      description=_("Podcast Index needs a free API key from api.podcastindex.org"))
        secrets = self.app.credentials

        def store_key(name, value):
            keys = {**secrets.get("podcastindex"), name: value}
            secrets.set("podcastindex", keys if any(keys.values()) else None)

        for key, title in (("key", _("Podcast Index API key")), ("secret", _("Podcast Index API secret"))):
            row = (Adw.PasswordEntryRow if key == "secret" else Adw.EntryRow)(
                title=title, text=secrets.get("podcastindex").get(key) or "")
            row.set_show_apply_button(True)
            row.connect("apply", lambda r, k=key: store_key(k, r.get_text().strip()))
            search.add(row)
        page.add(search)
        parental = Adw.PreferencesGroup(title=_("Parental control"),
                                        description=_("Restrict subscribing with a password. It guards against "
                                                      "accidents, not against someone who can change omaPOD's "
                                                      "files."))
        enable = Adw.SwitchRow(title=_("Enable"), active=bool(self.settings.get("parental_enabled")))
        enable.connect("notify::active", self._on_parental)
        parental.add(enable)
        self._switch(parental, "parental_require_subscribe", _("Restrict subscribing"),
                     _("Ask for the parental control password when subscribing to new podcasts"))
        page.add(parental)
        stats = Adw.PreferencesGroup(title=_("Statistics"))
        reset = Adw.ActionRow(title=_("Reset statistics data"), activatable=True)
        reset.connect("activated", lambda _r: self._reset_stats())
        stats.add(reset)
        page.add(stats)
        return page

    # ------------------------------------------------------------ synchronization

    def _build_sync(self):
        page = self.sync_page
        for group in list(getattr(self, "_sync_groups", [])):
            page.remove(group)
        self._sync_groups = []
        provider = self.settings.get("sync_provider")
        creds = self.app.credentials.get("sync")
        intro = Adw.PreferencesGroup(
            title=_("Synchronization"),
            description=_("Sync subscriptions and played episodes with AntennaPod and other apps through a server. "
                          "omaPOD supports the same servers as AntennaPod."))
        self._sync_groups.append(intro)
        page.add(intro)
        if not provider:
            choose = Adw.PreferencesGroup(title=_("Choose synchronization provider"))
            nextcloud = Adw.ActionRow(title=_("Nextcloud (GPodder Sync)"),
                                      subtitle=_("GPodder Sync is an open-source Nextcloud app you can install on your "
                                                 "own server."), activatable=True)
            nextcloud.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            nextcloud.connect("activated", lambda _r: self._setup_nextcloud())
            choose.add(nextcloud)
            gpodder = Adw.ActionRow(title="gpodder.net",
                                    subtitle=_("An open-source synchronization service, also self-hostable."),
                                    activatable=True)
            gpodder.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            gpodder.connect("activated", lambda _r: self._setup_gpodder())
            choose.add(gpodder)
            self._sync_groups.append(choose)
            page.add(choose)
            return
        status = Adw.PreferencesGroup(title=_("Status"))
        name = _("Nextcloud") if provider == "nextcloud" else "gpodder.net"
        account = text_row(_("Logged in as {user}").format(user=creds.get("username", "?")),
                           f"{name} · {creds.get('server', '')}"
                           + (f" · {creds.get('device_id')}" if provider == "gpodder" else ""))
        status.add(account)
        if str(creds.get("server", "")).startswith("http://"):
            plain = Adw.ActionRow(title=_("Unencrypted connection"),
                                  subtitle=_("Your password and listening history travel unencrypted. That is only "
                                             "acceptable in your own network or VPN; log in again with https:// "
                                             "once the server offers it."))
            plain.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            status.add(plain)
        last = self.settings.get("sync_last_attempt")
        ok = self.settings.get("sync_last_success")
        self.sync_status = text_row(
            _("Last synchronization"),
            (fmt_datetime(last) + " · " + (_("Successful") if ok else _("Failed")) if last else _("Never"))
            + ("\n" + self.settings.get("sync_last_error") if not ok and self.settings.get("sync_last_error") else ""))
        status.add(self.sync_status)
        page.add(status)
        self._sync_groups.append(status)
        actions = Adw.PreferencesGroup()
        for title, subtitle, handler in (
            (_("Synchronize now"), _("Synchronize subscription and episode state changes"),
             lambda: self.app.sync_now()),
            (_("Force full synchronization"), _("Re-synchronize all subscriptions and episode states"),
             lambda: self.app.sync_now(full=True)),
            (_("Log out"), _("Stop synchronizing with this server"), self._logout),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.connect("activated", lambda _r, h=handler: h())
            actions.add(row)
        page.add(actions)
        self._sync_groups.append(actions)
        extras = Adw.PreferencesGroup(title=_("More than AntennaPod synchronizes"))
        description = _("AntennaPod sends neither its queue nor its favorites.")
        if provider == "nextcloud" and not self.settings.get("remote_downloads_seen"):
            description += " " + _("Downloads of other devices arrive once GPodder Sync on your Nextcloud keeps "
                                   "download reports; version 3.18 and older discard them.")
        extras.set_description(description)
        self._switch(extras, "sync_follow_playback", _("Continue where another device stopped"),
                     _("When you listened on your phone or another computer last, the player gets that episode "
                       "ready at the same position. Playback here is never interrupted."))
        mirror = self._switch(extras, "sync_mirror_downloads", _("Download what other devices downloaded"),
                              _("Episodes downloaded with AntennaPod or another omaPOD are downloaded here too, "
                                "unless they are played. Deleting a download stays on each device."))
        mirror.connect("notify::active", lambda r, _p: self._sync_option_on(r, {"mirror_downloads_ready": False}))
        if provider == "nextcloud":
            shared = self._switch(extras, "sync_queue_favorites", _("Share queue and favorites between computers"),
                                  _("For computers with omaPOD; AntennaPod cannot take part. They are kept in the "
                                    "hidden folder .omapod in your Nextcloud. The first time, both sides are "
                                    "combined."))
            shared.connect("notify::active", lambda r, _p: self._sync_option_on(r, SHARED_SYNC_RESET))
        page.add(extras)
        self._sync_groups.append(extras)

    def _sync_option_on(self, row, reset):
        if row.get_active():
            self.settings.update(reset)
            self.app.sync_now()

    def _on_setting(self, key, _value):
        # Only what the page shows; rebuilding for its own switches would pull them away under the pointer.
        if key in ("sync_provider", "sync_last_attempt", "sync_last_success", "sync_last_error"):
            GLib.idle_add(lambda: (self._build_sync(), False)[1])

    def _finish_login(self, provider, server, username, password, device_id="", cert_sha256=""):
        self.app.credentials.set("sync", {"provider": provider, "server": server, "username": username,
                                          "password": password, "device_id": device_id, "cert_sha256": cert_sha256})
        self.settings.update({"sync_provider": provider, "sync_last_subscriptions": 0, "sync_last_actions": 0,
                              "sync_last_error": "", "sync_last_success": None, "mirror_downloads_ready": False,
                              "remote_downloads_ready": False, "remote_downloads_seen": False,
                              **SHARED_SYNC_RESET})
        self.app.db.sync_clear()
        self.toast(_("Logged in. The first synchronization starts now."))
        self.app.sync_now()
        self._build_sync()

    def _logout(self):
        self.app.credentials.set("sync", None)
        self.settings.update({"sync_provider": "", "sync_last_subscriptions": 0, "sync_last_actions": 0,
                              "sync_last_attempt": 0, "sync_last_success": None, "sync_last_error": "",
                              "mirror_downloads_ready": False, "remote_downloads_ready": False,
                              "remote_downloads_seen": False, **SHARED_SYNC_RESET})
        self.app.db.sync_clear()
        self.toast(_("Logout was successful"))
        self._build_sync()

    def _setup_nextcloud(self):
        dialog = Adw.AlertDialog(heading=_("Nextcloud"),
                                 body=_("Enter the address of your Nextcloud. Your browser opens so you can grant "
                                        "access; come back here afterwards. The GPodder Sync app must be installed "
                                        "on the Nextcloud."))
        entry = Gtk.Entry(placeholder_text="https://cloud.example.com", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Proceed to login"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def on_response(_d, response):
            if response == "ok" and entry.get_text().strip():
                self._run_nextcloud_flow(entry.get_text().strip())

        dialog.connect("response", on_response)
        dialog.present(self)

    def _run_nextcloud_flow(self, server, cert_sha256=""):
        flow = NextcloudLoginFlow(server, cert_sha256)
        waiting = Adw.AlertDialog(heading=_("Connecting to Nextcloud"),
                                  body=_("Contacting {host}…").format(host=host_of(flow.server)))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spinner.set_size_request(24, 24)
        box.append(spinner)
        # Shown once the login page is known, for when the browser did not come up or opened elsewhere.
        address = label("", ("omp-small",), xalign=0.5, wrap=True, selectable=True)
        links = Gtk.Box(spacing=8, homogeneous=True, halign=Gtk.Align.CENTER)
        open_link = Gtk.Button(label=_("Open link"))
        copy_link = Gtk.Button(label=_("Copy link"))
        links.append(open_link)
        links.append(copy_link)
        for widget in (address, links):
            widget.set_visible(False)
            box.append(widget)
        waiting.set_extra_child(box)
        waiting.add_response("cancel", _("Cancel"))
        waiting.set_close_response("cancel")
        state = {"cancelled": False, "url": ""}
        waiting.connect("response", lambda *_a: state.update(cancelled=True))
        open_link.connect("clicked", lambda _b: self.app.open_uri(state["url"]))

        def copy(_button):
            # Not app.copy_text: its toast would sit hidden behind the dialogs.
            self.get_clipboard().set(state["url"])
            copy_link.set_label(_("Copied"))
            GLib.timeout_add_seconds(2, lambda: (copy_link.set_label(_("Copy link")), False)[1])

        copy_link.connect("clicked", copy)

        def work():
            try:
                url = flow.start()
                GLib.idle_add(opened, url)
                deadline = time.monotonic() + 600  # Nextcloud keeps a login open for 20 minutes
                while True:
                    time.sleep(2)
                    if state["cancelled"]:
                        return
                    if time.monotonic() > deadline:
                        raise SyncError(_("The login timed out. Please try again."))
                    result = flow.poll()
                    if result:
                        GLib.idle_add(done, result)
                        return
            except UntrustedCertificate as error:
                GLib.idle_add(untrusted, error)
            except PlainHttpOnly as error:
                GLib.idle_add(plain_only, error)
            except Exception as error:  # anything uncaught would leave the dialog waiting forever
                GLib.idle_add(fail, str(error) or error.__class__.__name__)

        def opened(url):
            if state["cancelled"]:
                return False
            state["url"] = url
            launched = self.app.open_uri(url)
            waiting.set_heading(_("Log in to Nextcloud"))
            if launched:
                waiting.set_body(_("Grant omaPOD access in your browser; this window continues on its own "
                                   "afterwards. Don't see the page? Look for a new tab in your browser, or open or "
                                   "copy the link:"))
            else:
                waiting.set_body(_("omaPOD could not start your browser. Open this link in a browser and grant "
                                   "access there; this window continues on its own afterwards:"))
            address.set_text(url)
            address.set_visible(True)
            links.set_visible(True)
            waiting.set_focus(open_link)  # a focused selectable label shows all of its text selected
            return False

        def done(result):
            if state["cancelled"]:
                return False
            waiting.close()
            server, login, app_password = result
            self._finish_login("nextcloud", server, login, app_password, cert_sha256=flow.cert_sha256)
            return False

        def untrusted(error):
            if state["cancelled"]:
                return False
            waiting.close()
            self._ask_trust(error)
            return False

        def plain_only(error):
            if state["cancelled"]:
                return False
            waiting.close()
            self._ask_plain_http(error)
            return False

        def fail(message):
            if state["cancelled"]:
                return False
            waiting.close()
            body = message + "\n\n" + _("Check the address and your network connection, and make sure that the "
                                         "GPodder Sync app is installed.")
            error = Adw.AlertDialog(heading=_("Unable to log into your Nextcloud"), body=body)
            error.add_response("copy", _("Copy"))
            error.add_response("ok", _("OK"))
            error.set_default_response("ok")
            error.set_close_response("ok")
            error.connect("response", lambda _d, response: self.get_clipboard().set(body) if response == "copy"
                          else None)
            error.present(self)
            return False

        waiting.present(self)
        threading.Thread(target=work, daemon=True).start()

    def _ask_trust(self, error):
        certificate = error.certificate
        if error.private:
            body = _("{host} uses a certificate that your system does not trust, for example a self-signed one or "
                     "one from your own certificate authority. Trust it only if this is your server: omaPOD then "
                     "accepts exactly this certificate for it.")
        else:
            body = _("{host} is reached over the internet, where servers normally have a certificate your system "
                     "trusts. That it does not trust this one can also mean that someone between you and the server "
                     "is listening in, for example in a public Wi-Fi. Trust it only if you set up this certificate "
                     "yourself and its fingerprint matches the one on your server.")
        dialog = Adw.AlertDialog(heading=_("Untrusted certificate"), body=body.format(host=host_of(error.server)))
        lines = []
        if certificate["subject"]:
            lines.append(_("Issued to: {name}").format(name=certificate["subject"]))
        if certificate["issuer"]:
            lines.append(_("Issued by: {name}").format(name=certificate["issuer"]))
        if certificate["expires"]:
            lines.append(_("Valid until: {date}").format(date=fmt_datetime(certificate["expires"])))
        lines.append(_("SHA-256 fingerprint:") + "\n" + certificate["fingerprint"])
        dialog.set_extra_child(label("\n".join(lines), ("omp-small",), wrap=True))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trust", _("Trust certificate"))
        dialog.set_response_appearance("trust", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")  # trusting takes a deliberate click
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response == "trust":
                self._run_nextcloud_flow(error.server, certificate["sha256"])

        dialog.connect("response", on_response)
        dialog.present(self)

    def _ask_plain_http(self, error):
        host = host_of(error.server)
        if not error.private:
            dialog = Adw.AlertDialog(
                heading=_("No encrypted connection"),
                body=_("{host} answers only without encryption (http). Over the internet, your app password and "
                       "listening history could be read along the way, so omaPOD logs in only with https there. "
                       "Set up https on the Nextcloud, or reach it through your home network or VPN.").format(
                    host=host))
            dialog.add_response("ok", _("OK"))
            dialog.present(self)
            return
        dialog = Adw.AlertDialog(
            heading=_("Use an unencrypted connection?"),
            body=_("{host} answers only without encryption (http). It is in your local network or VPN, where this "
                   "can be acceptable, but your app password and listening history travel through it unencrypted. "
                   "Continue only if this is your server and your network.").format(host=host))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("plain", _("Use http"))
        dialog.set_response_appearance("plain", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: self._run_nextcloud_flow(error.server)
                       if response == "plain" else None)
        dialog.present(self)

    def _setup_gpodder(self):
        dialog = Adw.AlertDialog(heading="gpodder.net",
                                 body=_("Enter the credentials of your account on the synchronization server."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        server = Gtk.Entry(text="gpodder.net", placeholder_text=_("Server address"))
        user = Gtk.Entry(placeholder_text=_("Username"))
        password = Gtk.PasswordEntry(placeholder_text=_("Password"), show_peek_icon=True, activates_default=True)
        for widget in (server, user, password):
            box.append(widget)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Log in"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def on_response(_d, response):
            if response != "ok" or not user.get_text():
                return
            self._gpodder_devices(server.get_text().strip() or "gpodder.net", user.get_text().strip(),
                                  password.get_text())

        dialog.connect("response", on_response)
        dialog.present(self)

    def _gpodder_devices(self, server, username, password):
        def work():
            client = GpodderNet(server, username, password, default_device_id())
            client.login()
            return client.devices()

        def done(devices):
            dialog = Adw.AlertDialog(heading=_("Choose device"),
                                     body=_("Please select the device that you created on the synchronization server "
                                            "or create a new one."))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            group = None
            radios = []
            for device in devices:
                radio = Gtk.CheckButton(label=f"{device['caption']} ({device['id']})")
                if group is None:
                    group = radio
                else:
                    radio.set_group(group)
                radios.append((radio, device["id"]))
                box.append(radio)
            new = Gtk.CheckButton(label=_("Create device"))
            if group is not None:
                new.set_group(group)
            new.set_active(True)
            box.append(new)
            name = Gtk.Entry(text=f"{APP_NAME} on {default_device_id().removeprefix('omapod-')}")
            box.append(name)
            dialog.set_extra_child(box)
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("ok", _("Continue"))
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

            def on_response(_d, response):
                if response != "ok":
                    return
                chosen = next((device_id for radio, device_id in radios if radio.get_active()), None)
                if chosen is None:
                    caption = name.get_text().strip() or APP_NAME
                    chosen = default_device_id()

                    def create():
                        client = GpodderNet(server, username, password, chosen)
                        client.login()
                        client.configure_device(chosen, caption, "laptop")
                        return chosen

                    from .app import run_in_thread

                    run_in_thread(create, lambda device: self._finish_login("gpodder", normalize_host(server), username,
                                                                            password, device),
                                  lambda error: self.toast(str(error), 6))
                else:
                    self._finish_login("gpodder", normalize_host(server), username, password, chosen)

            dialog.connect("response", on_response)
            dialog.present(self)

        from .app import run_in_thread

        run_in_thread(work, done, lambda error: self.toast(_("Login failed: {error}").format(error=error), 6))

    # ------------------------------------------------------------ files

    def _save_dialog(self, title, name, callback):
        dialog = Gtk.FileDialog(title=title, initial_name=name)

        def done(dlg, result):
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return
            if file is not None:
                try:
                    callback(file.get_path())
                    self.toast(_("Export successful"))
                except OSError as error:
                    self.toast(_("Export error: {error}").format(error=error), 6)

        dialog.save(self.app.window, None, done)

    def _export_db(self):
        self._save_dialog(_("Database export"), time.strftime("omapod-backup-%Y-%m-%d.db"),
                          lambda path: export_database(self.app, path))

    def _import_db(self):
        dialog = Gtk.FileDialog(title=_("Database import"))

        def done(dlg, result):
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            path = file.get_path() if file else None
            if not path:
                return
            problem = check_import(path)
            if problem:
                self.toast(problem, 6)
                return
            confirm = Adw.AlertDialog(heading=_("Database import"),
                                      body=_("Importing a database will replace all of your current subscriptions "
                                             "and playing history. A copy of the current database is kept next to it. "
                                             "omaPOD restarts afterwards."))
            confirm.add_response("cancel", _("Cancel"))
            confirm.add_response("ok", _("Replace and restart"))
            confirm.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(_d, response):
                if response != "ok":
                    return
                known = {episode.file_path for episode in self.app.db.get_episodes(("downloaded",))}
                try:
                    with private_file(pending_import_path()) as copy:
                        shutil.copyfile(path, copy)
                        problem = prepare_import(copy, known)
                        if problem:
                            raise ValueError(problem)
                except (OSError, ValueError) as error:
                    self.toast(str(error), 6)
                    return
                restart_app(self.app)

            confirm.connect("response", on_response)
            confirm.present(self)

        dialog.open(self.app.window, None, done)

    def _export_opml(self):
        from ..feeds.opml import build_opml

        feeds = [f for f in self.app.db.get_feeds(with_counts=False) if not f.is_local]

        self._save_dialog(_("OPML export"), "omapod-subscriptions.opml",
                          lambda path: write_export(path, build_opml(feeds)))

    def _import_opml(self):
        page = self.app._page("add")
        self.close()
        page._choose_opml()

    def _export_html(self):
        from ..feeds.opml import build_html_export

        feeds = [f for f in self.app.db.get_feeds(with_counts=False) if not f.is_local]

        self._save_dialog(_("HTML export"), "omapod-subscriptions.html",
                          lambda path: write_export(path, build_html_export(feeds)))

    def _export_favorites(self):
        import html

        episodes = self.app.db.get_episodes(("favorite",), sort="feed_asc")

        def link(episode):
            url = web_url(episode.link) or web_url(episode.media_url)  # feeds can name javascript: or file: ones
            title = html.escape(episode.title)
            return f'<a href="{html.escape(url)}">{title}</a>' if url else title

        def write(path):
            items = "".join(f'<li>{link(e)} <span>{html.escape(e.feed_title)}</span></li>' for e in episodes)
            write_export(path, "<!doctype html><html><head><meta charset=\"utf-8\"><title>Favorites</title>"
                               "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;"
                               "padding:0 16px}li{margin:8px 0}span{color:#666}</style></head><body>"
                               f"<h1>Favorite episodes</h1><ul>{items}</ul></body></html>\n")

        self._save_dialog(_("Favorites export"), "omapod-favorites.html", write)

    def _choose_folder(self):
        dialog = Gtk.FileDialog(title=_("Choose data folder"))

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            path = folder.get_path()

            def work():
                return self.app.library.move_downloads(path)

            from .app import run_in_thread

            run_in_thread(work, lambda moved: (self.folder_row.set_subtitle(path), self.toast(
                ngettext("{n} download moved", "{n} downloads moved", moved).format(n=moved))))

        dialog.select_folder(self.app.window, None, done)

    # ------------------------------------------------------------ misc

    def _on_parental(self, row, _pspec):
        if not row.get_active():
            if self.settings.get("parental_enabled"):
                from .dialogs import check_parental

                check_parental(self.app, lambda: self.settings.set("parental_enabled", False))
                row.set_active(True)
            return
        if self.settings.get("parental_enabled"):
            return
        dialog = Adw.AlertDialog(heading=_("Set Password"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        first = Gtk.PasswordEntry(placeholder_text=_("Password"), show_peek_icon=True)
        second = Gtk.PasswordEntry(placeholder_text=_("Confirm password"), show_peek_icon=True)
        box.append(first)
        box.append(second)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Enable"))

        def on_response(_d, response):
            if response == "ok" and first.get_text() and first.get_text() == second.get_text():
                self.settings.update({"parental_password_hash": password_hash(first.get_text()),
                                      "parental_enabled": True})
            else:
                if response == "ok":
                    self.toast(_("Passwords don't match") if first.get_text() else _("Password cannot be empty"))
                row.set_active(False)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _reset_stats(self):
        from .widgets.common import confirm

        confirm(self, _("Reset statistics data?"),
                _("This will erase the history of duration played for all episodes."), _("Reset"),
                self.app.library.reset_statistics)


def shortcuts_dialog(app):
    entries = [
        (_("Playback"), [("<Control>space", _("Play or pause (Space when no field has focus)")),
                         ("<Control>Left", _("Rewind (J)")), ("<Control>Right", _("Fast-forward (L)")),
                         ("<Control><Shift>Right", _("Skip episode")), ("<Control>Up", _("Faster")),
                         ("<Control>Down", _("Slower")), ("<Control>p", _("Now playing"))]),
        (_("Navigation"), [("<Alt>1", _("Home")), ("<Alt>2", _("Queue")), ("<Alt>3", _("Inbox")),
                           ("<Alt>4", _("Episodes")), ("<Alt>5", _("Subscriptions")), ("<Alt>6", _("Downloads")),
                           ("<Control>f", _("Search")), ("<Control>n", _("Add podcast")),
                           ("F9", _("Show or hide the navigation")), ("Escape", _("Back"))]),
        (_("General"), [("<Control>r", _("Refresh podcasts")), ("<Control>comma", _("Settings")),
                        ("<Control>question", _("Keyboard shortcuts")), ("<Control>q", _("Quit"))]),
    ]
    if hasattr(Adw, "ShortcutsDialog"):
        dialog = Adw.ShortcutsDialog()
        for title, items in entries:
            section = Adw.ShortcutsSection(title=title)
            for accel, text in items:
                section.add(Adw.ShortcutsItem(title=text, accelerator=accel))
            dialog.add(section)
        return dialog
    dialog = Adw.Dialog(title=_("Keyboard shortcuts"))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    for title, items in entries:
        box.append(label(title, ("heading",)))
        for accel, text in items:
            row = Gtk.Box(spacing=12)
            row.append(Gtk.ShortcutLabel(accelerator=accel))
            row.append(label(text))
            box.append(row)
    dialog.set_child(box)
    return dialog
