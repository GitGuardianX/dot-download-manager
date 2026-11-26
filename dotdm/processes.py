"""Run a helper program (yt-dlp) so that stopping it stops everything it started.

The bundled yt-dlp.exe is a launcher: the download runs in worker processes it starts, and
those start FFmpeg. Ending only the launcher would leave them downloading in the background
and holding DOT's files. On Windows each helper therefore runs in its own job object: stopping
the helper ends the whole job, and if DOT itself exits, Windows ends the job (kill on close).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

KILL_ON_JOB_CLOSE = 0x2000
EXTENDED_LIMITS = 9  # JobObjectExtendedLimitInformation

if os.name == "nt":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                    ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t),
                    ("processes", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6), ("process_commit", ctypes.c_size_t),
                    ("job_commit", ctypes.c_size_t), ("peak_process_commit", ctypes.c_size_t),
                    ("peak_job_commit", ctypes.c_size_t)]

    for _name, _args, _result in (("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
                                  ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
                                  ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                                  ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
                                  ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)):
        getattr(_kernel, _name).argtypes, getattr(_kernel, _name).restype = _args, _result


def _job_for(process: subprocess.Popen):
    """A kill-on-close job holding the process. Assigned right after start: the launcher spends
    its first moments unpacking itself, before it starts any worker, so the workers join too."""
    job = _kernel.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    limits.basic.flags = KILL_ON_JOB_CLOSE
    if (_kernel.SetInformationJobObject(job, EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits))
            and _kernel.AssignProcessToJobObject(job, int(process._handle))):
        return job
    _kernel.CloseHandle(job)
    return None


class Helper:
    """A started helper process (subprocess.Popen arguments) and the job holding its whole tree."""

    def __init__(self, args, **popen):
        self.process = subprocess.Popen(args, **popen)
        self.job = _job_for(self.process) if os.name == "nt" else None

    def stop(self, timeout: float = 5.0):
        """End the helper and every process it started."""
        if self.job:
            _kernel.TerminateJobObject(self.job, 1)
        elif self.process.poll() is None:
            if os.name == "nt":  # No job: end this helper's own process tree, by its process ID.
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            else:
                self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def close(self):
        """Release the job; anything still in it is ended (kill on close)."""
        if self.job:
            _kernel.CloseHandle(self.job)
            self.job = None
