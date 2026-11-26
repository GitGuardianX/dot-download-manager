"""Run Brave headless inside a Windows Job Object with hard resource ceilings.

Every browser this module starts is confined to a temporary profile and a job that
enforces: a hard CPU-rate cap, a total committed-memory cap, an active-process cap,
kill-on-close, below-normal priority, and a wall-clock watchdog. If anything inside
the browser misbehaves, the job is terminated; it cannot exhaust the machine.

External DNS is disabled; only 127.0.0.1 is reachable. No real profile is touched.
"""
from __future__ import annotations

import _winapi
import ctypes as ct
import json
import msvcrt
import os
import queue
import shutil
import ssl
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
from http.server import ThreadingHTTPServer
from pathlib import Path

BROWSER = Path(os.environ.get("DOT_TEST_BROWSER", r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"))


class BasicLimits(ct.Structure):
    _fields_ = [("process_time", ct.c_int64), ("job_time", ct.c_int64),
                ("flags", wt.DWORD), ("min_working_set", ct.c_size_t),
                ("max_working_set", ct.c_size_t), ("processes", wt.DWORD),
                ("affinity", ct.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]


class ExtendedLimits(ct.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ct.c_uint64 * 6),
                ("process_commit", ct.c_size_t), ("job_commit", ct.c_size_t),
                ("peak_process_commit", ct.c_size_t), ("peak_job_commit", ct.c_size_t)]


class Accounting(ct.Structure):
    _fields_ = [("times", ct.c_int64 * 4), ("faults", wt.DWORD),
                ("total", wt.DWORD), ("active", wt.DWORD), ("terminated", wt.DWORD)]


kernel = ct.WinDLL("kernel32", use_last_error=True)
kernel.CreateJobObjectW.argtypes = [ct.c_void_p, wt.LPCWSTR]
kernel.CreateJobObjectW.restype = wt.HANDLE
kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]
kernel.QueryInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p]
kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel.ResumeThread.argtypes = [wt.HANDLE]
kernel.ResumeThread.restype = wt.DWORD
kernel.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]

KILL_ON_CLOSE, JOB_MEMORY, ACTIVE_PROCESS = 0x2000, 0x0200, 0x0008


def require(result):
    if not result:
        raise ct.WinError(ct.get_last_error())


class LimitedBrave:
    """Headless Brave driven over a private DevTools pipe, inside a capped job."""

    def __init__(self, profile: Path, *extra: str, cpu_percent=10, commit_bytes=1536 * 1024**2,
                 process_limit=14, time_limit=75, call_timeout=15):
        self.profile, self.extra = Path(profile), extra
        self.cpu_percent, self.commit_bytes = cpu_percent, commit_bytes
        self.process_limit, self.time_limit, self.call_timeout = process_limit, time_limit, call_timeout
        self.job = kernel.CreateJobObjectW(None, None)
        require(self.job)
        self.process = self.reader = self.writer = self.timer = None
        self.replies: queue.Queue = queue.Queue()
        self.events: list[dict] = []
        self.sequence = 0
        self.timed_out = self.clean_exit = False
        self.stats: dict = {}

    def __enter__(self):
        try:
            limits = ExtendedLimits()
            limits.basic.flags = KILL_ON_CLOSE | JOB_MEMORY | ACTIVE_PROCESS
            limits.basic.processes = self.process_limit
            limits.job_commit = self.commit_bytes
            require(kernel.SetInformationJobObject(self.job, 9, ct.byref(limits), ct.sizeof(limits)))
            cpu = (wt.DWORD * 2)(0x1 | 0x4, 100 * self.cpu_percent)  # Enabled + hard cap.
            require(kernel.SetInformationJobObject(self.job, 15, ct.byref(cpu), ct.sizeof(cpu)))
            actual = (wt.DWORD * 2)()
            require(kernel.QueryInformationJobObject(self.job, 15, actual, ct.sizeof(actual), None))
            if list(actual) != list(cpu):
                raise RuntimeError("Windows did not apply the CPU cap; refusing to start the browser.")
            self._launch()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _launch(self):
        browser_read, self.writer = os.pipe()
        self.reader, browser_write = os.pipe()
        try:
            handles = [msvcrt.get_osfhandle(fd) for fd in (browser_read, browser_write)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": handles})
            args = [str(BROWSER), "--headless=new", "--disable-gpu", "--disable-background-networking",
                    "--disable-component-update", "--disable-sync", "--no-proxy-server",
                    "--host-resolver-rules=EXCLUDE 127.0.0.1, MAP dot-fixture.test 127.0.0.1, MAP * ~NOTFOUND",
                    "--ignore-certificate-errors",  # Throwaway fixture certificates; temporary profile only.
                    "--no-first-run", "--no-default-browser-check", "--disable-breakpad",
                    "--disable-crash-reporter", "--renderer-process-limit=4",
                    f"--user-data-dir={self.profile}", "--disk-cache-size=8388608",
                    "--remote-debugging-pipe", "--enable-unsafe-extension-debugging",
                    f"--remote-debugging-io-pipes={handles[0]},{handles[1]}", *self.extra]
            process, thread, self.pid, _ = _winapi.CreateProcess(
                str(BROWSER), subprocess.list2cmdline(args), None, None, True,
                0x00000004 | subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS,
                None, str(BROWSER.parent), startup)  # 0x4 = CREATE_SUSPENDED until it is in the job.
            self.process = process
            try:
                require(kernel.AssignProcessToJobObject(self.job, self.process))
                self.timer = threading.Timer(self.time_limit, self._timeout)
                self.timer.daemon = True
                self.timer.start()
                if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                    raise ct.WinError(ct.get_last_error())
            except BaseException:
                _winapi.TerminateProcess(self.process, 1)
                raise
            finally:
                _winapi.CloseHandle(thread)
        finally:
            os.close(browser_read)
            os.close(browser_write)
        self.read_thread = threading.Thread(target=self._read, daemon=True)
        self.read_thread.start()

    def _timeout(self):
        self.timed_out = True
        kernel.TerminateJobObject(self.job, 1)

    def _read(self):
        pending = b""
        try:
            while chunk := os.read(self.reader, 65536):
                messages = (pending + chunk).split(b"\0")
                pending = messages.pop()
                if len(pending) > 4 * 1024**2:
                    raise RuntimeError("Oversized browser debugging message.")
                for message in messages:
                    data = json.loads(message)
                    if "id" in data:
                        self.replies.put(data)
                    elif len(self.events) < 5000:
                        self.events.append(data)
        finally:
            self.replies.put(None)

    def call(self, method, params=None, session=None):
        self.sequence += 1
        command = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            command["sessionId"] = session
        os.write(self.writer, json.dumps(command).encode() + b"\0")
        try:
            reply = self.replies.get(timeout=self.call_timeout)
        except queue.Empty as exc:
            raise RuntimeError(f"Brave timed out during {method}.") from exc
        if reply is None:
            raise RuntimeError(f"Brave exited during {method} (watchdog fired: {self.timed_out}).")
        if reply["id"] != self.sequence or "error" in reply:
            raise RuntimeError(f"{method}: {reply.get('error', 'unexpected response id')}")
        return reply["result"]

    def targets(self):
        return self.call("Target.getTargets", {"filter": [{}]})["targetInfos"]

    def attach(self, target_id):
        return self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]

    def evaluate(self, session, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                               "awaitPromise": True}, session)
        if "exceptionDetails" in result:
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:800])
        return result["result"].get("value")

    def open_page(self, url, wait=True):
        target = self.call("Target.createTarget", {"url": url})["targetId"]
        session = self.attach(target)
        if wait:
            # The initial empty document is already 'complete'; wait for the requested URL to commit.
            check = (f"(location.href === {json.dumps(url)} || location.href.startsWith({json.dumps(url)}))"
                     " && document.readyState === 'complete'")
            wait_for(lambda: self.evaluate(session, check), f"page {url}", 15)
        return target, session

    def job_snapshot(self):
        limits, counts = ExtendedLimits(), Accounting()
        require(kernel.QueryInformationJobObject(self.job, 9, ct.byref(limits), ct.sizeof(limits), None))
        require(kernel.QueryInformationJobObject(self.job, 1, ct.byref(counts), ct.sizeof(counts), None))
        return {"peak_commit_bytes": limits.peak_job_commit, "total_processes": counts.total,
                "active_processes": counts.active,
                "cpu_seconds": round(sum(counts.times[:2]) / 10_000_000, 3)}

    def __exit__(self, *exc):
        if self.process:
            if not any(exc):
                try:
                    self.call("Browser.close")
                except (OSError, RuntimeError):
                    pass
                self.clean_exit = (_winapi.WaitForSingleObject(self.process, 8000) == 0
                                   and _winapi.GetExitCodeProcess(self.process) == 0)
            try:
                self.stats = {"pid": self.pid, **self.job_snapshot()}
            except OSError:
                self.stats = {"pid": self.pid}
            kernel.TerminateJobObject(self.job, 1)
            _winapi.WaitForSingleObject(self.process, 4000)
            self.stats.update(clean_exit=self.clean_exit, watchdog_triggered=self.timed_out,
                              limits={"cpu_percent": self.cpu_percent, "commit_bytes": self.commit_bytes,
                                      "processes": self.process_limit, "wall_seconds": self.time_limit})
        if self.timer:
            self.timer.cancel()
            self.timer.join()
        if self.process:
            _winapi.CloseHandle(self.process)
        _winapi.CloseHandle(self.job)
        if hasattr(self, "read_thread"):
            self.read_thread.join(timeout=2)
        for descriptor in (self.reader, self.writer):
            if descriptor is not None:
                os.close(descriptor)


def https_server(handler, folder: Path) -> ThreadingHTTPServer:
    """Serve `handler` over TLS on 127.0.0.1 using a throwaway self-signed certificate.

    An installed IDM watches unencrypted HTTP from every brave.exe process (including
    test browsers) and pops up its own download dialog for file types it captures.
    Without its browser extension it cannot see inside TLS, so fixtures use HTTPS.
    """
    openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
    key, cert = folder / "fixture.key", folder / "fixture.crt"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1"],
                   check=True, capture_output=True, timeout=60)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def enable_developer_mode(profile: Path):
    """Match a real 'Load unpacked' user: Chromium disables unpacked extensions at startup otherwise."""
    path = Path(profile) / "Default" / "Preferences"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
    path.write_text(json.dumps(data), encoding="utf-8")


def wait_for(predicate, description, seconds=8.0, interval=0.1):
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            if result := predicate():
                return result
        except RuntimeError as exc:
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {description}" + (f" (last error: {last_error})" if last_error else ""))
