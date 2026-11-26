"""Save ordinary HLS/DASH media with FFmpeg (bundled with the installed DOT, else from PATH)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections import deque
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from . import paths
from .engine import DownloadError, Segment


def download_stream(manager, task, control):
    ffmpeg = paths.find_tool("ffmpeg")  # Bundled with the installed DOT, else PATH.
    if not ffmpeg:
        raise DownloadError("This is a streaming video. Install FFmpeg, then retry; direct video files work without it.")
    folder = manager.store.root / "parts" / task.id
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    part = manager._part_path(task, 0)
    context = {k.lower(): v for k, v in manager.request_context.get(task.id, {}).items()}
    if "authorization" in context:
        raise DownloadError("This stream requires an authorization header that cannot safely be forwarded to its segment servers.")
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "http,https,tcp,tls,crypto,data", "-rw_timeout", "15000000"]
    for key, option in (("user-agent", "-user_agent"), ("referer", "-referer")):
        value = context.get(key) or (task.referer if key == "referer" else "")
        if value:
            args += [option, value]
    if context.get("cookie"):
        cookies = SimpleCookie()
        cookies.load(context["cookie"])
        domain = urlsplit(task.url).hostname
        value = "\n".join(f"{key}={item.value}; path=/; domain={domain};" for key, item in cookies.items())
        if value:
            args += ["-cookies", value]
    # FFmpeg's default selection keeps the single best video and audio track. Explicit
    # "-map 0:v -map 0:a" would copy every quality variant of an HLS master playlist.
    # Subtitle and data tracks cannot be stream-copied into MP4, so they are dropped.
    args += ["-i", task.url, "-sn", "-dn", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(part)]
    with manager.lock:
        task.transport, task.status, task.total = "stream", "Downloading", None
        task.downloaded, task.active_connections = 0, 1
        task.segments = [Segment(0, None)]
        task.range_supported, task.validator = False, ""
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    errors = deque(maxlen=12)

    def drain():
        for line in iter(process.stderr.readline, b""):
            errors.append(line.decode("utf-8", "replace").strip())

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            control.wait(0.2)
            downloaded = part.stat().st_size if part.exists() else 0
            with manager.lock:
                task.downloaded = task.segments[0].downloaded = downloaded
        reader.join(timeout=2)
        if process.returncode:
            error = " ".join(errors)[-600:]
            error = error.replace(task.url, "[media URL]")
            error = re.sub(r"https?://\S+", "[media URL]", error)
            for value in context.values():
                if len(value) > 8:
                    error = error.replace(value, "[request context]")
            raise DownloadError("The stream could not be saved: " + (error or "FFmpeg returned an error."))
        control.check()
        if not part.exists() or part.stat().st_size == 0:
            raise DownloadError("The stream produced no media bytes.")
        with manager.lock:
            task.total = task.downloaded = task.segments[0].end = task.segments[0].downloaded = part.stat().st_size
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=2)
        process.stderr.close()
        with manager.lock:
            task.active_connections = 0
