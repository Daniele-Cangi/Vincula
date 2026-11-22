# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Private foreground-process guardian; EOF or a missing heartbeat stops its children."""

import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Event, Thread
from time import monotonic, sleep


class WindowsJob:
    """Assign this guardian before spawning: descendants cannot race job assignment."""

    def __init__(self):
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_longlong), ("per_job", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.GetCurrentProcess.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.api.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.api.AssignProcessToJobObject(self.handle, self.api.GetCurrentProcess()):
            self.api.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self):
        # The guardian is in the job too. stdout must be flushed before this call.
        self.api.TerminateJobObject(self.handle, 0)


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def child_exited(child):
    if sys.platform == "win32":
        return child.poll() is not None
    # Keep the exited leader unreaped until its process group has been cleaned up.
    return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def stop_group(child):
    for sig, delay in ((signal.SIGTERM, 0.15), (signal.SIGKILL, 0)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        if delay:
            sleep(delay)
    return child.wait(timeout=3)


def run():
    specification = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    argv = specification["argv"]
    lease = specification["lease_seconds"]
    if (not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
            or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file()
            or type(lease) not in (float, int) or not 0.5 <= lease <= 10):
        raise ValueError("Invalid private guardian specification.")
    if sys.platform != "win32" and not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT")):
        raise OSError("The process guardian requires Windows Job Objects or Linux waitid/WNOWAIT.")
    job = WindowsJob() if sys.platform == "win32" else None
    stopped = Event()

    def control():
        # Nothing inherits this pipe. Parent EOF is independent of program logs.
        # Avoid BufferedReader locks held by a daemon thread at interpreter exit.
        try:
            os.read(sys.stdin.fileno(), 1)
        except OSError:
            pass
        finally:
            stopped.set()

    Thread(target=control, daemon=True).start()
    heartbeat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    heartbeat.bind(("127.0.0.1", 0))
    heartbeat.setblocking(False)
    token = specification["heartbeat_token"].encode("ascii")
    last_heartbeat = monotonic()
    with heartbeat, open(specification["log"], "ab", buffering=0) as log:
        child = subprocess.Popen(argv, cwd=specification.get("cwd"), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                 start_new_session=sys.platform != "win32")
        reason = "exited"
        try:
            emit({"event": "started", "pid": child.pid, "heartbeat_port": heartbeat.getsockname()[1]})
            while not child_exited(child):
                for _ in range(32):
                    try:
                        value, _address = heartbeat.recvfrom(128)
                    except BlockingIOError:
                        break
                    if value == token:
                        last_heartbeat = monotonic()
                if stopped.wait(0.03):
                    reason = "control_closed"
                    break
                if monotonic() - last_heartbeat >= lease:
                    reason = "guardian_lease_expired"
                    break
        finally:
            code = child.poll() if job is not None else stop_group(child)
            try:
                emit({"event": "finished", "reason": reason, "exit_code": code})
            except OSError:
                pass
            if job is not None:
                job.terminate()


def main():
    try:
        run()
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        emit({"event": "error", "message": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
