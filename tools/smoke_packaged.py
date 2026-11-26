"""Verify the packaged executable without starting any browser or GUI."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotdm import __version__
from tests.test_downloads import BODY, FixtureServer


def main():
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    binary = ROOT / "build" / "app" / "DOT" / "DOT.exe"  # build.ps1's one-folder output.
    server = FixtureServer()
    try:
        with tempfile.TemporaryDirectory(prefix="dot-contained-package-") as temporary:
            folder = Path(temporary)
            state = folder / "state"
            process = subprocess.run([str(binary), "--cli", server.url("/range"),
                "--state-dir", str(state), "--output", str(folder / "files")],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW)
            saved = json.loads((state / "state.json").read_text(encoding="utf-8"))
            assert process.returncode == 0, process.returncode
            task = saved["tasks"][-1]
            assert task["status"] == "Completed", task["error"]
            actual = (Path(task["directory"]) / task["filename"]).read_bytes()
            assert actual == BODY
            assert task["sha256"] == hashlib.sha256(BODY).hexdigest()
            assert saved["settings"]["browser_enabled"] is False
            for flag in ("--browser-start", "--install-browser"):
                blocked = folder / (flag.strip("-") + "-state")
                result = subprocess.run([str(binary), flag, "--state-dir", str(blocked)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                assert result.returncode == 1, (flag, result.returncode)
                assert not blocked.exists(), flag
            result = {"status": "passed", "version": __version__, "mode": "packaged manual download",
                "bytes_verified": len(actual), "sha256": task["sha256"],
                "withdrawn_browser_flags_refused": True, "real_browser_launches": 0,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
            (output / "packaged-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        server.thread.join()


if __name__ == "__main__":
    main()
