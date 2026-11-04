"""Walks the real app through every screen in the smallest windows; test_tiling.py reads what GTK says about it."""

import os
import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

from omapod.ui.app import OmaPodApp

SIZES = ((360, 420), (761, 420))  # the smallest window, and the narrowest one with the rail next to the page
PAGES = ("home", "queue", "inbox", "episodes", "favorites", "subscriptions", "downloads", "history", "statistics",
         "add", "search")
TABS = ("months", "downloads", "subscriptions")
LONG = "A title far too long for any row, with Donaudampfschifffahrtsgesellschaftskapitaen in the middle of it"


def add_library(app):
    with app.db.tx() as conn:
        feed = app.db.insert_feed(conn, {"url": "http://x.invalid/feed", "title": LONG, "state": 1})
    for number in range(3):
        app.db.execute("INSERT INTO episodes (feed_id, guid, title, duration, pub_date) VALUES (?, ?, ?, ?, ?)",
                       (feed, f"episode-{number}", LONG, 3_600_000, 1_700_000_000 + number))
    episode = app.db.get_episodes(limit=1)[0]
    app.db.execute("INSERT INTO listen_log(day, episode_id, feed_id, ms) VALUES(?, ?, ?, ?)",
                   (date.today().isoformat(), episode.id, feed, 3_600_000))
    return feed, episode.id


def resize(window, width, height):
    window.set_default_size(width, height)
    yield 200
    frame = width - window.get_width()  # without a compositor GTK draws a frame of its own around the window
    window.set_default_size(width + frame, height + frame)
    yield 200


def rail_pages(app):
    for name in PAGES:
        app.show_page(name)
        yield 60
        if name == "statistics":
            for tab in TABS:
                app.window.root_shown.stack.set_visible_child_name(tab)
                yield 60


def pushed_pages(app, feed, episode):
    for show in (lambda: app.show_feed(feed), lambda: app.show_episode(episode), app.show_now_playing):
        show()
        yield 60
        app.window.nav.pop()
        yield 60


def walk(app):
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    yield 1000
    window = app.window
    for width, height in SIZES:
        yield from resize(window, width, height)
        yield from rail_pages(app)
    feed, episode = add_library(app)
    app.player.play_episode(episode, start=False)
    yield 300
    for width, height in SIZES:
        yield from resize(window, width, height)
        yield from rail_pages(app)
        yield from pushed_pages(app, feed, episode)
    app.show_page("subscriptions")
    yield 60
    tile = window.root_shown._tiles[feed]
    if tile.get_width() > tile.cover.get_width() + 1:
        print("a tile is wider than its cover", flush=True)
    print("walked", flush=True)
    os._exit(0)


def advance(steps):
    try:
        GLib.timeout_add(next(steps), advance, steps)
    except Exception:
        traceback.print_exc()  # without this the app would just stay open
        os._exit(1)
    return False


app = OmaPodApp()
app.connect("activate", lambda _app: advance(walk(app)))
app.run([])
