"""Video pages through yt-dlp, tested against tests/fake_ytdlp.py (no network)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dotdm import sites
from dotdm.engine import DownloadError, DownloadManager

FAKE = Path(__file__).with_name("fake_ytdlp.py")
VIDEO, AUDIO = b"FAKEVIDEO" * 50000, b"FAKEAUDIO" * 50000
PAGE = "https://www.youtube.com/watch?v=abc123"


class SitesTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"DOT_YTDLP": str(FAKE)})
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = DownloadManager(self.root / "state", self.root / "files")

    def tearDown(self):
        self.manager.close()
        self.environment.stop()
        self.temp.cleanup()

    def task(self, task_id):
        return next(t for t in self.manager.snapshot() if t["id"] == task_id)

    def wait(self, task_id, predicate, timeout=20):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            task = self.task(task_id)
            if predicate(task):
                return task
            time.sleep(0.03)
        self.fail(f"Timed out: {self.task(task_id)}")

    def test_describe_lists_qualities_with_sizes(self):
        info = sites.describe(PAGE)
        self.assertEqual((info["title"], info["duration"], info["site"]), ("Fake: Clip/Title?", 19, "Youtube"))
        self.assertEqual([c["label"] for c in info["choices"]], ["1080p", "720p", "360p", "Audio only (M4A)"])
        self.assertEqual(info["choices"][0]["size"], 5_000_000 + 300_000, "MP4 video preferred, plus M4A audio")
        self.assertEqual(info["choices"][2]["size"], 900_000, "a format that already has audio")
        self.assertIn("height<=720", info["choices"][1]["format"])
        self.assertTrue(info["choices"][0]["format"].startswith("bv*[height<=1080][vcodec^=avc1]"), "H.264 first")
        self.assertEqual(info["warning"], "")

    def test_unknown_video_size_is_not_shown_as_the_audio_size(self):
        info = {"formats": [{"height": 240, "ext": "mp4", "vcodec": "av01", "acodec": "none"},
                            {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "filesize": 309288, "abr": 128}]}
        options = sites.choices(info)
        self.assertEqual(options[0]["size"], None)
        self.assertEqual(options[-1]["size"], 309288)

    def test_streamed_formats_are_estimated_from_bitrate(self):
        info = {"duration": 100, "formats": [
            {"height": 1080, "ext": "mp4", "vcodec": "avc1.64002A", "acodec": "none", "tbr": 8000, "protocol": "m3u8_native"},
            {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "filesize": 1_000_000, "abr": 128}]}
        self.assertEqual(sites.choices(info)[0]["size"], 8000 * 125 * 100 + 1_000_000)

    def test_bundled_tools_are_preferred(self):
        tools = {"yt-dlp": r"C:\DOT\tools\yt-dlp.exe", "deno": r"C:\DOT\tools\deno.exe"}
        with patch.dict(os.environ, {"DOT_YTDLP": ""}), patch.object(sites.paths, "find_tool", side_effect=tools.get):
            sites.forget()
            try:
                self.assertEqual(sites.command(), [r"C:\DOT\tools\yt-dlp.exe"])
                self.assertEqual(sites.js_runtime_args(), ["--js-runtimes", r"deno:C:\DOT\tools\deno.exe"])
            finally:
                sites.forget()

    def test_update_copies_the_bundled_ytdlp_to_a_writable_folder(self):
        bundled, user = self.root / "bundled", self.root / "user"
        bundled.mkdir()
        (bundled / ("yt-dlp" + sites.paths.EXE)).write_bytes(b"bundled")
        done = subprocess.CompletedProcess([], 0, stdout=b"Updated yt-dlp to stable@2026.12.01\n", stderr=b"")
        with patch.object(sites.paths, "bundled_tools_dir", return_value=bundled), \
                patch.object(sites.paths, "user_tools_dir", return_value=user), \
                patch.object(sites.subprocess, "run", return_value=done) as run:
            self.assertEqual(sites.update_ytdlp(), "Updated yt-dlp to stable@2026.12.01")
        copy = user / ("yt-dlp" + sites.paths.EXE)
        self.assertEqual(copy.read_bytes(), b"bundled")
        self.assertEqual(run.call_args.args[0], [str(copy), "-U"])

    def test_missing_challenge_solver_is_reported(self):
        self.assertEqual(sites.describe(PAGE + "&nsig")["warning"], sites.SOLVER_HINT)

    def test_unreadable_pages_are_explained(self):
        with self.assertRaisesRegex(DownloadError, "^Video unavailable$"):
            sites.describe(PAGE + "&unavailable")
        with self.assertRaisesRegex(DownloadError, "playlist"):
            sites.describe(PAGE + "&playlist")

    def test_video_page_downloads_and_publishes(self):
        task_id = self.manager.add(PAGE, filename="Clip.mp4", site=True, site_format="bv*+ba/b")
        task = self.wait(task_id, lambda t: t["status"] in ("Completed", "Failed"))
        self.assertEqual(task["status"], "Completed", task["error"])
        output = Path(task["directory"]) / task["filename"]
        self.assertEqual((task["filename"], task["transport"]), ("Clip.mp4", "site"))
        self.assertEqual(output.read_bytes(), VIDEO)
        self.assertEqual(task["sha256"], hashlib.sha256(VIDEO).hexdigest())
        self.assertFalse((self.root / "state" / "parts" / task_id).exists(), "temporary files are removed")

    def test_connecting_until_the_first_bytes(self):
        task_id = self.manager.add(PAGE + "&warmup", filename="Warm.mp4", site=True)
        seen = self.wait(task_id, lambda t: t["status"] == "Connecting")
        self.assertEqual(seen["downloaded"], 0)
        self.wait(task_id, lambda t: t["status"] == "Downloading" or t["status"] == "Completed")
        self.assertEqual(self.wait(task_id, lambda t: t["status"] in ("Completed", "Failed"))["status"], "Completed")

    def test_audio_choice_is_saved_as_m4a(self):
        task_id = self.manager.add(PAGE, filename="Song.mp4", site=True, site_format="ba[ext=m4a]/ba")
        task = self.wait(task_id, lambda t: t["status"] in ("Completed", "Failed"))
        self.assertEqual((task["status"], task["filename"]), ("Completed", "Song.m4a"))
        self.assertEqual((Path(task["directory"]) / task["filename"]).read_bytes(), AUDIO)

    def test_existing_files_are_never_overwritten(self):
        folder = self.root / "files"
        folder.mkdir(exist_ok=True)
        (folder / "Clip.mp4").write_bytes(b"keep me")
        task_id = self.manager.add(PAGE, filename="Clip.mp4", site=True)
        task = self.wait(task_id, lambda t: t["status"] == "Completed")
        self.assertEqual((folder / "Clip.mp4").read_bytes(), b"keep me")
        self.assertNotEqual(task["filename"], "Clip.mp4")

    def test_failure_reaches_the_task(self):
        task_id = self.manager.add(PAGE + "&unavailable", filename="x.mp4", site=True)
        task = self.wait(task_id, lambda t: t["status"] == "Failed")
        self.assertEqual(task["error"], "Video unavailable")

    def test_pause_and_resume_continue_the_partial_download(self):
        task_id = self.manager.add(PAGE + "&slow", filename="Long.mp4", site=True)
        self.wait(task_id, lambda t: 0 < t["downloaded"] < len(VIDEO))
        self.manager.pause(task_id)
        paused = self.wait(task_id, lambda t: t["status"] == "Paused")
        self.assertGreater(paused["downloaded"], 0)
        self.manager.resume(task_id)
        task = self.wait(task_id, lambda t: t["status"] in ("Completed", "Failed"), timeout=30)
        self.assertEqual(task["status"], "Completed", task["error"])
        self.assertEqual((Path(task["directory"]) / task["filename"]).read_bytes(), VIDEO)


if __name__ == "__main__":
    unittest.main()
