"""Command line entry point.

  omapod                      start (or raise) the app
  omapod URL|FILE             open a podcast link (podcast://, feed URLs) or an OPML file
  omapod ctl COMMAND [ARG]    control a running omaPOD (toggle, play, pause, rewind, forward,
                              skip, seek SECONDS, speed VALUE, sleep MINUTES, sleep-off, show, state, quit)
  omapod --refresh            refresh podcasts, auto-download and sync without a window
"""

import json
import os
import sys
import time


def _apply_pending_import():
    """Swap in a database chosen for import, before anything opens the database."""
    from .config import paths

    pending = str(paths.db) + ".import"
    if not os.path.exists(pending):
        return
    if os.path.exists(paths.db):
        import sqlite3

        from .config import private_file

        backup = str(paths.db) + time.strftime(".before-import-%Y%m%d-%H%M%S")
        # Private from its first byte, like every copy of the library (config.private_file).
        with private_file(backup) as path:
            try:
                # Through SQLite, so the copy includes what is still in the write-ahead log (deleted below).
                current, copy = sqlite3.connect(paths.db), sqlite3.connect(path)
                try:
                    current.backup(copy)
                finally:
                    copy.close()
                    current.close()
            except sqlite3.Error:
                import shutil

                shutil.copyfile(paths.db, path)
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(str(paths.db) + suffix)
        except FileNotFoundError:
            pass
    os.replace(pending, paths.db)


def _ctl_payload(command, value):
    payload = {}
    if command == "seek" and value is not None:
        if value.startswith(("+", "-")):
            command, payload = "seek_relative", {"delta": int(float(value) * 1000)}
        else:
            payload = {"position": int(float(value) * 1000)}
    elif command == "speed" and value is not None:
        payload = {"value": float(value)}
    elif command == "volume" and value is not None:
        payload = {"value": int(value)}
    elif command == "sleep" and value is not None:
        payload = {"minutes": float(value)}
    elif command == "sleep_off":
        command = "sleep_cancel"
    elif command == "play_episode" and value is not None:
        payload = {"id": int(value)}
    elif command == "show" and value is not None:
        payload = {"page": value}
    return command, payload


def _ctl(args):
    from .config import paths
    from .player.ipc import send_command

    if not args:
        print(__doc__.strip())
        return 2
    value = args[1] if len(args) > 1 else None
    try:
        command, payload = _ctl_payload(args[0].replace("-", "_"), value)
    except (ValueError, OverflowError):
        print(f"omaPOD: {value!r} is not a valid value for {args[0]}", file=sys.stderr)
        return 2
    try:
        reply, state = send_command(paths.control_socket, command, **payload)
    except OSError:
        if command in ("show", "toggle", "play", "now_playing"):
            return None  # start the app instead
        print("omaPOD is not running.", file=sys.stderr)
        return 1
    if command == "state":
        print(json.dumps(state, indent=2, ensure_ascii=False))
    if not reply.get("ok"):
        print(reply.get("error") or "failed", file=sys.stderr)
        return 1
    return 0


def _headless_refresh():
    import signal

    from .config import credentials, instance_lock, move_secrets, paths, settings
    from .core.library import Library
    from .db import Database
    from .i18n import set_language
    from .net.downloads import DownloadManager
    from .sync.service import SyncService

    paths.ensure()
    lock = instance_lock("refresh")
    if lock is None:
        print("omaPOD: the app or another refresh is running; nothing to do")
        return 0
    store = settings()
    move_secrets(store, credentials())
    set_language(store.get("language"))
    db = Database(paths.db)
    library = Library(db, store)
    library.forget_unused_folders()
    downloads = DownloadManager(library, store)
    library.downloads = downloads
    downloads.start()

    def stop(_signum, _frame):
        # The app is starting and takes over: podcasts not refreshed yet and running downloads are left to it.
        library.stopping = True
        downloads.stop()

    signal.signal(signal.SIGTERM, stop)
    sync = SyncService(library, store, credentials())  # run() reports its own errors and never raises
    if store.get("sync_provider"):
        sync.run()
    results = library.refresh_all(force=False)
    new = sum(len(r.new_ids) for r in results or [])
    failed = sum(1 for r in results or [] if r.error)
    while downloads.active_ids() and not library.stopping:
        time.sleep(1)
    if store.get("sync_provider") and not library.stopping:
        sync.run()
    downloads.stop()
    print(f"omaPOD: {len(results or [])} podcasts refreshed, {new} new episodes, {failed} failed")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv and argv[0] in ("-v", "--version"):
        from . import VERSION

        print(f"omaPOD {VERSION}")
        return 0
    if argv and argv[0] == "ctl":
        result = _ctl(argv[1:])
        if result is not None:
            return result
        argv = []
    _apply_pending_import()
    if argv and argv[0] == "--refresh":
        # A running omaPOD refreshes itself; only work headless when it is closed.
        from .config import paths
        from .player.ipc import send_command

        try:
            send_command(paths.control_socket, "refresh")
            return 0
        except OSError:
            return _headless_refresh()
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib

    from . import APP_ID

    GLib.set_prgname(APP_ID)
    from .ui.app import OmaPodApp

    app = OmaPodApp()
    code = app.run([sys.argv[0]] + argv)
    if app.restart_requested:
        # Only now, after do_shutdown stopped mpv and closed the database: a database import is swapped in at the
        # start (see _apply_pending_import).
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
        os.execve(sys.executable, [sys.executable, "-P", "-m", "omapod"], env)  # -P: as the launchers start it
    return code


if __name__ == "__main__":
    sys.exit(main())
