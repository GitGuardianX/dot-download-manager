"""Where DOT keeps its library, downloads and helper tools.

Three modes:
  installed  the packaged DOT.exe installed by the setup wizard (no portable.txt beside it):
             library in %LOCALAPPDATA%\\DOT, downloads in the Windows Downloads folder.
  portable   the packaged DOT.exe with portable.txt beside it: everything stays beside the exe.
  source     running from the source tree (development): the project folder's .local and downloads.

Helper tools (yt-dlp, FFmpeg, Deno) are looked up in this order: the user's tools folder (where
"Update yt-dlp" puts newer copies), the tools bundled with DOT, then PATH.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

EXE = ".exe" if os.name == "nt" else ""


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder holding DOT.exe (packaged) or the project root (source)."""
    return Path(sys.executable).resolve().parent if frozen() else Path(__file__).resolve().parent.parent


def mode() -> str:
    if not frozen():
        return "source"
    return "portable" if (app_dir() / "portable.txt").exists() else "installed"


def user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "DOT"


def library_dir() -> Path:
    return user_data_dir() if mode() == "installed" else app_dir() / ".local"


def known_downloads() -> Path | None:
    """The Windows Downloads folder, including a user relocation such as D:\\Downloads."""
    if os.name != "nt":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                    ("data4", ctypes.c_ubyte * 8)]

    folder_id = GUID.from_buffer_copy(uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}").bytes_le)
    shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.c_void_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)) != 0:
        return None
    try:
        return Path(ctypes.wstring_at(pointer.value))
    finally:
        ole32.CoTaskMemFree(pointer)


def downloads_dir() -> Path:
    if mode() != "installed":
        return app_dir() / "downloads"
    return known_downloads() or Path.home() / "Downloads"


def bundled_tools_dir() -> Path:
    # Packaged: tools\ beside DOT.exe. Source: what tools/fetch_tools.py prepared for the installer.
    return app_dir() / "tools" if frozen() else app_dir() / "build" / "tools" / "bin"


def user_tools_dir() -> Path:
    return library_dir() / "tools"


def find_tool(name: str) -> str | None:
    """Full path of a helper tool: user tools folder, then bundled tools, then PATH."""
    for folder in (user_tools_dir(), bundled_tools_dir()):
        candidate = folder / (name + EXE)
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def extension_dir() -> Path:
    return app_dir() / "browser-extension"
