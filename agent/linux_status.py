# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""The same standard-library status probe runs on local and remote Ubuntu."""

import json
import platform
from pathlib import Path
from time import sleep


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
        "memory_total_bytes": memory["MemTotal"],
        "memory_available_bytes": memory["MemAvailable"],
    }


if __name__ == "__main__":
    print(json.dumps(read_status()))
