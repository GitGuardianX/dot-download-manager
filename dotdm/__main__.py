from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import paths
from .engine import DownloadManager, InstanceLocked


def app_root() -> Path:
    return paths.app_dir()


def main():
    parser = argparse.ArgumentParser(description="DOT Download Manager")
    parser.add_argument("url", nargs="?", help="Optional HTTP/HTTPS download URL")
    parser.add_argument("--cli", action="store_true", help="Run a download without the desktop UI")
    parser.add_argument("--state-dir", type=Path, help="Use a separate data directory")
    parser.add_argument("--output", type=Path, help="Download folder")
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--minimized", action="store_true", help="Start in the tray (used by Start with Windows)")
    parser.add_argument("--quit", action="store_true", help="Ask the running DOT to exit (used by the installer)")
    parser.add_argument("--first-run", action="store_true", help=argparse.SUPPRESS)  # From the installer's finish page.
    parser.add_argument("--uninstall-browser", action="store_true",
                        help="Remove the withdrawn 0.2 native messaging registration, if present")
    # Flags of the withdrawn 0.2 native host. Accepted only so that they exit at once:
    # the browser can no longer start DOT, and nothing is created.
    parser.add_argument("--browser-start", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.browser_start or args.install_browser:
        return 1
    if args.quit:
        from .tray import signal_running
        return 0 if signal_running("exit") else 1  # 1: no running DOT (nothing to do).
    if args.uninstall_browser:
        from .integration import remove_legacy_native_host
        remove_legacy_native_host()
        return 0
    state_dir = args.state_dir or paths.library_dir()
    download_dir = args.output or paths.downloads_dir()
    if args.cli:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        if not args.url:
            parser.error("--cli requires a URL")
        manager = DownloadManager(state_dir, download_dir)
        try:
            task_id = manager.add(args.url, directory=str(args.output.resolve()) if args.output else None, connections=args.connections)
            while True:
                task = next(t for t in manager.snapshot() if t["id"] == task_id)
                total = str(task["total"]) if task["total"] is not None else "?"
                print(f"\r{task['status']:12} {task['downloaded']}/{total} bytes", end="", flush=True)
                if task["status"] in ("Completed", "Failed"):
                    print()
                    if task["status"] == "Failed":
                        print(task["error"], file=sys.stderr)
                        return 1
                    print(Path(task["directory"]) / task["filename"])
                    print("SHA-256:", task["sha256"])
                    return 0
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\nPaused. Open the desktop app to resume.")
            return 130
        finally:
            manager.close()
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except OSError:
            pass
    new_library = not (Path(state_dir) / "state.json").exists()
    # Take the single-instance lock before any window exists. A second launch asks the running
    # DOT (through its tray window) to show itself and exits immediately.
    try:
        manager = DownloadManager(state_dir, download_dir)
    except InstanceLocked:
        from .tray import signal_running
        from .ui import focus_running_instance, show_startup_message
        if not (signal_running("show") or focus_running_instance()):
            show_startup_message("DOT is already running", "DOT is already open for this download library.")
        return 0
    except Exception as exc:
        from .ui import show_startup_message
        show_startup_message("Couldn't open DOT", str(exc), error=True)
        return 1
    if new_library and paths.mode() == "installed":
        manager.configure(browser_enabled=True)  # The extension is part of the installed product.
    from .ui import Application
    app = Application(state_dir, download_dir, manager=manager)
    if args.minimized:
        app.withdraw() if app.tray else app.iconify()
    elif (args.first_run or paths.mode() == "installed") and not manager.get_settings()["browser_guide_done"]:
        app.after(800, app.show_browser_guide)  # Once, until the user finishes the guide.
    if args.url:
        app.after(200, lambda: app.show_add(args.url))
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
