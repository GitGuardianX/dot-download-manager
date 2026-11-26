"""Windows integration: optional start-with-Windows, and cleanup of the withdrawn 0.2 host.

DOT has no native messaging host. The browser extension only talks to a DOT that is
already running, over 127.0.0.1, so the browser can never start a program. To have DOT
ready the way IDM is, the user may opt in to starting DOT (minimized) at sign-in.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

LEGACY_HOST = "com.dot.downloadmanager"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DOT Download Manager"
BROWSERS = (r"Google\Chrome", r"Microsoft\Edge", r"BraveSoftware\Brave-Browser")


def extension_id(manifest: Path) -> str:
    """Chromium extension ID derived from the manifest's public key."""
    key = json.loads(manifest.read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).digest()[:16]
    return "".join(chr(97 + nibble) for byte in digest for nibble in (byte >> 4, byte & 15))


def startup_command() -> str | None:
    """Command for the Run key; only the packaged DOT.exe can start itself at sign-in."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    return f'"{Path(sys.executable).resolve()}" --minimized'


def start_with_windows_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value = winreg.QueryValueEx(key, RUN_VALUE)[0]
    except OSError:
        return False
    command = startup_command()
    return bool(command) and os.path.normcase(value) == os.path.normcase(command)


def set_start_with_windows(enabled: bool):
    if os.name != "nt":
        return
    import winreg
    command = startup_command()
    if enabled and not command:
        raise RuntimeError("Start with Windows is available in the packaged DOT.exe.")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def remove_legacy_native_host() -> int:
    """Delete any per-user registration of the withdrawn 0.2 native messaging host."""
    if os.name != "nt":
        return 0
    import winreg
    removed = 0
    for browser in BROWSERS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\{browser}\NativeMessagingHosts\{LEGACY_HOST}")
            removed += 1
        except FileNotFoundError:
            pass
    return removed
