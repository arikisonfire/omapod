"""The Tiling Rule: every screen fits the smallest window, in both languages, empty and with a podcast playing."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WALK = Path(__file__).with_name("tiling_walk.py")


@unittest.skipUnless(shutil.which("xvfb-run") and shutil.which("dbus-run-session"), "needs a virtual display")
class TilingTest(unittest.TestCase):
    def walk(self, language):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "LANGUAGE": language, "OMAPOD_HOME": home, "OMAPOD_RUNTIME_DIR": f"{home}/run",
                   "OMAPOD_MPV_AO": "null", "GSK_RENDERER": "cairo", "PYTHONDONTWRITEBYTECODE": "1"}
            done = subprocess.run(["dbus-run-session", "--", "xvfb-run", "-a", sys.executable, "-W", "ignore",
                                   str(WALK)], env=env, capture_output=True, text=True, timeout=120)
        complaints = [line for line in done.stderr.splitlines() if "exceeds" in line]
        self.assertEqual(complaints, [])
        self.assertIn("walked", done.stdout, done.stderr)
        self.assertNotIn("wider than its cover", done.stdout)
        self.assertNotIn("a hidden tab makes", done.stdout)
        self.assertNotIn("did not open", done.stdout)

    def test_english(self):
        self.walk("en")

    def test_german(self):
        self.walk("de")
