"""Drive DOT's real Tk window through the browser hand-off path (no browser needed).

An offer is posted to the real bridge exactly as the extension sends it; the test then
uses the Download File Info window, Cancel, minimized mode and Settings, and captures
only DOT's own windows to artifacts/.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotdm.bridge import EXTENSION_ID  # noqa: E402
from dotdm.ui import Application, DownloadConfirmation, SettingsDialog, VideoConfirmation  # noqa: E402
from tests.test_bridge import free_port  # noqa: E402
from tests.test_downloads import BODY, FixtureServer  # noqa: E402
from tools.smoke_ui import capture  # noqa: E402

ORIGIN = f"chrome-extension://{EXTENSION_ID}"
OUT = ROOT / "artifacts"


def post(port: int, body: dict) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode()
    connection.request("POST", "/v1/downloads", body=data, headers={
        "Origin": ORIGIN, "X-DOT-Client": "dot-extension/0.3", "Content-Type": "application/json",
        "Content-Length": str(len(data))})
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


def pump(app, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(0.02)


def dialog_of(app, kind=DownloadConfirmation, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        found = [w for w in app.winfo_children() if isinstance(w, kind) and w.winfo_exists()]
        if found:
            return found[0]
        time.sleep(0.05)
    raise AssertionError(f"no {kind.__name__} window appeared")


def main():
    OUT.mkdir(exist_ok=True)  # A fresh clone has no artifacts/ folder.
    server = FixtureServer()
    report = {}
    try:
        with tempfile.TemporaryDirectory(prefix="dot-ui-") as temp:
            root, port = Path(temp), free_port()
            app = Application(root / "state", root / "files", tray=False)  # Never collide with a real DOT's tray.
            app.manager.configure(browser_enabled=True, browser_port=port)
            app.sync_bridge()
            pump(app, 0.6)
            assert app.bridge.server and "listening" in app.footer.cget("text"), app.footer.cget("text")

            # 1. A browser hand-off opens Download File Info with the checked size; Start completes it.
            status, _ = post(port, {"url": server.url("/range"), "source": "download", "cookies": "sid=1", "userAgent": "UI-Test"})
            assert status == 202, status
            dialog = dialog_of(app)
            pump(app, 0.4)
            assert dialog.file_size.cget("text") == "2.0 MiB", dialog.file_size.cget("text")
            capture(dialog, OUT / "ui-0.3-download-info.png")
            dialog.submit(False)
            end = time.monotonic() + 15
            while time.monotonic() < end and not any(t["status"] == "Completed" for t in app.manager.snapshot()):
                pump(app, 0.1)
            task = next(t for t in app.manager.snapshot() if t["status"] == "Completed")
            assert (Path(task["directory"]) / task["filename"]).read_bytes() == BODY
            assert not app.bridge.open_offers, "closing the window frees its slot"
            report["start_download"] = {"file": task["filename"], "category_folder": Path(task["directory"]).name}

            # 2. Cancel creates nothing and frees the slot.
            post(port, {"url": server.url("/range"), "source": "link"})
            dialog_of(app).destroy()
            pump(app, 0.2)
            assert len(app.manager.snapshot()) == 1 and not app.bridge.open_offers
            report["cancel"] = "no task created"

            # 3. With DOT minimized, the window appears on its own, on screen (as IDM's does).
            app.iconify()
            pump(app, 0.5)
            post(port, {"url": server.url("/error"), "source": "link"})
            dialog = dialog_of(app)
            pump(app, 0.4)
            assert not dialog.anchored and app.state() == "iconic"
            x, y = dialog.winfo_rootx(), dialog.winfo_rooty()
            assert 0 <= x < dialog.winfo_screenwidth() and 0 <= y < dialog.winfo_screenheight(), (x, y)
            assert "403" in dialog.note.cget("text"), dialog.note.cget("text")
            capture(dialog, OUT / "ui-0.3-download-info-warning.png")
            dialog.destroy()
            app.deiconify()
            pump(app, 0.4)
            report["minimized"] = {"dialog_on_screen": [x, y], "main_window_stayed_minimized": True,
                                   "refusal_reason_shown": True}

            # 3b. A video page (YouTube-style): DOT reads it with yt-dlp, offers qualities, downloads it.
            os.environ["DOT_YTDLP"] = str(ROOT / "tests" / "fake_ytdlp.py")
            status, _ = post(port, {"url": "https://www.youtube.com/watch?v=abc123", "source": "page"})
            assert status == 202, status
            video = dialog_of(app, VideoConfirmation)
            end = time.monotonic() + 15
            while time.monotonic() < end and str(video.start.cget("state")) != "normal":
                pump(app, 0.1)
            assert "Fake: Clip/Title?" in video.title_label.cget("text"), video.title_label.cget("text")
            assert video.quality.get().startswith("1080p"), video.quality.get()
            pump(app, 0.3)
            capture(video, OUT / "ui-0.3-download-video.png")
            video.submit(False)
            end = time.monotonic() + 20
            while time.monotonic() < end and not any(t["transport"] == "site" and t["status"] == "Completed"
                                                     for t in app.manager.snapshot()):
                pump(app, 0.1)
            site_task = next(t for t in app.manager.snapshot() if t["transport"] == "site")
            assert site_task["status"] == "Completed", site_task["error"]
            report["video_page"] = {"title_shown": True, "default_quality": "1080p", "saved_as": site_task["filename"],
                                    "category_folder": Path(site_task["directory"]).name}

            # 4. Settings: turning the extension connection off stops the bridge.
            SettingsDialog(app)
            settings = dialog_of(app, SettingsDialog)
            pump(app, 0.4)
            capture(settings, OUT / "ui-0.3-settings.png")
            settings.browser.set(False)
            settings.save()
            pump(app, 0.4)
            assert app.bridge.server is None and "off" in app.footer.cget("text"), app.footer.cget("text")
            report["settings"] = "bridge stopped when unticked"

            app.close()
            end = time.monotonic() + 10
            while time.monotonic() < end:
                try:
                    app.update()
                    if not app.winfo_exists():
                        break
                except Exception:
                    break
                time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps({"status": "passed", **report}, indent=2))


if __name__ == "__main__":
    main()
