"""The browser bridge: who may connect, what it accepts, and that its work stays bounded."""
from __future__ import annotations

import http.client
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dotdm.bridge import EXTENSION_ID, MAX_BODY, MAX_CONNECTIONS, MAX_PENDING, BrowserBridge, TokenBucket
from dotdm.engine import DownloadManager
from tests.test_downloads import BODY, FixtureServer

ORIGIN = f"chrome-extension://{EXTENSION_ID}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = FixtureServer()

    @classmethod
    def tearDownClass(cls):
        cls.files.shutdown()
        cls.files.server_close()
        cls.files.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files.requests.clear()
        self.manager = DownloadManager(self.root / "state", self.root / "files")
        self.bridge = BrowserBridge(self.manager)
        self.port = free_port()
        self.bridge.start(self.port)
        self.assertEqual(self.bridge.error, "")

    def tearDown(self):
        self.bridge.stop()
        self.manager.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, origin=ORIGIN, client="dot-extension/0.3",
                host=None, content_type="application/json", raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        headers = {"Content-Type": content_type, "Content-Length": str(len(data))}
        if origin:
            headers["Origin"] = origin
        if client:
            headers["X-DOT-Client"] = client
        if host:
            headers["Host"] = host
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, (json.loads(payload) if payload else {}), response

    def offer(self, url, source="download", **extra):
        return self.request("POST", "/v1/downloads", {"url": url, "source": source, **extra})

    def test_only_the_extension_may_connect(self):
        status, body, response = self.request("POST", "/v1/hello", {})
        self.assertEqual((status, body["app"]), (200, "DOT"))
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ORIGIN)
        for kwargs in ({"origin": None}, {"origin": "https://evil.example"}, {"origin": "chrome-extension://other"},
                       {"client": None}, {"client": "curl/8"}, {"host": f"evil.example:{self.port}"}):
            with self.subTest(**kwargs):
                status, _, response = self.request("POST", "/v1/hello", {}, **kwargs)
                self.assertEqual(status, 403)
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.request("POST", "/v1/hello", {}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("GET", "/v1/hello")[0], 405)
        self.assertEqual(self.request("POST", "/v1/hello", raw=b"x" * (MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request("POST", "/v1/nope", {})[0], 404)

    def test_extension_version_and_last_contact_are_recorded(self):
        self.assertEqual((self.bridge.last_seen, self.bridge.client_version), (0.0, ""))
        self.request("POST", "/v1/hello", {}, client="dot-extension/0.4.0")
        self.assertEqual(self.bridge.client_version, "0.4.0")
        self.assertGreater(self.bridge.last_seen, 0)
        self.request("POST", "/v1/hello", {}, origin="https://evil.example", client="dot-extension/9.9")
        self.assertEqual(self.bridge.client_version, "0.4.0", "refused requests are not recorded")

    def test_cors_preflight_only_for_the_extension(self):
        self.assertEqual(self.request("OPTIONS", "/v1/downloads", origin="https://evil.example")[0], 403)
        status, _, response = self.request("OPTIONS", "/v1/downloads")
        self.assertEqual(status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ORIGIN)

    def test_offer_is_checked_then_queued_without_creating_a_task(self):
        status, body, _ = self.offer(self.files.url("/range"), cookies="sid=browser-secret", userAgent="TestBrowser/1")
        self.assertEqual((status, body["accepted"]), (202, True))
        offer = self.bridge.pending.get_nowait()
        self.assertEqual(offer["total"], len(BODY))
        self.assertEqual(offer["filename"], "test payload.bin")  # From Content-Disposition.
        self.assertEqual(offer["headers"], {"Cookie": "sid=browser-secret", "User-Agent": "TestBrowser/1"})
        self.assertEqual(self.manager.snapshot(), [])
        state = self.root / "state" / "state.json"
        self.assertFalse(state.exists() and "browser-secret" in state.read_text())

    def test_stream_offer_carries_the_players_origin_and_the_tab_title(self):
        status, body, _ = self.offer("https://cdn.example/v/master.m3u8", source="media", kind="stream",
                                     origin="https://player.example", title="A clip\n - Site", userAgent="TestBrowser/1")
        self.assertEqual((status, body["accepted"]), (202, True))
        offer = self.bridge.pending.get_nowait()
        self.bridge.done(offer["id"])  # As DOT's window does when it closes.
        self.assertTrue(offer["stream"])
        self.assertEqual(offer["headers"]["Origin"], "https://player.example")
        self.assertEqual(offer["title"], "A clip  - Site", "control characters removed")
        for bad in ("https://player.example/path", "javascript:alert(1)", "https://a.example\r\nX: y"):
            self.assertEqual(self.offer("https://cdn.example/v/master.m3u8", source="media", kind="stream", origin=bad)[0], 202)
            offer = self.bridge.pending.get_nowait()
            self.bridge.done(offer["id"])
            self.assertNotIn("Origin", offer["headers"], bad)

    def test_refused_browser_download_stays_in_the_browser(self):
        status, body, _ = self.offer(self.files.url("/error"))
        self.assertEqual((status, body["accepted"]), (409, False))
        self.assertIn("403", body["reason"])
        self.assertTrue(self.bridge.pending.empty())
        # An explicit choice (link or media) still opens the window, with the reason shown.
        status, body, _ = self.offer(self.files.url("/error"), source="link")
        self.assertEqual((status, body["accepted"]), (202, True))
        self.assertIn("403", self.bridge.pending.get_nowait()["warning"])

    def test_check_has_a_hard_deadline(self):
        started = time.monotonic()
        status, body, _ = self.offer(self.files.url("/stall"))
        self.assertLess(time.monotonic() - started, 6.5, "the extension waits at most 7 s")
        self.assertEqual((status, body["accepted"]), (409, False))
        self.assertIn("quickly", body["reason"])

    def test_video_pages_need_ytdlp(self):
        with patch("dotdm.sites.command", return_value=None):
            status, body, _ = self.offer("https://www.youtube.com/watch?v=abc123", source="page")
        self.assertEqual((status, body["accepted"]), (409, False))
        self.assertIn("yt-dlp", body["reason"])

    def test_video_pages_open_the_video_window_without_a_probe(self):
        with patch("dotdm.sites.command", return_value=["yt-dlp"]):
            status, _, _ = self.offer("https://www.youtube.com/watch?v=abc123", source="page")
        self.assertEqual(status, 202)
        offer = self.bridge.pending.get_nowait()
        self.assertTrue(offer["site"])
        self.assertFalse(offer["stream"])
        self.assertEqual(self.files.requests, [])

    def test_streams_are_not_probed(self):
        status, _, _ = self.offer(self.files.url("/live/master.m3u8"), source="media")
        self.assertEqual(status, 202)
        offer = self.bridge.pending.get_nowait()
        self.assertTrue(offer["stream"])
        self.assertEqual(offer["filename"], "master.mp4")
        self.assertEqual(self.files.requests, [])

    def test_open_windows_and_rate_are_bounded(self):
        url = self.files.url("/v.m3u8")  # Streams skip the probe, so this measures the limits alone.
        accepted = [self.offer(url)[1]["id"] for _ in range(MAX_PENDING)]
        status, body, _ = self.offer(url)
        self.assertEqual((status, body["accepted"]), (429, False))
        self.assertIn("windows", body["reason"])
        self.bridge.done(accepted[0])
        self.assertEqual(self.offer(url)[0], 202)   # Freed slot, last token of the automatic burst.
        self.bridge.done(accepted[1])
        status, body, _ = self.offer(url)
        self.assertEqual(status, 429)
        self.assertIn("Too many", body["reason"])
        # Clicks have their own allowance: the spent automatic one does not block them.
        self.bridge.done(accepted[2])
        self.assertEqual(self.offer(url, source="media")[0], 202)

    def test_concurrent_connections_are_capped(self):
        stalled = []
        try:
            for _ in range(MAX_CONNECTIONS):
                connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                connection.sendall(b"POST /v1/hello HTTP/1.0\r\n")  # Headers never finish.
                stalled.append(connection)
            time.sleep(0.4)
            extra = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            started = time.monotonic()
            self.assertEqual(extra.recv(1), b"", "an over-capacity connection is closed at once")
            self.assertLess(time.monotonic() - started, 1.5)
            extra.close()
        finally:
            for connection in stalled:
                connection.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if self.request("POST", "/v1/hello", {})[0] == 200:
                    break
            except (ConnectionError, http.client.HTTPException):
                time.sleep(0.1)
        else:
            self.fail("bridge did not recover after stalled connections closed")

    def test_port_is_bound_exclusively(self):
        other = BrowserBridge(self.manager)
        other.start(self.port)
        try:
            self.assertIsNone(other.server)
            self.assertIn(str(self.port), other.error)
        finally:
            other.stop()

    def test_token_bucket(self):
        now = [0.0]
        bucket = TokenBucket(2, 3.0, clock=lambda: now[0])
        self.assertEqual([bucket.take(), bucket.take(), bucket.take()], [True, True, False])
        now[0] = 3.0
        self.assertEqual([bucket.take(), bucket.take()], [True, False])


if __name__ == "__main__":
    unittest.main()
