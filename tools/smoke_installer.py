"""Install DOT silently for this user into a temporary folder, check it, then uninstall it.

It briefly adds DOT to the user's Start Menu and "Installed apps" list; the uninstall at the end
removes both. It refuses to run while any DOT is open or already installed.
Usage: python tools/smoke_installer.py dist/DOT-Setup-0.4.0.exe
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_downloads import BODY, FixtureServer  # noqa: E402

APP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{4070C5F9-106D-4D78-B862-7E2703D29E67}_is1"
SHORTCUT = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "DOT Download Manager.lnk"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
user32 = ctypes.windll.user32
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p


def dot_window_open() -> bool:
    return bool(user32.FindWindowW("DOT.Tray", None) or user32.FindWindowW(None, "DOT · Download Manager"))


def installed_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in ("DisplayName", "DisplayVersion", "InstallLocation")}
    except OSError:
        return None


def wait(predicate, what, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"Timed out: {what}")


def main(setup: str):
    setup = str(Path(setup).resolve())  # CreateProcess does not resolve relative "dist/..." paths.
    if dot_window_open():
        raise SystemExit("Close DOT first: the installer would stop to ask for that.")
    if installed_entry():
        raise SystemExit("DOT is already installed for this user; this check needs a clean slate.")
    report = {}
    with tempfile.TemporaryDirectory(prefix="dot-install-check-") as temp:
        temp = Path(temp)
        folder = temp / "DOT"
        started = time.monotonic()
        result = subprocess.run([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                                 f"/DIR={folder}", "/TASKS=", f"/LOG={temp / 'setup.log'}"], timeout=900)
        assert result.returncode == 0, f"setup exit code {result.returncode}"
        report["install_seconds"] = round(time.monotonic() - started, 1)
        expected = ["DOT.exe", "unins000.exe", "_internal/python314.dll", "_internal/tcl86t.dll", "_internal/tk86t.dll",
                    "_internal/libssl-3.dll", "tools/yt-dlp.exe", "tools/deno.exe", "tools/ffmpeg.exe",
                    "tools/ffprobe.exe", "browser-extension/manifest.json", "licenses/THIRD-PARTY.txt",
                    "licenses/FFmpeg-LICENSE.txt"]
        missing = [item for item in expected if not (folder / item).exists()]
        assert not missing, f"missing files: {missing}"
        entry = installed_entry()
        assert entry and Path(entry["InstallLocation"].rstrip("\\")) == folder, entry
        assert SHORTCUT.exists(), "Start Menu shortcut"
        report["installed"] = {"apps_list": f"{entry['DisplayName']} {entry['DisplayVersion']}", "start_menu": True,
                               "files": sum(1 for _ in folder.rglob("*") if _.is_file()),
                               "size_mib": round(sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) / 1048576)}

        server = FixtureServer()  # The installed DOT.exe downloads a local test file.
        try:
            state = temp / "cli-state"
            run = subprocess.run([str(folder / "DOT.exe"), "--cli", server.url("/range"), "--state-dir", str(state),
                                  "--output", str(temp / "cli-files")], timeout=60, capture_output=True, creationflags=NO_WINDOW)
            assert run.returncode == 0, run.stderr[-500:]
            task = json.loads((state / "state.json").read_text(encoding="utf-8"))["tasks"][-1]
            assert (Path(task["directory"]) / task["filename"]).read_bytes() == BODY
        finally:
            server.shutdown()
            server.server_close()
        report["installed_download"] = "2 MiB downloaded and verified"
        report["bundled_tools"] = {
            exe: subprocess.run([str(folder / "tools" / exe), flag], capture_output=True, text=True, timeout=60,
                                creationflags=NO_WINDOW).stdout.splitlines()[0][:60]
            for exe, flag in (("yt-dlp.exe", "--version"), ("deno.exe", "--version"), ("ffmpeg.exe", "-version"))}

        gui_state = ["--state-dir", str(temp / "gui-state"), "--output", str(temp / "gui-files")]
        app = subprocess.Popen([str(folder / "DOT.exe"), *gui_state])
        try:
            wait(lambda: user32.FindWindowW("DOT.Tray", None), "tray icon", 30)
            again = subprocess.run([str(folder / "DOT.exe"), *gui_state], timeout=30)
            assert again.returncode == 0 and app.poll() is None, "a second launch hands over and exits"
            quit_run = subprocess.run([str(folder / "DOT.exe"), "--quit"], timeout=30)
            assert quit_run.returncode == 0
            app.wait(timeout=30)
        finally:
            if app.poll() is None:
                app.kill()
        report["app"] = {"tray_icon": True, "second_launch_handed_over": True, "quit_exit_code": app.returncode}

        subprocess.run([str(folder / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
        wait(lambda: not (folder / "DOT.exe").exists() and installed_entry() is None, "uninstall", 60)
        assert not SHORTCUT.exists(), "shortcut removed"
        report["uninstalled"] = {"files_removed": True, "apps_list_entry_removed": True, "shortcut_removed": True}
    print(json.dumps({"status": "passed", **report}, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "DOT-Setup-0.4.0.exe"))
