"""Exercise HLS and DASH remuxing against our own generated local media."""
import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unittest.mock import patch

from dotdm import sites
from dotdm.engine import DownloadManager


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


class StrictHost(QuietFiles):
    """Like a real stream host that refused FFmpeg (HTTP 410): the Host header must come first,
    as browsers send it, and a Referer is required."""
    def send_head(self):
        names = [name.lower() for name in self.headers.keys()]
        if not names or names[0] != "host" or not self.headers.get("Referer"):
            self.send_error(410, "Gone")
            return None
        return super().send_head()


class MislabeledHost(QuietFiles):
    """Like Dailymotion's CDN: HLS playlists are labelled video/mp4."""
    def guess_type(self, path):
        return "video/mp4" if str(path).endswith(".m3u8") else super().guess_type(path)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe are required for stream integration tests")
class StreamsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="dot-streams-")
        cls.root = Path(cls.temp.name)
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        subprocess.run(base + ["-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.root / "source.mp4")], check=True, creationflags=flags)
        subprocess.run(base + ["-i", str(cls.root / "source.mp4"), "-c", "copy", "-f", "hls", "-hls_time", "0.5", "-hls_list_size", "0", str(cls.root / "index.m3u8")], check=True, creationflags=flags)
        subprocess.run(base + ["-i", str(cls.root / "source.mp4"), "-c", "copy", "-f", "dash", "-seg_duration", "1", (cls.root / "manifest.mpd").as_posix()], check=True, creationflags=flags)
        # A two-quality HLS master playlist (320x180 and 160x90), as adaptive players use.
        subprocess.run(base + ["-f", "lavfi", "-i", "color=c=red:s=320x180:r=10", "-t", "1",
                               "-filter_complex", "[0:v]split=2[hi][lo];[lo]scale=160:90[small]",
                               "-map", "[hi]", "-map", "[small]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                               "-b:v:0", "300k", "-b:v:1", "80k", "-f", "hls", "-hls_time", "0.5", "-hls_list_size", "0",
                               "-master_pl_name", "master.m3u8", "-var_stream_map", "v:0 v:1",
                               "-hls_segment_filename", (cls.root / "variant%v_%03d.ts").as_posix(),
                               (cls.root / "variant%v.m3u8").as_posix()], check=True, creationflags=flags)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietFiles, directory=str(cls.root)))
        cls.strict = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(StrictHost, directory=str(cls.root)))
        cls.mislabeled = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(MislabeledHost, directory=str(cls.root)))
        cls.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (cls.server, cls.strict, cls.mislabeled)]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.strict, cls.mislabeled):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join()
        cls.temp.cleanup()

    def run_stream(self, name, server=None, **options):
        server = server or self.server
        manager = DownloadManager(self.root / f"{name}-{server.server_port}-{time.monotonic_ns()}", self.root / "downloads")
        try:
            identity = manager.add(f"http://127.0.0.1:{server.server_port}/{name}", **options)
            until = time.monotonic() + 30
            while time.monotonic() < until:
                task = next(t for t in manager.snapshot() if t["id"] == identity)
                if task["status"] in ("Completed", "Failed"):
                    break
                time.sleep(.05)
            return task
        finally:
            manager.close()

    def check_stream(self, name, server=None, **options):
        task = self.run_stream(name, server, **options)
        self.assertEqual(task["status"], "Completed", task["error"])
        self.assertEqual(Path(task["filename"]).suffix, ".mp4")
        output = Path(task["directory"]) / task["filename"]
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), task["sha256"])
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width", "-of", "json", str(output)], capture_output=True, check=True)
        streams = json.loads(probe.stdout)["streams"]
        self.assertTrue(any(stream["codec_type"] == "video" for stream in streams))
        return streams

    def test_hls_saves_playable_mp4(self):
        self.check_stream("index.m3u8")

    def test_master_playlist_keeps_only_the_best_variant(self):
        videos = [s for s in self.check_stream("master.m3u8") if s["codec_type"] == "video"]
        self.assertEqual(len(videos), 1, "one video track, not every quality variant")
        self.assertEqual(videos[0]["width"], 320)

    def test_dash_saves_playable_mp4(self):
        self.check_stream("manifest.mpd")

    @unittest.skipUnless(sites.command(), "yt-dlp is required")
    def test_host_that_refuses_ffmpeg_is_downloaded_with_ytdlp(self):
        self.check_stream("index.m3u8", self.strict, referer="https://site.example/watch?v=1")

    def test_ffmpeg_alone_is_refused_by_that_host(self):
        with patch("dotdm.sites.command", return_value=None):
            task = self.run_stream("index.m3u8", self.strict, referer="https://site.example/watch?v=1")
        self.assertEqual(task["status"], "Failed")
        self.assertIn("4XX", task["error"], "why browser streams go through yt-dlp")

    @unittest.skipUnless(sites.command(), "yt-dlp is required")
    def test_mislabeled_playlist_with_a_fragment_saves_the_video(self):
        # Dailymotion: ".../manifest.m3u8#cell=cf3" served as video/mp4. 0.4.1 saved the playlist text.
        self.check_stream("index.m3u8#cell=cf3", self.mislabeled, referer="https://site.example/")

    def test_ffmpeg_fallback_still_saves_hls(self):
        with patch("dotdm.sites.command", return_value=None):
            self.check_stream("index.m3u8")
