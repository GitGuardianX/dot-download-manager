"""Clearing the download list in bulk: one save however many, files kept unless asked."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dotdm.engine import DownloadManager
from tests.test_downloads import FixtureServer


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = DownloadManager(self.root / "state", self.root / "files")

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def completed(self, name: str) -> tuple[str, Path]:
        """A finished download with its file on disk."""
        task_id = self.manager.add(f"https://example.com/{name}", filename=name, paused=True)
        task = self.manager.tasks[task_id]
        output = Path(task.directory) / task.filename
        output.write_bytes(b"finished file")
        task.status, task.total = "Completed", 13
        return task_id, output

    def test_thousands_are_removed_with_one_save(self):
        with patch.object(DownloadManager, "_save"):  # Building the list quickly is not what is tested.
            ids = [self.manager.add(f"https://example.com/file-{i}.zip", paused=True) for i in range(3000)]
        saves = []
        original = DownloadManager._save
        with patch.object(DownloadManager, "_save", autospec=True, side_effect=lambda manager: saves.append(original(manager))):
            started = time.monotonic()
            result = self.manager.remove_many(ids)
            elapsed = time.monotonic() - started
        self.assertEqual(result, {"removed": 3000, "files_deleted": 0})
        self.assertEqual(self.manager.snapshot(), [])
        self.assertEqual(len(saves), 1, "one save for the whole removal")
        self.assertLess(elapsed, 5, "thousands are removed in moments")

    def test_files_stay_unless_deleting_them_is_chosen(self):
        kept_id, kept = self.completed("kept.zip")
        gone_id, gone = self.completed("gone.zip")
        unfinished = self.manager.add("https://example.com/partial.zip", paused=True)
        parts = self.manager.store.root / "parts" / unfinished
        parts.mkdir(parents=True)
        (parts / "00000.part").write_bytes(b"partial")
        self.assertEqual(self.manager.remove_many([kept_id]), {"removed": 1, "files_deleted": 0})
        self.assertTrue(kept.exists(), "removing from the list keeps the file")
        self.assertEqual(self.manager.remove_many([gone_id, unfinished], delete_files=True), {"removed": 2, "files_deleted": 1})
        self.assertFalse(gone.exists(), "deleted on request")
        self.assertFalse(parts.exists(), "an unfinished download's partial data is always deleted")
        self.assertEqual(self.manager.snapshot(), [])

    def test_a_running_download_is_stopped_and_removed(self):
        server = FixtureServer()
        try:
            task_id = self.manager.add(server.url("/slow"))
            until = time.monotonic() + 10
            while time.monotonic() < until and next(t for t in self.manager.snapshot() if t["id"] == task_id)["downloaded"] == 0:
                time.sleep(0.05)
            self.manager.remove_many([task_id], delete_files=True)
            until = time.monotonic() + 10
            while time.monotonic() < until and self.manager.snapshot():
                time.sleep(0.05)
            self.assertEqual(self.manager.snapshot(), [])
            self.assertFalse((self.manager.store.root / "parts" / task_id).exists())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
