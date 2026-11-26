"""Guarantees about the browser extension and DOT's browser-facing entry points.

These guard against the hazards found in the withdrawn 0.2 integration: native-messaging
launches, unbounded page scanning, timers in every frame, and whole-traffic interception.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotdm import integration
from dotdm.__main__ import main
from dotdm.bridge import EXTENSION_ID

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "browser-extension"
ALLOWED_PERMISSIONS = {"downloads", "storage", "contextMenus", "cookies", "webRequest"}


def code(name: str) -> str:
    """Source without full-line comments, so explanatory comments cannot mask or trip checks."""
    text = (EXTENSION / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


class ExtensionSafetyTest(unittest.TestCase):
    def test_manifest_permissions_and_entry_points(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertLessEqual(set(manifest["permissions"]), ALLOWED_PERMISSIONS)
        self.assertNotIn("nativeMessaging", json.dumps(manifest))
        self.assertEqual(manifest["background"], {"service_worker": "worker.js"})
        self.assertEqual([script["js"] for script in manifest["content_scripts"]], [["content.js"]])
        self.assertEqual(integration.extension_id(EXTENSION / "manifest.json"), EXTENSION_ID, "extension ID must stay stable")
        shipped = {path.name for path in EXTENSION.iterdir()}
        self.assertEqual(shipped, {"manifest.json", "worker.js", "content.js", "popup.html", "popup.js", "popup.css", "icon.png"})

    def test_worker_has_no_launchers_timers_or_full_interception(self):
        worker = code("worker.js")
        for forbidden in ("sendNativeMessage", "connectNative", "chrome.alarms", "setInterval(", "onCreated.addListener",
                          "extraHeaders", "webRequestBlocking", "requestHeaders", "executeScript"):
            self.assertNotIn(forbidden, worker, forbidden)
        self.assertIn("onDeterminingFilename.addListener", worker)
        filters = re.findall(r'types:\s*(\[[^\]]*\])', worker)
        self.assertEqual(filters, ['["media", "xmlhttprequest"]'], "media detection sees media and XHR responses only")
        self.assertEqual(len(re.findall(r"\bsetTimeout\(", worker)), 1, "only the per-download release guard")

    def test_content_script_is_event_driven(self):
        content = code("content.js")
        for forbidden in ("new MutationObserver", "setInterval(", "querySelectorAll(", "sendNativeMessage", "fetch("):
            self.assertNotIn(forbidden, content, forbidden)
        self.assertIn('addEventListener("playing"', content)
        self.assertIn("i < 16", content, "the one start-up look is bounded")
        self.assertEqual(content.count("event.isTrusted"), 3, "every button acts on real user clicks only")

    def test_popup_has_no_timers(self):
        popup = code("popup.js")
        self.assertNotIn("setInterval(", popup)
        self.assertNotIn("setTimeout(", popup)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the worker behavior tests")
    def test_worker_behavior(self):
        result = subprocess.run(["node", "--max-old-space-size=128", str(ROOT / "tools" / "test_extension_worker.mjs")],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:] or result.stdout[-2000:])
        self.assertEqual(json.loads(result.stdout)["status"], "passed")


class AppEntryPointTest(unittest.TestCase):
    def test_withdrawn_browser_flags_exit_before_creating_state_or_ui(self):
        for flag in ("--browser-start", "--install-browser"):
            with tempfile.TemporaryDirectory() as temporary, self.subTest(flag=flag):
                state = Path(temporary) / "state"
                with patch.object(sys, "argv", ["dot", flag, "--state-dir", str(state)]):
                    self.assertEqual(main(), 1)
                self.assertFalse(state.exists())

    def test_no_native_messaging_host_is_built_or_registered(self):
        self.assertFalse(hasattr(integration, "register_browser"))
        build = (ROOT / "build.ps1").read_text(encoding="utf-8")
        self.assertNotIn("csc.exe", build, "the native host is no longer compiled")
        self.assertNotIn("DotBrowserHost.cs", build)
        self.assertNotIn("BrowserHost", (ROOT / "installer" / "DOT.iss").read_text(encoding="utf-8"), "nor installed")
        with patch("winreg.DeleteKey", side_effect=FileNotFoundError) as delete:
            self.assertEqual(integration.remove_legacy_native_host(), 0)
        self.assertTrue(all("com.dot.downloadmanager" in str(call.args[1]) for call in delete.call_args_list))

    def test_start_with_windows_needs_the_packaged_app(self):
        self.assertIsNone(integration.startup_command())  # Source mode never writes a Run entry.
        with patch("winreg.CreateKey") as create:
            with self.assertRaises(RuntimeError):
                integration.set_start_with_windows(True)
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
