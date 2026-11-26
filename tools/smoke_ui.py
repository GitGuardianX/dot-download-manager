"""Exercise real transfers and capture only DOT's own windows on Windows."""
from __future__ import annotations

import ctypes
import json
import socket
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotdm.ui import AddDialog, Application, SettingsDialog
from tests.test_downloads import BODY, FixtureServer


def capture(window, path):
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    hwnd = user.GetAncestor(window.winfo_id(), 2)
    rect = wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    dc = user.GetDC(hwnd)
    memory = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    old = gdi.SelectObject(memory, bitmap)

    class Header(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                    ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG), ("yppm", wintypes.LONG),
                    ("colors", wintypes.DWORD), ("important", wintypes.DWORD)]

    class Info(ctypes.Structure):
        _fields_ = [("header", Header), ("colors", wintypes.DWORD * 3)]

    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(Info), wintypes.UINT]

    info = Info()
    info.header.size, info.header.width, info.header.height = ctypes.sizeof(Header), width, -height
    info.header.planes, info.header.bits = 1, 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    try:
        if not user.PrintWindow(hwnd, memory, 2):
            raise RuntimeError("PrintWindow failed")
        if not gdi.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            raise RuntimeError("GetDIBits failed")
        Image.frombuffer("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1).save(path)
    finally:
        gdi.SelectObject(memory, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, dc)


def pump(app, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.update()
        time.sleep(0.02)


def assert_content_visible(dialog):
    for widget in dialog.body.winfo_children():
        assert widget.winfo_ismapped(), f"Hidden dialog widget: {widget}"
        bottom = widget.winfo_rooty() + widget.winfo_height()
        assert bottom <= dialog.winfo_rooty() + dialog.winfo_height(), f"Clipped widget: {widget}"


def main():
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    server = FixtureServer()
    with tempfile.TemporaryDirectory(prefix="dot-ui-") as temporary:
        root = Path(temporary)
        app = Application(root / "state", root / "downloads", tray=False)  # Never collide with a real DOT's tray.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        app.manager.configure(browser_port=port)
        app.sync_bridge()
        errors = []
        app.report_callback_exception = lambda kind, value, trace: errors.append(str(value))
        try:
            pump(app, 0.6)
            capture(app, output / "ui-empty.png")
            manager = app.manager
            manager.configure(connections=4, max_active=2)
            manager.add(server.url("/range"), filename="design-reference.zip")
            pump(app, 0.9)
            server.body = BODY * 8
            manager.configure(speed_limit=1024 * 1024)
            task_id = manager.add(server.url("/slow"), filename="sample-video.mp4")
            manager.add(server.url("/range"), filename="archive-toolkit.zip", scheduled_at=time.time() + 7200)
            manager.add(server.url("/range"), filename="field-notes.pdf", paused=True)
            pump(app, 3)
            app.tree.selection_set(task_id)
            app.refresh()
            pump(app, 0.2)
            capture(app, output / "ui-transfers.png")
            assert len(app.tree.get_children()) == 4
            assert app.selected()[0]["id"] == task_id
            app.choose_filter("Video")
            assert len(app.tree.get_children()) == 1
            app.choose_filter("All downloads")
            dialog = AddDialog(app, server.url("/range"))
            pump(app, 0.25)
            assert_content_visible(dialog)
            capture(dialog, output / "ui-add.png")
            dialog.destroy()
            settings = SettingsDialog(app)
            pump(app, 0.25)
            assert_content_visible(settings)
            capture(settings, output / "ui-settings.png")
            settings.destroy()
            app.geometry("1050x740")
            pump(app, 0.25)
            assert app.details.winfo_ismapped()
            assert app.footer.winfo_y() + app.footer.winfo_height() <= app.main.winfo_height()
            capture(app, output / "ui-minimum.png")
            assert not errors, errors
            result = {"status": "passed", "rows": 4, "filters": "passed", "dialogs": ["Add download", "Settings"],
                      "screenshots": [p.name for p in output.glob("ui-*.png")], "callback_errors": errors}
            (output / "ui-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
        finally:
            app.close()
            while True:
                try:
                    if not app.winfo_exists():
                        break
                    app.update()
                except Exception:
                    break
                time.sleep(0.02)
            server.shutdown()
            server.server_close()
            server.thread.join()


if __name__ == "__main__":
    main()
