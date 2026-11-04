"""The player dock on its own, with a stand-in for the app."""

import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk  # noqa: E402

from omapod.config import DEFAULTS  # noqa: E402

MOON = "weather-clear-night-symbolic"


class FakeSettings:
    def get(self, key):
        return DEFAULTS.get(key)

    def connect(self, _listener):
        pass


class FakePlayer:
    sleep = None
    volume = 100


class FakeTheme:
    values = {}

    def color(self, _key):
        return "#888888"


class FakeTextures:
    def get(self, _url, _size, _ready):
        return None


class FakeApp:
    def __init__(self):
        self.settings = FakeSettings()
        self.player = FakePlayer()
        self.theme = FakeTheme()
        self.textures = FakeTextures()

    def show_page(self, _name):
        pass

    def show_now_playing(self):
        pass


def state(sleep):
    return {"episode": {"title": "Episode", "podcast": "Podcast", "image": "", "feed_image": ""}, "playing": True,
            "chapter": "", "chapter_index": -1, "chapters": [], "rewind_secs": 10, "forward_secs": 30, "speed": 1.0,
            "sleep": sleep, "volume": 100, "position": 60_000, "duration": 3_600_000}


@unittest.skipIf(Gdk.Display.get_default() is None, "needs a display")
class SleepButtonTest(unittest.TestCase):
    def setUp(self):
        Adw.init()
        from omapod.ui.widgets.dock import PlayerDock

        self.dock = PlayerDock(FakeApp())

    def test_keeps_its_moon_while_the_timer_runs(self):
        self.dock._on_state(state({"mode": "time", "remaining": 1_800_000, "episodes": 0}))
        self.assertEqual(self.dock.sleep.get_icon_name(), MOON)
        self.assertIsNone(self.dock.sleep.get_label())
        self.assertIn("omp-sleep-active", self.dock.sleep.get_css_classes())

    def test_has_its_moon_after_the_timer(self):
        self.dock._on_state(state({"mode": "time", "remaining": 1_800_000, "episodes": 0}))
        self.dock._on_state(state({"mode": "episodes", "remaining": 0, "episodes": 1}))
        self.dock._on_state(state(None))
        self.assertEqual(self.dock.sleep.get_icon_name(), MOON)
        self.assertNotIn("omp-sleep-active", self.dock.sleep.get_css_classes())


if __name__ == "__main__":
    unittest.main()
