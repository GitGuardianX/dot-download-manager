"""Video pages (YouTube and other sites) through yt-dlp.

The installed DOT bundles yt-dlp, Deno and FFmpeg; otherwise the copies on PATH are used.
DOT starts yt-dlp only after the user confirms in DOT's own window: one metadata read per
open window and one process per active download. The browser never starts it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag

from . import paths
from .engine import DownloadError, Segment
from .processes import Helper

INSTALL_HINT = ("Video sites such as YouTube need yt-dlp. Reinstall DOT (it includes yt-dlp), or install it "
                "with: winget install yt-dlp.yt-dlp")
SOLVER_HINT = ("Some qualities may be missing because yt-dlp could not solve YouTube's challenge. "
               "Try Settings > Update yt-dlp.")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
DESCRIBE_TIMEOUT = 60
PROGRESS = "download:DOT %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s"
DEFAULT_FORMAT = "bv*+ba/b/b*"  # Best video and audio; a silent video when that is all there is.
_found: dict = {"at": 0.0, "command": None}
_lock = threading.Lock()


def _python_has_ytdlp(python: str) -> bool:
    try:
        # -P keeps user site-packages but never imports from the current directory.
        return subprocess.run([python, "-P", "-c", "import yt_dlp"], capture_output=True, timeout=20,
                              creationflags=NO_WINDOW, cwd=Path.home()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def command() -> list[str] | None:
    """How to run yt-dlp: the user's updated copy, the bundled yt-dlp.exe, one on PATH, or a
    Python that has the yt_dlp module. A found tool is remembered for the session (see forget());
    a missing one is looked up again after a minute."""
    override = os.environ.get("DOT_YTDLP")  # Tests point this at a stand-in script.
    if override:
        return [sys.executable, override] if override.endswith(".py") else [override]
    with _lock:
        if _found["command"] or time.monotonic() - _found["at"] < 60:
            return _found["command"]
        found = None
        if executable := paths.find_tool("yt-dlp"):
            found = [executable]
        else:
            for name in ("python", "py"):
                python = shutil.which(name)
                if python and _python_has_ytdlp(python):
                    found = [python, "-P", "-m", "yt_dlp"]
                    break
        _found.update(at=time.monotonic(), command=found)
        return found


def forget():
    """Look yt-dlp up again next time (after an update)."""
    with _lock:
        _found.update(at=0.0, command=None)


def js_runtime_args() -> list[str]:
    """YouTube needs a JavaScript runtime: the bundled Deno, else Deno or Node on PATH."""
    if deno := paths.find_tool("deno"):
        return ["--js-runtimes", f"deno:{deno}"]
    if shutil.which("node"):
        return ["--js-runtimes", "node"]
    return []


def update_ytdlp() -> str:
    """Bring yt-dlp up to date. The bundled copy is first copied to the user's tools folder,
    which is always writable, so this also works for an all-users (Program Files) install."""
    target = paths.user_tools_dir() / ("yt-dlp" + paths.EXE)
    if not target.exists():
        source = paths.bundled_tools_dir() / ("yt-dlp" + paths.EXE)
        if not source.exists():
            raise DownloadError("This DOT has no bundled yt-dlp to update. Update the yt-dlp you installed "
                                "(for example: python -m pip install -U yt-dlp).")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    try:
        result = subprocess.run([str(target), "-U"], capture_output=True, timeout=300,
                                creationflags=NO_WINDOW, cwd=target.parent)
    except subprocess.TimeoutExpired as exc:
        raise DownloadError("Updating yt-dlp took too long. Check your internet connection.") from exc
    finally:
        forget()
    output = (result.stdout + result.stderr).decode("utf-8", "replace").splitlines()
    if result.returncode:
        raise DownloadError(_error(output))
    lines = [line.strip() for line in output if line.strip() and not line.startswith("[debug]")]
    return _clean(lines[-1]) if lines else "yt-dlp is up to date."


def _clean(text: str) -> str:
    return re.sub(r"https?://\S+", "[link]", text).strip()[-400:]


def _error(lines) -> str:
    lines = [line for line in lines if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR")]
    message = (errors or lines or ["yt-dlp stopped without saying why."])[-1]
    message = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", message)
    if "Sign in to confirm" in message:
        message += " DOT does not send your account login to yt-dlp."
    return _clean(message) or "yt-dlp could not download this video."


def browser_args(referer: str, context: dict | None) -> list[str]:
    """Replay the browser's own request for a stream: its referrer, user agent, Origin and the
    stream host's cookies. Hosts often refuse anything else. Authorization is never forwarded."""
    headers = {str(k).lower(): str(v) for k, v in (context or {}).items()}
    args = ["--referer", referer] if referer else []
    if headers.get("user-agent"):
        args += ["--user-agent", headers["user-agent"]]
    for name in ("origin", "cookie"):
        if headers.get(name):
            args += ["--add-header", f"{name.title()}:{headers[name]}"]
    return args


def choices(info: dict) -> list[dict]:
    """Quality options for the window: each with a yt-dlp format selector and a size estimate."""
    formats = info.get("formats") or []
    duration = info.get("duration") or 0
    # Reported size, else bitrate (kbit/s) x duration: streamed formats often report no size.
    size = lambda f: f.get("filesize") or f.get("filesize_approx") or int((f.get("tbr") or 0) * 125 * duration)
    video = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), default=None)
    # Same preference as the selector below: H.264 (plays everywhere), then any MP4, then bitrate.
    preference = lambda f: (str(f.get("vcodec", "")).startswith("avc1"), f.get("ext") == "mp4", f.get("tbr") or 0)
    options = []
    for height in sorted({f["height"] for f in video}, reverse=True)[:6]:
        best = max((f for f in video if f["height"] == height), key=preference)
        with_audio = best.get("acodec") not in (None, "none")
        estimate = None  # Unknown unless the site reports the video's own size.
        if size(best):
            estimate = size(best) + (0 if with_audio or not best_audio else size(best_audio))
        h = height  # Short name keeps the selector readable.
        kbps = round(best.get("tbr") or 0)
        # The last alternatives keep silent videos (video-only streams) downloadable.
        options.append({"label": f"{height}p" + (f" · {kbps} kbps" if kbps else ""), "height": height, "audio": False,
                        "size": estimate,
                        "format": f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
                                  f"/bv*[height<={h}]+ba/b[height<={h}]/bv*[height<={h}]/b/b*"})
    if audio:
        options.append({"label": "Audio only (M4A)", "audio": True, "size": size(best_audio) or None, "format": "ba[ext=m4a]/ba"})
    if not options:
        options.append({"label": "Best available", "audio": False, "size": None, "format": DEFAULT_FORMAT})
    return options


def stream_address(url: str) -> str:
    """A stream's address as servers see it. The "#..." part is never sent, but it hides the
    ".m3u8" ending from yt-dlp, which then trusts a wrong Content-Type (Dailymotion labels its
    playlists video/mp4) and saves the playlist text as if it were the video."""
    return urldefrag(url)[0]


def is_playlist(path: Path) -> bool:
    """A downloaded "video" that is really an HLS or DASH playlist (text)."""
    with path.open("rb") as stream:
        head = stream.read(512).lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"#EXTM3U") or head.startswith(b"<MPD") or (head.startswith(b"<?xml") and b"<MPD" in head)


def describe(url: str, timeout: float = DESCRIBE_TIMEOUT, referer: str = "", headers: dict | None = None,
             stream: bool = False) -> dict:
    """Read a video page, or a browser stream with the browser's request context (no download)."""
    runner = command()
    if not runner:
        raise DownloadError(INSTALL_HINT)
    target = stream_address(url) if stream else url
    try:
        helper = Helper(runner + ["-J", "--no-playlist", *js_runtime_args(), *browser_args(referer, headers), "--", target],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW, cwd=Path.home())
    except OSError as exc:
        raise DownloadError(f"Could not start yt-dlp: {exc}") from exc
    try:
        stdout, stderr = helper.process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        helper.stop()
        raise DownloadError("yt-dlp took too long to read this page.") from exc
    finally:
        helper.close()
    stderr = stderr.decode("utf-8", "replace")
    if helper.process.returncode:
        raise DownloadError(_error(stderr.splitlines()))
    try:
        info = json.loads(stdout)
    except ValueError as exc:
        raise DownloadError("yt-dlp returned details DOT could not read.") from exc
    if info.get("_type") == "playlist":
        raise DownloadError("This page is a playlist. Open a single video and try again.")
    return {"title": str(info.get("title") or "video")[:200], "duration": info.get("duration"),
            "site": str(info.get("extractor_key") or info.get("extractor") or ""), "choices": choices(info),
            "warning": SOLVER_HINT if "challenge solving failed" in stderr else ""}


def _number(text: str) -> int | None:
    try:
        return int(float(text))
    except ValueError:
        return None


def download_site(manager, task, control):
    """Run yt-dlp into this task's parts folder; the engine then verifies and publishes it.

    Used for video pages and for browser streams (HLS/DASH): yt-dlp fetches playlists and
    segments the way a browser does, while FFmpeg's own HTTP requests are refused by some
    stream hosts (it sends the Host header late). A stream task replays the browser's
    request context; a page task gets none (no browser cookies or logins reach yt-dlp)."""
    runner = command()
    if not runner:
        raise DownloadError(INSTALL_HINT)
    folder = manager.store.root / "parts" / task.id
    folder.mkdir(parents=True, exist_ok=True)
    args = runner + ["--no-playlist", "--newline", "--no-color", "--progress", "--progress-template", PROGRESS,
                     "--print", "after_move:DOTFILE %(filepath)s", "-f", task.site_format or DEFAULT_FORMAT,
                     "--merge-output-format", "mp4", "-P", str(folder), "-o", "download.%(ext)s",
                     "--concurrent-fragments", "4", "--socket-timeout", "20", "--retries", "5",
                     "--fragment-retries", "10", *js_runtime_args()]
    if ffmpeg := paths.find_tool("ffmpeg"):
        args += ["--ffmpeg-location", str(Path(ffmpeg).parent)]  # The folder, so ffprobe is found too.
    if manager.rate.limit:
        args += ["--limit-rate", str(manager.rate.limit)]
    if task.stream:
        args += browser_args(task.referer, manager.request_context.get(task.id))
    args += ["--", stream_address(task.url) if task.stream else task.url]
    with manager.lock:
        # "Connecting" while yt-dlp reads the page (a few seconds); "Downloading" from the first bytes.
        task.transport, task.status, task.active_connections = "site", "Connecting", 1
    helper = Helper(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, cwd=folder)
    process = helper.process
    lines, finals = deque(maxlen=20), []
    progress = {"done": 0, "last": 0, "file_total": 0}

    def read():
        for raw in iter(process.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("DOT "):
                parts = line.split()
                downloaded = _number(parts[1]) if len(parts) > 1 else None
                total = next((n for n in (_number(p) for p in parts[2:4]) if n), None)
                if downloaded is None:
                    continue
                if downloaded < progress["last"]:  # yt-dlp moved on to the next file (audio after video).
                    progress["done"] += progress["file_total"] or progress["last"]
                progress["last"], progress["file_total"] = downloaded, total or progress["file_total"]
                with manager.lock:
                    task.downloaded = progress["done"] + downloaded
                    if total:
                        task.total = max(task.total or 0, progress["done"] + total)
                    if task.status == "Connecting":
                        task.status = "Downloading"
            elif line.startswith("DOTFILE "):
                finals.append(line[8:].strip())
            elif line:
                lines.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            control.wait(0.2)
        reader.join(timeout=5)
        if process.returncode:
            raise DownloadError(_error(lines))
        control.check()
        path = Path(finals[-1]) if finals else next(iter(sorted(folder.glob("download.*"))), None)
        if not path or not path.is_file() or path.suffix == ".part":
            raise DownloadError("yt-dlp finished without producing a file.")
        if task.stream and is_playlist(path):  # Never report a playlist as the finished video.
            raise DownloadError("The server sent the stream's playlist instead of the video.")
        size = path.stat().st_size
        os.replace(path, manager._part_path(task, 0))
        with manager.lock:
            if path.suffix and Path(task.filename).suffix.lower() != path.suffix.lower():
                task.filename = manager._unique_name(task.directory, Path(task.filename).stem + path.suffix, task.id)
            task.total = task.downloaded = size
            task.segments = [Segment(0, size, size)]
    finally:
        if process.poll() is None:
            helper.stop()  # Pause, cancel or exit: ends yt-dlp's workers and FFmpeg too.
        helper.close()
        reader.join(timeout=5)
        if not reader.is_alive():  # Closing the pipe under a blocked reader would crash it.
            process.stdout.close()
        with manager.lock:
            task.active_connections = 0
