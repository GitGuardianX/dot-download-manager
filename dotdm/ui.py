"""Tk desktop interface; the download engine never runs on the UI thread."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, paths, sites
from .bridge import EXTENSION_ID, BrowserBridge
from .engine import ACTIVE, DownloadManager, category, safe_filename
from .integration import set_start_with_windows, start_with_windows_enabled, startup_command

WINDOW_TITLE = "DOT · Download Manager"

BG = "#0c111d"
SIDE = "#101827"
PANEL = "#151e30"
LINE = "#273249"
FG = "#e7edf8"
MUTED = "#8f9db5"
ACCENT = "#a393ff"
GREEN = "#65d7b0"
RED = "#ff899c"
FONT = "Segoe UI"


def size(value: float | None) -> str:
    if value is None:
        return "Unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024


def eta(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60)}m"


def label(parent, text="", *, color=FG, bg=None, font=(FONT, 10), **kwargs):
    return tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=color, font=font, **kwargs)


def button(parent, text, command, *, primary=False, small=False, **kwargs):
    return tk.Button(parent, text=text, command=command, font=(FONT, 9 if small else 10, "bold"),
                     bg=ACCENT if primary else PANEL, fg=BG if primary else FG,
                     activebackground="#b9adff" if primary else LINE, activeforeground=BG if primary else FG,
                     relief="flat", bd=0, padx=14 if small else 18, pady=8 if small else 10,
                     cursor="hand2", highlightthickness=0, **kwargs)


def entry(parent, variable, **kwargs):
    return tk.Entry(parent, textvariable=variable, bg=BG, fg=FG, insertbackground=FG,
                    relief="flat", highlightthickness=1, highlightbackground=LINE,
                    highlightcolor=ACCENT, font=(FONT, 10), selectbackground=ACCENT,
                    selectforeground=BG, readonlybackground=BG, **kwargs)


class Dialog(tk.Toplevel):
    def __init__(self, app, title, geometry):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.configure(bg=SIDE)
        self.resizable(False, False)
        # While the main window is minimized (e.g. started with Windows), a browser
        # download opens its own window, as IDM does, instead of restoring DOT.
        self.anchored = app.state() == "normal"
        if self.anchored:
            self.transient(app)
        self.geometry(geometry)
        self.update_idletasks()
        x, y = self._origin(self.winfo_width(), self.winfo_height())
        self.geometry(f"+{x}+{y}")
        self.bind("<Escape>", lambda _: self.destroy())
        self.grab_set()
        self.body = tk.Frame(self, bg=SIDE, padx=28, pady=24)
        self.body.pack(fill="both", expand=True)

    def heading(self, title, subtitle):
        label(self.body, title, font=(FONT, 19, "bold")).pack(anchor="w")
        label(self.body, subtitle, color=MUTED).pack(anchor="w", pady=(5, 18))

    def field(self, title, variable):
        label(self.body, title, color=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(10, 5))
        widget = entry(self.body, variable)
        widget.pack(fill="x", ipady=7)
        return widget

    def _origin(self, width, height):
        if self.anchored:
            x = self.app.winfo_x() + (self.app.winfo_width() - width) // 2
            y = self.app.winfo_y() + (self.app.winfo_height() - height) // 2
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        return (max(0, min(x, self.winfo_screenwidth() - width - 20)),
                max(10, min(y, self.winfo_screenheight() - height - 60)))

    def finish_layout(self):
        self.update_idletasks()
        width = self.winfo_width()
        height = max(self.winfo_height(), self.body.winfo_reqheight())
        height = min(height, self.winfo_screenheight() - 80)
        x, y = self._origin(width, height)
        self.geometry(f"{width}x{height}+{x}+{y}")


class AddDialog(Dialog):
    def __init__(self, app, initial=""):
        super().__init__(app, "New download · DOT", "660x640")
        self.heading("Add a download", "Paste a direct file link, or add one URL per line.")
        label(self.body, "DOWNLOAD URLS", color=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 6))
        self.urls = tk.Text(self.body, height=4, bg=BG, fg=FG, insertbackground=FG, font=(FONT, 10),
                            relief="flat", padx=10, pady=10, highlightthickness=1,
                            highlightbackground=LINE, highlightcolor=ACCENT, wrap="word")
        self.urls.pack(fill="x")
        self.urls.insert("1.0", initial)
        settings = app.manager.get_settings()
        self.directory = tk.StringVar(value=settings["download_dir"])
        label(self.body, "SAVE TO", color=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(17, 5))
        row = tk.Frame(self.body, bg=SIDE)
        row.pack(fill="x")
        entry(row, self.directory).pack(side="left", fill="x", expand=True, ipady=8)
        button(row, "Browse", self.browse, small=True).pack(side="right", padx=(8, 0))
        self.filename = tk.StringVar()
        self.field("Filename (optional, for a single URL)", self.filename)
        options = tk.Frame(self.body, bg=SIDE)
        options.pack(fill="x", pady=(16, 0))
        self.connections = tk.StringVar(value=str(settings["connections"]))
        self.delay = tk.StringVar(value="0")
        for title, variable, end in (("Connections per file", self.connections, 16), ("Start in (minutes)", self.delay, 10080)):
            col = tk.Frame(options, bg=SIDE)
            col.pack(side="left", fill="x", expand=True)
            label(col, title, color=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(0, 5))
            ttk.Spinbox(col, from_=1 if variable is self.connections else 0, to=end,
                        textvariable=variable, width=12).pack(anchor="w", ipady=5)
        self.referer = tk.StringVar()
        self.field("Source page URL (optional)", self.referer)
        label(self.body, "Scheduled downloads start while DOT is open.", color=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(14, 10))
        actions = tk.Frame(self.body, bg=SIDE)
        actions.pack(fill="x", pady=(7, 0))
        button(actions, "Start download", lambda: self.submit(False), primary=True).pack(side="right")
        button(actions, "Add paused", lambda: self.submit(True)).pack(side="right", padx=8)
        self.finish_layout()
        self.urls.focus_set()

    def browse(self):
        path = filedialog.askdirectory(parent=self, initialdir=self.directory.get(), mustexist=False)
        if path:
            self.directory.set(path)

    def submit(self, paused):
        from .engine import validate_url
        try:
            urls = [validate_url(s) for s in self.urls.get("1.0", "end").splitlines() if s.strip()]
            if not urls:
                raise ValueError("Paste at least one download URL.")
            count, delay = int(self.connections.get()), float(self.delay.get())
            if not 1 <= count <= 16 or not 0 <= delay <= 10080:
                raise ValueError("Choose 1–16 connections and a delay between 0 and 10080 minutes.")
            filename = self.filename.get().strip() or None
            if filename and len(urls) != 1:
                raise ValueError("Leave the filename empty when adding several URLs.")
            referer = validate_url(self.referer.get()) if self.referer.get().strip() else ""
            scheduled = time.time() + delay * 60 if delay else None
            for url in urls:
                task_id = self.app.manager.add(url, directory=self.directory.get(), filename=filename,
                                                connections=count, scheduled_at=scheduled, paused=paused, referer=referer)
            self.app.filter = "All downloads"
            self.app.search.set("")
            self.app.refresh()
            if self.app.tree.exists(task_id):
                self.app.tree.selection_set(task_id)
                self.app.tree.see(task_id)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Couldn't add download", str(exc), parent=self)


class DownloadConfirmation(Dialog):
    """Browser handoff: no task is created until the user chooses now or later."""
    def __init__(self, app, offer):
        self.offer = offer
        self.closed = False
        self.preview_after = None
        super().__init__(app, "Download File Info · DOT", "740x440")
        self.heading("Download file", "Choose where to save this file.")
        self.url = tk.StringVar(value=offer["url"])
        self.field("URL", self.url).configure(state="readonly")
        options = tk.Frame(self.body, bg=SIDE)
        options.pack(fill="x", pady=(14, 0))
        label(options, "Category", color=MUTED).pack(side="left", padx=(0, 14))
        self.category = tk.StringVar(value=category(offer["filename"]))
        combo = ttk.Combobox(options, textvariable=self.category, values=("Video", "Music", "Archives", "Documents", "Programs", "Images", "Other"), state="readonly", width=20)
        combo.pack(side="left", ipady=5)
        combo.bind("<<ComboboxSelected>>", self.change_category)
        self.file_size = label(options, size(offer.get("total")), color=ACCENT, font=(FONT, 12, "bold"))
        self.file_size.pack(side="right")
        settings = app.manager.get_settings()
        directory = settings.get("category_dirs", {}).get(self.category.get(), str(Path(settings["download_dir"]) / self.category.get()))
        self.path = tk.StringVar(value=str(Path(directory) / offer["filename"]))
        label(self.body, "Save as", color=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(14, 5))
        save = tk.Frame(self.body, bg=SIDE)
        save.pack(fill="x")
        entry(save, self.path).pack(side="left", fill="x", expand=True, ipady=8)
        button(save, "Browse", self.browse, small=True).pack(side="right", padx=(8, 0))
        self.remember = tk.BooleanVar(value=False)
        tk.Checkbutton(self.body, text="Remember this folder for this category", variable=self.remember,
                       bg=SIDE, fg=FG, selectcolor=PANEL, activebackground=SIDE,
                       activeforeground=FG, font=(FONT, 9)).pack(anchor="w", pady=(8, 0))
        self.description = tk.StringVar()
        self.field("Description (optional)", self.description)
        note, color = "DOT will download this file directly.", MUTED
        if offer.get("warning"):
            note, color = "Check failed: " + offer["warning"], RED
        elif offer.get("stream"):
            note = "Streaming media · FFmpeg required · A paused stream restarts from the beginning."
        elif offer.get("headers", {}).get("Cookie"):
            note = "Your browser session is used for this download."
        self.note = label(self.body, note, color=color, font=(FONT, 9), wraplength=680, justify="left")
        self.note.pack(anchor="w", pady=(12, 14))
        actions = tk.Frame(self.body, bg=SIDE)
        actions.pack(fill="x")
        button(actions, "Start download", lambda: self.submit(False), primary=True).pack(side="right")
        button(actions, "Download later", lambda: self.submit(True)).pack(side="right", padx=8)
        button(actions, "Cancel", self.destroy).pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.finish_layout()
        self.lift()
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False) if self.winfo_exists() else None)
        self.focus_force()
        if offer.get("total") is None and not offer.get("stream") and not offer.get("probed"):
            self.preview = queue.Queue()

            def inspect():
                try:
                    self.preview.put(app.manager.inspect(dict(offer)))
                except Exception:
                    self.preview.put(None)

            threading.Thread(target=inspect, daemon=True).start()
            self.preview_after = self.after(150, self.check_preview)

    def check_preview(self):
        try:
            metadata = self.preview.get_nowait()
        except queue.Empty:
            self.preview_after = self.after(150, self.check_preview)
            return
        self.preview_after = None
        self.file_size.configure(text=size(metadata.total) if metadata else "Size unavailable")
        if not metadata:
            self.note.configure(text="If this site asks for verification, complete it in the browser first.")

    def change_category(self, _=None):
        settings = self.app.manager.get_settings()
        folder = settings.get("category_dirs", {}).get(self.category.get(), str(Path(settings["download_dir"]) / self.category.get()))
        self.path.set(str(Path(folder) / Path(self.path.get()).name))

    def browse(self):
        current = Path(self.path.get())
        path = filedialog.asksaveasfilename(parent=self, initialdir=str(current.parent), initialfile=current.name)
        if path:
            self.path.set(path)

    def submit(self, later):
        try:
            path = Path(self.path.get()).expanduser().resolve()
            if not path.name:
                raise ValueError("Choose a filename.")
            task_id = self.app.manager.add(self.offer["url"], directory=str(path.parent), filename=safe_filename(path.name),
                paused=later, referer=self.offer["referer"], headers=self.offer["headers"], browser_context=True,
                category_name=self.category.get(), description=self.description.get(), stream=self.offer.get("stream", False))
            if self.remember.get():
                categories = self.app.manager.get_settings().get("category_dirs", {})
                categories[self.category.get()] = str(path.parent)
                self.app.manager.configure(category_dirs=categories)
            self.app.filter = "All downloads"
            self.app.search.set("")
            self.app.refresh()
            if self.app.tree.exists(task_id):
                self.app.tree.selection_set(task_id)
                self.app.tree.see(task_id)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Couldn't start download", str(exc), parent=self)

    def destroy(self):
        if self.preview_after:
            self.after_cancel(self.preview_after)
        if not self.closed:
            self.closed = True
            self.app.bridge.done(self.offer["id"])  # Frees one of the bridge's pending-window slots.
        super().destroy()


def duration_text(seconds) -> str:
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


class VideoConfirmation(Dialog):
    """A video page (YouTube and many other sites), read and downloaded with yt-dlp."""
    def __init__(self, app, offer):
        self.offer, self.closed, self.after_id = offer, False, None
        self.info, self.options, self.path_edited, self._setting = None, [], False, False
        self.results = queue.Queue()
        # A stream the page's player loaded (HLS/DASH), rather than a page for yt-dlp to read.
        self.stream = bool(offer.get("stream")) and not offer.get("site")
        super().__init__(app, "Download Video · DOT", "740x470")
        label(self.body, "Download video", font=(FONT, 19, "bold")).pack(anchor="w")
        reading = "Reading the stream…" if self.stream else "Reading the page with yt-dlp…"
        self.title_label = label(self.body, reading, color=MUTED, wraplength=680, justify="left")
        self.title_label.pack(anchor="w", pady=(5, 14))
        self.url = tk.StringVar(value=offer["url"])
        self.field("Stream" if self.stream else "Page", self.url).configure(state="readonly")
        row = tk.Frame(self.body, bg=SIDE)
        row.pack(fill="x", pady=(14, 0))
        label(row, "Quality", color=MUTED).pack(side="left", padx=(0, 14))
        self.quality = tk.StringVar(value="Getting video details…")
        self.combo = ttk.Combobox(row, textvariable=self.quality, values=(), state="disabled", width=32)
        self.combo.pack(side="left", ipady=5)
        self.combo.bind("<<ComboboxSelected>>", lambda _: self.apply_choice())
        self.size_label = label(row, "", color=ACCENT, font=(FONT, 12, "bold"))
        self.size_label.pack(side="right")
        settings = app.manager.get_settings()
        self.folders, self.base = settings.get("category_dirs", {}), Path(settings["download_dir"])
        self.path = tk.StringVar(value=str(self.folder("Video") / "video.mp4"))
        self.path.trace_add("write", self._edited)
        label(self.body, "Save as", color=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(14, 5))
        save = tk.Frame(self.body, bg=SIDE)
        save.pack(fill="x")
        entry(save, self.path).pack(side="left", fill="x", expand=True, ipady=8)
        button(save, "Browse", self.browse, small=True).pack(side="right", padx=(8, 0))
        self.description = tk.StringVar()
        self.field("Description (optional)", self.description)
        note = ("Downloaded piece by piece with your browser's referrer and user agent, then saved as one MP4."
                if self.stream else "Downloaded with yt-dlp; FFmpeg joins video and audio.")
        self.note = label(self.body, note, color=MUTED, font=(FONT, 9), wraplength=680, justify="left")
        self.note.pack(anchor="w", pady=(12, 14))
        actions = tk.Frame(self.body, bg=SIDE)
        actions.pack(fill="x")
        self.start = button(actions, "Start download", lambda: self.submit(False), primary=True, state="disabled")
        self.start.pack(side="right")
        self.later = button(actions, "Download later", lambda: self.submit(True), state="disabled")
        self.later.pack(side="right", padx=8)
        button(actions, "Cancel", self.destroy).pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.finish_layout()
        self.lift()
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False) if self.winfo_exists() else None)
        self.focus_force()
        threading.Thread(target=self.load, name="dot-describe", daemon=True).start()
        self.after_id = self.after(200, self.poll)

    def folder(self, category):
        return Path(self.folders.get(category, str(self.base / category)))

    def _edited(self, *_):
        if not self._setting:
            self.path_edited = True

    def _set_path(self, path):
        self._setting = True
        self.path.set(str(path))
        self._setting = False

    def load(self):
        try:
            if self.stream:  # The stream host expects the browser's own request details.
                info = sites.describe(self.offer["url"], referer=self.offer.get("referer", ""),
                                      headers=self.offer.get("headers"), stream=True)
            else:  # A page is read without the browser's cookies or logins.
                info = sites.describe(self.offer["url"])
            self.results.put(("ok", info))
        except Exception as exc:
            self.results.put(("error", str(exc) or type(exc).__name__))

    def poll(self):
        try:
            kind, value = self.results.get_nowait()
        except queue.Empty:
            self.after_id = self.after(200, self.poll)
            return
        self.after_id = None
        if kind == "error":
            failed = "DOT could not read this stream." if self.stream else "DOT could not read a video on this page."
            self.title_label.configure(text=failed, fg=RED)
            self.quality.set("Unavailable")
            self.note.configure(text=value, fg=RED)
            return
        if self.stream:
            # A playlist's own name ("master") says nothing: the tab's title names the video, as in IDM.
            value = {**value, "title": self.offer.get("title") or value["title"], "site": "Stream"}
        self.info, self.options = value, value["choices"]
        details = [value["title"]] + ([duration_text(value["duration"])] if value["duration"] else []) + ([value["site"]] if value["site"] else [])
        self.title_label.configure(text="  ·  ".join(details), fg=FG)
        self.combo.configure(values=[option["label"] + (f"  ·  ~{size(option['size'])}" if option["size"] else "")
                                     for option in self.options], state="readonly")
        heights = [option.get("height") or 0 for option in self.options]
        self.combo.current(next((i for i, h in enumerate(heights) if 0 < h <= 1080), 0))
        self.apply_choice()
        self.start.configure(state="normal")
        self.later.configure(state="normal")
        if value["warning"]:
            self.note.configure(text=value["warning"], fg=ACCENT)

    def selected(self):
        index = self.combo.current()
        return self.options[index] if 0 <= index < len(self.options) else None

    def apply_choice(self):
        option = self.selected()
        if not option or not self.info:
            return
        self.size_label.configure(text=("~" + size(option["size"])) if option["size"] else "Size unknown")
        suffix = ".m4a" if option["audio"] else ".mp4"
        if not self.path_edited:
            self._set_path(self.folder("Music" if option["audio"] else "Video") / (safe_filename(self.info["title"]) + suffix))
        else:
            current = Path(self.path.get())
            if current.suffix.lower() in (".mp4", ".m4a") and current.suffix.lower() != suffix:
                self._set_path(current.with_suffix(suffix))

    def browse(self):
        current = Path(self.path.get())
        path = filedialog.asksaveasfilename(parent=self, initialdir=str(current.parent), initialfile=current.name)
        if path:
            self.path.set(path)

    def submit(self, later):
        option = self.selected()
        if not option:
            return
        try:
            path = Path(self.path.get()).expanduser().resolve()
            if not path.name:
                raise ValueError("Choose a filename.")
            common = dict(directory=str(path.parent), filename=safe_filename(path.name), paused=later,
                          referer=self.offer.get("referer", ""), site_format=option["format"],
                          category_name="Music" if option["audio"] else "Video", description=self.description.get())
            if self.stream:  # Keeps the browser's request details (memory only) for the stream host.
                task_id = self.app.manager.add(self.offer["url"], headers=self.offer.get("headers"), browser_context=True,
                                               stream=True, **common)
            else:
                task_id = self.app.manager.add(self.offer["url"], site=True, **common)
            self.app.filter = "All downloads"
            self.app.search.set("")
            self.app.refresh()
            if self.app.tree.exists(task_id):
                self.app.tree.selection_set(task_id)
                self.app.tree.see(task_id)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Couldn't start download", str(exc), parent=self)

    def destroy(self):
        if self.after_id:
            self.after_cancel(self.after_id)
            self.after_id = None
        if not self.closed:
            self.closed = True
            self.app.bridge.done(self.offer["id"])
        super().destroy()


class SettingsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Settings · DOT", "640x640")
        self.heading("Make it yours", "Transfer limits, download location, and browser connection.")
        settings = app.manager.get_settings()
        self.directory = tk.StringVar(value=settings["download_dir"])
        self.field("Default download folder", self.directory)
        button(self.body, "Choose folder", self.browse, small=True).pack(anchor="e", pady=(5, 0))
        grid = tk.Frame(self.body, bg=SIDE)
        grid.pack(fill="x", pady=(15, 8))
        self.active = tk.StringVar(value=str(settings["max_active"]))
        self.connections = tk.StringVar(value=str(settings["connections"]))
        self.limit = tk.StringVar(value=str(settings["speed_limit"] // 1024))
        for column, (title, variable, low, high) in enumerate((
            ("Parallel downloads", self.active, 1, 6), ("Connections / file", self.connections, 1, 16),
            ("Limit in KiB/s", self.limit, 0, 10000000))):
            grid.columnconfigure(column, weight=1)
            label(grid, title, color=MUTED, font=(FONT, 9)).grid(row=0, column=column, sticky="w", pady=(0, 6))
            ttk.Spinbox(grid, from_=low, to=high, textvariable=variable, width=12).grid(row=1, column=column, sticky="w", ipady=5)
        label(self.body, "0 means unlimited. Streaming transfers use FFmpeg's own connection.", color=MUTED, justify="left", font=(FONT, 9)).pack(anchor="w", pady=(3, 20))
        tk.Frame(self.body, bg=LINE, height=1).pack(fill="x", pady=(0, 16))
        label(self.body, "BROWSER EXTENSION", color=ACCENT, font=(FONT, 9, "bold")).pack(anchor="w")
        check = dict(bg=SIDE, fg=FG, selectcolor=PANEL, activebackground=SIDE, activeforeground=FG, font=(FONT, 10), cursor="hand2")
        self.browser = tk.BooleanVar(value=settings["browser_enabled"])
        tk.Checkbutton(self.body, text="Accept downloads from the DOT browser extension", variable=self.browser, **check).pack(anchor="w", pady=(8, 0))
        self.port = tk.StringVar(value=str(settings["browser_port"]))
        self.field("Local port (the extension uses 17643 unless you change it there too)", self.port)
        self.startup = tk.BooleanVar(value=start_with_windows_enabled())
        self.startup_available = startup_command() is not None
        tk.Checkbutton(self.body, text="Start DOT in the tray when I sign in to Windows", variable=self.startup,
                       state="normal" if self.startup_available else "disabled", **check).pack(anchor="w", pady=(12, 0))
        self.keep_in_tray = tk.BooleanVar(value=settings.get("keep_in_tray", True))
        tk.Checkbutton(self.body, text="Keep DOT running in the tray when its window is closed", variable=self.keep_in_tray,
                       state="normal" if app.tray else "disabled", **check).pack(anchor="w", pady=(4, 0))
        label(self.body, "The browser never starts DOT. While DOT is closed, downloads simply stay in\n"
                         f"the browser. Extension ID: {EXTENSION_ID}", color=MUTED, justify="left", font=(FONT, 9)).pack(anchor="w", pady=(8, 0))
        tools = tk.Frame(self.body, bg=SIDE)
        tools.pack(fill="x", pady=(12, 0))
        button(tools, "Connect your browser…", self.connect_browser, small=True).pack(side="left")
        self.update_button = button(tools, "Update yt-dlp", self.update_ytdlp, small=True)
        self.update_button.pack(side="left", padx=8)
        self.update_note = label(tools, "", color=MUTED, font=(FONT, 9))
        self.update_note.pack(side="left")
        actions = tk.Frame(self.body, bg=SIDE)
        actions.pack(side="bottom", fill="x", pady=(15, 0))
        button(actions, "Save settings", self.save, primary=True).pack(side="right")
        button(actions, "Cancel", self.destroy).pack(side="right", padx=8)
        self.finish_layout()

    def browse(self):
        path = filedialog.askdirectory(parent=self, initialdir=self.directory.get(), mustexist=False)
        if path:
            self.directory.set(path)

    def connect_browser(self):
        self.destroy()  # One modal window at a time.
        self.app.after(50, self.app.show_browser_guide)

    def update_ytdlp(self):
        self.update_button.configure(state="disabled")
        self.update_note.configure(text="Updating yt-dlp…", fg=MUTED)
        results = queue.Queue()

        def work():
            try:
                results.put(("ok", sites.update_ytdlp()))
            except Exception as exc:
                results.put(("error", str(exc) or type(exc).__name__))

        def poll():
            if not self.winfo_exists():
                return
            try:
                kind, text = results.get_nowait()
            except queue.Empty:
                self.after(300, poll)
                return
            self.update_button.configure(state="normal")
            self.update_note.configure(text=text[:80], fg=GREEN if kind == "ok" else RED)

        threading.Thread(target=work, name="dot-update-ytdlp", daemon=True).start()
        self.after(300, poll)

    def save(self):
        try:
            self.app.manager.configure(download_dir=self.directory.get(), max_active=int(self.active.get()),
                                        connections=int(self.connections.get()), speed_limit=int(self.limit.get()) * 1024,
                                        browser_enabled=self.browser.get(), browser_port=int(self.port.get()),
                                        keep_in_tray=self.keep_in_tray.get())
            if self.startup_available and self.startup.get() != start_with_windows_enabled():
                set_start_with_windows(self.startup.get())
            self.app.sync_bridge()
            self.destroy()
        except (ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror("Couldn't save settings", str(exc), parent=self)


BROWSERS = (("Brave", "brave.exe", "brave://extensions"), ("Chrome", "chrome.exe", "chrome://extensions"),
            ("Edge", "msedge.exe", "edge://extensions"))


def installed_browsers() -> list[tuple[str, str, str]]:
    """(name, executable, extensions page) for each installed browser DOT's extension supports."""
    if os.name != "nt":
        return []
    import winreg
    found = []
    for name, exe, page in BROWSERS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                    path = winreg.QueryValue(key, None).strip().strip('"')
            except OSError:
                continue
            if path and Path(path).is_file():
                found.append((name, path, page))
                break
    return found


class BrowserGuide(Dialog):
    """Load the DOT extension once in Developer mode (until DOT is in the Chrome Web Store)."""
    def __init__(self, app):
        self.after_id = None
        super().__init__(app, "Connect your browser · DOT", "720x620")
        self.heading("Connect your browser", "DOT receives browser downloads and videos through its extension.")
        folder = str(paths.extension_dir())

        def step(text):
            label(self.body, text, justify="left", wraplength=650).pack(anchor="w", pady=(10, 5))

        step("1.  Open your browser's extensions page:")
        row = tk.Frame(self.body, bg=SIDE)
        row.pack(anchor="w")
        browsers = installed_browsers()
        for name, path, page in browsers:
            button(row, f"Open {name} extensions", lambda p=path, u=page: self.open_page(p, u), small=True).pack(side="left", padx=(0, 8))
        if not browsers:
            label(row, "Type brave://extensions or chrome://extensions into the address bar.", color=MUTED).pack(side="left")
        step("2.  Turn on Developer mode (the switch in the top-right corner).")
        step("3.  If DOT is already in the list, click Remove on it.")
        step("4.  Click Load unpacked and choose this folder:")
        self.folder = tk.StringVar(value=folder)
        place = tk.Frame(self.body, bg=SIDE)
        place.pack(fill="x")
        entry(place, self.folder, state="readonly").pack(side="left", fill="x", expand=True, ipady=7)
        button(place, "Copy", lambda: app.copy(folder), small=True).pack(side="left", padx=(8, 0))
        button(place, "Open folder", lambda: app.open_folder(Path(folder)), small=True).pack(side="left", padx=(8, 0))
        step("5.  Click the DOT icon in the browser's toolbar once. This window turns green when it connects.")
        self.status = label(self.body, "", font=(FONT, 11, "bold"))
        self.status.pack(anchor="w", pady=(16, 4))
        self.enable = button(self.body, "Turn on the browser connection", self.turn_on, small=True)
        self.note = label(self.body, "", color=MUTED, font=(FONT, 9), wraplength=650, justify="left")
        self.note.pack(anchor="w")
        actions = tk.Frame(self.body, bg=SIDE)
        actions.pack(fill="x", pady=(16, 0))
        button(actions, "Done", self.destroy, primary=True).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.finish_layout()
        self.poll()

    def open_page(self, browser, page):
        self.app.copy(page)
        try:
            subprocess.Popen([browser, page], close_fds=True)
            self.note.configure(text=f"If the page did not open, paste {page} into the address bar (it is copied).")
        except OSError as exc:
            self.note.configure(text=f"Couldn't start the browser ({exc}). Paste {page} into its address bar (copied).")

    def turn_on(self):
        self.app.manager.configure(browser_enabled=True)
        self.app.sync_bridge()

    def poll(self):
        self.after_id = None
        bridge = self.app.bridge
        if not bridge.server:
            self.status.configure(text="The browser connection is off." if not bridge.error
                                  else "The browser connection is unavailable: " + bridge.error, fg=RED)
            if not self.enable.winfo_ismapped():
                self.enable.pack(anchor="w", pady=(0, 6), before=self.note)
        else:
            if self.enable.winfo_ismapped():
                self.enable.pack_forget()
            if bridge.last_seen:
                self.status.configure(text=f"✓ Connected · DOT extension {bridge.client_version or ''}".strip(), fg=GREEN)
            else:
                self.status.configure(text="Waiting for the extension…", fg=ACCENT)
        self.after_id = self.after(700, self.poll)

    def destroy(self):
        if self.after_id:
            self.after_cancel(self.after_id)
            self.after_id = None
        self.app.manager.configure(browser_guide_done=True)
        super().destroy()


def focus_running_instance() -> bool:
    """Bring an already running DOT window to the front. Returns False if none is found."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
    user32.SetForegroundWindow(hwnd)
    return True


def show_startup_message(title: str, message: str, error: bool = False):
    root = tk.Tk()
    root.withdraw()
    try:
        (messagebox.showerror if error else messagebox.showinfo)(title, message, parent=root)
    finally:
        root.destroy()


class Application(tk.Tk):
    def __init__(self, state_dir: Path, download_dir: Path, manager: DownloadManager | None = None, tray: bool = True):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("1220x820")
        self.minsize(1050, 740)
        self.configure(bg=BG)
        try:
            self.iconphoto(True, tk.PhotoImage(file=str(Path(__file__).resolve().parent.parent / "assets" / "dot.png")))
        except tk.TclError:
            pass
        self._style()
        if manager is None:
            try:
                manager = DownloadManager(state_dir, download_dir)
            except Exception as exc:
                self.withdraw()
                messagebox.showerror("Couldn't open DOT", str(exc), parent=self)
                self.destroy()
                raise SystemExit(1) from exc
        self.manager = manager
        self.last_error = ""
        self.bridge = BrowserBridge(self.manager)
        self.filter = "All downloads"
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh())
        self.speed_history = deque([0] * 50, maxlen=50)
        self._closing = False
        self._rows = {}
        self._nav = {}
        self.tray = None
        if tray:
            from .tray import AVAILABLE, Tray
            candidate = Tray(Path(__file__).resolve().parent.parent / "assets" / "dot.ico", "DOT Download Manager") if AVAILABLE else None
            if candidate and candidate.start():
                self.tray = candidate
        self.extension_version = self._shipped_extension_version()
        self._build()
        self.sync_bridge()
        self.bind("<Control-n>", lambda _: self.show_add())
        self.bind("<Control-v>", self.paste_urls)
        self.tree.bind("<Delete>", lambda _: self.remove_selected())
        self.tree.bind("<space>", lambda _: self.toggle_selected())
        self.protocol("WM_DELETE_WINDOW", self.close_button)
        self.after(100, self.tick)

    @staticmethod
    def _shipped_extension_version() -> str:
        try:
            return json.loads((paths.extension_dir() / "manifest.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            return ""

    def close_button(self):
        """The window's X: hide to the tray (like IDM) unless the tray is off or unavailable."""
        settings = self.manager.get_settings()
        if not self.tray or not settings.get("keep_in_tray", True):
            self.close()
            return
        self.withdraw()
        if not settings.get("tray_hint_shown"):
            self.tray.notify("DOT is still running", "It keeps catching browser downloads. Right-click the DOT icon here to exit.")
            self.manager.configure(tray_hint_shown=True)

    def show_window(self):
        self.deiconify()
        self.refresh()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False) if self.winfo_exists() else None)
        self.focus_force()

    def tray_event(self, event: str):
        if event == "show":
            self.show_window()
        elif event == "exit":
            self.close()
        elif event == "pause-all":
            self.manager.pause_all()
        elif event == "resume-all":
            self.manager.resume_all()

    def show_browser_guide(self):
        if not self._closing and not self.grab_active():
            BrowserGuide(self)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=BG, foreground=FG, fieldbackground=BG,
                        rowheight=47, borderwidth=0, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, font=(FONT, 10))
        style.configure("Treeview.Heading", background=SIDE, foreground=MUTED, relief="flat",
                        padding=(12, 13), font=(FONT, 9, "bold"))
        style.map("Treeview", background=[("selected", "#283351")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", PANEL)])
        style.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG, bordercolor=BG,
                        arrowcolor=MUTED, lightcolor=LINE, darkcolor=LINE, width=11)
        style.configure("TSpinbox", fieldbackground=BG, foreground=FG, background=PANEL,
                        bordercolor=LINE, arrowcolor=FG, insertcolor=FG, padding=5, font=(FONT, 10))
        style.configure("TCombobox", fieldbackground=BG, foreground=FG, background=PANEL, arrowcolor=FG, bordercolor=LINE)
        style.map("TCombobox", fieldbackground=[("readonly", BG)], foreground=[("readonly", FG)], selectbackground=[("readonly", BG)], selectforeground=[("readonly", FG)])

    def _build(self):
        self.sidebar = tk.Frame(self, width=205, bg=SIDE)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=SIDE)
        brand.pack(fill="x", padx=23, pady=(28, 28))
        logo = tk.Canvas(brand, width=34, height=36, bg=SIDE, highlightthickness=0)
        logo.pack(side="left", padx=(0, 10))
        logo.create_oval(1, 2, 33, 34, fill=ACCENT, outline="")
        logo.create_line(17, 9, 17, 24, fill=BG, width=3)
        logo.create_line(10, 18, 17, 25, 24, 18, fill=BG, width=3, joinstyle="round")
        label(brand, "DOT", font=(FONT, 22, "bold")).pack(anchor="w")
        label(self.sidebar, "YOUR LIBRARY", color=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", padx=24, pady=(0, 12))
        for name in ("All downloads", "Downloading", "Queued", "Completed", "Paused", "Failed"):
            self._nav_item(name)
        tk.Frame(self.sidebar, bg=LINE, height=1).pack(fill="x", padx=24, pady=20)
        label(self.sidebar, "CATEGORIES", color=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", padx=24, pady=(0, 10))
        for name in ("Video", "Music", "Archives", "Documents", "Programs", "Images", "Other"):
            self._nav_item(name)
        bottom = tk.Frame(self.sidebar, bg=SIDE)
        bottom.pack(side="bottom", fill="x", padx=20, pady=22)
        button(bottom, "Settings", lambda: SettingsDialog(self)).pack(fill="x")
        label(bottom, f"DOT  {__version__}  ·  Personal edition", color=MUTED, font=(FONT, 8)).pack(pady=(14, 0))
        self.main = tk.Frame(self, bg=BG, padx=28, pady=25)
        self.main.pack(side="left", fill="both", expand=True)
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(3, weight=1)
        heading = tk.Frame(self.main, bg=BG)
        heading.grid(row=0, column=0, sticky="ew")
        titles = tk.Frame(heading, bg=BG)
        titles.pack(side="left")
        label(titles, "Your downloads", font=(FONT, 23, "bold")).pack(anchor="w")
        label(titles, "Every file. At your pace.", color=MUTED).pack(anchor="w", pady=(4, 0))
        button(heading, "+  New download", self.show_add, primary=True).pack(side="right", pady=(4, 0))
        cards = tk.Frame(self.main, bg=BG)
        cards.grid(row=1, column=0, sticky="ew", pady=(26, 23))
        self.card_values = {}
        self.card_captions = {}
        for index, (key, title, caption) in enumerate((
            ("active", "ACTIVE DOWNLOADS", "Ready when you are"),
            ("speed", "TRANSFER SPEED", "Across all connections"),
            ("completed", "COMPLETED", "Your finished files"))):
            cards.columnconfigure(index, weight=1, uniform="card")
            card = tk.Frame(cards, bg=PANEL, padx=18, pady=14)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 2 else 6))
            label(card, title, color=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
            value = label(card, "0 B/s" if key == "speed" else "0", font=(FONT, 22, "bold"))
            value.pack(anchor="w", pady=(5, 3))
            self.card_values[key] = value
            self.card_captions[key] = label(card, caption, color=MUTED, font=(FONT, 8))
            self.card_captions[key].pack(anchor="w")
        tools = tk.Frame(self.main, bg=BG)
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 15))
        button(tools, "Resume", lambda: self.act("resume"), small=True).pack(side="left", padx=(0, 7))
        button(tools, "Pause", lambda: self.act("pause"), small=True).pack(side="left", padx=(0, 7))
        button(tools, "Pause all", self.manager.pause_all, small=True).pack(side="left", padx=(0, 7))
        self.more = tk.Menu(self, tearoff=False, bg=PANEL, fg=FG, activebackground=LINE, activeforeground=FG, font=(FONT, 10))
        self.more.add_command(label="Start all paused downloads", command=self.manager.resume_all)
        self.more.add_command(label="Open download folder", command=self.open_default_folder)
        self.more.add_separator()
        self.more.add_command(label="Remove selected", command=self.remove_selected)
        more = button(tools, "•••", lambda: self.more.tk_popup(tools.winfo_rootx() + 200, tools.winfo_rooty() + 35), small=True)
        more.pack(side="left")
        search = entry(tools, self.search, width=22)
        search.pack(side="right", ipady=8)
        label(tools, "Search", color=MUTED, font=(FONT, 9)).pack(side="right", padx=9)
        table = tk.Frame(self.main, bg=BG)
        table.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table, columns=("name", "status", "size", "progress", "speed", "eta"), show="headings", selectmode="extended")
        for key, title, width, anchor in (("name", "FILE NAME", 270, "w"), ("status", "STATUS", 110, "w"),
                                         ("size", "SIZE", 90, "e"), ("progress", "PROGRESS", 112, "e"),
                                         ("speed", "SPEED", 100, "e"), ("eta", "LEFT", 65, "e")):
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=width, minwidth=60, anchor=anchor, stretch=key == "name")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("Completed", foreground=GREEN)
        self.tree.tag_configure("Failed", foreground=RED)
        self.tree.tag_configure("Paused", foreground=MUTED)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.render_details())
        self.tree.bind("<Double-1>", lambda _: self.open_selected_folder())
        self.tree.bind("<Button-3>", self.context_menu)
        self.empty = tk.Frame(table, bg=BG)
        label(self.empty, "↓", color=ACCENT, font=(FONT, 37)).pack()
        self.empty_title = label(self.empty, "Your next download starts here", font=(FONT, 15, "bold"))
        self.empty_title.pack(pady=(7, 5))
        self.empty_caption = label(self.empty, "Add a link to start building your library.", color=MUTED)
        self.empty_caption.pack()
        button(self.empty, "+  Add a download", self.show_add, primary=True, small=True).pack(pady=18)
        self.details = tk.Frame(self.main, bg=PANEL, padx=18, pady=14, height=172)
        self.details.grid(row=4, column=0, sticky="ew", pady=(18, 13))
        self.details.pack_propagate(False)
        detail_top = tk.Frame(self.details, bg=PANEL)
        detail_top.pack(fill="x")
        self.detail_title = label(detail_top, "Transfer details", font=(FONT, 11, "bold"))
        self.detail_title.pack(side="left")
        self.detail_state = label(detail_top, "Select a download", color=MUTED, font=(FONT, 9))
        self.detail_state.pack(side="right")
        self.progress = tk.Canvas(self.details, height=13, bg=LINE, highlightthickness=0)
        self.progress.pack(fill="x", pady=(12, 10))
        self.progress.bind("<Configure>", lambda _: self.render_details())
        self.detail_url = label(self.details, "Direct HTTP and HTTPS downloads", color=MUTED, anchor="w", font=(FONT, 9))
        self.detail_url.pack(fill="x")
        self.detail_path = label(self.details, "Parallel connections · Pause and resume · Scheduled starts", color=MUTED, anchor="w", font=(FONT, 9))
        self.detail_path.pack(fill="x", pady=(3, 0))
        self.detail_note = label(self.details, "", color=ACCENT, anchor="w", font=(FONT, 9))
        self.detail_note.pack(fill="x", pady=(4, 0))
        self.footer = label(self.main, "", color=MUTED, anchor="w", font=(FONT, 8))
        self.footer.grid(row=5, column=0, sticky="ew")

    def _nav_item(self, name):
        row = tk.Frame(self.sidebar, bg=SIDE, cursor="hand2")
        row.pack(fill="x", padx=12, pady=1)
        text = label(row, name, color=MUTED, font=(FONT, 10), padx=12, pady=5, cursor="hand2")
        text.pack(side="left")
        count = label(row, "", color=MUTED, font=(FONT, 9), padx=12, cursor="hand2")
        count.pack(side="right")
        for widget in (row, text, count):
            widget.bind("<Button-1>", lambda _, value=name: self.choose_filter(value))
        self._nav[name] = (row, text, count)

    def choose_filter(self, name):
        self.filter = name
        self.refresh()

    @staticmethod
    def matches(task, name):
        if name == "All downloads":
            return True
        if name == "Downloading":
            return task["status"] in ACTIVE
        if name == "Queued":
            return task["status"] in {"Queued", "Scheduled"}
        if name in {"Completed", "Paused", "Failed"}:
            return task["status"] == name
        return (task.get("category_name") or category(task["filename"])) == name

    def refresh(self):
        if self._closing or not hasattr(self, "tree"):
            return
        tasks = self.manager.snapshot()
        self._rows = {t["id"]: t for t in tasks}
        active = [t for t in tasks if t["status"] in ACTIVE]
        completed = [t for t in tasks if t["status"] == "Completed"]
        speed = sum(t["speed"] for t in active)
        self.speed_history.append(speed)
        self.card_values["active"].configure(text=str(len(active)))
        self.card_values["speed"].configure(text=size(speed) + "/s")
        self.card_values["completed"].configure(text=str(len(completed)))
        self.card_captions["active"].configure(text=f"{sum(t['active_connections'] for t in active)} connections open")
        self.card_captions["completed"].configure(text=size(sum(t["downloaded"] for t in completed)) + " downloaded")
        for name, (row, text, count) in self._nav.items():
            chosen = name == self.filter
            bg = "#282d49" if chosen else SIDE
            for widget in (row, text, count):
                widget.configure(bg=bg)
            text.configure(fg=ACCENT if chosen else MUTED)
            count.configure(text=str(sum(self.matches(t, name) for t in tasks)), fg=ACCENT if chosen else MUTED)
        query = self.search.get().casefold()
        visible = [t for t in tasks if self.matches(t, self.filter) and query in (t["filename"] + t["url"] + t["status"]).casefold()]
        ids = {t["id"] for t in visible}
        for task_id in self.tree.get_children():
            if task_id not in ids:
                self.tree.delete(task_id)
        for position, task in enumerate(sorted(visible, key=lambda t: t["created"], reverse=True)):
            total = task["total"]
            progress = f"{task['downloaded'] / total * 100:.1f}%" if total else ("100%" if task["status"] == "Completed" else "—")
            remaining = max(0, (total or 0) - task["downloaded"])
            left = eta(remaining / task["speed"]) if task["speed"] > 0 and total else "—"
            values = ("  " + task["filename"], task["status"], size(total), progress,
                      size(task["speed"]) + "/s" if task["speed"] else "—", left)
            if self.tree.exists(task["id"]):
                self.tree.item(task["id"], values=values, tags=(task["status"],))
            else:
                self.tree.insert("", "end", iid=task["id"], values=values, tags=(task["status"],))
            self.tree.move(task["id"], "", position)
        if not visible:
            self.empty_title.configure(text="Your next download starts here" if not tasks else "No downloads here yet")
            self.empty_caption.configure(text="Add a link to start building your library." if not tasks else "Try another category or search.")
            self.empty.place(relx=0.5, rely=0.54, anchor="center")
        else:
            self.empty.place_forget()
        if self.bridge.error:
            state = "Browser extension unavailable: " + self.bridge.error
        elif self.bridge.server:
            state = f"Browser extension: listening on 127.0.0.1:{self.bridge.port}"
            if self.bridge.client_version and self.extension_version and self.bridge.client_version != self.extension_version:
                state += (f"  ·  Extension update ready: click Reload on DOT in your browser's extensions page"
                          f" ({self.bridge.client_version} → {self.extension_version})")
        else:
            state = "Browser extension: off (turn it on in Settings)"
        warning = self.manager.persistence_error
        problem = ("Couldn't save progress: " + warning) if warning else \
                  ("Something went wrong: " + self.last_error[:160]) if self.last_error else ""
        self.footer.configure(text=problem or f"{len(tasks)} downloads in your library     ·     {state}", fg=RED if problem else MUTED)
        self.render_details()

    def selected(self):
        return [self._rows[i] for i in self.tree.selection() if i in self._rows]

    def render_details(self):
        if not hasattr(self, "progress"):
            return
        selected = self.selected()
        self.progress.delete("all")
        if not selected:
            self.detail_title.configure(text="Transfer details")
            self.detail_state.configure(text="Select a download", fg=MUTED)
            self.detail_url.configure(text="Direct HTTP and HTTPS downloads")
            self.detail_path.configure(text="Parallel connections · Pause and resume · Scheduled starts")
            self.detail_note.configure(text="")
            return
        task = selected[0]
        self.detail_title.configure(text=task["filename"][:65])
        self.detail_state.configure(text=task["status"], fg=GREEN if task["status"] == "Completed" else ACCENT)
        width = self.progress.winfo_width()
        total = task["total"] or 0
        if total:
            for segment in task["segments"]:
                start = segment["start"] / total * width
                end = (segment["start"] + segment["downloaded"]) / total * width
                self.progress.create_rectangle(start, 0, max(start, end), 13, fill=GREEN if task["status"] == "Completed" else ACCENT, outline="")
        elif task["status"] == "Completed":
            self.progress.create_rectangle(0, 0, width, 13, fill=GREEN, outline="")
        self.detail_url.configure(text="From  " + task["url"][:135])
        self.detail_path.configure(text="To      " + str(Path(task["directory"]) / task["filename"])[:130])
        note_color = MUTED
        if task["error"]:
            note = task["error"]
            note_color = RED
        elif task["status"] == "Completed":
            note = "SHA-256  " + task["sha256"]
            note_color = GREEN
        elif task["status"] == "Scheduled":
            note = "Starts " + time.strftime("%d %b, %H:%M", time.localtime(task["scheduled_at"])) + " while DOT is open."
        elif task.get("transport") == "site":
            note = ("yt-dlp is reading the video page; the download starts in a few seconds…" if task["status"] == "Connecting"
                    else size(task["downloaded"]) + " saved · Video page via yt-dlp · Pausing keeps the partial download")
        elif task.get("transport") == "stream":
            note = size(task["downloaded"]) + " saved · Streaming media · Paused streams restart from the beginning"
        elif task["total"] is not None:
            resumable = "Resume available" if task["range_supported"] and task["validator"] else "Full restart if paused (server limitation)"
            note = f"{size(task['downloaded'])} of {size(task['total'])}    ·    {task['active_connections']} / {task['connections']} connections    ·    {resumable}"
        else:
            note = "Connecting to the server…" if task["status"] == "Connecting" else "Waiting for transfer details."
        self.detail_note.configure(text=note if task["error"] else note[:145], fg=note_color, wraplength=max(300, self.progress.winfo_width()))

    def act(self, action):
        for task in self.selected():
            getattr(self.manager, action)(task["id"])
        self.refresh()

    def toggle_selected(self):
        selected = self.selected()
        if selected:
            self.act("pause" if selected[0]["status"] in ACTIVE | {"Queued", "Scheduled"} else "resume")

    def remove_selected(self):
        selected = self.selected()
        if selected and messagebox.askyesno("Remove downloads", f"Remove {len(selected)} download(s) and their temporary parts?\nCompleted files stay on disk.", parent=self):
            for task in selected:
                self.manager.remove(task["id"])
            self.refresh()

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        menu = tk.Menu(self, tearoff=False, bg=PANEL, fg=FG, activebackground=LINE, activeforeground=FG)
        menu.add_command(label="Resume / retry", command=lambda: self.act("resume"))
        menu.add_command(label="Pause", command=lambda: self.act("pause"))
        menu.add_command(label="Open containing folder", command=self.open_selected_folder)
        menu.add_command(label="Copy download URL", command=lambda: self.copy(self._rows[row]["url"]))
        menu.add_command(label="Copy SHA-256", command=lambda: self.copy(self._rows[row]["sha256"]))
        menu.add_separator()
        menu.add_command(label="Remove", command=self.remove_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def open_default_folder(self):
        folder = Path(self.manager.get_settings()["download_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        self.open_folder(folder)

    def open_selected_folder(self):
        selected = self.selected()
        if selected:
            self.open_folder(Path(selected[0]["directory"]))

    def open_folder(self, folder):
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror("Couldn't open folder", str(exc), parent=self)

    def show_add(self, initial=""):
        if not self._closing:
            AddDialog(self, initial)

    def paste_urls(self, event):
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Spinbox)):
            return
        try:
            text = self.clipboard_get().strip()
            if text.startswith(("http://", "https://")):
                self.show_add(text)
                return "break"
        except tk.TclError:
            pass

    def sync_bridge(self):
        self.bridge.stop()
        settings = self.manager.get_settings()
        if settings["browser_enabled"]:
            self.bridge.start(settings["browser_port"])

    def grab_active(self) -> bool:
        # Not grab_current(): while a dropdown list is open the grab belongs to Tk's internal
        # popdown window, and grab_current() raises KeyError looking it up.
        return bool(self.tk.call("grab", "current"))

    def tick(self):
        if self._closing:
            return
        try:
            while self.tray and not self._closing:
                try:
                    self.tray_event(self.tray.events.get_nowait())
                except queue.Empty:
                    break
            if self._closing:
                return
            if self.state() != "withdrawn":  # Hidden in the tray: nothing to redraw.
                self.refresh()
            if not self.grab_active():
                try:
                    offer = self.bridge.pending.get_nowait()
                    # Pages and (with yt-dlp available) streams get the quality list, as in IDM.
                    video = offer.get("site") or (offer.get("stream") and sites.command())
                    (VideoConfirmation if video else DownloadConfirmation)(self, offer)
                except queue.Empty:
                    pass
        finally:
            self.after(400, self.tick)  # Always reschedule: one failure must not freeze the window.

    def report_callback_exception(self, kind, value, trace):
        # The packaged app has no console, so errors are shown instead of silently lost.
        self.last_error = f"{kind.__name__}: {value}"
        if hasattr(self, "footer"):
            self.footer.configure(text="Something went wrong: " + self.last_error[:160], fg=RED)

    def close(self):
        if self._closing:
            return
        self._closing = True
        self.footer.configure(text="Saving progress and stopping connections…")
        done = threading.Event()
        errors = []

        def shutdown():
            try:
                self.bridge.stop()
                self.manager.close()
            except Exception as exc:
                errors.append(str(exc))
            finally:
                done.set()

        threading.Thread(target=shutdown, daemon=True).start()

        def finish():
            if done.is_set():
                if self.tray:
                    self.tray.stop()
                if errors:
                    messagebox.showerror("Couldn't save progress", "\n".join(errors), parent=self)
                self.destroy()
            else:
                self.after(100, finish)

        finish()
