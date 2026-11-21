# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise authorized file access and transfers on the two configured nodes."""

import argparse
import asyncio
from hashlib import sha256
import sys
import time
from uuid import uuid4

from check_jobs import call, session
from jobs import ROOT as STATE_DIRECTORY

FIXTURE = bytes(range(256)) * 2048
TEXT = "Collaudo file Vincula · caffè\n"
ROOT = "vincula-files-test"
FIXTURE_DIRECTORY = STATE_DIRECTORY / "file-fixture"


def endpoint(node, path):
    return {"node_id": node, "root": ROOT, "path": path}


async def check(peer):
    origin = "windows" if sys.platform == "win32" else "ubuntu"
    async with session() as client:
        roots = await call(client, "list_file_roots", {"node_id": peer})
        assert {"alias": ROOT, "writable": True} in roots["roots"], roots
        common = {"node_id": peer, "root": ROOT}
        folder = await call(client, "request_file", {**common, "request_id": str(uuid4()), "operation": "browse"})
        assert folder["state"] == "completed"
        assert {"message.txt", "fixture.bin"} <= {entry["name"] for entry in folder["result"]["entries"]}
        text = await call(client, "request_file", {
            **common, "request_id": str(uuid4()), "operation": "read", "path": "message.txt",
        })
        assert text["state"] == "completed" and text["result"]["text"] == TEXT, text

        for source, destination, label in ((origin, peer, "upload"), (peer, origin, "download")):
            args = {"transfer_id": str(uuid4()), "source": endpoint(source, "fixture.bin"),
                    "destination": endpoint(destination, f"{origin}-{label}.bin")}
            sent = await call(client, "transfer_file", args)
            assert sent["destination"]["state"] == "completed", sent
            assert sent["destination"]["result"] == {"size": len(FIXTURE), "sha256": sha256(FIXTURE).hexdigest()}
            replay = await call(client, "transfer_file", args)
            assert replay["destination"] == sent["destination"]
            print(f"{origin} client: {source} -> {destination}, 512 KiB checksum and retry verified", flush=True)


async def check_approval(peer, transfer_id):
    origin = "windows" if sys.platform == "win32" else "ubuntu"
    args = {"transfer_id": transfer_id, "source": endpoint(origin, "fixture.bin"),
            "destination": endpoint(peer, f"{origin}-approved.bin")}
    async with session() as client:
        initial = await call(client, "transfer_file", args)
        assert initial["source"]["state"] == initial["destination"]["state"] == "pending", initial
        end = time.monotonic() + 45
        while time.monotonic() < end:
            result = await call(client, "transfer_file", args)
            if result["destination"]["state"] == "completed":
                assert result["destination"]["result"]["sha256"] == sha256(FIXTURE).hexdigest()
                print(f"{origin} -> {peer}: approvals on both nodes respected", flush=True)
                return
            await asyncio.sleep(0.6)
        raise AssertionError(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fixture", "verify", "ubuntu", "windows"))
    parser.add_argument("--approval-transfer")
    args = parser.parse_args()
    if args.command == "fixture":
        FIXTURE_DIRECTORY.mkdir()
        (FIXTURE_DIRECTORY / "fixture.bin").write_bytes(FIXTURE)
        (FIXTURE_DIRECTORY / "message.txt").write_bytes(TEXT.encode())
        print(FIXTURE_DIRECTORY)
    elif args.command == "verify":
        patterns = ("*-upload.bin", "*-download.bin", "*-approved.bin")
        outputs = [path for pattern in patterns for path in FIXTURE_DIRECTORY.glob(pattern)]
        assert len(outputs) == 3 and all(path.read_bytes() == FIXTURE for path in outputs)
        print("Physical destination files verified", flush=True)
    elif args.approval_transfer:
        asyncio.run(check_approval(args.command, args.approval_transfer))
    else:
        asyncio.run(check(args.command))


if __name__ == "__main__":
    main()
