"""The tray window: its icon, and messages from another DOT process.

A private window class keeps these tests from ever reaching the user's real, running DOT.
"""
from __future__ import annotations

import queue
import time
import unittest
from pathlib import Path

from dotdm import tray

ICON = Path(__file__).resolve().parent.parent / "assets" / "dot.ico"
TEST_CLASS = "DOT.Tray.Test"


@unittest.skipUnless(tray.AVAILABLE, "Windows only")
class TrayTest(unittest.TestCase):
    def test_second_launch_and_quit_messages_reach_the_queue(self):
        icon = tray.Tray(ICON, "DOT test", class_name=TEST_CLASS)
        self.assertTrue(icon.start(), icon.error)
        try:
            self.assertTrue(tray.signal_running("show", TEST_CLASS))
            self.assertTrue(tray.signal_running("exit", TEST_CLASS))
            events, deadline = [], time.monotonic() + 3
            while len(events) < 2 and time.monotonic() < deadline:
                try:
                    events.append(icon.events.get(timeout=0.2))
                except queue.Empty:
                    pass
            self.assertEqual(events, ["show", "exit"])
        finally:
            icon.stop()
        self.assertFalse(tray.signal_running("show", TEST_CLASS), "the window is gone after stop()")

    def test_nothing_running_is_reported(self):
        self.assertFalse(tray.signal_running("exit", "DOT.Tray.NoSuchWindow"))

    def test_menu_choices(self):
        self.assertEqual([item[2] for item in tray.MENU if item], ["show", "pause-all", "resume-all", "exit"])


if __name__ == "__main__":
    unittest.main()
