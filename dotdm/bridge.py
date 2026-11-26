"""Loopback bridge for the DOT browser extension. Data flows one way: browser -> DOT.

Safety properties (each one is covered by tests/test_bridge.py):
  * Listens on 127.0.0.1 only, with an exclusive port binding.
  * Every request must carry the DOT extension's Origin, the extension client header,
    a loopback Host header, and a small JSON body. Web pages cannot forge these, CORS
    preflights from other origins are refused, and DNS rebinding fails the Host check.
  * At most MAX_CONNECTIONS requests are handled at once; extra connections are closed
    immediately. One request per connection, short socket timeouts.
  * Offers are rate-limited (token bucket) and at most MAX_PENDING confirmation windows
    may be open. Overflow is answered with 429 so the browser keeps the download.
  * Nothing here launches a process or calls back into the browser.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import uuid
import queue
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .engine import DownloadError, DownloadManager, refusal_message, request_headers, safe_filename, url_filename, validate_url

EXTENSION_ID = "hpdhbnmbbjmgnccfbgffanhcfdkhalpn"  # Fixed by the "key" in browser-extension/manifest.json.
CLIENT_PREFIX = "dot-extension/"
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 4
MAX_PENDING = 3
# Separate allowances, so a burst of automatic captures never crowds out your own clicks.
AUTOMATIC = (4, 3.0)   # Browser downloads: 4 at once, then one per 3 s.
CLICKS = (6, 2.0)      # Button, menu and popup: 6 at once, then one per 2 s.
PROBE_TIMEOUT = 4.0   # Per socket operation.
PROBE_DEADLINE = 5.0  # Whole check; the extension waits up to 7 s for an answer.
SOCKET_TIMEOUT = 3.0
STALE_OFFER_SECONDS = 15 * 60
SOURCES = {"download", "link", "media", "page"}  # "page": a video page for yt-dlp (YouTube and others).


class TokenBucket:
    def __init__(self, capacity: int, refill_seconds: float, clock=time.monotonic):
        self.capacity, self.refill_seconds, self.clock = capacity, refill_seconds, clock
        self.tokens, self.updated = float(capacity), clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) / self.refill_seconds)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def describe_probe_failure(exc: BaseException) -> str:
    if message := refusal_message(exc):
        return message
    if isinstance(exc, urllib.error.HTTPError):
        return f"The server replied HTTP {exc.code}."
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return "DOT could not reach the server quickly."
    if isinstance(exc, DownloadError):
        return str(exc)
    return f"DOT could not check this file ({type(exc).__name__})."


class BrowserBridge:
    def __init__(self, manager: DownloadManager, origins: set[str] | None = None):
        self.manager = manager
        self.origins = set(origins or {f"chrome-extension://{EXTENSION_ID}"})
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.error = ""
        self.port: int | None = None
        self.pending: queue.Queue = queue.Queue()  # Offers waiting for the UI.
        self.lock = threading.Lock()
        self.open_offers: dict[str, float] = {}     # Offer id -> time it was accepted.
        self.buckets = {"download": TokenBucket(*AUTOMATIC), "click": TokenBucket(*CLICKS)}
        self.last_seen = 0.0           # When the extension last made an authorized request.
        self.client_version = ""       # The extension's version, from its X-DOT-Client header.

    def saw_client(self, header: str):
        self.last_seen = time.time()
        self.client_version = header[len(CLIENT_PREFIX):][:20]

    # ---- offers -----------------------------------------------------------------------
    def done(self, offer_id: str):
        """The UI closed the confirmation window for this offer (any outcome)."""
        with self.lock:
            self.open_offers.pop(offer_id, None)

    def _expire(self):
        now = time.monotonic()
        for key, created in list(self.open_offers.items()):
            if now - created > STALE_OFFER_SECONDS:
                self.open_offers.pop(key)

    def _busy(self, source: str) -> str:
        """Reserve one offer slot, or explain why none is available. Caller holds the lock."""
        self._expire()
        if len(self.open_offers) >= MAX_PENDING:
            return "DOT already has download windows open. The browser kept this download."
        if not self.buckets["download" if source == "download" else "click"].take():
            return "Too many downloads at once. The browser kept this download."
        return ""

    def _check(self, payload: dict):
        """Probe the file within a hard total deadline (socket timeouts apply per operation,
        so a server trickling bytes could otherwise outlast the extension's wait)."""
        outcome = {}

        def run():
            try:
                outcome["meta"] = self.manager.inspect(payload, timeout=PROBE_TIMEOUT)
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=run, name="dot-check", daemon=True)
        worker.start()
        worker.join(PROBE_DEADLINE)
        if worker.is_alive():
            raise TimeoutError("check deadline passed")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["meta"]

    def handle_download(self, data: dict) -> tuple[int, dict]:
        url = validate_url(str(data.get("url", "")))
        source = data.get("source") if data.get("source") in SOURCES else "link"
        referrer = str(data.get("referrer") or "")
        referrer = validate_url(referrer) if referrer.startswith(("http://", "https://")) else ""
        context = {}
        if cookies := str(data.get("cookies") or "")[:16384]:
            context["Cookie"] = cookies
        if agent := str(data.get("userAgent") or "")[:512]:
            context["User-Agent"] = agent
        context = request_headers(context)
        mime = str(data.get("mime") or "")[:200]
        size = data.get("size") if type(data.get("size")) is int and data["size"] >= 0 else None
        filename = safe_filename(str(data.get("filename") or "")[:255] or url_filename(url))
        stream = bool(re.search(r"\.(m3u8|mpd)(?:$|[?#])", url, re.I) or re.search(r"mpegurl|dash\+xml", mime, re.I)
                      or data.get("kind") == "stream")
        site = source == "page"
        if site:
            from .sites import INSTALL_HINT, command
            if not command():
                return 409, {"accepted": False, "reason": INSTALL_HINT}
            stream = False
        with self.lock:
            if reason := self._busy(source):
                return 429, {"accepted": False, "reason": reason}
        warning = ""
        if site:
            filename, size = "video.mp4", None  # yt-dlp reads the page in DOT's window.
        elif stream:
            filename, size = Path(filename).stem + ".mp4", None
        else:
            try:
                meta = self._check({"url": url, "referer": referrer, "headers": context, "filename": filename})
                size = meta.total if meta.total is not None else size
                filename = meta.filename or filename
            except Exception as exc:  # The probe is advisory; any failure is explained.
                reason = describe_probe_failure(exc)
                if source == "download":
                    # The browser already has this download in hand; let it continue.
                    return 409, {"accepted": False, "reason": reason}
                warning = reason
        offer_id = uuid.uuid4().hex
        offer = {"id": offer_id, "url": url, "referer": referrer, "headers": context, "filename": filename,
                 "total": size, "mime": mime, "stream": stream, "site": site, "source": source, "warning": warning,
                 "probed": True}
        with self.lock:
            if len(self.open_offers) >= MAX_PENDING:
                return 429, {"accepted": False, "reason": "DOT already has download windows open. The browser kept this download."}
            self.open_offers[offer_id] = time.monotonic()
        self.pending.put(offer)
        return 202, {"accepted": True, "id": offer_id}

    # ---- server -----------------------------------------------------------------------
    def start(self, port: int):
        if self.server:
            return
        self.port = int(port)
        try:
            self.server = _Server(("127.0.0.1", self.port), _Handler, self)
            self.thread = threading.Thread(target=self.server.serve_forever, name="dot-browser", daemon=True)
            self.thread.start()
            self.error = ""
        except OSError as exc:
            self.server = None
            self.error = "port %d is in use" % self.port if getattr(exc, "winerror", None) in (10048, 10013) else str(exc)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.server = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = False  # SO_REUSEADDR would let another program share the port on Windows.

    def __init__(self, address, handler, bridge: BrowserBridge):
        self.bridge = bridge
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, handler)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT)
        return connection, address

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # A client that gave up or timed out is normal; nothing to report.
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)  # Over capacity: refuse at once; the browser keeps its download.
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Handler(BaseHTTPRequestHandler):
    server_version = "DOT-Bridge/" + __version__
    protocol_version = "HTTP/1.0"  # One request per connection; nothing holds a slot open.

    def log_message(self, *_):
        pass

    @property
    def bridge(self) -> BrowserBridge:
        return self.server.bridge

    authorized = False  # Set once every check in do_POST has passed; gates the CORS header.

    def _origin(self) -> str:
        origin = self.headers.get("Origin", "")
        return origin if origin in self.bridge.origins else ""

    def _reply(self, status: int, body: dict | None = None):
        data = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.authorized:
            self.send_header("Access-Control-Allow-Origin", self._origin())
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        port = self.bridge.port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_OPTIONS(self):
        if not self._host_ok() or not self._origin():
            self._reply(403, {"error": "Only the DOT extension may connect."})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._origin())
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DOT-Client")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._reply(405, {"error": "Use the DOT browser extension."})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0:
            self._reply(411, {"error": "Content-Length required."})
            return
        if length > MAX_BODY:
            self._reply(413, {"error": "Request too large."})
            return
        # Read the (small, bounded) body before any refusal: closing a socket with unread
        # data resets the connection on Windows, which would hide the real status code.
        raw = self.rfile.read(length)
        if not self._host_ok():
            self._reply(403, {"error": "Invalid host."})
            return
        if not self._origin() or not self.headers.get("X-DOT-Client", "").startswith(CLIENT_PREFIX):
            self._reply(403, {"error": "Only the DOT extension may connect."})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._reply(415, {"error": "JSON required."})
            return
        self.authorized = True
        self.bridge.saw_client(self.headers.get("X-DOT-Client", ""))
        try:
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("A JSON object is required.")
            if self.path == "/v1/hello":
                self._reply(200, {"app": "DOT", "version": __version__, "accepting": True})
            elif self.path == "/v1/downloads":
                status, body = self.bridge.handle_download(data)
                self._reply(status, body)
            else:
                self._reply(404, {"error": "Unknown route."})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)[:300]})
        except Exception as exc:
            self._reply(500, {"error": str(exc)[:300]})
