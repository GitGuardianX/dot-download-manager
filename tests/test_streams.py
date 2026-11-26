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

from dotdm.engine import DownloadManager


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


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
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def check_stream(self, name):
        manager = DownloadManager(self.root / (name + "-state"), self.root / "downloads")
        try:
            identity = manager.add(f"http://127.0.0.1:{self.server.server_port}/{name}")
            until = time.monotonic() + 20
            while time.monotonic() < until:
                task = next(t for t in manager.snapshot() if t["id"] == identity)
                if task["status"] in ("Completed", "Failed"):
                    break
                time.sleep(.05)
            self.assertEqual(task["status"], "Completed", task["error"])
            self.assertEqual(Path(task["filename"]).suffix, ".mp4")
            output = Path(task["directory"]) / task["filename"]
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), task["sha256"])
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width", "-of", "json", str(output)], capture_output=True, check=True)
            streams = json.loads(probe.stdout)["streams"]
            self.assertTrue(any(stream["codec_type"] == "video" for stream in streams))
            return streams
        finally:
            manager.close()

    def test_hls_saves_playable_mp4(self):
        self.check_stream("index.m3u8")

    def test_master_playlist_keeps_only_the_best_variant(self):
        videos = [s for s in self.check_stream("master.m3u8") if s["codec_type"] == "video"]
        self.assertEqual(len(videos), 1, "one video track, not every quality variant")
        self.assertEqual(videos[0]["width"], 320)

    def test_dash_saves_playable_mp4(self):
        self.check_stream("manifest.mpd")
