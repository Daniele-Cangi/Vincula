# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the normal MCP acceptance checks with private, credential-free profiles."""

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4


def prepare_windows_probe():
    if sys.platform == "win32":
        from server import WINDOWS_STATUS, windows_ssh

        # Hosted Windows images initialize their CIM providers on the first query.
        # Keep that startup outside the normal, bounded MCP availability probe.
        windows_ssh.local_status(timeout=60, command=WINDOWS_STATUS)
        print("Windows status providers initialized.")


def main():
    with TemporaryDirectory(prefix="vincula-ci-") as directory:
        # Set the profile before importing the transport and runner, which cache paths.
        for name in list(os.environ):
            if name.startswith("VINCULA_") or name in {"SSH_AUTH_SOCK", "SSH_AGENT_PID"}:
                del os.environ[name]
        env = {name: directory for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CONFIG_HOME")}
        env["PYTHONWARNINGS"] = "error::DeprecationWarning"
        os.environ.update(env)
        agent = Path(__file__).resolve().parents[2] / "agent"
        sys.path.insert(0, str(agent))
        from check import check, legacy_registry, registry

        prepare_windows_probe()
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
    main()
