# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Verify a split prime-count workload across Vincula's two physical nodes."""

import argparse
import asyncio
import json
from math import isqrt
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PARTS = (("ubuntu", "vincula-primes-a", 2, 250_000), ("windows", "vincula-primes-b", 250_000, 500_000))


def count_primes(start, end):
    return sum(
        1 for candidate in range(start, end)
        if (candidate == 2 or candidate % 2)
        and all(candidate % divisor for divisor in range(3, isqrt(candidate) + 1, 2))
    )


async def check():
    from check_jobs import call, session

    requests = [{"program": name} for _, name, _, _ in PARTS]
    ref = {"batch_id": str(uuid4())}
    async with session() as first:
        inventory = await call(first, "list_nodes", {})
        selectors = {alias: node["node_id"] for node in inventory["nodes"]
                     for alias in (node["node_id"], *node["aliases"])}
        expected_nodes = [selectors[alias] for alias, _, _, _ in PARTS]
        submitted = await call(first, "submit_batch", {**ref, "tasks": requests})
        assert [part["node_id"] for part in submitted["tasks"]] == expected_nodes, submitted
        assert all(part["job"] and not part["error"] for part in submitted["tasks"]), submitted
        ids = [part["job_id"] for part in submitted["tasks"]]

    async with session() as client:
        parallel = False
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            report = await call(client, "get_batch", ref)
            assert [part["job_id"] for part in report["tasks"]] == ids
            assert all(part["job"] and not part["error"] for part in report["tasks"]), report
            states = [part["job"]["state"] for part in report["tasks"]]
            parallel |= states == ["running", "running"]
            if states == ["completed", "completed"]:
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError(report)
        assert parallel, "Did not observe both workers running at the same time."

        replay = await call(client, "submit_batch", {**ref, "tasks": requests})
        assert [part["job_id"] for part in replay["tasks"]] == ids
        assert [part["job"]["created"] for part in replay["tasks"]] == [
            part["job"]["created"] for part in report["tasks"]
        ]
        total = 0
        for task, (_, _, start, end) in zip(report["tasks"], PARTS):
            details = await call(client, "read_job", {"node_id": task["node_id"], "job_id": task["job_id"]})
            assert details["exit_code"] == 0 and not details["stdout"]["truncated"], details
            shard = json.loads(details["stdout"]["text"])
            assert (shard["start"], shard["end"]) == (start, end), shard
            total += shard["primes"]
    reference = count_primes(PARTS[0][2], PARTS[-1][3])
    assert total == reference == 41_538, (total, reference)
    print(f"{sys.platform}: 2 concurrent shards, durable batch, no duplicate submission, {total:,} primes", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ubuntu", "windows", "shard", "check"))
    parser.add_argument("bounds", nargs="*", type=int)
    args = parser.parse_args()
    if args.action == "shard":
        start, end = args.bounds
        # Keep the observation window long enough for SSH polling on fast test PCs.
        started = time.monotonic()
        result = count_primes(start, end)
        time.sleep(max(0, 6 - (time.monotonic() - started)))
        print(json.dumps({"start": start, "end": end, "primes": result}))
    elif args.action == "check":
        asyncio.run(check())
    else:
        _, name, start, end = next(part for part in PARTS if part[0] == args.action)
        subprocess.run([sys.executable, HERE / "jobs.py", "register", name, sys.executable, "-I",
                        HERE / "check_batch.py", "shard", str(start), str(end)], check=True)


if __name__ == "__main__":
    main()
