"""Concurrent range downloads with persistent, validated partial files.

Only the Python standard library is needed at runtime. The transport deliberately
requires a strong validator before combining independent HTTP responses.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import os
import queue
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

ACTIVE = {"Connecting", "Downloading", "Verifying", "Pausing"}
CHUNK = 64 * 1024
TIMEOUT = 8
PROGRESS_SAVE_SECONDS = 2
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
# Statuses written by earlier versions that no longer exist; they are restored as Paused.
RETIRED_STATUSES = {"Waiting for browser"}


def request_headers(values: dict | None) -> dict[str, str]:
    """Keep request context in memory; never accept hop-by-hop or range headers."""
    result = {}
    blocked = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding", "range", "if-range", "proxy-authorization"}
    for key, value in (values or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Invalid browser request headers.")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or "\r" in value or "\n" in value:
            raise ValueError("Invalid browser request header.")
        if key.lower() not in blocked and not key.lower().startswith("sec-"):
            if len(value) > 65536:
                raise ValueError("Browser request header is too large.")
            result[key] = value
    return result


class DownloadError(Exception):
    pass


class InstanceLocked(DownloadError):
    """Another DOT process already owns this data folder."""


class RepresentationChanged(DownloadError):
    pass


class RangeUnsupported(DownloadError):
    pass


class Interrupted(Exception):
    pass


def validate_url(url: str) -> str:
    url = url.strip()
    if any(ord(c) < 33 for c in url):
        raise ValueError("Use a URL without spaces or control characters (encode spaces as %20).")
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("Enter an HTTP or HTTPS URL.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URLs containing a username or password are not supported.")
        _ = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise ValueError(str(exc)) from exc
    return url


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:180].rstrip(" .")
    if not name:
        name = "download.bin"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I):
        name = "_" + name
    return name


def url_filename(url: str) -> str:
    return safe_filename(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))


def disposition_filename(value: str) -> str | None:
    message = Message()
    message["Content-Disposition"] = value
    name = message.get_filename()
    return safe_filename(name) if name else None


def category(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    for label, extensions in {
        "Video": ".mp4 .mkv .webm .mov .avi .m4v",
        "Music": ".mp3 .flac .wav .m4a .ogg .aac",
        "Archives": ".zip .7z .rar .tar .gz .xz .bz2",
        "Documents": ".pdf .txt .doc .docx .xls .xlsx .epub .csv",
        "Programs": ".exe .msi .msix .apk .dmg .iso",
        "Images": ".jpg .jpeg .png .gif .webp .svg .avif",
    }.items():
        if ext and ext in extensions.split():
            return label
    return "Other"


def refusal_message(exc: BaseException) -> str:
    """Explain an access refusal in plain terms; empty when `exc` is not a refusal."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        headers = getattr(exc, "headers", None) or {}
        if headers.get("cf-mitigated") == "challenge":
            return f"HTTP {exc.code}: the site requires a browser verification check, so only the browser can download this file."
        return f"HTTP {exc.code}: the server refused DOT. This file needs the browser's own session; download it in the browser."
    return ""


@dataclass
class Segment:
    start: int
    end: int | None  # Exclusive; None for an unknown-length sequential response.
    downloaded: int = 0


@dataclass
class Task:
    id: str
    url: str
    directory: str
    filename: str
    connections: int = 8
    status: str = "Queued"
    total: int | None = None
    downloaded: int = 0
    speed: float = 0
    created: float = field(default_factory=time.time)
    completed: float | None = None
    scheduled_at: float | None = None
    error: str = ""
    range_supported: bool = False
    validator: str = ""
    etag: str = ""
    last_modified: str = ""
    use_server_filename: bool = True
    referer: str = ""
    sha256: str = ""
    segments: list[Segment] = field(default_factory=list)
    active_connections: int = 0
    category_name: str = ""
    description: str = ""
    transport: str = "http"  # "http", "stream" (FFmpeg) or "site" (yt-dlp: video page or browser stream).
    browser_context: bool = False
    stream: bool = False
    site_format: str = ""   # yt-dlp format selector for "site" tasks.


@dataclass
class Metadata:
    total: int | None
    ranged: bool
    etag: str
    modified: str
    validator: str
    filename: str | None


class Control:
    def __init__(self):
        self.stop = threading.Event()
        self.abort = threading.Event()
        self.remove = False
        self.thread: threading.Thread | None = None

    def check(self):
        if self.stop.is_set() or self.abort.is_set():
            raise Interrupted()

    def wait(self, seconds: float):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.stop.wait(min(0.1, max(0, until - time.monotonic())))
        self.check()


class RateLimiter:
    """One aggregate budget shared by all tasks and connections."""
    def __init__(self):
        self.lock = threading.Lock()
        self.limit = 0
        self.next_slot = 0.0

    def configure(self, limit: int):
        with self.lock:
            if self.limit != limit:
                self.limit = limit
                self.next_slot = time.monotonic()

    def consume(self, count: int, control: Control):
        with self.lock:
            if not self.limit:
                return
            now = time.monotonic()
            self.next_slot = max(now, self.next_slot) + count / self.limit
            delay = self.next_slot - now
        control.wait(delay)


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if redirected and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            for key in list(redirected.headers):
                if key.lower() in {"cookie", "authorization", "origin"} or key.lower().startswith("x-"):
                    redirected.remove_header(key)
        return redirected


class StateStore:
    """Atomic snapshots and an OS-held single-instance lock."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.lockfile = (self.root / "instance.lock").open("a+b")
        try:
            if os.fstat(self.lockfile.fileno()).st_size == 0:
                self.lockfile.write(b"0")
                self.lockfile.flush()
            self.lockfile.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lockfile.close()
            raise InstanceLocked("DOT is already using this data folder. Close the other instance first.") from exc

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("tasks"), list):
                raise ValueError("Unsupported state format")
            return data
        except (ValueError, OSError) as exc:
            self.close()
            raise DownloadError(f"Cannot read {self.path}. Keep it for recovery: {exc}") from exc

    def save(self, data: dict):
        temporary = self.root / "state.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def close(self):
        if not self.lockfile.closed:
            self.lockfile.close()


class DownloadManager:
    def __init__(self, state_dir: Path, download_dir: Path | None = None):
        self.lock = threading.RLock()
        self.store = StateStore(Path(state_dir))
        data = self.store.load()
        self.settings = {"download_dir": str((download_dir or Path.cwd() / "downloads").resolve()),
                         "connections": 8, "max_active": 2, "speed_limit": 0,
                         "browser_enabled": False, "browser_port": 17643, "category_dirs": {},
                         "keep_in_tray": True, "tray_hint_shown": False, "browser_guide_done": False,
                         # Used only by experimental/click_preview; the 0.3 bridge authenticates by origin.
                         "browser_token": secrets.token_urlsafe(24)}
        self.settings.update(data.get("settings", {}))
        self.tasks: dict[str, Task] = {}
        valid_fields = {f.name for f in fields(Task)} - {"segments"}
        try:
            for row in data.get("tasks", []):
                task = Task(**{k: v for k, v in row.items() if k in valid_fields})
                if not re.fullmatch(r"[a-f0-9]{32}", task.id):
                    raise ValueError("Invalid task identifier")
                task.segments = [Segment(**s) for s in row.get("segments", [])]
                if task.status in ACTIVE | RETIRED_STATUSES:
                    task.status = "Paused"
                task.speed = 0
                task.active_connections = 0
                self.tasks[task.id] = task
        except (TypeError, ValueError) as exc:
            self.store.close()
            raise DownloadError(f"Invalid saved task data: {exc}") from exc
        self.controls: dict[str, Control] = {}
        self.request_context: dict[str, dict] = {}
        self.rate = RateLimiter()
        self.closing = threading.Event()
        self.persistence_error = ""
        self._samples: dict[str, tuple[float, int]] = {}
        self._last_save = 0.0
        self.scheduler = threading.Thread(target=self._schedule, name="dot-queue", daemon=True)
        self.scheduler.start()

    def _save(self):
        with self.lock:
            self.store.save({"version": 1, "settings": self.settings,
                             "tasks": [asdict(t) for t in self.tasks.values()]})
            self.persistence_error = ""
            self._last_save = time.monotonic()

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [asdict(t) for t in self.tasks.values()]

    def get_settings(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.settings)

    def configure(self, **values):
        for key, low, high in (("connections", 1, 16), ("max_active", 1, 6), ("speed_limit", 0, 2**40)):
            if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
                raise ValueError(f"{key} must be between {low} and {high}.")
        if "browser_port" in values and not 1024 <= int(values["browser_port"]) <= 65535:
            raise ValueError("Browser port must be between 1024 and 65535.")
        if "download_dir" in values:
            values["download_dir"] = str(Path(values["download_dir"]).expanduser().resolve())
        with self.lock:
            self.settings.update({k: v for k, v in values.items() if k in self.settings})
            self.rate.configure(self.settings["speed_limit"])
            self._save()

    def _unique_name(self, directory: str, filename: str, exclude: str = "") -> str:
        name = safe_filename(filename)
        stem, suffix = Path(name).stem, Path(name).suffix
        occupied = {os.path.normcase(str(Path(t.directory) / t.filename)) for t in self.tasks.values() if t.id != exclude}
        for count in range(10000):
            candidate = name if count == 0 else f"{stem} ({count}){suffix}"
            path = Path(directory) / candidate
            if not path.exists() and os.path.normcase(str(path)) not in occupied:
                return candidate
        raise DownloadError("Too many files with this name.")

    def add(self, url: str, *, directory: str | None = None, filename: str | None = None,
            connections: int | None = None, scheduled_at: float | None = None,
            paused: bool = False, referer: str = "", headers: dict | None = None,
            browser_context: bool = False, category_name: str = "", description: str = "", stream: bool = False,
            site: bool = False, site_format: str = "") -> str:
        url = validate_url(url)
        stream = not site and bool(stream or urllib.parse.urlsplit(url).path.lower().endswith((".m3u8", ".mpd")))
        if stream and not filename:
            filename = Path(url_filename(url)).stem + ".mp4"
        if site and not filename:
            filename = "video.mp4"
        if referer:
            referer = validate_url(referer)
        context = request_headers(headers)
        with self.lock:
            if self.closing.is_set():
                raise DownloadError("The download manager is closing.")
            count = self.settings["connections"] if connections is None else int(connections)
            if not 1 <= count <= 16:
                raise ValueError("Choose between 1 and 16 connections.")
            destination = str(Path(directory or self.settings["download_dir"]).expanduser().resolve())
            Path(destination).mkdir(parents=True, exist_ok=True)
            task = Task(id=uuid.uuid4().hex, url=url, directory=destination,
                        filename=self._unique_name(destination, filename or url_filename(url)), connections=count,
                        use_server_filename=not bool(filename), referer=referer, scheduled_at=scheduled_at,
                        browser_context=browser_context, category_name=category_name, description=description[:500], stream=stream,
                        transport="site" if site else "http", site_format=site_format[:300] if site or stream else "")
            self.request_context[task.id] = context
            if paused:
                task.status = "Paused"
            elif scheduled_at and scheduled_at > time.time():
                task.status = "Scheduled"
            self.tasks[task.id] = task
            self._save()
            return task.id

    def pause(self, task_id: str):
        with self.lock:
            task = self.tasks[task_id]
            if task.status == "Completed":
                return
            control = self.controls.get(task_id)
            if control:
                task.status = "Pausing"
                control.stop.set()
            elif task.status != "Completed":
                task.status = "Paused"
                task.speed = 0
            self._save()

    def resume(self, task_id: str):
        with self.lock:
            task = self.tasks[task_id]
            if task_id not in self.controls and task.status != "Completed":
                task.status = "Queued"
                task.error = ""
                task.scheduled_at = None
                self._save()

    def pause_all(self):
        with self.lock:
            for task in list(self.tasks.values()):
                if task.status in ACTIVE | {"Queued", "Scheduled"}:
                    self.pause(task.id)

    def resume_all(self):
        with self.lock:
            for task in list(self.tasks.values()):
                if task.status in {"Paused", "Failed"}:
                    self.resume(task.id)

    def remove(self, task_id: str):
        """Remove history and temporary parts, preserving the completed output."""
        self.remove_many([task_id])

    def remove_many(self, task_ids, delete_files: bool = False) -> dict:
        """Remove downloads from the library in one step, with one save however many there are.

        Unfinished downloads are stopped and their temporary parts deleted. Downloaded files stay
        on disk unless delete_files is set; then only each completed download's own file is
        deleted (never a folder, and nothing for unfinished downloads)."""
        folders, files, removed = [], [], 0
        with self.lock:
            for task_id in dict.fromkeys(task_ids):
                task = self.tasks.get(task_id)
                if not task:
                    continue
                removed += 1
                if delete_files and task.status == "Completed":
                    files.append(Path(task.directory) / task.filename)
                if control := self.controls.get(task_id):  # Running: its worker removes it as it stops.
                    control.remove = True
                    control.stop.set()
                    task.status = "Pausing"
                    continue
                self.tasks.pop(task_id, None)
                self.request_context.pop(task_id, None)
                folders.append(self.store.root / "parts" / task_id)
            if removed:
                self._save()
        for folder in folders:  # Disk work happens after the library is saved and unlocked.
            shutil.rmtree(folder, ignore_errors=True)
        deleted = 0
        for path in files:
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                pass  # Open in another program, or already gone: the list entry is removed anyway.
        return {"removed": removed, "files_deleted": deleted}

    def _schedule(self):
        while not self.closing.wait(0.2):
            with self.lock:
                self.rate.configure(self.settings["speed_limit"])
                now = time.monotonic()
                for task in self.tasks.values():
                    old_time, old_bytes = self._samples.get(task.id, (now, task.downloaded))
                    elapsed = now - old_time
                    if elapsed >= 0.6:
                        instant = max(0, task.downloaded - old_bytes) / elapsed
                        task.speed = (task.speed * 0.4 + instant * 0.6) if task.status == "Downloading" else 0
                        self._samples[task.id] = (now, task.downloaded)
                    elif task.id not in self._samples:
                        self._samples[task.id] = (now, task.downloaded)
                available = self.settings["max_active"] - len(self.controls)
                for task in self.tasks.values():
                    ready = task.status == "Queued" or (task.status == "Scheduled" and (task.scheduled_at or 0) <= time.time())
                    if ready and available > 0 and task.id not in self.controls:
                        control = Control()
                        self.controls[task.id] = control
                        task.status = "Connecting"
                        task.error = ""
                        control.thread = threading.Thread(target=self._run, args=(task, control), daemon=True, name=f"dot-{task.id[:6]}")
                        control.thread.start()
                        available -= 1
                # State changes are saved where they happen. This periodic checkpoint only
                # preserves byte progress, so it runs only while a transfer is active.
                if self.controls and now - self._last_save >= PROGRESS_SAVE_SECONDS:
                    try:
                        self._save()
                    except OSError as exc:
                        self.persistence_error = str(exc)

    def _open(self, task: Task, extra: dict | None = None, timeout: float | None = None):
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if task.referer:
            headers["Referer"] = task.referer
        headers.update(self.request_context.get(task.id, {}))
        headers.update(extra or {})
        opener = urllib.request.build_opener(SafeRedirects())
        return opener.open(urllib.request.Request(task.url, headers=headers), timeout=timeout or TIMEOUT)

    @staticmethod
    def _length(headers) -> int | None:
        value = headers.get("Content-Length")
        if value is None:
            return None
        if not re.fullmatch(r"\d+", value.strip()):
            raise DownloadError("Invalid Content-Length from the server.")
        return int(value)

    @staticmethod
    def _encoding(headers):
        if headers.get("Content-Encoding", "identity").lower().strip() not in ("", "identity"):
            raise DownloadError("Server sent compressed content despite Accept-Encoding: identity.")

    @staticmethod
    def _validator(headers) -> str:
        etag = headers.get("ETag", "").strip()
        if re.fullmatch(r'"[^"\x00-\x20]*"', etag):
            return etag
        # RFC 9110 8.8.2.2: Last-Modified is strong when Date is >= 60s later.
        try:
            modified = parsedate_to_datetime(headers["Last-Modified"])
            dated = parsedate_to_datetime(headers["Date"])
            if modified.tzinfo and dated.tzinfo and (dated - modified).total_seconds() >= 60:
                return headers["Last-Modified"]
        except (KeyError, ValueError, TypeError, IndexError):
            pass
        return ""

    @staticmethod
    def _content_range(value: str) -> tuple[int, int, int]:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value.strip())
        if not match:
            raise RangeUnsupported("Invalid Content-Range from server.")
        start, end, total = map(int, match.groups())
        if not 0 <= start <= end < total:
            raise RangeUnsupported("Inconsistent Content-Range from server.")
        return start, end, total

    def _probe(self, task: Task, control: Control, timeout: float | None = None) -> Metadata:
        control.check()
        try:
            response = self._open(task, {"Range": "bytes=0-0"}, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and exc.headers.get("Content-Range", "").strip() == "bytes */0":
                exc.close()
                return Metadata(0, False, "", "", "", None)
            # Some CDNs refuse Range probes but permit an ordinary GET.
            if exc.code in (400, 403, 405, 416) and exc.headers.get("cf-mitigated") != "challenge":
                exc.close()
                response = self._open(task, timeout=timeout)
            else:
                exc.close()  # Headers stay readable for the caller after closing.
                raise
        with response:
            self._encoding(response.headers)
            self._check_content(task, response.headers)
            ranged = response.status == 206
            if ranged:
                start, end, total = self._content_range(response.headers.get("Content-Range", ""))
                if (start, end) != (0, 0) or len(response.read(2)) != 1:
                    raise RangeUnsupported("Server did not return the requested one-byte probe.")
            else:
                total = self._length(response.headers)
                if response.status != 200:
                    raise DownloadError(f"Unexpected HTTP status {response.status}.")
            return Metadata(total, ranged, response.headers.get("ETag", ""),
                            response.headers.get("Last-Modified", ""), self._validator(response.headers),
                            disposition_filename(response.headers.get("Content-Disposition", "")))

    @staticmethod
    def _check_content(task: Task, headers):
        if "text/html" in headers.get("Content-Type", "").lower() and category(task.filename) in {"Video", "Music", "Archives", "Programs"}:
            raise DownloadError("The site returned a web page instead of the file. Open the source page in your browser and capture the download again.")

    def inspect(self, data: dict, timeout: float | None = None) -> Metadata:
        """Probe a URL with the given request context without creating a task."""
        task = Task(uuid.uuid4().hex, validate_url(data["url"]), "", safe_filename(data.get("filename") or url_filename(data["url"])), referer=data.get("referer", ""))
        self.request_context[task.id] = request_headers(data.get("headers"))
        try:
            return self._probe(task, Control(), timeout)
        finally:
            self.request_context.pop(task.id, None)

    def _part_path(self, task: Task, index: int) -> Path:
        return self.store.root / "parts" / task.id / f"{index:05d}.part"

    def _prepare(self, task: Task, meta: Metadata, force_single: bool):
        folder = self.store.root / "parts" / task.id
        with self.lock:
            reusable = bool(meta.ranged and meta.validator and not force_single and task.validator == meta.validator
                            and task.total == meta.total and task.segments)
            cursor = 0
            if reusable:
                for segment in task.segments:
                    if segment.start != cursor or segment.end is None or segment.end < segment.start:
                        reusable = False
                        break
                    cursor = segment.end
                reusable = reusable and cursor == meta.total
            if not reusable:
                shutil.rmtree(folder, ignore_errors=True)
                task.segments = []
                task.downloaded = 0
                task.sha256 = ""
            folder.mkdir(parents=True, exist_ok=True)
            if task.use_server_filename and meta.filename and not reusable:
                task.filename = self._unique_name(task.directory, meta.filename, task.id)
            task.total, task.range_supported = meta.total, meta.ranged
            task.etag, task.last_modified, task.validator = meta.etag, meta.modified, meta.validator
            parallel = bool(meta.ranged and meta.validator and meta.total and not force_single)
            if not task.segments:
                if parallel:
                    size = max(256 * 1024, math.ceil(meta.total / (task.connections * 4)))
                    task.segments = [Segment(start, min(start + size, meta.total)) for start in range(0, meta.total, size)]
                else:
                    task.segments = [Segment(0, meta.total)]
            for index, segment in enumerate(task.segments):
                part = self._part_path(task, index)
                length = part.stat().st_size if part.exists() else 0
                if segment.end is not None and length > segment.end - segment.start:
                    part.unlink()
                    length = 0
                segment.downloaded = length
            task.downloaded = sum(s.downloaded for s in task.segments)
            task.status = "Downloading"
            self._samples[task.id] = (time.monotonic(), task.downloaded)
            self._save()
            return parallel

    def _progress(self, task: Task, segment: Segment, count: int):
        with self.lock:
            segment.downloaded += count
            task.downloaded += count

    def _read_body(self, task: Task, index: int, response, control: Control, expected: int | None):
        segment = task.segments[index]
        received = 0
        with self._part_path(task, index).open("ab", buffering=0) as stream:
            while True:
                control.check()
                limit = self.rate.limit
                size = min(CHUNK, max(1024, limit // 16)) if limit else CHUNK
                if expected is not None:
                    if received == expected:
                        break
                    size = min(size, expected - received)
                block = response.read1(size) if hasattr(response, "read1") else response.read(size)
                if not block:
                    break
                self.rate.consume(len(block), control)
                control.check()
                stream.write(block)
                received += len(block)
                self._progress(task, segment, len(block))
            os.fsync(stream.fileno())
        if expected is not None and received != expected:
            raise ConnectionError(f"Connection ended early ({received} of {expected} response bytes).")

    def _download_segment(self, task: Task, index: int, control: Control):
        segment = task.segments[index]
        retries = 0
        while segment.start + segment.downloaded < segment.end:
            control.check()
            start = segment.start + segment.downloaded
            try:
                with self._open(task, {"Range": f"bytes={start}-{segment.end - 1}", "If-Range": task.validator}) as response:
                    self._encoding(response.headers)
                    if response.status == 200:
                        raise RangeUnsupported("Server ignored a byte range or the file changed; restarting as one stream.")
                    if response.status != 206:
                        raise DownloadError(f"Unexpected HTTP status {response.status} for a range.")
                    a, b, total = self._content_range(response.headers.get("Content-Range", ""))
                    if a != start or b >= segment.end or total != task.total:
                        raise RangeUnsupported("Server returned the wrong byte range; restarting as one stream.")
                    if task.etag and response.headers.get("ETag", task.etag) != task.etag:
                        raise RepresentationChanged("The file changed during transfer.")
                    length = self._length(response.headers)
                    if length is not None and length != b - a + 1:
                        raise RangeUnsupported("Content-Length does not match Content-Range.")
                    self._read_body(task, index, response, control, b - a + 1)
                retries = 0
            except (urllib.error.URLError, OSError, ConnectionError, http.client.HTTPException) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    code = exc.code
                    exc.close()
                    if code == 416:
                        raise RepresentationChanged("The file size changed during transfer.") from exc
                    if code < 500 and code not in (408, 429):
                        raise
                retries += 1
                if retries > 3:
                    raise DownloadError(f"Transfer failed after 3 retries: {exc}") from exc
                control.wait(0.4 * 2 ** (retries - 1))

    def _parallel(self, task: Task, control: Control):
        work = queue.Queue()
        for index, segment in enumerate(task.segments):
            if segment.downloaded < segment.end - segment.start:
                work.put(index)
        errors = []

        def worker():
            with self.lock:
                task.active_connections += 1
            try:
                while not work.empty():
                    control.check()
                    try:
                        index = work.get_nowait()
                    except queue.Empty:
                        return
                    self._download_segment(task, index, control)
            except Interrupted:
                pass
            except Exception as exc:
                with self.lock:
                    errors.append(exc)
                control.abort.set()
            finally:
                with self.lock:
                    task.active_connections -= 1

        with ThreadPoolExecutor(max_workers=task.connections, thread_name_prefix="dot-range") as pool:
            futures = [pool.submit(worker) for _ in range(min(task.connections, work.qsize()))]
            for future in futures:
                future.result()
        if errors:
            raise errors[0]
        control.check()

    def _sequential(self, task: Task, control: Control):
        part = self._part_path(task, 0)
        part.write_bytes(b"")
        with self.lock:
            task.downloaded = task.segments[0].downloaded = 0
            task.active_connections = 1
        try:
            with self._open(task) as response:
                if response.status != 200:
                    raise DownloadError(f"Expected a full file; server returned HTTP {response.status}.")
                self._encoding(response.headers)
                self._check_content(task, response.headers)
                actual = self._length(response.headers)
                with self.lock:
                    task.total = task.segments[0].end = actual
                self._read_body(task, 0, response, control, actual)
                if actual is None:
                    with self.lock:
                        task.total = task.segments[0].end = task.downloaded
        finally:
            with self.lock:
                task.active_connections = 0

    def _assemble(self, task: Task, control: Control):
        with self.lock:
            task.status = "Verifying"
        directory = Path(task.directory)
        temporary = directory / f".dot-{task.id}.assembling"
        digest = hashlib.sha256()
        written = 0
        try:
            with temporary.open("wb") as output:
                for index, segment in enumerate(task.segments):
                    control.check()
                    part = self._part_path(task, index)
                    expected = segment.end - segment.start
                    if not part.exists() or part.stat().st_size != expected:
                        raise DownloadError("A partial file is incomplete; resume to retry it.")
                    with part.open("rb") as stream:
                        while block := stream.read(1024 * 1024):
                            control.check()
                            output.write(block)
                            digest.update(block)
                            written += len(block)
                output.flush()
                os.fsync(output.fileno())
            if task.total is not None and written != task.total:
                raise DownloadError("Assembled size does not match the server's file size.")
            with self.lock:
                task.sha256 = digest.hexdigest()
                self._save()
            control.check()
            while True:
                target = directory / task.filename
                try:
                    # Atomic, exclusive publication on NTFS. Never replace an existing user file.
                    os.link(temporary, target)
                    break
                except FileExistsError:
                    with self.lock:
                        task.filename = self._unique_name(task.directory, task.filename, task.id)
                except OSError:
                    # Filesystems such as exFAT may not implement hard links.
                    try:
                        output = target.open("xb")
                    except FileExistsError:
                        with self.lock:
                            task.filename = self._unique_name(task.directory, task.filename, task.id)
                        continue
                    try:
                        with output, temporary.open("rb") as source:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                    except BaseException:
                        target.unlink(missing_ok=True)
                        raise
                    break
            with self.lock:
                task.status = "Completed"
                task.completed = time.time()
                task.total = task.downloaded = written
                task.speed = 0
                self._save()
            shutil.rmtree(self.store.root / "parts" / task.id, ignore_errors=True)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self, task: Task, control: Control):
        outcome = None  # (status, error) applied together with releasing the control.
        try:
            if task.transport == "site":
                from .sites import download_site
                download_site(self, task, control)
                self._assemble(task, control)
                return
            if task.stream or urllib.parse.urlsplit(task.url).path.lower().endswith((".m3u8", ".mpd")):
                from .sites import command, download_site
                from .streams import download_stream
                if command():  # Fetches playlists and segments like a browser; FFmpeg only joins them.
                    try:
                        download_site(self, task, control)
                    except DownloadError as first:
                        control.check()  # A pause or cancel is not a failure to recover from.
                        try:
                            download_stream(self, task, control)  # Some streams only FFmpeg reads.
                        except DownloadError:
                            raise first from None  # yt-dlp's explanation is the more useful one.
                else:
                    download_stream(self, task, control)
                self._assemble(task, control)
                return
            # Recover a crash between publication and the completed-state checkpoint.
            existing = Path(task.directory) / task.filename
            if task.sha256 and existing.is_file() and existing.stat().st_size == task.total:
                with existing.open("rb") as stream:
                    recovered = hashlib.file_digest(stream, "sha256").hexdigest() == task.sha256
                if recovered:
                    with self.lock:
                        task.status = "Completed"
                        task.completed = time.time()
                    return
            for attempt in range(2):
                meta = self._probe(task, control)
                parallel = self._prepare(task, meta, force_single=bool(attempt))
                try:
                    if meta.total == 0:
                        self._part_path(task, 0).write_bytes(b"")
                    elif parallel:
                        self._parallel(task, control)
                    else:
                        self._sequential(task, control)
                    self._assemble(task, control)
                    break
                except (RangeUnsupported, RepresentationChanged):
                    if control.stop.is_set():
                        raise Interrupted()
                    if attempt:
                        raise
                    control.abort.clear()
        except Interrupted:
            outcome = ("Paused", None)
        except Exception as exc:
            outcome = ("Failed", refusal_message(exc) or str(exc) or type(exc).__name__)
        finally:
            with self.lock:
                # The final status and the release of this task's control happen in one locked
                # step, so a Resume/Retry clicked as the task stops is never silently ignored.
                if outcome and task.status != "Completed":
                    task.status = outcome[0]
                    if outcome[1] is not None:
                        task.error = outcome[1]
                task.speed = 0
                task.active_connections = 0
                if control.remove:
                    self.tasks.pop(task.id, None)
                    self.request_context.pop(task.id, None)
                    shutil.rmtree(self.store.root / "parts" / task.id, ignore_errors=True)
                self.controls.pop(task.id, None)
                if task.status == "Completed":
                    self.request_context.pop(task.id, None)
                try:
                    self._save()
                except OSError as exc:
                    self.persistence_error = str(exc)

    def close(self):
        self.closing.set()
        self.scheduler.join()
        with self.lock:
            running = list(self.controls.values())
            for task in self.tasks.values():
                if task.status == "Queued":
                    task.status = "Paused"
            for control in running:
                control.stop.set()
        for control in running:
            if control.thread:
                control.thread.join()
        try:
            self._save()
        finally:
            self.store.close()
