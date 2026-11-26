"""The desktop window: it keeps refreshing, and it lives in the tray like IDM."""
from __future__ import annotations

import queue
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

from dotdm.ui import Application, DownloadConfirmation


class FakeTray:
    def __init__(self):
        self.events, self.notes, self.stopped = queue.Queue(), [], False

    def notify(self, title, text):
        self.notes.append(title)

    def stop(self):
        self.stopped = True


class UiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = Application(root / "state", root / "files", tray=False)
        self.app.update()

    def tearDown(self):
        self.app._closing = True
        try:
            self.app.manager.close()
        except Exception:
            pass
        try:
            self.app.destroy()
        except tk.TclError:
            pass  # Already closed by the test.
        self.temp.cleanup()

    def test_refresh_survives_an_open_dropdown(self):
        offer = {"id": "a" * 32, "url": "https://example.com/file.zip", "referer": "", "headers": {},
                 "filename": "file.zip", "total": 10, "stream": False, "probed": True}
        dialog = DownloadConfirmation(self.app, offer)

        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)
        combo = next(w for w in widgets(dialog) if w.winfo_class() == "TCombobox")  # The Category list.
        self.app.update()
        self.app.tk.call("ttk::combobox::Post", combo)  # What clicking the dropdown arrow does.
        self.app.update()
        try:
            self.assertTrue(self.app.grab_active())
            with self.assertRaises(KeyError):
                self.app.grab_current()  # The trap the window used to fall into.
            task_id = self.app.manager.add("https://example.com/later.zip", paused=True)
            pending = len(self.app.tk.call("after", "info"))
            self.app.tick()  # Must not raise, and must schedule the next refresh.
            self.assertGreater(len(self.app.tk.call("after", "info")), pending)
            self.assertTrue(self.app.tree.exists(task_id), "the list still updates")
            self.assertEqual(self.app.last_error, "")
        finally:
            self.app.tk.call("ttk::combobox::Unpost", combo)
            dialog.destroy()

    def test_unexpected_errors_are_shown_not_lost(self):
        self.app.report_callback_exception(ValueError, ValueError("example"), None)
        self.app.refresh()
        self.assertIn("Something went wrong: ValueError: example", self.app.footer.cget("text"))

    def test_close_button_hides_to_the_tray(self):
        self.app.tray = FakeTray()
        self.app.close_button()
        self.app.update()
        self.assertEqual(self.app.state(), "withdrawn")
        self.assertEqual(self.app.tray.notes, ["DOT is still running"])
        self.app.show_window()
        self.app.close_button()
        self.assertEqual(len(self.app.tray.notes), 1, "the hint is shown only once")
        self.assertFalse(self.app._closing)

    def test_tray_menu_drives_the_window(self):
        self.app.tray = FakeTray()
        self.app.withdraw()
        self.app.tray.events.put("show")
        self.app.tick()
        self.app.update()
        self.assertEqual(self.app.state(), "normal")
        task_id = self.app.manager.add("https://example.com/later.zip", scheduled_at=time.time() + 3600)
        self.app.tray.events.put("pause-all")
        self.app.tick()
        self.assertEqual(next(t for t in self.app.manager.snapshot() if t["id"] == task_id)["status"], "Paused")
        self.app.tray.events.put("exit")
        self.app.tick()
        self.assertTrue(self.app._closing)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                self.app.update()
                if not self.app.winfo_exists():
                    break
            except tk.TclError:
                break
            time.sleep(0.05)
        self.assertTrue(self.app.tray.stopped, "Exit removes the tray icon")

    def test_close_button_exits_when_the_tray_is_off(self):
        self.app.tray = FakeTray()
        self.app.manager.configure(keep_in_tray=False)
        self.app.close_button()
        self.assertTrue(self.app._closing)


if __name__ == "__main__":
    unittest.main()
