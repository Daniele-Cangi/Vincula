# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Diagnose real Windows queries only after a failed, unwarmed MCP smoke test."""

import base64
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch


AGENT = Path(__file__).resolve().parents[2] / "agent"
MEASURE_STAGE = r"""
function Measure-CI([string]$name, [scriptblock]$body) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try { & $body }
    finally {
        [Console]::Error.WriteLine('VINCULA_CI ' + $name + ' ' +
            $watch.Elapsed.TotalSeconds.ToString('F3', [Globalization.CultureInfo]::InvariantCulture) + 's')
    }
}
"""


def cold_command(base, resources):
    for label, expression in (
        ("OS", "Get-CimInstance Win32_OperatingSystem"),
        ("CPU", 'Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name=\'_Total\'"'),
        ("disks", "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'"),
    ):
        base = base.replace(expression, f"(Measure-CI '{label}' {{ {expression} }})", 1)
    expression = "Get-CimInstance Win32_VideoController"
    resources = resources.replace(expression, f"(Measure-CI 'GPU' {{ {expression} }})", 1)
    expression = "Get-Command nvidia-smi.exe -CommandType Application -ErrorAction SilentlyContinue"
    resources = resources.replace(expression, f"(Measure-CI 'NVIDIA' {{ {expression} }})", 1)
    return MEASURE_STAGE + "$status = & {\n" + base + "\n} | ConvertFrom-Json\n" + resources


def cold_probe():
    sys.path.insert(0, str(AGENT))
    from server import WINDOWS_RESOURCES, windows_ssh

    process_run = subprocess.run

    def show_stderr(*args, **kwargs):
        try:
            result = process_run(*args, **kwargs)
        except subprocess.TimeoutExpired as error:
            print((error.stderr or b"").decode("utf-8", "replace"), end="", flush=True)
            raise
        print(result.stderr.decode("utf-8", "replace"), end="", flush=True)
        return result

    started = monotonic()
    try:
        # This is the first and only status call in this fresh runner process.
        with patch.object(windows_ssh.subprocess, "run", show_stderr):
            windows_ssh.local_status(timeout=60, command=cold_command(windows_ssh.STATUS_COMMAND, WINDOWS_RESOURCES))
    except (subprocess.SubprocessError, windows_ssh.LinkError, OSError, ValueError) as error:
        raise SystemExit(f"Cold Windows status: {type(error).__name__} after {monotonic() - started:.2f}s.") from None
    print(f"Cold Windows status: completed in {monotonic() - started:.2f}s.", flush=True)


def query(label, command, timeout):
    from server import windows_ssh

    started = monotonic()
    try:
        value = windows_ssh.local_status(timeout=timeout, command=command)
    except (subprocess.SubprocessError, windows_ssh.LinkError, OSError, ValueError) as error:
        print(f"{label}: {type(error).__name__} after {monotonic() - started:.2f}s.", flush=True)
        return None
    print(f"{label}: completed in {monotonic() - started:.2f}s.", flush=True)
    return value


def probe():
    sys.path.insert(0, str(AGENT))
    from server import WINDOWS_RESOURCES, WINDOWS_STATUS, windows_ssh

    query(".NET startup", "[Console]::WriteLine('{\"ci\":true}')", 20)
    paths = query("Effective module paths", r'''[Console]::WriteLine('{"paths":"' +
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($env:PSModulePath)) + '"}')''', 20)
    if paths is not None:
        print("Effective PSModulePath: " + base64.b64decode(paths["paths"]).decode("utf-8"), flush=True)
    restricted = "$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')\n"
    query("JSON with built-in modules only", restricted + "@{ci=$true} | ConvertTo-Json", 20)
    query("Full status with built-in modules only", restricted + WINDOWS_STATUS, 60)
    query("JSON cmdlet", "@{ci=$true} | ConvertTo-Json", 20)
    status = query("Base status", windows_ssh.STATUS_COMMAND, 60)
    if status is not None:
        script = "$status = " + windows_ssh._ps_string(json.dumps(status)) + " | ConvertFrom-Json\n" + WINDOWS_RESOURCES
        query("Hardware inventory", script, 60)
    query("Complete status", WINDOWS_STATUS, 60)


def main():
    if sys.argv[1:] == ["--cold-probe"]:
        cold_probe()
        return
    if sys.argv[1:] == ["--probe"]:
        probe()
        return
    from mcp.client.stdio import get_default_environment

    with TemporaryDirectory(prefix="vincula-ci-diagnostic-") as directory:
        environment = get_default_environment() | dict.fromkeys(
            ("HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CONFIG_HOME"), directory,
        )
        environment["PYTHONWARNINGS"] = "error::DeprecationWarning"
        mode = "--cold-probe" if sys.argv[1:] == ["--cold"] else "--probe"
        subprocess.run([sys.executable, str(Path(__file__).resolve()), mode],
                       env=environment, check=True, timeout=340)


if __name__ == "__main__":
    main()
