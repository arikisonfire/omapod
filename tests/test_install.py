"""The installer in a throwaway home: what it copies, what it removes, and that it leaves a marketplace clone of the
bar widget alone. Commands that would reach the real session (systemctl, xdg-mime, the desktop caches) are stubs:
run on a computer with omaPOD installed, systemctl --user would otherwise switch off its real timer."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ID = "io.github.arikisonfire.pod"
STUBS = ("systemctl", "xdg-mime", "update-desktop-database", "gtk-update-icon-cache")


def _what_install_checks_for():
    if not (shutil.which("mpv") and shutil.which("git") and shutil.which("python3")):
        return False
    probe = ("import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); "
             "from gi.repository import Adw, Gtk")
    return subprocess.run(["python3", "-c", probe], capture_output=True).returncode == 0


@unittest.skipUnless(_what_install_checks_for(), "needs mpv, git and GTK 4 with libadwaita, as install.sh does")
class InstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        stubs = Path(tmp.name) / "stubs"
        stubs.mkdir()
        for name in STUBS:
            stub = stubs / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"}
        self.plugin = self.home / ".config" / "omarchy" / "plugins" / PLUGIN_ID

    def install(self, *args, script=ROOT / "install.sh"):
        done = subprocess.run(["bash", str(script), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def marketplace_clone(self):
        """What omarchy plugin add leaves: a git clone of the whole repository, here with a change of the user's."""
        shutil.copytree(ROOT, self.plugin, ignore=shutil.ignore_patterns(
            ".git", ".dev", "__pycache__", ".ruff_cache", "tests", ".impeccable"))
        git = ["git", "-C", str(self.plugin), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "clone"]):
            subprocess.run(git + command, env=self.env, check=True, capture_output=True)
        widget = self.plugin / "plugin" / "BarWidget.qml"
        widget.write_text(widget.read_text() + "// changed by the user\n")

    def written_bytecode(self, folder):
        return [path for path in Path(folder).rglob("*") if path.name == "__pycache__" or path.suffix == ".pyc"]

    def test_widget_copied_with_its_license_and_removed(self):
        self.assertIn("Installed the bar widget", self.install("--plugin"))
        self.assertEqual(sorted(path.name for path in self.plugin.iterdir()),
                         ["LICENSE", "README.md", "manifest.json", "plugin"])
        self.assertEqual([path.name for path in (self.plugin / "plugin").iterdir()], ["BarWidget.qml"])
        self.assertIn("up to date", self.install("--plugin"))
        self.install("--uninstall")
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.home / ".local" / "bin" / "omapod").exists())

    def test_marketplace_clone_left_alone(self):
        self.marketplace_clone()
        self.assertIn(f"omarchy plugin update {PLUGIN_ID}", self.install("--plugin"))
        self.assertTrue((self.plugin / ".git").is_dir())
        self.assertTrue((self.plugin / "plugin" / "BarWidget.qml").read_text().endswith("// changed by the user\n"))
        self.assertIn(f"omarchy plugin remove {PLUGIN_ID}", self.install("--uninstall"))
        self.assertTrue((self.plugin / ".git").is_dir())
        self.assertFalse((self.home / ".local" / "bin" / "omapod").exists())  # the app itself is gone

    def test_extras_of_the_marketplace_copy_run_the_app_from_there(self):
        self.marketplace_clone()
        script = self.plugin / "install.sh"
        self.install("--timer", script=script)
        app = self.home / ".local" / "share" / "omapod-app"
        self.assertTrue(app.is_symlink())
        self.assertEqual(app.resolve(), self.plugin.resolve())  # omarchy plugin update updates what runs
        launcher = self.home / ".local" / "bin" / "omapod"
        self.assertIn("PYTHONPYCACHEPREFIX", launcher.read_text())
        self.assertIn('exec python3 -P -m omapod "$@"', launcher.read_text())  # see LaunchersTest
        self.assertTrue((self.home / ".config" / "systemd" / "user" / "omapod-refresh.timer").exists())
        done = subprocess.run([str(launcher), "--version"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.strip(), "omaPOD " + json.loads((ROOT / "manifest.json").read_text())["version"])
        self.assertEqual(self.written_bytecode(self.plugin), [])  # each new file there reloads every plugin
        self.assertNotEqual(self.written_bytecode(self.home / ".cache" / "omapod"), [])
        self.install("--uninstall", script=script)
        self.assertFalse(app.exists() or app.is_symlink())
        self.assertTrue((self.plugin / ".git").is_dir())  # removed with omarchy plugin remove

    def test_help_shows_the_whole_usage(self):
        usage = self.install("--help")
        self.assertIn("--uninstall", usage)
        self.assertIn("marketplace", usage)
        self.assertNotIn("set -euo", usage)


class LaunchersTest(unittest.TestCase):
    """python3 -m puts the working directory first on the module path, and omaPOD is mostly started from the home
    folder. Every way of starting it passes -P, which leaves the working directory out."""

    def test_every_launcher_passes_minus_p(self):
        self.assertIn('"PYTHONPATH=" + root.appDir, "python3", "-P", "-m", "omapod"]',
                      (ROOT / "plugin" / "BarWidget.qml").read_text())
        installer = (ROOT / "install.sh").read_text()
        self.assertIn('exec python3 -P -m omapod "\\$@"', installer)
        self.assertIn("python3 -P - <<'PY'", installer)
        self.assertIn('[sys.executable, "-P", "-m", "omapod"]', (ROOT / "omapod" / "__main__.py").read_text())

    def test_a_restart_keeps_the_working_directory_off_the_module_path(self):
        """After a database import omaPOD starts itself again. An empty entry in PYTHONPATH stands for the working
        directory, -P or not; the restart used to add one whenever the variable was not set."""
        import os

        from omapod.__main__ import _restart_command

        for value in (None, "", os.pathsep, f"/opt/modules{os.pathsep}", f"{os.pathsep}/opt/modules",
                      f"{ROOT}{os.pathsep}{os.pathsep}/opt/modules"):
            with self.subTest(value=value):
                arguments, environ = _restart_command({} if value is None else {"PYTHONPATH": value, "HOME": "/h"})
                entries = environ["PYTHONPATH"].split(os.pathsep)
                self.assertEqual(arguments[1:], ["-P", "-m", "omapod"])
                self.assertEqual(entries[0], str(ROOT))
                self.assertNotIn("", entries)
                self.assertEqual(entries.count(str(ROOT)), 1)
                self.assertEqual("/opt/modules" in entries, "/opt/modules" in (value or ""))
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "json.py").write_text("open(__file__ + '.ran', 'w').close()\n")
            arguments, environ = _restart_command({"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
            done = subprocess.run(arguments + ["--version"], cwd=folder, env=environ, capture_output=True, text=True,
                                  timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertFalse(Path(folder, "json.py.ran").exists())

    def test_a_module_in_the_working_directory_does_not_run(self):
        import os

        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "json.py").write_text("open(__file__ + '.ran', 'w').close()\n")  # omaPOD imports json first
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
            for flags, runs in ((["-P"], False), ([], True)):
                done = subprocess.run(["python3", *flags, "-m", "omapod", "--version"], cwd=folder, env=env,
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(Path(folder, "json.py.ran").exists(), runs, flags)
                self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
