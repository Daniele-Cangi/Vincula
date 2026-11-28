# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real supervised file jobs; only fresh owner-local temporary files, no SSH/GUI."""

from contextlib import ExitStack, contextmanager
from argparse import Namespace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import select
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


@unittest.skipUnless(sys.platform in {"linux", "win32"}, "Qualified native worker platforms")
class LargeFilesRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-large-runtime-")
        self.temporary, self.quiescent = temporary, False
        self.addCleanup(self.cleanup_temp_if_confirmed)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / ("Vincula-Jobs" if jobs.WINDOWS else "jobs")
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        self.handles, self.workers = ExitStack(), []
        self.addCleanup(self.handles.close)
        self.addCleanup(self.cleanup)
        if not jobs.WINDOWS:
            self.ownership = (self.root / "worker.lock").open("a+b")
            self.handles.callback(self.ownership.close)
        self.source, self.target = self.home / "source", self.home / "target"
        self.source.mkdir(); self.target.mkdir()
        self.source_node, self.target_node, self.authority = (str(uuid4()) for _ in range(3))
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")
            jobs.files.grant(db, "source", str(self.source), False)
            jobs.files.grant(db, "target", str(self.target), True)
            self.state_id = jobs.resource_policy.state_id(db)
            policy = dict(jobs.transfer_state.DEFAULT_POLICY, enabled=True,
                          max_file_bytes=128 * 1024 * 1024, max_reserved_bytes=512 * 1024 * 1024,
                          margin_bytes=0)
            jobs.transfer_state.set_policy(db, policy)

    def cleanup(self):
        (self.root / "worker.stop").touch()
        with jobs.transaction() as db:
            for row in db.execute("SELECT id FROM jobs WHERE state IN ('pending','queued','running','stopping')").fetchall():
                jobs.request_stop(db, row[0])
        for worker in self.workers:
            self.assertEqual(worker.wait(timeout=20), 0, "The original private worker did not drain cleanly")
        if not jobs.WINDOWS:
            import fcntl

            def acquire():
                try:
                    fcntl.flock(self.ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True

            self.wait(acquire)
        with jobs.transaction() as db:
            self.assertEqual(jobs.resource_policy.status(db)["claims"], [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_attempts WHERE cleanup_confirmed=0").fetchone()[0], 0)
        self.quiescent = True

    def cleanup_temp_if_confirmed(self):
        if self.quiescent:
            self.temporary.cleanup()
        else:
            # Preserve diagnostics and possible owned I/O. Never remove a live
            # namespace because a worker/guardian wait or receipt was uncertain.
            self.temporary._finalizer.detach()
            self.fail("Original process cleanup is unconfirmed; preserved fixture: " + self.temporary.name)

    def start_worker(self):
        code = ("import runpy;from pathlib import Path;"
                "m=runpy.run_path(" + repr(str(self.root / "jobs.py")) + ",run_name='private_file_worker');"
                "g=m['work'].__globals__;g['ROOT']=Path(" + repr(str(self.root)) + ");"
                "g['DATABASE']=g['ROOT']/'jobs.sqlite3';m['work']()")
        output = (self.home / ("worker-" + uuid4().hex + ".log")).open("wb")
        self.handles.callback(output.close)
        options = {"env": dict(os.environ, LOCALAPPDATA=str(self.home))} if jobs.WINDOWS else {}
        worker = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], cwd=self.root,
                                  stdout=output, stderr=output, **options)
        self.workers.append(worker)
        self.wait(lambda: self.rpc("info")[0]["file_worker_fresh"])
        return worker

    def wait(self, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.025)
        with jobs.transaction() as db:
            rows = [dict(row) for row in db.execute("SELECT " + jobs.SUMMARY + " FROM jobs")]
        for row in rows:
            row["stderr"] = jobs.tail(row["id"], "err")
        logs = {path.name: path.read_text(errors="replace")[-4096:] for path in self.home.glob("worker-*.log")}
        self.fail("Private file worker timed out: " + json.dumps({"jobs": rows, "logs": logs}))

    def rpc(self, operation, endpoint=None, *, payload=b"", **fields):
        if endpoint is not None:
            fields.update(request_id=endpoint["id"], expected_state_id=self.state_id)
            if operation not in {"status", "blocks"}:
                fields.setdefault("epoch", endpoint["epoch"])
        return jobs.large_files.dispatch(jobs.LOCAL_RUNTIME, operation, protocol_version=2, payload=payload, **fields)

    def status(self, endpoint):
        return self.rpc("status", endpoint)[0]

    def approve(self, endpoint):
        value = self.status(endpoint)
        self.assertIsNotNone(value["approval_stage"])
        with jobs.transaction() as db:
            with self.assertRaises(ValueError):
                jobs.transfer_state.decide(db, endpoint["id"], True, "f" * 64)
            return jobs.transfer_state.decide(db, endpoint["id"], True, value["approval_sha256"])

    def fixture_source(self, size=64 * 1024 * 1024 + 257):
        path = self.source / "source.bin"
        block = bytes(range(256)) * (jobs.transfer_wire.BLOCK_BYTES // 256)
        digest = sha256()
        with path.open("xb") as output:
            remaining = size
            while remaining:
                data = block[:min(remaining, len(block))]
                output.write(data); digest.update(data); remaining -= len(data)
        return path, size, digest.hexdigest()

    def request_pair(self, *, access="full", size=64 * 1024 * 1024 + 257):
        path, size, digest = self.fixture_source(size)
        with jobs.transaction() as db:
            jobs.set_mode(db, access)
        transfer = str(uuid4())
        base = {"transfer_id": transfer, "authority_node_id": self.authority,
                "source": {"node_id": self.source_node, "root": "source", "path": path.name},
                "destination": {"node_id": self.target_node, "root": "target", "path": "received.bin"},
                "expected_state_id": self.state_id, "max_bytes": size, "expires_at": time.time() + 600}
        export = self.rpc("request", spec=dict(base, role="export", source_sha256=None))[0]
        if access == "ask":
            with self.assertRaises(ValueError):
                self.rpc("reserve", export)
            export = self.approve(export)
        self.assertTrue(self.rpc("reserve", export)[0]["reservation"]["reserved"])
        self.rpc("queue", export)
        self.complete_attempt(export, "prepare")
        export = self.status(export)
        self.assertEqual(export["state"], "ready")
        self.assertEqual(export["header"]["sha256"], digest)
        target = self.rpc("request", spec=dict(base, role="import", source_sha256=export["request_sha256"]))[0]
        if access == "ask":
            target = self.approve(target)
        self.assertTrue(self.rpc("reserve", target)[0]["reservation"]["reserved"])
        target = self.rpc("bind", target, header=export["header"])[0]
        if access == "ask":
            self.assertEqual(target["approval_stage"], "content")
            with self.assertRaises(ValueError):
                self.rpc("queue", target)
            target = self.approve(target)
        return export, target, size, digest

    def request_export(self, size=4 * 1024 * 1024 + 19):
        path, size, _ = self.fixture_source(size)
        spec = {"transfer_id": str(uuid4()), "authority_node_id": self.authority,
                "source": {"node_id": self.source_node, "root": "source", "path": path.name},
                "destination": {"node_id": self.target_node, "root": "target", "path": "received.bin"},
                "expected_state_id": self.state_id, "max_bytes": size, "expires_at": time.time() + 600,
                "role": "export", "source_sha256": None}
        return self.rpc("request", spec=spec)[0]

    def replace_history(self, endpoint):
        """Install a self-consistent foreign history between receiver transactions."""
        changed = str(uuid4())
        with jobs.transaction() as db:
            record = jobs.transfer_state.details(db, endpoint["id"])
            spec = dict(record["spec"], expected_state_id=changed)
            digest = jobs.transfer_state.digest({"spec": spec, "grant": record["grant_snapshot"],
                                                  "source_identity": record["source_identity"]})
            header = dict(record["header"], request_sha256=digest) if record["header"] is not None else None
            db.execute("UPDATE resource_identity SET state_id=? WHERE id=1", (changed,))
            db.execute("UPDATE large_file_endpoints SET spec=?,request_sha256=?,intent_sha256=?,header=? WHERE id=?",
                       (jobs.transfer_state.canonical(spec), digest, digest,
                        jobs.transfer_state.canonical(header) if header is not None else None, endpoint["id"]))
        return changed

    def pin_race(self, operation):
        self.start_worker()
        endpoint = self.request_export()
        if operation in {"queue", "blocks"}:
            self.assertTrue(self.rpc("reserve", endpoint)[0]["reservation"]["reserved"])
        if operation == "blocks":
            self.rpc("queue", endpoint)
            self.complete_attempt(endpoint, "prepare")
        original = jobs.transaction
        enters, changed = 0, []

        @contextmanager
        def racing_transaction():
            nonlocal enters
            enters += 1
            if enters == 2:
                # This COMMIT precedes the receiver's second operation TX.
                # Calling the original context prevents recursion into the hook.
                with patch.object(jobs, "transaction", original):
                    changed.append(self.replace_history(endpoint))
            with original() as db:
                yield db

        with patch.object(jobs, "transaction", racing_transaction), self.assertRaisesRegex(ValueError, "history"):
            self.rpc(operation, endpoint)
        self.assertEqual(len(changed), 1)
        # The foreign row itself is authorized and would pass a self-only
        # state/epoch guard. The captured client pin is the necessary fence.
        with original() as db:
            row = jobs.transfer_state.guard(db, endpoint["id"], endpoint["epoch"], require_reserved=operation != "reserve")
            self.assertEqual(row["spec"]["expected_state_id"], changed[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_jobs").fetchone()[0], int(operation == "blocks"))
            if operation == "reserve":
                self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_claims").fetchone()[0], 0)

    def complete_attempt(self, endpoint, kind):
        with jobs.transaction() as db:
            key = db.execute("SELECT job_id FROM large_file_jobs WHERE endpoint_id=? AND kind=? ORDER BY rowid DESC LIMIT 1",
                             (endpoint["id"], kind)).fetchone()[0]
        def complete():
            with jobs.transaction() as db:
                row = db.execute("SELECT j.state,j.exit_code,a.cleanup_confirmed,a.outcome FROM jobs j "
                                 "JOIN large_file_attempts a ON a.id=j.id WHERE j.id=?", (key,)).fetchone()
            return row if row[0] not in jobs.ACTIVE and row[2] else None
        result = self.wait(complete)
        self.assertEqual(tuple(result), ("completed", 0, 1, "completed"), jobs.tail(key, "err"))
        public = self.status(endpoint)["job"]
        self.assertEqual(set(public), {"id", "kind", "state", "exit_code", "cleanup_confirmed", "created", "finished"})
        self.assertEqual((public["id"], public["kind"], public["state"], public["exit_code"], public["cleanup_confirmed"]),
                         (key, kind, "completed", 0, True))
        self.assertLessEqual(public["created"], public["finished"])
        return key

    def cli_rpc(self, operation, endpoint, *, payload=b"", **fields):
        wire = jobs.transfer_wire
        request = dict(action="large-files", operation=operation, protocol_version=2,
                       request_id=endpoint["id"], expected_state_id=self.state_id, epoch=endpoint["epoch"], **fields)
        if jobs.WINDOWS:
            command = wire.WINDOWS_COMMAND
            options = {"env": dict(os.environ, LOCALAPPDATA=str(self.home))}
        else:
            code = ("import runpy,sys;from pathlib import Path;"
                    "m=runpy.run_path(" + repr(str(self.root / "jobs.py")) + ",run_name='private_block_receiver');"
                    "g=m['main'].__globals__;g['ROOT']=Path(" + repr(str(self.root)) + ");"
                    "g['DATABASE']=g['ROOT']/'jobs.sqlite3';"
                    "sys.argv=['jobs.py','large-files-stdin'];m['main']()")
            command, options = [sys.executable, "-I", "-B", "-c", code], {}
        with TemporaryFile(dir=self.home) as source, TemporaryFile(dir=self.home) as output, \
                TemporaryFile(dir=self.home) as errors:
            source.write(wire.pack(request, payload)); source.seek(0)
            completed = subprocess.run(command, stdin=source, stdout=output, stderr=errors, timeout=30, **options)
            errors.seek(max(0, errors.seek(0, 2) - 4096))
            self.assertEqual(completed.returncode, 0, errors.read(4096).decode("utf-8", "replace"))
            size = output.seek(0, 2)
            self.assertLessEqual(size, wire.MAX_FRAME_BYTES)
            output.seek(0)
            reply, data = wire.read(output)
            self.assertEqual(output.tell(), size)
            self.assertNotIn("error", reply, reply)
            return reply["result"], data

    def relay(self, export, target, *, start=0, stop=None, binary_first=False):
        stop = export["header"]["block_count"] if stop is None else stop
        for index in range(start, stop):
            exchange = self.cli_rpc if binary_first and index == 0 else self.rpc
            reply, data = exchange("block-read", export, index=index)
            block = reply["block"]
            self.assertEqual(len(data), block["size"])
            self.assertEqual(sha256(data).hexdigest(), block["sha256"])
            self.assertEqual(exchange("block-write", target, index=index, expected=block, payload=data), ({"block": block}, b""))

    def publish_and_cleanup(self, export, target, size, digest):
        received = self.status(target)
        self.assertEqual(received["verified_bytes"], size)
        self.assertEqual(received["verified_blocks"], export["header"]["block_count"])
        self.rpc("queue", target)
        self.complete_attempt(target, "commit")
        published = self.status(target)
        self.assertEqual(published["state"], "published")
        path = self.target / "received.bin"
        self.assertEqual(path.stat().st_size, size)
        with path.open("rb") as source:
            actual = sha256()
            while data := source.read(1024 * 1024):
                actual.update(data)
        self.assertEqual(actual.hexdigest(), digest)
        with jobs.transaction() as db:
            self.assertEqual(jobs.resource_policy.status(db)["claims"], [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_claims WHERE released IS NULL").fetchone()[0], 2)
        for endpoint in (export, target):
            cleaned = self.rpc("cleanup", self.status(endpoint))[0]
            self.assertEqual(cleaned["state"], "cleaned")
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_claims WHERE released IS NULL").fetchone()[0], 0)
        self.assertTrue(path.is_file(), "Endpoint staging cleanup removed the published target")

    def test_full_real_worker_transfers_more_than_64_mib_and_releases_only_staging(self):
        self.start_worker()
        export, target, size, digest = self.request_pair()
        self.assertGreater(size, 64 * 1024 * 1024)
        self.relay(export, target, binary_first=True)
        self.publish_and_cleanup(export, target, size, digest)
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM programs").fetchone()[0], 0)

    def test_ask_real_worker_requires_exact_intent_and_content_approvals(self):
        self.start_worker()
        export, target, size, digest = self.request_pair(access="ask", size=8 * 1024 * 1024 + 37)
        self.assertIsNone(self.status(export)["approval_stage"])
        self.assertIsNone(self.status(target)["approval_stage"])
        self.relay(export, target)
        self.publish_and_cleanup(export, target, size, digest)
        with jobs.transaction() as db:
            self.assertEqual(jobs.mode(db), "ask")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_attempts").fetchone()[0], 2)

    def test_partial_transfer_stop_invalid_control_epoch_and_resume_keep_verified_blocks(self):
        self.start_worker()
        export, target, size, digest = self.request_pair(size=8 * 1024 * 1024 + 257)
        self.relay(export, target, stop=1)
        reply, data = self.rpc("block-read", export, index=0)
        block = reply["block"]
        before = self.status(target)
        self.assertEqual(before["verified_bytes"], len(data))
        self.assertEqual(before["verified_blocks"], 1)
        for operation in ("stop", "cleanup"):
            for invalid in (True, None, 0):
                with self.subTest(operation=operation, epoch=invalid), self.assertRaises(ValueError):
                    self.rpc(operation, target, epoch=invalid)
                self.assertEqual(self.status(target), before)
                self.assertEqual(self.rpc("verify-block", target, index=0, expected=block), ({"valid": True}, b""))
        stopped = self.rpc("stop", target)[0]
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["reserved_bytes"], before["reserved_bytes"])
        self.assertEqual(stopped["verified_bytes"], before["verified_bytes"])
        with self.assertRaises(ValueError):
            self.rpc("block-write", target, index=0, expected=block, payload=data)
        resumed = self.rpc("resume", stopped)[0]
        self.assertGreater(resumed["epoch"], stopped["epoch"])
        self.assertEqual(resumed["verified_blocks"], 1)
        self.assertEqual(self.rpc("verify-block", resumed, index=0, expected=block), ({"valid": True}, b""))
        self.relay(export, resumed, stop=1)  # Idempotent retry does not double count block zero.
        self.assertEqual(self.status(resumed)["verified_blocks"], 1)
        self.relay(export, resumed, start=1)
        self.publish_and_cleanup(export, resumed, size, digest)

    def test_file_job_shares_pool_with_real_tree_and_stop_confirms_descendant_cleanup(self):
        with jobs.transaction() as db:
            jobs.resource_policy.set_policy(db, dict(jobs.resource_policy.DEFAULT_POLICY, max_jobs=2,
                legacy_defaults={"cpu_millis": 0, "memory_bytes": 0, "gpus": []}))
        marker = self.home / "holding-tree.json"
        code = ("import json,os,subprocess,sys,time;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                "Path(" + repr(str(marker)) + ").write_text(json.dumps([os.getpid(),child.pid]));time.sleep(60)")
        jobs.operator(Namespace(command="register", name="holding", argv=[sys.executable, "-c", code]))
        self.start_worker()
        holding = str(uuid4())
        jobs.rpc("start", holding, "holding")
        self.wait(marker.exists)
        pids = json.loads(marker.read_text())
        if jobs.WINDOWS:
            import win32api
            import win32con
            import win32event
            handles = [win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid) for pid in pids]
            for handle in handles:
                self.handles.callback(handle.Close)
            ended = lambda: all(win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0 for handle in handles)
        else:
            descriptors = [os.pidfd_open(pid) for pid in pids]
            for descriptor in descriptors:
                self.handles.callback(os.close, descriptor)
            ended = lambda: all(select.select([descriptor], [], [], 0)[0] for descriptor in descriptors)
        export, target, size, digest = self.request_pair(size=8 * 1024 * 1024 + 17)
        self.assertEqual(jobs.rpc("read", holding)["state"], "running", "The file job did not overlap the other pool member")
        started = time.monotonic()
        stopped = self.rpc("stop", export)[0]
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(jobs.rpc("read", holding)["state"], "running")
        export = self.rpc("resume", stopped)[0]
        jobs.rpc("stop", holding)
        self.wait(lambda: jobs.rpc("read", holding)["state"] == "cancelled")
        self.assertTrue(ended(), "Pool capacity was released before its original descendant handles terminated")
        self.relay(export, target)
        self.publish_and_cleanup(export, target, size, digest)

    def test_blocks_metadata_rechecks_captured_history_in_its_own_transaction(self):
        self.pin_race("blocks")

    def test_reserve_rechecks_captured_history_before_quota_or_filesystem_io(self):
        self.pin_race("reserve")

    def test_queue_rechecks_captured_history_before_creating_a_supervised_attempt(self):
        self.pin_race("queue")

    def test_helper_argv_freezes_history_and_rejects_new_database_before_file_io(self):
        # This owner-local metadata setup creates no OS program. It captures the
        # same immutable argv that the real worker sends across Popen.
        generation = str(uuid4())
        with jobs.transaction() as db:
            jobs.resource_policy.worker_start(db, generation, platform="windows" if jobs.WINDOWS else "linux")
            jobs.resource_policy.heartbeat(db, generation, observation={
                "observed_at": time.time(), "cpu_millis": 1000, "cpu_percent": None,
                "memory_total_bytes": 1024 ** 3, "memory_available_bytes": 1024 ** 3, "gpus": []})
            jobs.transfer_state.worker_seen(db, generation)
        endpoint = self.request_export()
        self.assertTrue(self.rpc("reserve", endpoint)[0]["reservation"]["reserved"])
        self.rpc("queue", endpoint)
        rows, failures = jobs.claim_jobs(generation, 1)
        self.assertEqual(failures, [])
        self.assertEqual(len(rows), 1)
        key = rows[0]["id"]
        with jobs.transaction() as db:
            argv = jobs.large_files.arguments(jobs.LOCAL_RUNTIME, db, key)
        self.assertEqual(argv[-2:], [key, self.state_id])
        changed = self.replace_history(endpoint)
        with patch.object(jobs.large_files, "_path", side_effect=AssertionError("Foreign history reached file I/O")), \
                patch.object(jobs.transfer_storage, "prepare_export") as prepare:
            with self.assertRaisesRegex(ValueError, "history"):
                jobs.large_files.worker(jobs.LOCAL_RUNTIME, key, argv[-1])
            prepare.assert_not_called()
        with jobs.transaction() as db:
            self.assertEqual(jobs.transfer_state.guard(db, endpoint["id"], endpoint["epoch"])["spec"]["expected_state_id"], changed)
            # The test invoked only the function above and never Popen; these
            # owner cleanup facts therefore describe a proven never-launch.
            jobs.large_files.finish_job(db, key, True, "failed", None)
            jobs.resource_policy.finish(db, key, generation, cleanup_confirmed=True)
            db.execute("UPDATE jobs SET state='failed',finished=? WHERE id=?", (time.time(), key))

    def test_stopping_a_queued_file_attempt_confirms_never_launch_and_can_resume(self):
        marker = self.home / "capacity-held"
        code = ("import time;from pathlib import Path;Path(" + repr(str(marker)) + ").touch();time.sleep(60)")
        jobs.operator(Namespace(command="register", name="capacity", argv=[sys.executable, "-c", code]))
        self.start_worker()
        holding = str(uuid4())
        jobs.rpc("start", holding, "capacity")
        self.wait(marker.exists)
        endpoint = self.request_export(size=1024 * 1024 + 7)
        self.assertTrue(self.rpc("reserve", endpoint)[0]["reservation"]["reserved"])
        for control in ("generic-job-stop", "endpoint-stop"):
            with self.subTest(control=control):
                self.rpc("queue", endpoint)
                queued = self.status(endpoint)
                key = queued["job"]["id"]
                self.assertEqual(queued["job"]["state"], "queued")
                self.assertFalse(queued["job"]["cleanup_confirmed"])
                with jobs.transaction() as db:
                    self.assertIsNone(db.execute("SELECT 1 FROM resource_claims WHERE id=?", (key,)).fetchone())
                if control == "generic-job-stop":
                    jobs.rpc("stop", key)
                else:
                    self.rpc("stop", endpoint)
                stopped = self.status(endpoint)
                self.assertEqual(stopped["state"], "stopped")
                self.assertGreater(stopped["epoch"], endpoint["epoch"])
                self.assertEqual(stopped["job"]["state"], "cancelled")
                self.assertTrue(stopped["job"]["cleanup_confirmed"])
                self.assertGreater(stopped["reserved_bytes"], 0)
                self.assertEqual(jobs.rpc("read", holding)["state"], "running")
                with self.assertRaises(ValueError):
                    self.rpc("queue", endpoint)
                endpoint = self.rpc("resume", stopped)[0]
        jobs.rpc("stop", holding)
        self.wait(lambda: jobs.rpc("read", holding)["state"] == "cancelled")
        self.rpc("queue", endpoint)
        key = self.complete_attempt(endpoint, "prepare")
        ready = self.status(endpoint)
        self.assertEqual(ready["state"], "ready")
        jobs.rpc("stop", key)  # A terminal job must not stop a later endpoint phase.
        self.assertEqual(self.status(endpoint), ready)
        self.assertEqual(self.rpc("cleanup", endpoint)[0]["state"], "cleaned")

    def test_scoped_never_launch_reconciliation_finishes_orphan_attempt_without_releasing_disk(self):
        generation = str(uuid4())
        with jobs.transaction() as db:
            jobs.resource_policy.worker_start(db, generation, platform="windows" if jobs.WINDOWS else "linux")
            jobs.resource_policy.heartbeat(db, generation, observation={
                "observed_at": time.time(), "cpu_millis": 1000, "cpu_percent": None,
                "memory_total_bytes": 1024 ** 3, "memory_available_bytes": 1024 ** 3, "gpus": []})
            jobs.transfer_state.worker_seen(db, generation)
        endpoint = self.request_export(size=1024 * 1024 + 13)
        self.assertTrue(self.rpc("reserve", endpoint)[0]["reservation"]["reserved"])
        self.rpc("queue", endpoint)
        rows, failures = jobs.claim_jobs(generation, 1)
        self.assertEqual(failures, [])
        self.assertEqual(len(rows), 1)
        key = rows[0]["id"]
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT launch_possible FROM worker_launches WHERE id=?", (key,)).fetchone()[0], 0)
            attempt = db.execute("SELECT finished,cleanup_confirmed FROM large_file_attempts WHERE id=?", (key,)).fetchone()
            self.assertEqual(tuple(attempt), (None, 0))
            self.assertEqual(len(jobs.resource_policy.status(db)["claims"]), 1)
        # No Popen was ever attempted. Reconciliation must validate that scoped
        # ledger fact, then finish both resource and endpoint cleanup atomically.
        with patch.object(jobs.subprocess, "Popen", side_effect=AssertionError("Recovery must not relaunch an orphan")):
            jobs.reconcile_cleanup(set())
        stopped = self.status(endpoint)
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["job"]["state"], "unconfirmed")
        self.assertIsNone(stopped["job"]["exit_code"])
        self.assertTrue(stopped["job"]["cleanup_confirmed"])
        self.assertGreater(stopped["reserved_bytes"], 0)
        with jobs.transaction() as db:
            self.assertEqual(jobs.resource_policy.status(db)["claims"], [])
            attempt = db.execute("SELECT finished,cleanup_confirmed,outcome FROM large_file_attempts WHERE id=?", (key,)).fetchone()
            self.assertIsNotNone(attempt[0])
            self.assertEqual(tuple(attempt[1:]), (1, "unconfirmed"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_claims WHERE released IS NULL").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_jobs").fetchone()[0], 1)
        self.assertEqual(jobs.claim_jobs(generation, 1), ([], []))
        with self.assertRaises(ValueError):
            self.rpc("queue", endpoint)
        resumed = self.rpc("resume", stopped)[0]
        self.assertGreater(resumed["epoch"], stopped["epoch"])
        self.rpc("queue", resumed)
        replacement = self.status(resumed)["job"]["id"]
        self.assertNotEqual(replacement, key)
        jobs.rpc("stop", replacement)  # Explicitly cancel the new, never-claimed test attempt.
        self.assertEqual(self.rpc("cleanup", self.status(resumed))[0]["state"], "cleaned")


if __name__ == "__main__":
    unittest.main()
