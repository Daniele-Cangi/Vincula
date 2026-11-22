# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Private foreground-process guardian; EOF or a missing heartbeat stops its children."""

import ctypes
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
from threading import Event, Thread
from time import monotonic, sleep

import desktop_security

MAX_SPEC_BYTES = 65536


def validate_spec(specification):
    """Internal launch data only: no file selectors or output paths are accepted."""
    if not isinstance(specification, dict) or set(specification) != {"argv", "cwd", "lease_seconds", "heartbeat_token"}:
        raise ValueError("Invalid private guardian specification fields.")
    argv, cwd = specification["argv"], specification["cwd"]
    lease, token = specification["lease_seconds"], specification["heartbeat_token"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or not all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 8192 for arg in argv)
            or not os.path.isabs(argv[0])
            or cwd is not None and (not isinstance(cwd, str) or "\0" in cwd or not os.path.isabs(cwd))
            or type(lease) not in (float, int) or not 0.5 <= lease <= 10
            or not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None):
        raise ValueError("Invalid private guardian specification.")
    # Existence/access validation belongs to the authorized supervisor prepare step.
    # This parser deliberately does not query paths supplied over the wire.
    return {"argv": list(argv), "cwd": cwd, "lease_seconds": lease, "heartbeat_token": token}


def spec_frame(specification):
    payload = json.dumps(validate_spec(specification), ensure_ascii=True).encode("ascii")
    if len(payload) > MAX_SPEC_BYTES:
        raise ValueError("The private guardian specification is too large.")
    return len(payload).to_bytes(4, "big") + payload


def inherited_spec_pipe():
    if len(sys.argv) != 3 or not re.fullmatch(r"[0-9]{1,20}", sys.argv[2]):
        raise ValueError("The guardian requires a private inherited specification pipe, not a filename.")
    number = int(sys.argv[2])
    if number > (sys.maxsize if sys.platform == "win32" else 0x7FFFFFFF):
        raise ValueError("The inherited specification capability is out of range.")
    if sys.platform == "win32":
        if sys.argv[1] != "--internal-spec-handle" or number == 0:
            raise ValueError("Invalid inherited specification handle.")
        from ctypes import wintypes
        import msvcrt
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetFileType.argtypes = [wintypes.HANDLE]
        api.GetFileType.restype = wintypes.DWORD
        api.GetStdHandle.argtypes = [wintypes.DWORD]
        api.GetStdHandle.restype = wintypes.HANDLE
        if number in {api.GetStdHandle(kind) for kind in (0xFFFFFFF6, 0xFFFFFFF5, 0xFFFFFFF4)} or api.GetFileType(number) != 3:
            raise ValueError("The specification must use a distinct inherited pipe.")
        descriptor = msvcrt.open_osfhandle(number, os.O_RDONLY | os.O_BINARY)
    else:
        if sys.argv[1] != "--internal-spec-fd" or number < 3:
            raise ValueError("Invalid inherited specification descriptor.")
        descriptor = number
    if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("The specification descriptor must refer to a pipe.")
    return descriptor


def read_private_spec(descriptor, stopped):
    done, values, errors = Event(), [], []

    def exact(count):
        value = bytearray()
        while len(value) < count:
            chunk = os.read(descriptor, count - len(value))
            if not chunk:
                raise ValueError("The private specification pipe closed before its frame was complete.")
            value.extend(chunk)
        return bytes(value)

    def reader():
        try:
            length = int.from_bytes(exact(4), "big")
            if not 1 <= length <= MAX_SPEC_BYTES:
                raise ValueError("The private specification frame is too large.")
            value = exact(length)
            if os.read(descriptor, 1):
                raise ValueError("The private specification pipe contains trailing data.")
            values.append(validate_spec(json.loads(value)))
        except (OSError, ValueError, RecursionError) as error:
            errors.append(str(error))
        finally:
            os.close(descriptor)
            done.set()

    Thread(target=reader, daemon=True).start()
    deadline = monotonic() + 4
    while not done.wait(0.03):
        if stopped.is_set() or monotonic() >= deadline:
            raise ValueError("The guardian was stopped or timed out before receiving its private specification.")
    if errors:
        raise ValueError(errors[0])
    if stopped.is_set():
        raise ValueError("The guardian was stopped before program startup.")
    return values[0]


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
    descriptor = inherited_spec_pipe()
    if sys.platform != "win32" and not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT")):
        raise OSError("The process guardian requires Windows Job Objects or Linux waitid/WNOWAIT.")
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
    specification = read_private_spec(descriptor, stopped)
    # The supervisor sets this private component directory before exec. Output
    # always uses a fresh literal basename here, never a JSON/CLI-selected path.
    desktop_security.private_directory(Path.cwd())
    argv, lease = specification["argv"], specification["lease_seconds"]
    job = WindowsJob() if sys.platform == "win32" else None
    heartbeat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    heartbeat.bind(("127.0.0.1", 0))
    heartbeat.setblocking(False)
    token = specification["heartbeat_token"].encode("ascii")
    last_heartbeat = monotonic()
    try:
        log_descriptor = os.open("stdout.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        heartbeat.close()
        raise
    with heartbeat, os.fdopen(log_descriptor, "ab", buffering=0) as log:
        desktop_security.protect_new_file("stdout.log")
        if stopped.is_set():
            raise ValueError("The guardian was stopped before program startup.")
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
