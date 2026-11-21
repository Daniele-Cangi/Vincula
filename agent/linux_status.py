# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Report resources on local or remote Linux nodes."""

import csv
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
from time import sleep


def nvidia_devices():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        output = subprocess.check_output(
            [executable, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, text=True,
        )
        return [{"name": name.strip(), "vram_total_bytes": int(total) * 1048576,
                 "vram_available_bytes": int(free) * 1048576} for name, total, free in csv.reader(output.splitlines())]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def sysfs_value(path, convert=str):
    try:
        return convert(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def gpu_devices():
    devices = nvidia_devices()
    have_nvidia = bool(devices)
    for card in sorted(Path("/sys/class/drm").glob("card*")):
        if not re.fullmatch(r"card[0-9]+", card.name):
            continue
        device = card / "device"
        vendor, product = (sysfs_value(device / name) for name in ("vendor", "device"))
        if vendor is None or (vendor == "0x10de" and have_nvidia):
            continue
        total, used = (sysfs_value(device / ("mem_info_vram_" + item), int) for item in ("total", "used"))
        devices.append({"name": f"PCI {vendor}:{product}" if product else f"PCI {vendor}",
                        "vram_total_bytes": total,
                        "vram_available_bytes": max(0, total - used) if total is not None and used is not None else None})
    return devices or None


def disk_volumes():
    volumes = []
    seen = set()
    for path in (Path("/"), Path.home()):
        try:
            device = path.stat().st_dev
            if device not in seen:
                disk = shutil.disk_usage(path)
                volumes.append({"path": "/" if path == Path("/") else "home",
                                "total_bytes": disk.total, "available_bytes": disk.free})
                seen.add(device)
        except OSError:
            continue
    return volumes or None


def cpu_times():
    with Path("/proc/stat").open(encoding="ascii") as stats:
        fields = [int(field) for field in stats.readline().split()[1:9]]
    return sum(fields), fields[3] + fields[4]


def read_status():
    os_info = platform.freedesktop_os_release()
    memory = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, _, value = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0]) * 1024

    total_before, idle_before = cpu_times()
    sleep(0.25)
    total_after, idle_after = cpu_times()
    total = total_after - total_before
    busy = total - (idle_after - idle_before)
    return {
        "os": os_info["PRETTY_NAME"],
        "os_version": os_info.get("VERSION_ID"),
        "uptime_seconds": int(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])),
        "cpu_percent": round(100 * busy / total, 2) if total > 0 and 0 <= busy <= total else None,
        "cpu_logical": os.cpu_count(),
        "architecture": platform.machine() or None,
        "memory_total_bytes": memory["MemTotal"],
        "memory_available_bytes": memory["MemAvailable"],
        "gpus": gpu_devices(),
        "volumes": disk_volumes(),
    }


if __name__ == "__main__":
    print(json.dumps(read_status()))
