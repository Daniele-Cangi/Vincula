# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Check dynamic PAW inputs and partition plans on both configured PCs."""

import argparse
import asyncio
from email.message import EmailMessage
from email.policy import SMTP
from hashlib import sha256
import json
import sys
import time
from uuid import UUID, uuid4

from check_jobs import call, session
import jobs

NODES = ("ubuntu", "windows")
SPLITS = {
    "full": [
        ("ubuntu", [("ubuntu", 0), ("windows", 0)]),
        ("windows", [("ubuntu", 1), ("windows", 1)]),
    ],
    "ask": [
        ("ubuntu", [("windows", 2)]),
        ("windows", [("ubuntu", 2), ("ubuntu", 3), ("windows", 3)]),
    ],
}
FIXTURES = jobs.ROOT / "paw-fixtures"
INBOX = jobs.ROOT / "paw-inbox"


def message(node, index):
    email = EmailMessage(policy=SMTP)
    email["Subject"] = f"Promemoria interno {node} {index}"
    email["From"] = "sender@example.invalid"
    email["To"] = "reader@example.invalid"
    email.set_content(f"Riunione di verifica {node}, turno {index}. Allegati assenti.")
    return email.as_bytes()


def prepare():
    INBOX.mkdir()
    FIXTURES.mkdir()
    node = "windows" if sys.platform == "win32" else "ubuntu"
    for index in range(4):
        (FIXTURES / f"sample-{index}.eml").write_bytes(message(node, index))
    with jobs.transaction() as db:
        jobs.files.grant(db, "paw-samples", str(FIXTURES.resolve()), False)
        jobs.files.grant(db, "paw-inbox", str(INBOX.resolve()), True)


async def check(mode, analysis_id):
    parts = [{"node_id": dest, "files": [{"node_id": node, "root": "paw-samples", "path": f"sample-{index}.eml"}
                                       for node, index in items]} for dest, items in SPLITS[mode]]
    args = {"analysis_id": analysis_id, "parts": parts}
    async with session() as client:
        for node in NODES:
            info = await call(client, "discover_capabilities", {"node_id": node})
            provider, = info["capabilities"]
            assert provider["id"] == "paw.analyze_offline"
        report = await call(client, "paw.analyze_offline", args)
        if mode == "ask":
            assert all(part["job"] is None and part["waiting_transfers"] for part in report["parts"]), report
            print("Trasferimenti in attesa nei due nodi.", flush=True)
        else:
            assert all(part["job"] is not None and not part["error"] for part in report["parts"]), report

    deadline = time.monotonic() + 150
    async with session() as client:
        while time.monotonic() < deadline:
            assert not any(part["error"] for part in report["parts"]), report
            if report["results"] is not None:
                break
            assert not any(part["job"] and (
                part["job"]["state"] in {"cancelled", "failed", "unconfirmed"}
                or part["job"]["state"] == "completed" and part["job"]["exit_code"] != 0
            ) for part in report["parts"]), report
            await asyncio.sleep(.5)
            report = await call(client, "paw.analyze_offline" if any(
                not part["job"] for part in report["parts"]) else "get_paw_analysis",
                args if any(not part["job"] for part in report["parts"]) else {"analysis_id": analysis_id})
        else:
            raise AssertionError(report)
        replay = await call(client, "paw.analyze_offline", args)
        assert replay["results"] == report["results"]
        assert [part["job"]["created"] for part in replay["parts"]] == [
            part["job"]["created"] for part in report["parts"]
        ]
    expected = [sha256(message(node, index)).hexdigest() for _, part in SPLITS[mode] for node, index in part]
    assert [item["sha256"] for item in report["results"]] == expected
    print(json.dumps(report["results"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "reference-full", "reference-ask", "full", "ask"))
    parser.add_argument("--analysis-id", type=UUID)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action.startswith("reference-"):
        layout = SPLITS[args.action.removeprefix("reference-")]
        inputs = [message(node, index) for _, items in layout for node, index in items]
        print(json.dumps([jobs.paw.run_sample(index, raw) for index, raw in enumerate(inputs)]))
    else:
        asyncio.run(check(args.action, str(args.analysis_id or uuid4())))


if __name__ == "__main__":
    main()
