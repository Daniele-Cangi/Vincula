# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cross-node acceptance test. Register the two fixed fixtures on a dedicated test node first."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PROGRAMS = ("vincula-check-exit", "vincula-check-stop")


@asynccontextmanager
async def session():
    from mcp import Client, StdioServerParameters
    env = {k: v for k, v in os.environ.items() if k.startswith("VINCULA_") or k == "SSH_AUTH_SOCK"}
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env)
    async with Client(params, read_timeout_seconds=20) as client:
        yield client


async def call(client, name, args):
    reply = await client.call_tool(name, args)
    if reply.is_error:
        raise RuntimeError(str(reply.content))
    return reply.structured_content


async def exercise(target):
    node = {"node_id": target}
    for program in PROGRAMS:
        ref = dict(node, job_id=str(uuid4()))
        request = dict(ref, program=program)
        async with session() as first:
            catalog = await call(first, "list_jobs", node)
            assert catalog["mode"] == "full" and program in catalog["programs"]
            started = await call(first, "start_job", request)
            assert started["id"] == ref["job_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                job = await call(first, "read_job", ref)
                if job["state"] == "running" and "fixture-start" in job["stdout"]["text"]:
                    break
                await asyncio.sleep(.2)
            else:
                raise AssertionError(job)
        # Exiting the context closes both the client session and its stdio server.
        async with session() as second:
            catalog = await call(second, "list_jobs", node)
            assert ref["job_id"] in {j["id"] for j in catalog["jobs"]}
            retried = await call(second, "start_job", request)
            assert retried["id"] == ref["job_id"]
            if program == PROGRAMS[1]:
                await call(second, "stop_job", ref)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                job = await call(second, "read_job", ref)
                if job["state"] in {"completed", "cancelled"}:
                    break
                await asyncio.sleep(.2)
            else:
                raise AssertionError(job)
            assert job["stdout"]["text"].count("fixture-start") == 1
            assert "fixture-stderr" in job["stderr"]["text"]
            if program == PROGRAMS[0]:
                assert job["state"] == "completed" and job["exit_code"] == 7, job
            else:
                assert job["state"] == "cancelled" and job["exit_code"] is not None, job
            print(f"{sys.platform} -> {target}: {program}, {job['state']}, exit={job['exit_code']}", flush=True)
    async with session() as client:
        reply = await client.call_tool("start_job", dict(node, program="vincula-unregistered-test", job_id=str(uuid4())))
        assert reply.is_error
        print("Unregistered program rejected.", flush=True)


async def check_approval(target, job_id):
    ref = {"node_id": target, "job_id": job_id}
    async with session() as client:
        started = await call(client, "start_job", {**ref, "program": PROGRAMS[0]})
        assert started["state"] == "pending", started
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = await call(client, "read_job", ref)
            if job["state"] == "completed":
                assert job["exit_code"] == 7
                print(f"{sys.platform} -> {target}: approved on destination, exit=7", flush=True)
                return
            if job["state"] == "pending":
                assert not job["stdout"]["text"] and not job["stderr"]["text"]
            await asyncio.sleep(.5)
        raise AssertionError("Approval or completion timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "ubuntu", "windows", *PROGRAMS))
    parser.add_argument("--await-approval", metavar="UUID", help="Wait for a local operator to approve this test ID")
    args = parser.parse_args()
    action = args.action
    if action == "register":
        for name in PROGRAMS:
            subprocess.run([sys.executable, HERE / "jobs.py", "register", name, sys.executable,
                            "-I", Path(__file__).resolve(), name], check=True)
    elif action in PROGRAMS:
        print("fixture-start", flush=True)
        print("fixture-stderr", file=sys.stderr, flush=True)
        time.sleep(8 if action == PROGRAMS[0] else 60)
        sys.exit(7)
    elif args.await_approval:
        asyncio.run(check_approval(action, args.await_approval))
    else:
        asyncio.run(exercise(action))


if __name__ == "__main__":
    main()
