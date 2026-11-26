from __future__ import annotations

import hashlib
import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotdm.engine import DownloadManager, InstanceLocked, safe_filename, validate_url

BODY = bytes(range(256)) * 8192
NEW_BODY = bytes(reversed(range(256))) * 8192


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.body = BODY
        self.etag = '"v1"'
        self.requests = []
        self.failed_once = False
        self.guard = threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server_port}{path}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass

    def do_GET(self):
        with self.server.guard:
            data, tag = self.server.body, self.server.etag
            request_range = self.headers.get("Range", "")
            self.server.requests.append((self.path, request_range, self.headers.get("If-Range", "")))
        if self.path == "/session" and self.headers.get("Cookie") != "dot_session=test-session-secret":
            self.send_error(403)
            return
        if self.path == "/range-forbidden" and request_range:
            self.send_error(403)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/range")
            self.end_headers()
            return
        if self.path == "/error":
            self.send_error(403)
            return
        if self.path == "/stall":  # Accepts the connection, then says nothing for a long time.
            time.sleep(8)
            try:
                self.send_error(504)
            except ConnectionError:
                pass  # The client's deadline closed the connection first, as intended.
            return
        if self.path == "/zero":
            self.send_response(416)
            self.send_header("Content-Range", "bytes */0")
            self.end_headers()
            return
        start, end = 0, len(data) - 1
        range_allowed = self.path not in ("/no-range", "/unknown")
        if self.path == "/bad-range" and request_range != "bytes=0-0":
            range_allowed = False
        if self.headers.get("If-Range") and self.headers["If-Range"] != tag:
            range_allowed = False
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request_range)
        ranged = bool(range_allowed and match)
        if ranged:
            start, end = map(int, match.groups())
            end = min(end, len(data) - 1)
        selected = data[start:end + 1]
        self.send_response(206 if ranged else 200)
        if self.path != "/weak":
            self.send_header("ETag", tag)
        else:
            self.send_header("ETag", 'W/"weak"')
        if self.path == "/encoded":
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''test%20payload.bin")
        if self.path != "/unknown":
            self.send_header("Content-Length", str(len(selected)))
        if ranged:
            range_start = start + 1 if self.path == "/wrong-range" and request_range != "bytes=0-0" else start
            self.send_header("Content-Range", f"bytes {range_start}-{end}/{len(data)}")
        self.end_headers()
        try:
            if self.path == "/flaky" and request_range not in ("", "bytes=0-0"):
                with self.server.guard:
                    fail = not self.server.failed_once
                    self.server.failed_once = True
                if fail:
                    self.wfile.write(selected[:32768])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
            for pos in range(0, len(selected), 16384):
                self.wfile.write(selected[pos:pos + 16384])
                self.wfile.flush()
                if self.path in ("/slow", "/weak") and len(selected) > 1:
                    time.sleep(0.008)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class DownloadsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FixtureServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server.body = BODY
        self.server.etag = '"v1"'
        self.server.requests.clear()
        self.server.failed_once = False
        self.manager = DownloadManager(self.root / "state", self.root / "files")
        self.manager.configure(connections=4)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def task(self, task_id):
        return next(t for t in self.manager.snapshot() if t["id"] == task_id)

    def wait(self, task_id, predicate, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            task = self.task(task_id)
            if predicate(task):
                return task
            if task["status"] == "Failed":
                self.fail(task["error"])
            time.sleep(0.02)
        self.fail(f"Timed out: {self.task(task_id)}")

    def complete(self, task_id, expected=BODY):
        task = self.wait(task_id, lambda t: t["status"] == "Completed")
        output = Path(task["directory"]) / task["filename"]
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(task["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(task["total"], len(expected))
        return task

    def test_parallel_download_and_checksum(self):
        task = self.complete(self.manager.add(self.server.url("/range")))
        self.assertGreater(len(task["segments"]), 1)
        self.assertEqual(task["filename"], "test payload.bin")
        self.assertTrue(any(validator == '"v1"' for _, _, validator in self.server.requests))

    def test_pause_resume_and_restart_persistence(self):
        task_id = self.manager.add(self.server.url("/slow"))
        self.wait(task_id, lambda t: 65536 < t["downloaded"] < len(BODY))
        self.manager.pause(task_id)
        paused = self.wait(task_id, lambda t: t["status"] == "Paused")
        progress = paused["downloaded"]
        self.manager.close()
        self.manager = DownloadManager(self.root / "state")
        self.assertEqual(self.task(task_id)["status"], "Paused")
        self.assertEqual(self.task(task_id)["downloaded"], progress)
        self.server.requests.clear()
        self.manager.resume(task_id)
        self.complete(task_id)
        resumed_starts = [int(re.match(r"bytes=(\d+)-", r).group(1)) for _, r, _ in self.server.requests if r and r != "bytes=0-0"]
        self.assertTrue(any(start % (256 * 1024) != 0 for start in resumed_starts))

    def test_changed_etag_discards_old_parts(self):
        task_id = self.manager.add(self.server.url("/slow"))
        self.wait(task_id, lambda t: t["downloaded"] > 65536)
        self.manager.pause(task_id)
        self.wait(task_id, lambda t: t["status"] == "Paused")
        self.server.body, self.server.etag = NEW_BODY, '"v2"'
        self.manager.resume(task_id)
        task = self.complete(task_id, NEW_BODY)
        self.assertEqual(task["etag"], '"v2"')

    def test_no_range_single_stream(self):
        task = self.complete(self.manager.add(self.server.url("/no-range")))
        self.assertFalse(task["range_supported"])
        self.assertEqual(len(task["segments"]), 1)

    def test_lying_range_server_falls_back(self):
        task = self.complete(self.manager.add(self.server.url("/bad-range")))
        self.assertEqual(len(task["segments"]), 1)

    def test_wrong_content_range_never_combines_bad_bytes(self):
        task = self.complete(self.manager.add(self.server.url("/wrong-range")))
        self.assertEqual(len(task["segments"]), 1)

    def test_unknown_length_stream(self):
        self.complete(self.manager.add(self.server.url("/unknown")))

    def test_empty_file(self):
        self.complete(self.manager.add(self.server.url("/zero")), b"")

    def test_redirect(self):
        self.complete(self.manager.add(self.server.url("/redirect")))

    def test_broken_connection_retries_partial_range(self):
        self.complete(self.manager.add(self.server.url("/flaky")))
        self.assertTrue(self.server.failed_once)
        starts = [int(re.match(r"bytes=(\d+)-", r).group(1)) for _, r, _ in self.server.requests if r]
        self.assertTrue(any(start % (256 * 1024) == 32768 for start in starts))

    def test_weak_validator_is_not_used_for_segmentation(self):
        task = self.complete(self.manager.add(self.server.url("/weak")))
        self.assertEqual(task["validator"], "")
        self.assertEqual(len(task["segments"]), 1)

    def test_existing_files_are_preserved(self):
        folder = self.root / "files"
        folder.mkdir(exist_ok=True)
        original = folder / "test payload.bin"
        original.write_bytes(b"keep me")
        task = self.complete(self.manager.add(self.server.url("/range")))
        self.assertEqual(original.read_bytes(), b"keep me")
        self.assertNotEqual(task["filename"], original.name)
        self.manager.remove(task["id"])
        self.assertTrue((folder / task["filename"]).exists())

    def test_queue_and_scheduled_start(self):
        self.manager.configure(max_active=1)
        first = self.manager.add(self.server.url("/slow"))
        second = self.manager.add(self.server.url("/range"), scheduled_at=time.time() + 0.8)
        self.assertEqual(self.task(second)["status"], "Scheduled")
        self.wait(first, lambda t: t["downloaded"] > 0)
        self.assertIn(self.task(second)["status"], ("Queued", "Scheduled"))
        self.complete(first)
        self.complete(second)

    def test_aggregate_speed_limit(self):
        self.server.body = BODY[:128 * 1024]
        self.manager.configure(speed_limit=128 * 1024)
        started = time.monotonic()
        self.complete(self.manager.add(self.server.url("/range")), self.server.body)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_http_error_is_visible_and_retryable(self):
        task_id = self.manager.add(self.server.url("/error"))
        task = self.wait(task_id, lambda t: t["status"] == "Failed")
        self.assertIn("403", task["error"])
        self.manager.resume(task_id)
        self.wait(task_id, lambda t: t["status"] == "Failed")

    def test_unexpected_content_encoding_fails(self):
        task_id = self.manager.add(self.server.url("/encoded"))
        task = self.wait(task_id, lambda t: t["status"] == "Failed")
        self.assertIn("compressed", task["error"])

    def test_single_instance_lock(self):
        with self.assertRaisesRegex(InstanceLocked, "already"):
            DownloadManager(self.root / "state")

    def test_idle_library_is_not_rewritten(self):
        self.complete(self.manager.add(self.server.url("/range")))
        state = self.root / "state" / "state.json"
        time.sleep(0.3)
        before = state.stat().st_mtime_ns
        time.sleep(2.6)  # Longer than the progress checkpoint interval.
        self.assertEqual(state.stat().st_mtime_ns, before, "no periodic writes while nothing is downloading")

    def test_retired_status_is_restored_as_paused(self):
        task_id = self.manager.add(self.server.url("/range"), paused=True)
        self.manager.close()
        state = self.root / "state" / "state.json"
        data = json.loads(state.read_text())
        data["tasks"][0]["status"] = "Waiting for browser"  # Written by the withdrawn 0.2 release.
        state.write_text(json.dumps(data))
        self.manager = DownloadManager(self.root / "state")
        self.assertEqual(self.task(task_id)["status"], "Paused")

    def test_corrupt_part_is_restarted(self):
        task_id = self.manager.add(self.server.url("/slow"))
        self.wait(task_id, lambda t: t["downloaded"] > 65536)
        self.manager.pause(task_id)
        paused = self.wait(task_id, lambda t: t["status"] == "Paused")
        first = self.root / "state" / "parts" / task_id / "00000.part"
        first.write_bytes(b"x" * (paused["segments"][0]["end"] + 100))
        self.manager.resume(task_id)
        self.complete(task_id)

    def test_input_validation(self):
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/a", "https://a:b@example.com/a", "https://example.com/a\nb"):
            with self.assertRaises(ValueError):
                validate_url(url)
        self.assertEqual(safe_filename("../CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("NUL.txt"), "_NUL.txt")
        self.assertNotIn("/", safe_filename("../../test.exe"))

    def test_browser_session_headers_are_used_but_not_saved(self):
        task_id = self.manager.add(self.server.url("/session"), headers={"Cookie": "dot_session=test-session-secret"}, browser_context=True)
        self.complete(task_id)
        saved = (self.root / "state" / "state.json").read_text()
        self.assertNotIn("test-session-secret", saved)
        self.assertNotIn("Cookie", saved)

    def test_server_rejecting_range_probe_can_still_download(self):
        task = self.complete(self.manager.add(self.server.url("/range-forbidden")))
        self.assertFalse(task["range_supported"])

    def test_refused_browser_download_fails_with_an_explanation(self):
        task_id = self.manager.add(self.server.url("/error"), browser_context=True)
        task = self.wait(task_id, lambda t: t["status"] == "Failed")
        self.assertIn("HTTP 403", task["error"])
        self.assertIn("browser", task["error"])

    def test_sensitive_request_headers_are_stripped_on_redirect(self):
        from dotdm.engine import SafeRedirects
        original = urllib.request.Request("https://one.example/file", headers={"Cookie": "session=secret", "Authorization": "Bearer secret", "X-Api-Key": "secret", "User-Agent": "Browser"})
        redirected = SafeRedirects().redirect_request(original, None, 302, "Found", {}, "https://two.example/file")
        names = {k.lower() for k in redirected.headers}
        self.assertNotIn("cookie", names)
        self.assertNotIn("authorization", names)
        self.assertNotIn("x-api-key", names)
        self.assertIn("user-agent", names)


if __name__ == "__main__":
    unittest.main()
