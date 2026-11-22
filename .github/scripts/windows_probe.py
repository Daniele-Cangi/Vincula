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


AGENT = Path(__file__).resolve().parents[2] / "agent"


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
    if sys.argv[1:] == ["--probe"]:
        probe()
        return
    from mcp.client.stdio import get_default_environment

    with TemporaryDirectory(prefix="vincula-ci-diagnostic-") as directory:
        environment = get_default_environment() | dict.fromkeys(
            ("HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CONFIG_HOME"), directory,
        )
        environment["PYTHONWARNINGS"] = "error::DeprecationWarning"
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe"],
                       env=environment, check=True, timeout=340)


if __name__ == "__main__":
    main()
