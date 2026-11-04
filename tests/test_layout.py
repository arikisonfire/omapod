"""The repository is the plugin the Omarchy marketplace installs: manifest at the top, the widget in plugin/, the
app next to it. And an update that lands under a running app must not mix two versions of it."""

import json
import pkgutil
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PluginLayout(unittest.TestCase):
    def test_the_repository_is_the_plugin(self):
        from omapod import VERSION

        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "io.github.arikisonfire.pod")
        self.assertEqual(manifest["version"], VERSION)
        self.assertLessEqual(len(manifest["description"]), 500)  # the marketplace shows only this text
        for entry in manifest["entryPoints"].values():
            self.assertFalse(entry.startswith("/") or ".." in entry, entry)  # the shell's rule for entry points
            self.assertTrue((ROOT / entry).is_file(), entry)
        for name in ("README.md", "LICENSE"):  # required at the top of a marketplace repository
            self.assertTrue((ROOT / name).is_file(), name)

    def test_the_widget_finds_the_app_one_folder_up(self):
        widget = ROOT / json.loads((ROOT / "manifest.json").read_text())["entryPoints"]["barWidget"]
        # BarWidget.qml looks for <its folder>/../omapod/__main__.py.
        self.assertTrue((widget.parent.parent / "omapod" / "__main__.py").is_file())
        self.assertIn("$1/omapod/__main__.py", widget.read_text())


class OneVersionWhileRunning(unittest.TestCase):
    def setUp(self):
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
            from gi.repository import Adw, Gtk  # noqa: F401
        except (ImportError, ValueError):
            self.skipTest("needs GTK 4 with libadwaita")

    def test_every_module_is_loaded_at_the_start(self):
        import omapod
        from omapod.ui.app import preload_modules

        preload_modules()
        names = {info.name for info in pkgutil.walk_packages(omapod.__path__, "omapod.")} - {"omapod.__main__"}
        self.assertEqual(names - set(sys.modules), set())

    def test_the_version_on_disk_is_read_from_the_package(self):
        from omapod import VERSION
        from omapod.ui.app import version_on_disk

        self.assertEqual(version_on_disk(), VERSION)
        self.assertRegex(VERSION, re.compile(r"^\d+\.\d+\.\d+"))


if __name__ == "__main__":
    unittest.main()
