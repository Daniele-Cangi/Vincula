# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the normal MCP acceptance checks with private, credential-free profiles."""

import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4


AGENT = Path(__file__).resolve().parents[2] / "agent"


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
        import check as checks

        if sys.platform == "win32":
            from mcp_smoke_diagnostics import diagnostic_server
            observer = diagnostic_server(checks, AGENT, directory)
        else:
            observer = nullcontext()
        with observer:
            asyncio.run(checks.check(checks.legacy_registry(), False, env))
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
            asyncio.run(checks.check(checks.registry(), False, env))
        print("MCP legacy and three-node registry checks passed with isolated profiles.")


if __name__ == "__main__":
    main()
