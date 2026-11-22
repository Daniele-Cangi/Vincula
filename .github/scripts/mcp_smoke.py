# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the normal MCP acceptance checks with private, credential-free profiles."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4


AGENT = Path(__file__).resolve().parents[2] / "agent"


def windows_query(label, command, timeout):
    from server import windows_ssh

    started = monotonic()
    print(f"Starting Windows {label} query (limit {timeout}s).", flush=True)
    try:
        result = windows_ssh.local_status(timeout=timeout, command=command)
    except subprocess.TimeoutExpired:
        print(f"Windows {label} query timed out after {monotonic() - started:.2f}s.", flush=True)
        raise
    print(f"Windows {label} query completed in {monotonic() - started:.2f}s.", flush=True)
    return result


def diagnose_windows_components():
    from server import WINDOWS_RESOURCES, windows_ssh

    print("Diagnosing the failed probe with separate real queries.", flush=True)
    windows_query("PowerShell startup", "@{ci='started'} | ConvertTo-Json", 20)
    status = windows_query("base status", windows_ssh.STATUS_COMMAND, 60)
    command = "$status = " + windows_ssh._ps_string(json.dumps(status)) + " | ConvertFrom-Json\n" + WINDOWS_RESOURCES
    windows_query("hardware inventory", command, 60)


def windows_probe():
    sys.path.insert(0, str(AGENT))
    from server import WINDOWS_STATUS

    try:
        result = windows_query("normal MCP", WINDOWS_STATUS, 12)
    except subprocess.TimeoutExpired:
        diagnose_windows_components()
        raise RuntimeError("The normal Windows MCP probe exceeded its 12-second limit.") from None
    print(json.dumps(result, ensure_ascii=True))


def prepare_windows_probe(env):
    if sys.platform == "win32":
        from mcp.client.stdio import get_default_environment
        from server import WINDOWS_STATUS, windows_ssh

        # Hosted Windows images initialize their CIM providers on the first query.
        # Keep that startup outside the normal, bounded MCP availability probe.
        windows_ssh.local_status(timeout=60, command=WINDOWS_STATUS)
        print("Windows status providers initialized.")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--windows-probe"],
            env=get_default_environment() | env, capture_output=True, text=True, timeout=180,
        )
        print(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("The Windows probe failed with the MCP client environment.")


def main():
    with TemporaryDirectory(prefix="vincula-ci-") as directory:
        # Set the profile before importing the transport and runner, which cache paths.
        credentials = {name for name in os.environ
                       if name.startswith("VINCULA_") or name in {"SSH_AUTH_SOCK", "SSH_AGENT_PID"}}
        for name in credentials:
            del os.environ[name]
        env = dict.fromkeys(("HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CONFIG_HOME"), directory)
        env["PYTHONWARNINGS"] = "error::DeprecationWarning"
        os.environ.update(env)
        sys.path.insert(0, str(AGENT))
        from check import check, legacy_registry, registry

        prepare_windows_probe(env)
        asyncio.run(check(legacy_registry(), False, env))
        identifiers = {name: str(uuid4()) for name in ("ubuntu", "windows", "kali")}
        local = "windows" if sys.platform == "win32" else "ubuntu"
        document = {"local": identifiers[local], "nodes": [
            {"id": identifiers[name], "label": f"CI {name}", "family": family, "aliases": [name]}
            for name, family in (("ubuntu", "linux"), ("windows", "windows"), ("kali", "linux"))
        ]}
        config = Path(directory) / "nodes.json"
        config.write_text(json.dumps(document), encoding="utf-8")
        env["VINCULA_NODE_REGISTRY"] = str(config)
        os.environ.update(env)
        asyncio.run(check(registry(), False, env))
        print("MCP legacy and three-node registry checks passed with isolated profiles.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--windows-probe"]:
        windows_probe()
    else:
        main()
