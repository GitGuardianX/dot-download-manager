"""The installer script and the app agree, and bundled tools are pinned and verified."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from dotdm import integration, tray
from tools.fetch_tools import published_hash

ROOT = Path(__file__).resolve().parent.parent
ISS = (ROOT / "installer" / "DOT.iss").read_text(encoding="utf-8")


class InstallerTest(unittest.TestCase):
    def test_installer_can_close_the_running_app(self):
        exit_message = int(re.search(r"WM_DOT_EXIT = \$([0-9A-F]+)", ISS).group(1), 16)
        self.assertEqual(exit_message, tray.WM_EXIT)
        self.assertIn(f"FindWindowByClassName('{tray.CLASS_NAME}')", ISS)

    def test_start_with_windows_uses_the_apps_own_entry(self):
        self.assertIn(f'ValueName: "{integration.RUN_VALUE}"', ISS)
        self.assertIn(f"RUN_VALUE = '{integration.RUN_VALUE}'", ISS)
        self.assertIn("--minimized", ISS)

    def test_installs_per_user_with_tools_and_extension(self):
        for expected in ("PrivilegesRequired=lowest", "PrivilegesRequiredOverridesAllowed=dialog",
                         r'Source: "..\build\app\DOT\*"', r'Source: "..\build\tools\bin\*"; DestDir: "{app}\tools"',
                         r'DestDir: "{app}\browser-extension"', "--first-run"):
            self.assertIn(expected, ISS)

    def test_tools_are_pinned_official_releases(self):
        spec = json.loads((ROOT / "installer" / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(set(spec), {"yt-dlp", "deno", "ffmpeg"})
        for item in spec.values():
            self.assertTrue(item["url"].startswith("https://github.com/") and item["checksums"].startswith("https://github.com/"))
            self.assertNotIn("/latest/", item["url"], "a fixed release, not a moving one")
        lock = ROOT / "installer" / "tools.lock.json"
        if lock.exists():
            for item in json.loads(lock.read_text(encoding="utf-8")).values():
                self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")

    def test_published_hash_formats(self):
        digest = "a" * 64
        self.assertEqual(published_hash(f"{'b' * 64}  other.zip\n{digest}  yt-dlp.exe\n", "yt-dlp.exe"), digest)
        self.assertEqual(published_hash(f"{digest.upper()} *deno.zip", "deno.zip"), digest)
        self.assertEqual(published_hash(f"Hash: {digest}", "anything.zip"), digest)
        with self.assertRaises(SystemExit):
            published_hash(f"{'b' * 64}  one.zip\n{'c' * 64}  two.zip", "missing.zip")


if __name__ == "__main__":
    unittest.main()
