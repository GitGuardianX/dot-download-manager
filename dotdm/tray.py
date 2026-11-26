"""DOT's notification-area (tray) icon, through the Win32 API (ctypes; no extra packages).

A hidden top-level window of class DOT.Tray runs on its own thread. Clicks on the icon, its menu,
and messages posted by another DOT process ("show" from a second launch, "exit" from
"DOT.exe --quit") become strings on `Tray.events`, which the Tk main loop drains. This thread
never touches Tk. The window being top-level (not message-only) lets it receive Explorer's
"TaskbarCreated" broadcast, so the icon comes back if Explorer restarts.
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading

CLASS_NAME = "DOT.Tray"
WM_NULL, WM_DESTROY, WM_CLOSE, WM_CONTEXTMENU = 0x0000, 0x0002, 0x0010, 0x007B
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1     # Icon callback.
WM_SHOW = WM_APP + 2     # Posted by a second DOT launch.
WM_EXIT = WM_APP + 3     # Posted by "DOT.exe --quit" (the installer, before upgrading or uninstalling).
WM_BALLOON = WM_APP + 4  # Posted by the Tk thread to show a notification.
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON = 0x1, 1, 0x10, 49
MF_STRING, MF_SEPARATOR, TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0, 0x800, 0x0002, 0x0100
ASFW_ANY = 0xFFFFFFFF
MENU = [(1, "Open DOT", "show"), (2, "Pause all downloads", "pause-all"),
        (3, "Start all paused downloads", "resume-all"), None, (4, "Exit DOT", "exit")]

AVAILABLE = os.name == "nt"
if AVAILABLE:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wintypes.HICON)]

    def _proto(function, restype, *argtypes):
        function.restype, function.argtypes = restype, list(argtypes)

    _proto(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    _proto(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
           wintypes.HINSTANCE, wintypes.LPVOID)
    _proto(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _proto(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    _proto(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    _proto(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    _proto(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
    _proto(user32.PostQuitMessage, None, ctypes.c_int)
    _proto(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _proto(user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    _proto(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
    _proto(user32.CreatePopupMenu, wintypes.HMENU)
    _proto(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
    _proto(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, wintypes.UINT)
    _proto(user32.TrackPopupMenu, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, wintypes.HWND, wintypes.LPVOID)
    _proto(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
    _proto(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    _proto(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
    _proto(user32.AllowSetForegroundWindow, wintypes.BOOL, wintypes.DWORD)
    _proto(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
           ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _proto(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
    _proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _proto(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    _proto(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

    TASKBAR_CREATED = user32.RegisterWindowMessageW("TaskbarCreated")
    _windows: dict[int, "Tray"] = {}
    _class_lock = threading.Lock()
    _registered: set[str] = set()

    def _procedure(hwnd, message, wparam, lparam):
        tray = _windows.get(hwnd)
        if tray is not None:
            result = tray._handle(message, lparam)
            if result is not None:
                return result
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    _PROCEDURE = WNDPROC(_procedure)  # Kept alive for the life of the process.

    def _register_class(name: str):
        with _class_lock:
            if name in _registered:
                return
            spec = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=_PROCEDURE,
                               hInstance=kernel32.GetModuleHandleW(None), lpszClassName=name)
            if not user32.RegisterClassExW(ctypes.byref(spec)) and ctypes.get_last_error() != 1410:  # Already exists.
                raise ctypes.WinError(ctypes.get_last_error())
            _registered.add(name)


def signal_running(action: str, class_name: str = CLASS_NAME) -> bool:
    """Ask an already running DOT to show its window ("show") or to exit ("exit").
    Tests pass their own class_name so they never reach the user's real DOT."""
    if not AVAILABLE:
        return False
    hwnd = user32.FindWindowW(class_name, None)
    if not hwnd:
        return False
    if action == "show":
        user32.AllowSetForegroundWindow(ASFW_ANY)  # Let the running DOT come to the front.
    return bool(user32.PostMessageW(hwnd, WM_SHOW if action == "show" else WM_EXIT, 0, 0))


class Tray:
    def __init__(self, icon_path, tooltip: str, class_name: str = CLASS_NAME):
        self.icon_path, self.tooltip, self.class_name = str(icon_path), tooltip[:127], class_name
        self.events: queue.Queue = queue.Queue()
        self.hwnd = None
        self.hicon = None
        self.error = ""
        self.balloon = ("", "")
        self._ready = threading.Event()
        self._thread = None

    def start(self) -> bool:
        """Create the window and icon on a new thread. Returns whether the icon is showing."""
        if not AVAILABLE:
            return False
        self._thread = threading.Thread(target=self._run, name="dot-tray", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return bool(self.hwnd) and not self.error

    def notify(self, title: str, text: str):
        """Show a notification balloon from the icon (safe to call from any thread)."""
        self.balloon = (title[:63], text[:255])
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_BALLOON, 0, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=3)

    # ---- tray thread ----------------------------------------------------------------------
    def _run(self):
        try:
            _register_class(self.class_name)
            self.hwnd = user32.CreateWindowExW(0, self.class_name, "DOT", 0, 0, 0, 0, 0, None, None,
                                               kernel32.GetModuleHandleW(None), None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            _windows[self.hwnd] = self
            size = user32.GetSystemMetrics(SM_CXSMICON)
            self.hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if not self._notify(NIM_ADD):
                raise OSError("Windows did not accept the tray icon.")
        except OSError as exc:
            self.error = str(exc)
            if self.hwnd:
                _windows.pop(self.hwnd, None)
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        if self.hicon:
            user32.DestroyIcon(self.hicon)
            self.hicon = None

    def _notify(self, action: int, balloon: bool = False) -> bool:
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                               uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
                               hIcon=self.hicon, szTip=self.tooltip)
        if balloon:
            data.uFlags |= NIF_INFO
            data.szInfoTitle, data.szInfo = self.balloon
            data.dwInfoFlags = NIIF_INFO
        return bool(shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

    def _menu(self):
        menu = user32.CreatePopupMenu()
        for item in MENU:
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING, item[0], item[1])
        user32.SetMenuDefaultItem(menu, 1, 0)
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)  # Otherwise the menu does not close when clicking elsewhere.
        chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        for item in MENU:
            if item and item[0] == chosen:
                self.events.put(item[2])

    def _handle(self, message: int, lparam: int):
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.events.put("show")
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            return 0
        if message == WM_SHOW:
            self.events.put("show")
            return 0
        if message == WM_EXIT:
            self.events.put("exit")
            return 0
        if message == WM_BALLOON:
            self._notify(NIM_MODIFY, balloon=True)
            return 0
        if message == TASKBAR_CREATED:
            self._notify(NIM_ADD)  # Explorer restarted: put the icon back.
            return 0
        if message == WM_CLOSE:
            self._notify(NIM_DELETE)
            user32.DestroyWindow(self.hwnd)
            return 0
        if message == WM_DESTROY:
            _windows.pop(self.hwnd, None)
            self.hwnd = None
            user32.PostQuitMessage(0)
            return 0
        return None
