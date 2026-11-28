# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only Windows base metrics without PowerShell, WMI or child processes.

Standalone stdlib source, also sent verbatim to an authenticated Windows peer.
GPU/edition metadata is unknown; CPU uses a short real GetSystemTimes sample.
Win32 contracts: learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/
ns-sysinfoapi-memorystatusex and processthreadsapi/nf-processthreadsapi-getsystemtimes.
"""
import ctypes
import json
import os
import shutil
import sys
import time


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                *[(name, ctypes.c_uint64) for name in (
                    "total", "available", "total_page", "available_page",
                    "total_virtual", "available_virtual", "extended")]]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def ticks(self):
        return self.low | (self.high << 32)


def _api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    api.GetSystemTimes.argtypes = [ctypes.POINTER(_FileTime)] * 3
    api.GetSystemTimes.restype = ctypes.c_int
    api.GetTickCount64.argtypes = []
    api.GetTickCount64.restype = ctypes.c_uint64
    api.GetLogicalDrives.argtypes = []
    api.GetLogicalDrives.restype = ctypes.c_uint32
    api.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    api.GetDriveTypeW.restype = ctypes.c_uint32
    return api


def _memory(api):
    info = _MemoryStatus()
    info.length = ctypes.sizeof(info)
    if not api.GlobalMemoryStatusEx(ctypes.byref(info)):
        raise OSError("Native Windows memory observation failed.")
    if not 0 <= info.available <= info.total or not info.total:
        raise ValueError("Native Windows memory observation is invalid.")
    return info.total, info.available


def _cpu_times(api):
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    if not api.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("Native Windows CPU observation failed.")
    # Kernel time includes idle; both counters use 100-nanosecond units.
    return kernel.ticks() + user.ticks(), idle.ticks()


def _cpu_percent(api):
    try:
        total_before, idle_before = _cpu_times(api)
        time.sleep(0.05)
        total_after, idle_after = _cpu_times(api)
    except OSError:
        return None
    total, idle = total_after - total_before, idle_after - idle_before
    return round(100 * (total - idle) / total, 2) if total > 0 and 0 <= idle <= total else None


def _volumes(api):
    drives = api.GetLogicalDrives()
    if not drives:
        raise OSError("Native Windows drive enumeration failed.")
    result = []
    for index in range(26):
        if not drives & (1 << index):
            continue
        root = chr(65 + index) + ":\\"
        if api.GetDriveTypeW(root) != 3:  # DRIVE_FIXED; never query network/removable capacity.
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue  # An inaccessible volume is unknown, not a zero-byte disk.
        result.append({"path": root, "total_bytes": usage.total, "available_bytes": usage.free})
    return result


def read_status():
    if sys.platform != "win32":
        raise OSError("Native Windows metrics require Windows.")
    api = _api()
    total, available = _memory(api)
    version = sys.getwindowsversion()
    # platform_version reports the actual OS rather than compatibility-mode labels.
    major, minor, build = version.platform_version
    count = os.cpu_count()
    # GetSystemTimes covers only one processor group on systems with >64 CPUs.
    cpu = _cpu_percent(api) if count is not None and count <= 64 else None
    return {"os": "Windows", "os_version": f"{major}.{minor}.{build}",
            "uptime_seconds": int(api.GetTickCount64() // 1000), "cpu_percent": cpu,
            "memory_total_bytes": total, "memory_available_bytes": available,
            "cpu_logical": count, "architecture": os.environ.get("PROCESSOR_ARCHITECTURE"),
            "gpus": None, "volumes": _volumes(api)}


if __name__ == "__main__":
    print(json.dumps(read_status(), ensure_ascii=True, allow_nan=False))
