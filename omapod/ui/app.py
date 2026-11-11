"""The application object. It owns every service and is the `ctx` that
pages and widgets use to reach the library, the player and navigation."""

import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_ID, APP_NAME, VERSION
from ..config import credentials, instance_lock, move_secrets, paths, settings as load_settings
from ..core.images import ImageCache, TextureCache
from ..core.library import AuthRequired, Library
from ..db import Database
from ..events import bus
from ..feeds.parser import FeedParseError, NotAFeedError
from ..i18n import _, ngettext, set_language
from ..net import http
from ..net.downloads import DownloadManager
from ..player.controller import PlayerController
from ..player.ipc import ControlServer
from ..sync.service import SyncService
from ..util import now_s

ROOT_PAGES = ("home", "queue", "inbox", "episodes", "favorites", "subscriptions", "downloads", "history",
              "statistics", "add", "search")
# Ready before their first click, because building a page takes longer than opening it.
PREPARED_PAGES = ("home", "queue", "inbox", "episodes", "favorites", "subscriptions", "downloads", "history",
                  "statistics")
PREPARE_AFTER_MS = 300
SUBSCRIBE_SCHEMES = ("podcast://", "itpc://", "pcast://", "feed://", "antennapod-subscribe://")


def preload_modules():
    """Imports every module of omaPOD. From the plugin marketplace, `omarchy plugin update` replaces the files while
    the app may be running, and a module imported later (a dialog, say) would come from the new version and meet
    the old ones. Loaded at the start, the app stays one version until it starts again."""
    import importlib
    import pkgutil

    import omapod

    for module in pkgutil.walk_packages(omapod.__path__, "omapod."):
        if module.name == "omapod.__main__":
            continue
        try:
            importlib.import_module(module.name)
        except Exception:  # it fails the same way when it is used, and says so then
            traceback.print_exc()


def version_on_disk():
    """The version of the files installed now, which an update can change under the running app; "" if unknown."""
    try:
        text = (Path(__file__).resolve().parent.parent / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    return match.group(1) if match else ""


def session_bus_available():
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return bool(runtime) and os.path.exists(os.path.join(runtime, "bus"))


def run_in_thread(work, done=None, error=None):
    """Run work() in a thread; call done(result) or error(exc) on the main loop."""

    def runner():
        try:
            result = work()
        except Exception as exc:  # handed to the caller
            if error is not None:
                # Bind exc now: Python unbinds it when the except block ends, before the callback runs.
                GLib.idle_add(lambda exc=exc: (error(exc), False)[1])
            else:
                import traceback

                traceback.print_exc()
            return
        if done is not None:
            GLib.idle_add(lambda: (done(result), False)[1])

    threading.Thread(target=runner, daemon=True).start()


class OmaPodApp(Adw.Application):
    ROOT_PAGES = ROOT_PAGES

    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        self.has_bus = session_bus_available()
        if not self.has_bus:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None
        self.pages = {}
        self._held = False
        self._sync_pending = 0
        self._push_pending = 0
        self._quitting = False
        self._update_noticed = False
        self.restart_requested = False  # see preferences.restart_app
        self.ready = False
        GLib.set_application_name(APP_NAME)

    # ------------------------------------------------------------ lifecycle

    def do_startup(self):
        Adw.Application.do_startup(self)
        bus.attach_main_loop()
        paths.ensure()
        self._take_instance_lock()
        self.settings = load_settings()
        set_language(self.settings.get("language"))
        self.credentials = credentials()
        move_secrets(self.settings, self.credentials)
        self.db = Database(paths.db)
        self.library = Library(self.db, self.settings)
        self.library.forget_unused_folders()  # after a database import, too (see __main__._apply_pending_import)
        self.images = ImageCache(paths.images)
        self.textures = TextureCache(self.images)
        self.downloads = DownloadManager(self.library, self.settings)
        self.library.downloads = self.downloads
        self.downloads.start()
        self.player = PlayerController(self.library, self.settings, paths)
        self.library.player = self.player
        from .theme import ThemeManager

        self.theme = ThemeManager()
        self.theme.start()
        display = Gdk.Display.get_default()
        if display is not None:
            icons = Path(__file__).resolve().parent.parent / "data" / "icons"
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
        self.sync = SyncService(self.library, self.settings, self.credentials)
        self.player.sync_check = self._sync_before_play
        self.mpris = None
        if self.has_bus:
            from ..player.mpris import Mpris

            self.mpris = Mpris(self, self.player, self.images)
            self.mpris.start()
        self.control = ControlServer(self, self.player, self.library, self.images, paths.control_socket)
        self.control.start()
        self._install_app_actions()
        bus.subscribe("message", self._on_message)
        bus.subscribe("new-episodes", self._on_new_episodes)
        bus.subscribe("download-error", self._on_download_error)
        bus.subscribe("sync-error", self._on_sync_error)
        bus.subscribe("refresh-errors", self._on_refresh_errors)
        bus.subscribe("auth-required", self._on_auth_required)
        bus.subscribe("sync-requested", self._schedule_sync)
        bus.subscribe("sync-push", self._schedule_push)
        bus.subscribe("remote-playback", self.player.adopt_remote)
        bus.subscribe("remote-follow", self.player.follow_remote)
        self.player.restore_last()
        self.library.cleanup_previews()
        GLib.timeout_add_seconds(3, self._startup_tasks)
        GLib.timeout_add_seconds(60, self._periodic)
        import signal

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)
        self.ready = True

    def do_activate(self):
        if self.window is None:
            from .window import MainWindow

            self.window = MainWindow(self)
            self.window.connect("notify::is-active", self._on_window_active)
            start = self.settings.get("default_page")
            if start == "remember":
                start = self.settings.get("last_page") or "home"
            self.show_page(start if start in ROOT_PAGES else "home")
            self.player._emit_state()
            # With the window on screen, so that it does not open any later.
            GLib.timeout_add(PREPARE_AFTER_MS, self._start_preparing)
        self.window.present()
        if self._held:
            self.release()
            self._held = False

    def do_open(self, files, _count, _hint):
        self.activate()
        for file in files:
            uri = file.get_uri() or ""
            path = file.get_path()
            if path and path.lower().endswith((".opml", ".xml")) and os.path.exists(path):
                from .pages.add import import_opml_file

                import_opml_file(self, path)
            else:
                self.subscribe_from_uri(uri)

    def do_shutdown(self):
        try:
            self.player.shutdown()
            # The position just saved goes to the server before omaPOD is gone (the window is closed by now).
            if self.settings.get("sync_provider"):
                self.sync.push(timeout=4)
            self.downloads.stop()
            self.control.stop()
            if self.mpris is not None:
                self.mpris.stop()
        finally:
            self.db.close()
            Adw.Application.do_shutdown(self)

    def _on_signal(self):
        # Save positions and remove the socket and presence file, then leave.
        self.quit()
        return GLib.SOURCE_REMOVE

    def quit(self):
        """Ctrl+Q, the bar widget, MPRIS, signals. The window goes at once; what was just heard is uploaded first, so
        the phone can continue right there."""
        if self._quitting:
            return
        self._quitting = True
        if self.player.plays_here():
            self.player.pause()  # saves the position and queues it for the server
        if self.window is not None:
            self.window.set_visible(False)
        if self.settings.get("sync_provider") and self.db.sync_pending("action"):
            def leave(*_args):
                Adw.Application.quit(self)
                return False

            run_in_thread(lambda: self.sync.push(timeout=4), leave, leave)
            GLib.timeout_add_seconds(10, leave)  # an unreachable server does not keep omaPOD around
            return
        Adw.Application.quit(self)

    def on_window_close(self):
        """Keep running in the background while something is going on."""
        busy = self.player.status in ("playing", "loading") or self.downloads.active_ids()
        if busy:
            self.window.set_visible(False)
            if not self._held:
                self.hold()
                self._held = True
            self.notify(_("omaPOD keeps playing"), _("Open it again from the bar widget or the app launcher."),
                        "background")
            return True
        return False

    def window_visible(self):
        return self.window is not None and self.window.get_visible()

    def _take_instance_lock(self, attempts=120):
        """Keeps the hourly background refresh away while the app runs. One that is running right now (the timer
        fires at login, too) is asked to stop; the lock is tried again for a minute, until it is ours."""
        self._instance_lock = instance_lock("app", stop_refresh=True)
        if self._instance_lock is None and attempts > 1:
            GLib.timeout_add(500, self._take_instance_lock, attempts - 1)
        return False

    # ------------------------------------------------------------ periodic work

    def _startup_tasks(self):
        preload_modules()
        if self._refresh_due():
            self.refresh_all(only_due=False)
        # The window coming up has usually started one already.
        if self.settings.get("sync_provider") and now_s() - int(self.settings.get("sync_last_attempt") or 0) >= 30:
            self.sync_now()
        self._auto_backup()
        return False

    def _refresh_due(self):
        interval = int(self.settings.get("refresh_interval_min") or 0)
        if interval <= 0:
            return False
        last = int(self.db.get_meta("last_refresh_all", "0") or 0)
        return now_s() - last >= interval * 60

    def _periodic(self):
        if self._refresh_due() and not self.library.refreshing:
            self.refresh_all()
        if self.settings.get("sync_provider"):
            last = int(self.settings.get("sync_last_attempt") or 0)
            if now_s() - last >= 30 * 60:
                self.sync_now()
            elif self.sync.poll_due() and not self.player.plays_here():
                # Where the phone stopped shows up in the player and the bar without a full sync.
                run_in_thread(self.sync.pull)
        return True

    def _on_window_active(self, window, _pspec):
        if not window.is_active():
            return
        self._check_for_update()
        # Back at the computer: fetch what happened on the other devices meanwhile.
        if self.settings.get("sync_provider") and now_s() - int(self.settings.get("sync_last_attempt") or 0) >= 120:
            self.sync_now()

    def _check_for_update(self):
        """Says once that a newer omaPOD was installed since this one started (omarchy plugin update)."""
        installed = version_on_disk()
        if installed and installed != VERSION and not self._update_noticed:
            self._update_noticed = True
            self.toast(_("omaPOD {version} is installed. Quit and start omaPOD again to use it.").format(
                version=installed), timeout=10)

    def _sync_before_play(self, done, follow):
        """Asked by the player before playback starts: fetch where other devices stopped first (switching from
        the phone mid-episode). Returns False when there is nothing to wait for."""
        if not self.sync.pull_due():
            return False
        run_in_thread(lambda: self.sync.pull(follow=follow), lambda _ok: done(), lambda _error: done())
        return True

    def refresh_all(self, only_due=False, force=False):
        if self.library.refreshing:
            return

        def work():
            return self.library.refresh_all(force=force, only_due=only_due)

        def done(results):
            new = sum(len(r.new_ids) for r in results or [])
            if new:
                self.toast(ngettext("{n} new episode", "{n} new episodes", new).format(n=new))

        run_in_thread(work, done)

    def sync_now(self, full=False):
        if not self.settings.get("sync_provider"):
            return
        run_in_thread(lambda: self.sync.run(full=full))

    def _schedule_sync(self):
        if not self.settings.get("sync_provider"):
            return
        if self._sync_pending:
            GLib.source_remove(self._sync_pending)
        self._sync_pending = GLib.timeout_add_seconds(20, self._run_scheduled_sync)

    def _run_scheduled_sync(self):
        self._sync_pending = 0
        self.sync_now()
        return False

    def _schedule_push(self):
        # A moment's grace collects a burst (marking many episodes as played).
        if not self.settings.get("sync_provider"):
            return
        if self._push_pending:
            GLib.source_remove(self._push_pending)
        self._push_pending = GLib.timeout_add_seconds(2, self._run_push)

    def _run_push(self):
        self._push_pending = 0
        run_in_thread(self.sync.push)
        return False

    def _auto_backup(self):
        if not self.settings.get("auto_db_export"):
            return
        if now_s() - int(self.settings.get("auto_db_export_last") or 0) < 3 * 86400:
            return
        from .preferences import export_database

        folder = paths.backups
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / time.strftime("omapod-backup-%Y-%m-%d.db")
        try:
            export_database(self, str(target))
            backups = sorted(folder.glob("omapod-backup-*.db"))
            for old in backups[:-5]:
                old.unlink()
            self.settings.set("auto_db_export_last", now_s())
        except OSError as error:
            self.toast(_("Automatic database backup failed: {error}").format(error=error))

    # ------------------------------------------------------------ navigation

    def _page(self, name):
        page = self.pages.get(name)
        if page is not None:
            return page
        from .pages import lists

        factories = {
            "queue": lists.QueuePage,
            "inbox": lists.InboxPage,
            "episodes": lists.AllEpisodesPage,
            "favorites": lists.FavoritesPage,
            "downloads": lists.DownloadsPage,
            "history": lists.HistoryPage,
        }
        if name in factories:
            page = factories[name](self)
        elif name == "home":
            from .pages.home import HomePage

            page = HomePage(self)
        elif name == "subscriptions":
            from .pages.subscriptions import SubscriptionsPage

            page = SubscriptionsPage(self)
        elif name == "statistics":
            from .pages.stats import StatisticsPage

            page = StatisticsPage(self)
        elif name == "add":
            from .pages.add import AddPage

            page = AddPage(self)
        elif name == "search":
            from .pages.search import SearchPage

            page = SearchPage(self)
        else:
            return None
        self.pages[name] = page
        # It stays in the widget tree when another page opens. Out of the tree, its lists would lose all their rows
        # and build them anew each time.
        self.window.add_root(page)
        page.keep_in_tree()
        return page

    def _prepare_pages(self):
        """The pages not opened yet, one step at a time, for the idle loop."""
        for name in PREPARED_PAGES:
            if name not in self.pages:
                page = self._page(name)
                yield
                yield from page.prepare()

    def _start_preparing(self):
        steps = self._prepare_pages()
        # Until the window is closed: drawing and input go first, each step yields to them.
        GLib.idle_add(lambda: self.window.get_realized() and next(steps, False) is None,
                      priority=GLib.PRIORITY_LOW)
        return False

    def revision(self):
        """Differs whenever what a page shows may differ. The day counts: lists say 'Today' and 'Yesterday'."""
        return self.db.revision(), self.settings.revision, date.today()

    def show_page(self, name):
        if self.window is None:
            self.activate()
        if name == "now-playing":
            self.show_now_playing()
            return
        kind, _sep, value = name.partition(":")
        if kind == "feed" and value.isdigit():
            self.show_feed(int(value))
            return
        if kind == "episode" and value.isdigit():
            self.show_episode(int(value))
            return
        if kind == "preferences":
            self.show_preferences(value or None)
            return
        page = self._page(name)
        if page is None:
            return
        self.window.show_root(name, page)
        self.settings.set("last_page", name)

    def show_feed(self, feed_id):
        from .pages.feed import FeedPage

        self.window.push(FeedPage(self, feed_id))
        self.window.rail.select_feed(feed_id)

    def show_episode(self, episode_id):
        from .pages.episode import EpisodePage

        self.window.push(EpisodePage(self, episode_id))

    def show_now_playing(self):
        if self.player.episode is None:
            return
        from .pages.nowplaying import NowPlayingPage

        self.window.push(NowPlayingPage(self))

    def show_preferences(self, page=None):
        from .preferences import Preferences

        dialog = Preferences(self)
        if page:
            dialog.set_visible_page_name(page)
        dialog.present(self.window)

    # ------------------------------------------------------------ actions

    def _install_app_actions(self):
        def add(name, callback, accels=None):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        add("quit", self.quit, ["<Control>q"])
        add("preferences", self.show_preferences)
        add("refresh", lambda: self.refresh_all(force=True))
        add("sync", lambda: self.sync_now())
        add("about", self.show_about)
        add("shortcuts", self.show_shortcuts, ["<Control>question"])

    def show_about(self):
        about = Adw.AboutDialog(application_name=APP_NAME, version=VERSION, developer_name="arikisonfire",
                                application_icon=APP_ID, website="https://github.com/arikisonfire/omapod",
                                license_type=Gtk.License.GPL_3_0,
                                comments=_("A podcast manager for Omarchy, modelled on AntennaPod."))
        about.add_credit_section(_("Based on the ideas of"), ["AntennaPod https://antennapod.org"])
        about.present(self.window)

    def show_shortcuts(self):
        from .preferences import shortcuts_dialog

        shortcuts_dialog(self).present(self.window)

    def play_episode(self, episode_id):
        episode = self.db.get_episode(episode_id, full=False)
        if episode is None:
            return
        if not episode.has_media:
            if episode.link:
                self.open_uri(episode.link)
            return
        self.player.play_episode(episode_id)

    def delete_downloads(self, ids):
        """Downloads go right away. Episodes from a local folder are the user's own files: they go to the trash,
        and only after asking."""
        local = [e for e in self.db.get_episodes_by_ids(ids) if e.is_local and e.is_downloaded]
        if not local:
            self.library.delete_downloads(ids)
            return
        from .widgets.common import confirm

        folders = sorted({os.path.dirname(e.file_path) for e in local})
        # The file's own name and folder, not the episode's title: that is what goes to the trash.
        body = ngettext("{name} is a file in a folder you added, not a download. omaPOD moves it to the trash.",
                        "{n} episodes are files in a folder you added, not downloads. omaPOD moves them to the "
                        "trash.", len(local)).format(name=os.path.basename(local[0].file_path), n=len(local))
        body += "\n\n" + "\n".join(folders[:3] + (["…"] if len(folders) > 3 else []))
        confirm(self.window, ngettext("Move the file to the trash?", "Move {n} files to the trash?",
                                      len(local)).format(n=len(local)),
                body, _("Move to Trash"), lambda: self.library.delete_downloads(ids, local_files=True))

    def episode_menu(self, episode):
        menu = Gio.Menu()
        target = GLib.Variant("x", episode.id)

        def item(section, text, action):
            entry = Gio.MenuItem.new(text, None)
            entry.set_action_and_target_value(f"win.{action}", target)
            section.append_item(entry)

        play = Gio.Menu()
        if self.player.current_id() == episode.id and self.player.status in ("playing", "loading"):
            item(play, _("Skip episode"), "ep-skip")
        elif episode.has_media:
            item(play, _("Play") if episode.is_downloaded else _("Stream"), "ep-play")
        item(play, _("Episode details"), "ep-details")
        item(play, _("Open podcast"), "ep-feed")
        menu.append_section(None, play)
        queue = Gio.Menu()
        if episode.in_queue:
            item(queue, _("Remove from queue"), "ep-unqueue")
            if not self.settings.get("queue_locked") and not self.settings.get("queue_keep_sorted"):
                item(queue, _("Move to top"), "ep-top")
                item(queue, _("Move to bottom"), "ep-bottom")
        elif episode.has_media:
            item(queue, _("Add to queue"), "ep-queue")
            item(queue, _("Play next"), "ep-queue-front")
        menu.append_section(None, queue)
        state = Gio.Menu()
        if episode.is_new:
            item(state, _("Remove from inbox"), "ep-inbox-remove")
        item(state, _("Mark as unplayed") if episode.is_played else _("Mark as played"),
             "ep-unplayed" if episode.is_played else "ep-played")
        item(state, _("Remove from favorites") if episode.is_favorite else _("Add to favorites"),
             "ep-unfavorite" if episode.is_favorite else "ep-favorite")
        if episode.position > 0:
            item(state, _("Reset playback position"), "ep-reset")
        if episode.completed_at:
            item(state, _("Remove from history"), "ep-history-remove")
        menu.append_section(None, state)
        files = Gio.Menu()
        if self.downloads.is_active(episode.id):
            item(files, _("Cancel download"), "ep-cancel")
        elif episode.is_downloaded:
            item(files, _("Delete download"), "ep-delete")
        elif episode.has_media and not episode.is_local:
            item(files, _("Download"), "ep-download")
        menu.append_section(None, files)
        share = Gio.Menu()
        if episode.social_url:
            item(share, _("Show comments"), "ep-comments")
        if episode.link:
            item(share, _("Visit website"), "ep-website")
            item(share, _("Copy episode link"), "ep-copy-link")
        if episode.media_url and not episode.is_local:
            item(share, _("Copy media address"), "ep-copy-media")
        menu.append_section(None, share)
        return menu

    # ------------------------------------------------------------ desktop integration

    def open_uri(self, uri):
        """Open a web or mailto: link in the default browser or mail app. Returns False if nothing was started.
        Links come from feeds, and other schemes would start whatever handles them (file:, smb:, apt: and the
        like), so those are only copied."""
        if not uri:
            return False
        if urlsplit(uri.strip()).scheme.lower() not in ("http", "https", "mailto"):
            if self.window is not None:
                self.window.get_clipboard().set(uri)
                self.toast(_("omaPOD opens only web links. This one was copied to the clipboard."))
            return False
        # Not Gtk.UriLauncher: on the host it goes through the desktop portal, which reports success
        # even when the browser it starts never shows up, so the fallback below never ran. GIO starts
        # the browser from our own session and falls back to the portal only in a Flatpak or Snap.
        display = self.window.get_display() if self.window is not None else Gdk.Display.get_default()
        context = display.get_app_launch_context() if display is not None else None
        try:
            if Gio.AppInfo.launch_default_for_uri(uri, context):
                return True
        except GLib.Error:
            pass
        return self._fallback_open(uri)

    def _fallback_open(self, uri):
        opener = shutil.which("xdg-open")
        if opener:
            try:
                subprocess.Popen([opener, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return True
            except OSError:
                pass
        self.copy_text(uri)
        self.toast(_("Could not open a browser. The address was copied to the clipboard."))
        return False

    def copy_text(self, text):
        if not text or self.window is None:
            return
        self.window.get_clipboard().set(text)
        self.toast(_("Copied to clipboard"))

    def toast(self, text, timeout=3, action_label=None, action=None):
        if self.window is None:
            return
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if action_label and action:
            toast.set_button_label(action_label)
            toast.connect("button-clicked", lambda _t: action())
        self.window.toasts.add_toast(toast)

    def notify(self, title, body, tag="omapod", icon=None):
        if not self.has_bus:
            return
        notification = Gio.Notification.new(title)
        notification.set_body(GLib.markup_escape_text(body))  # mako reads the body as Pango markup
        try:
            notification.set_icon(Gio.ThemedIcon.new(icon or APP_ID))
        except TypeError:
            pass
        self.send_notification(tag, notification)

    def _on_message(self, text, kind="info"):
        self.toast(text, timeout=5 if kind == "error" else 3)

    def _on_new_episodes(self, feed, episodes):
        count = len(episodes)
        title = ngettext("New episode", "New episodes", count)
        body = ngettext("{podcast} has a new episode", "{podcast} has {n} new episodes", count).format(
            podcast=feed.display_title, n=count)
        if count == 1 and episodes:
            body += f": {episodes[0].title}"
        self.notify(title, body, f"new-{feed.id}")

    def _on_download_error(self, episode, reason):
        text = _("Download of “{title}” failed. {reason}").format(title=episode.title, reason=reason)
        if self.window_visible():
            self.toast(text, timeout=6, action_label=_("Details"), action=lambda: self.show_page("downloads"))
        else:
            self.notify(_("Episode download failed"), text, f"download-{episode.id}")

    def _on_sync_error(self, message, auth):
        text = _("Synchronization failed: {error}").format(error=message)
        if self.window_visible():
            self.toast(text, timeout=8, action_label=_("Settings"),
                       action=lambda: self.show_preferences("sync"))
        else:
            self.notify(_("Synchronization failed"), message, "sync-error")

    def _on_refresh_errors(self, failed):
        count = len(failed)
        self.toast(ngettext("{n} podcast could not be refreshed", "{n} podcasts could not be refreshed",
                            count).format(n=count), timeout=6, action_label=_("Details"),
                   action=lambda: self.show_page("downloads"))

    def _on_auth_required(self, feed_id):
        feed = self.db.get_feed(feed_id)
        if feed is None or self.window is None:
            return
        from .dialogs import ask_credentials

        def retry(username, password):
            self.library.update_feed_settings(feed_id, username=username, password=password)
            run_in_thread(lambda: self.library.refresh_feed(feed_id, force=True))

        # The host the password goes to, not the title, which the feed chooses.
        ask_credentials(self.window, urlsplit(feed.url).hostname or feed.url, retry, feed.username,
                        plain=urlsplit(feed.url).scheme.lower() == "http")

    # ------------------------------------------------------------ subscribing

    def subscribe_from_uri(self, uri):
        """podcast://, itpc://, feed:// and plain http(s) feed links open as a preview."""
        url = uri
        for scheme in SUBSCRIBE_SCHEMES:
            if url.lower().startswith(scheme):
                rest = url[len(scheme):]
                if scheme == "antennapod-subscribe://":
                    from urllib.parse import unquote

                    rest = unquote(rest)
                url = rest if rest.startswith("http") else "https://" + rest
                break
        if not url.startswith(("http://", "https://")):
            return
        self.preview_url(url)

    def preview_url(self, url, username="", password="", on_done=None):
        from ..net.search import resolve_feed_url

        def work():
            feed_url = resolve_feed_url(url)
            return self.library.preview(feed_url, username, password)

        def done(feed_id):
            if on_done:
                on_done(feed_id)
            self.show_feed(feed_id)

        def failed(error):
            if on_done:
                on_done(None)
            if isinstance(error, AuthRequired):
                from .dialogs import ask_credentials

                # The address that asked, which a redirect may have led to: the dialog names its host, and the
                # password entered there goes only to that host (the podcast is subscribed at that address).
                asked = error.url or url
                ask_credentials(self.window, urlsplit(asked).hostname or asked,
                                lambda u, p: self.preview_url(asked, u, p, on_done),
                                plain=urlsplit(asked).scheme.lower() == "http")
            elif isinstance(error, NotAFeedError) and error.candidates:
                from .dialogs import choose_feed

                choose_feed(self.window, error.candidates, lambda chosen: self.preview_url(chosen))
            elif isinstance(error, NotAFeedError):
                self.toast(_("The address you entered is a website, not a podcast RSS address."), timeout=6)
            elif isinstance(error, FeedParseError):
                self.toast(_("The podcast feed is broken and could not be read."), timeout=6)
            elif isinstance(error, http.HttpError):
                self.toast(str(error), timeout=6)
            else:
                self.toast(_("Could not load the podcast: {error}").format(error=error), timeout=6)

        run_in_thread(work, done, failed)
