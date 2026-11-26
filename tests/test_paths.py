"""Where DOT keeps its library, downloads and helper tools in each mode."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotdm import paths


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = root / "Programs" / "DOT"
        self.app.mkdir(parents=True)
        (self.app / "DOT.exe").write_bytes(b"")
        self.local = root / "LocalAppData"
        self.patches = [patch.object(sys, "frozen", True, create=True),
                        patch.object(sys, "executable", str(self.app / "DOT.exe")),
                        patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)})]
        for active in self.patches:
            active.start()

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()
        self.temp.cleanup()

    def test_installed_mode_uses_the_user_profile(self):
        with patch.object(paths, "known_downloads", return_value=Path(r"D:\Downloads")):
            self.assertEqual(paths.mode(), "installed")
            self.assertEqual(paths.library_dir(), self.local / "DOT")
            self.assertEqual(paths.downloads_dir(), Path(r"D:\Downloads"))
        self.assertEqual(paths.user_tools_dir(), self.local / "DOT" / "tools")
        self.assertEqual(paths.bundled_tools_dir(), self.app / "tools")
        self.assertEqual(paths.extension_dir(), self.app / "browser-extension")

    def test_portable_mode_keeps_everything_beside_the_exe(self):
        (self.app / "portable.txt").write_text("")
        self.assertEqual(paths.mode(), "portable")
        self.assertEqual(paths.library_dir(), self.app / ".local")
        self.assertEqual(paths.downloads_dir(), self.app / "downloads")
        self.assertEqual(paths.user_tools_dir(), self.app / ".local" / "tools")

    def test_tools_prefer_updates_then_bundled_then_path(self):
        bundled, user = self.app / "tools", self.local / "DOT" / "tools"
        bundled.mkdir()
        user.mkdir(parents=True)
        name = "yt-dlp" + paths.EXE
        with patch("shutil.which", return_value=r"C:\Elsewhere\yt-dlp.exe"):
            self.assertEqual(paths.find_tool("yt-dlp"), r"C:\Elsewhere\yt-dlp.exe")
            (bundled / name).write_bytes(b"")
            self.assertEqual(paths.find_tool("yt-dlp"), str(bundled / name))
            (user / name).write_bytes(b"")
            self.assertEqual(paths.find_tool("yt-dlp"), str(user / name))

    def test_source_mode_uses_the_project_folder(self):
        with patch.object(sys, "frozen", False):
            project = Path(paths.__file__).resolve().parent.parent
            self.assertEqual(paths.mode(), "source")
            self.assertEqual(paths.library_dir(), project / ".local")
            self.assertEqual(paths.bundled_tools_dir(), project / "build" / "tools" / "bin")

    @unittest.skipUnless(os.name == "nt", "Windows known folders")
    def test_the_real_downloads_folder_is_found(self):
        folder = paths.known_downloads()
        self.assertTrue(folder is not None and folder.is_absolute(), folder)


if __name__ == "__main__":
    unittest.main()
