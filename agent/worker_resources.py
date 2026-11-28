# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only OS observations. Call outside SQLite transactions; failures stay unknown."""
import csv
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryFile
import time
from uuid import UUID


def linux_memory():
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        name, _, value = line.partition(":")
        if name in {"MemTotal", "MemAvailable"}:
            fields = value.split()
            if len(fields) != 2 or fields[1] != "kB":
                raise ValueError("Unknown memory units.")
            values[name] = int(fields[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def windows_memory():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                    *[(name, ctypes.c_uint64) for name in (
                        "total", "available", "total_page", "available_page", "total_virtual",
                        "available_virtual", "extended")]]
    info = MemoryStatus()
    info.length = ctypes.sizeof(info)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not api.GlobalMemoryStatusEx(ctypes.byref(info)):
        raise OSError("Memory observation unavailable.")
    return info.total, info.available


def nvidia_observation(raw):
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise ValueError("GPU observation is not bounded.")
    devices = []
    for row in csv.reader(raw.decode("ascii").splitlines()):
        if len(row) != 3:
            raise ValueError("Unknown GPU observation fields.")
        identity, total, available = (value.strip() for value in row)
        if not identity.startswith(("GPU-", "MIG-")) or str(UUID(identity[4:])) != identity[4:]:
            raise ValueError("Stable GPU UUID unavailable.")
        total, available = int(total) * 1048576, int(available) * 1048576
        if not 0 <= available <= total:
            raise ValueError("Unknown GPU memory.")
        devices.append({"id": identity, "vram_total_bytes": total, "vram_available_bytes": available})
    if len(devices) > 128 or len({gpu["id"] for gpu in devices}) != len(devices):
        raise ValueError("Ambiguous GPU inventory.")
    return sorted(devices, key=lambda gpu: gpu["id"])


def nvidia_devices():
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    # Files avoid inherited-pipe EOF hangs on Windows. This fixed read-only tool
    # is bounded in time, and only its bounded public hardware metadata is read.
    with TemporaryFile() as output:
        subprocess.run([executable, "--query-gpu=uuid,memory.total,memory.free", "--format=csv,noheader,nounits"],
                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                       timeout=2, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        output.seek(0)
        return nvidia_observation(output.read(65537))


def cpu_times():
    if sys.platform == "win32":
        idle, kernel, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetSystemTimes.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 3
        api.GetSystemTimes.restype = ctypes.c_int
        if not api.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("CPU observation unavailable.")
        return kernel.value + user.value, idle.value
    with Path("/proc/stat").open(encoding="ascii") as stream:
        values = [int(value) for value in stream.readline().split()[1:9]]
    return sum(values), values[3] + values[4]


def cpu_percent():
    total_before, idle_before = cpu_times()
    time.sleep(0.05)
    total_after, idle_after = cpu_times()
    total, idle = total_after - total_before, idle_after - idle_before
    return round(100 * (total - idle) / total, 2) if total > 0 and 0 <= idle <= total else None


def observe():
    """Unknown metrics are None; no guessed GPU IDs or zero-valued failures."""
    observed_at = time.time()
    total = available = None
    try:
        total, available = windows_memory() if sys.platform == "win32" else linux_memory()
        if not (type(total) is int and type(available) is int and 0 <= available <= total):
            total = available = None
    except (OSError, ValueError, KeyError):
        total = available = None
    try:
        load = cpu_percent()
    except (OSError, ValueError, IndexError):
        load = None
    try:
        gpus = nvidia_devices()
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        gpus = None
    count = os.cpu_count()
    return {"observed_at": observed_at, "cpu_millis": count * 1000 if count else None, "cpu_percent": load,
            "memory_total_bytes": total, "memory_available_bytes": available, "gpus": gpus}
