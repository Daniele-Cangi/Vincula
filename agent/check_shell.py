# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Verify discovery and durable catalog sessions on a configured Vincula node."""

import argparse
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from check_jobs import call, session

VERSION = "vincula-shell-version"
WAIT = "vincula-shell-wait"
HERE = Path(__file__).resolve().parent


async def observe(client, ref, key, state, message=None):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        view = await call(client, "read_shell", {**ref, "job_id": key})
        output = view["output"]
        if output["state"] == state and (message is None or message in output["stdout"]["text"]):
            return view
        if output["state"] in {"cancelled", "completed", "failed", "unconfirmed"}:
            raise AssertionError(output)
        await asyncio.sleep(.2)
    raise AssertionError(view)


async def check(target, approval_job):
    ref = {"node_id": target, "session_id": str(uuid4())}
    key = approval_job or str(uuid4())
    prefix = "cmd" if target == "windows" else "bash"
    expected = "cmd.exe" if target == "windows" else "bash"
    async with session() as first:
        inventory = await call(first, "discover_tools", {"node_id": target, "prefix": prefix})
        assert inventory["source"] == "worker" and expected in inventory["commands"], inventory
        opened = await call(first, "open_shell", ref)
        assert opened["id"] == ref["session_id"] and not opened["commands"]
        sent = await call(first, "send_shell_command", {**ref, "job_id": key, "program": VERSION})
        assert sent["state"] == ("pending" if approval_job else "queued"), sent
        if approval_job:
            waiting = await call(first, "read_shell", {**ref, "job_id": key})
            assert waiting["output"]["state"] == "pending" and not waiting["output"]["stdout"]["text"], waiting
    async with session() as second:
        done = await observe(second, ref, key, "completed", "PowerShell" if target == "windows" else "GNU bash")
        assert done["output"]["exit_code"] == 0
        if not approval_job:
            rejected = await second.call_tool("send_shell_command", {
                **ref, "job_id": str(uuid4()), "program": "vincula-unregistered",
            })
            assert rejected.is_error
            running_key = str(uuid4())
            await call(second, "send_shell_command", {**ref, "job_id": running_key, "program": WAIT})
            await observe(second, ref, running_key, "running", "wait-ready")
            closed = await call(second, "close_shell", ref)
            assert closed["closed"] is not None
            stopped = await observe(second, ref, running_key, "cancelled")
            assert len(stopped["commands"]) == 2 and stopped["closed"] == closed["closed"]
        else:
            await call(second, "close_shell", ref)
    print(f"{sys.platform} -> {target}: PATH discovered; session recovered; "
          f"{'local approval verified' if approval_job else 'automatic execution and close/stop verified'}",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "ubuntu", "windows"))
    parser.add_argument("--approval-job", metavar="UUID")
    args = parser.parse_args()
    if args.action == "register":
        base = [sys.executable, HERE / "jobs.py", "register"]
        if sys.platform == "win32":
            shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            version = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                       "Write-Output ('PowerShell ' + $PSVersionTable.PSVersion)"]
        else:
            version = [shutil.which("bash"), "--version"]
        subprocess.run([*base, VERSION, *version], check=True)
        subprocess.run([*base, WAIT, sys.executable, "-I", "-c",
                        "import time; print('wait-ready', flush=True); time.sleep(60)"], check=True)
    else:
        asyncio.run(check(args.action, args.approval_job))


if __name__ == "__main__":
    main()
