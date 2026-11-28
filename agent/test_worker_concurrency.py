# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real owner-local pool/crash tests, confined to fresh temporary storage."""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


ZERO = {"cpu_millis": 0, "memory_bytes": 0, "gpus": []}


@unittest.skipUnless(sys.platform in {"linux", "win32"}, "Qualified native worker platforms")
class WorkerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-worker-pool-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "jobs"
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        self.workers, self.streams, self.handles = [], [], ExitStack()
        self.addCleanup(self.handles.close)
        self.addCleanup(self.cleanup)
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")
            jobs.direct_execution.set_enabled(db, True)
        self.fixture = self.home / "program.py"
        self.fixture.write_text(
            "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
            "marker,gate=map(Path,sys.argv[1:3])\n"
            "with marker.with_suffix('.history').open('a') as h: h.write('started\\n')\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(25)'],"
            "start_new_session=(sys.platform=='linux' and sys.argv[3]=='detached'))\n"
            "stage=marker.with_suffix('.tmp')\n"
            "stage.write_text(json.dumps([os.getpid(),child.pid]));stage.replace(marker)\n"
            "print('pool-fixture-start',flush=True)\n"
            "deadline=time.monotonic()+25\n"
            "while not gate.exists() and time.monotonic()<deadline:time.sleep(.025)\n"
            "sys.exit(7)\n", encoding="utf-8")

    def cleanup(self):
        for worker in self.workers:
            if worker.poll() is None:
                worker.terminate()  # Original Popen handle; never a saved PID.
                worker.wait(timeout=10)
        for stream in self.streams:
            stream.close()

    def policy(self, **overrides):
        with jobs.transaction() as db:
            value = dict(jobs.resource_policy.DEFAULT_POLICY, **overrides)
            jobs.resource_policy.set_policy(db, value)

    def start_worker(self, slow_marker=None, slow_gate=None):
        code = ("import runpy,time\nfrom pathlib import Path\nfrom contextlib import contextmanager\n"
                "m=runpy.run_path(" + repr(str(self.root / "jobs.py")) + ",run_name='owned_worker_fixture')\n"
                "g=m['work'].__globals__\ng['ROOT']=Path(" + repr(str(self.root)) + ")\n"
                "g['DATABASE']=g['ROOT']/'jobs.sqlite3'\n")
        if slow_marker is not None:
            code += ("original=g['program_process']\n@contextmanager\ndef slow(argv,*a,**kw):\n"
                     " if " + repr(str(slow_marker)) + " in str(argv):\n"
                     "  Path(" + repr(str(slow_marker)) + ").touch()\n"
                     "  while not Path(" + repr(str(slow_gate)) + ").exists():time.sleep(.025)\n"
                     " with original(argv,*a,**kw) as p:yield p\ng['program_process']=slow\n")
        code += "m['work']()\n"
        stream = (self.home / ("worker-" + uuid4().hex + ".log")).open("wb")
        self.streams.append(stream)
        worker = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], cwd=self.root,
                                  stdout=stream, stderr=stream)
        self.workers.append(worker)
        return worker

    def wait(self, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.025)
        for stream in self.streams:
            stream.flush()
        diagnostics = "\n".join(path.read_text(errors="replace") for path in self.home.glob("worker-*.log"))
        self.fail("Isolated worker did not reach expected state.\n" + diagnostics[-12000:])

    def worker_ready(self):
        with jobs.transaction() as db:
            worker = jobs.resource_policy.status(db)["worker"]
            return worker and worker["fresh"] and worker["observation_fresh"]

    def registered(self, name, detached=False):
        marker, gate = self.home / (name + ".json"), self.home / (name + ".done")
        argv = [sys.executable, str(self.fixture), str(marker), str(gate), "detached" if detached else "normal"]
        jobs.operator(Namespace(command="register", name=name, argv=argv))
        key = str(uuid4())
        jobs.rpc("start", key, name)
        return key, marker, gate

    def stopped_handles(self, pids):
        if jobs.WINDOWS:
            import win32api
            import win32con
            import win32event
            handles = [win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid) for pid in pids]
            for handle in handles:
                self.handles.callback(handle.Close)
            return lambda: all(win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0 for handle in handles)
        descriptors = [os.pidfd_open(pid) for pid in pids]
        for descriptor in descriptors:
            self.handles.callback(os.close, descriptor)
        return lambda: all(select.select([descriptor], [], [], 0)[0] for descriptor in descriptors)

    def test_default_policy_preserves_serial_legacy_execution(self):
        first, a, gate = self.registered("serial_a")
        second, b, other = self.registered("serial_b")
        self.start_worker()
        self.wait(a.exists)
        self.assertFalse(b.exists())
        self.assertEqual(jobs.rpc("read", second)["state"], "queued")
        gate.touch()
        self.wait(b.exists)
        other.touch()
        self.wait(lambda: jobs.rpc("read", second)["state"] == "completed")
        self.assertEqual(jobs.rpc("read", first)["exit_code"], 7)

    def test_two_real_process_trees_overlap_and_third_waits(self):
        self.policy(max_jobs=2, legacy_defaults=ZERO)
        first, a, gate_a = self.registered("parallel_a")
        second, b, gate_b = self.registered("parallel_b")
        third, c, gate_c = self.registered("parallel_c")
        self.start_worker()
        self.wait(lambda: a.exists() and b.exists())
        self.assertFalse(c.exists())
        with jobs.transaction() as db:
            claims = jobs.resource_policy.status(db)["claims"]
        self.assertEqual({item["job_id"] for item in claims}, {first, second})
        gate_a.touch()
        self.wait(c.exists)
        gate_b.touch(); gate_c.touch()
        self.wait(lambda: all(jobs.rpc("read", key)["state"] == "completed" for key in (first, second, third)))

    def test_declared_quota_waits_for_confirmed_cleanup(self):
        self.policy(max_jobs=2, cpu_millis=1000, legacy_defaults=dict(ZERO, cpu_millis=1000))
        first, a, gate_a = self.registered("budget_a")
        second, b, gate_b = self.registered("budget_b")
        self.start_worker()
        self.wait(a.exists)
        stopped = self.stopped_handles(json.loads(a.read_text()))
        self.assertFalse(b.exists())
        self.assertEqual(jobs.rpc("read", second)["state"], "queued")
        gate_a.touch()
        self.wait(b.exists)
        self.assertTrue(stopped(), "Quota was reused before the old process tree exited")
        self.assertEqual(jobs.rpc("read", first)["state"], "completed")
        gate_b.touch()
        self.wait(lambda: jobs.rpc("read", second)["state"] == "completed")

    def test_stop_and_heartbeat_are_not_blocked_by_another_launch_io(self):
        self.policy(max_jobs=2, legacy_defaults=ZERO)
        first, a, gate_a = self.registered("slow_a")
        second, b, gate_b = self.registered("fast_b")
        release = self.home / "release-launch"
        # Delay only the first job's launch I/O after claim, outside all SQLite transactions.
        # Its argv contains the marker; the wrapper creates a distinct entered marker.
        code_marker = a
        self.start_worker(slow_marker=code_marker, slow_gate=release)
        self.wait(lambda: code_marker.exists() and b.exists())
        start = time.monotonic()
        jobs.rpc("stop", second)
        self.assertLess(time.monotonic() - start, .75)
        self.wait(lambda: jobs.rpc("read", second)["state"] == "cancelled")
        self.assertTrue(self.worker_ready())
        release.touch()
        gate_a.touch()
        self.wait(lambda: jobs.rpc("read", first)["state"] == "completed")

    def test_crash_two_guardians_reconciles_only_scoped_receipts_and_never_relaunches(self):
        self.policy(max_jobs=2, legacy_defaults=ZERO)
        first, a, gate_a = self.registered("crash_a", detached=True)
        second, b, gate_b = self.registered("crash_b", detached=True)
        worker = self.start_worker()
        self.wait(lambda: a.exists() and b.exists())
        stopped = self.stopped_handles(json.loads(a.read_text()) + json.loads(b.read_text()))
        with jobs.transaction() as db:
            original_generation = jobs.resource_policy.status(db)["worker"]["generation"]
        worker.kill()  # The original owner Popen handle, not a reconstructed PID.
        worker.wait(timeout=5)
        replacement = self.start_worker()
        deadline = time.monotonic() + 12
        while True:
            if replacement.poll() is not None:
                replacement = self.start_worker()  # Linux guardians still share the original flock.
            with jobs.transaction() as db:
                status = jobs.resource_policy.status(db)
            if (status["worker"] and status["worker"]["generation"] != original_generation and not status["claims"]):
                break
            if time.monotonic() > deadline:
                self.fail("Original guardians did not produce trusted cleanup receipts")
            time.sleep(.05)
        self.assertTrue(stopped())
        for key, marker, name in ((first, a, "crash_a"), (second, b, "crash_b")):
            self.assertEqual(jobs.rpc("read", key)["state"], "unconfirmed")
            self.assertEqual(jobs.rpc("start", key, name)["state"], "unconfirmed")
            self.assertEqual(marker.with_suffix(".history").read_text().splitlines(), ["started"])

    def test_native_v2_resource_row_is_required_and_impossible_request_fails_without_launch(self):
        self.policy(max_jobs=2, cpu_millis=1000, legacy_defaults=ZERO)
        self.start_worker(); self.wait(self.worker_ready)
        marker = self.home / "must-not-launch"
        with self.assertRaisesRegex(ValueError, "exceeds_total"):
            jobs.dispatch_process("execute", 2, job_id=str(uuid4()),
                argv=[sys.executable, "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()"],
                cwd=str(self.home), resources=dict(ZERO, cpu_millis=2000))
        self.assertFalse(marker.exists())
        # Keep the pool occupied by an exclusive legacy job while queueing a
        # valid v2 row, then corrupt only that row's resource header atomically.
        self.policy(max_jobs=2, legacy_defaults=None)
        _, blocker, release = self.registered("header_blocker")
        self.wait(blocker.exists)
        key = str(uuid4())
        with jobs.transaction() as db:
            jobs.direct_execution.queue(db, "full", key, [sys.executable, "-c", "raise SystemExit(0)"],
                                        cwd=str(self.home), resources=ZERO, protocol_version=2)
            db.execute("DELETE FROM resource_requests WHERE id=?", (key,))
        release.touch()
        self.wait(lambda: jobs.rpc("read", key)["state"] == "failed")
        self.assertIn("immutable resource request", jobs.rpc("read", key)["stderr"]["text"])

    def test_registered_native_v2_and_external_capability_share_one_pool(self):
        self.policy(max_jobs=2, legacy_defaults=ZERO)
        self.start_worker(); self.wait(self.worker_ready)
        first, a, gate_a = self.registered("mixed_legacy")
        b, gate_b = self.home / "mixed-native.json", self.home / "mixed-native.done"
        native = jobs.dispatch_process("execute", 2, job_id=str(uuid4()),
            argv=[sys.executable, str(self.fixture), str(b), str(gate_b), "normal"],
            cwd=str(self.home), resources=ZERO)
        c, gate_c = self.home / "mixed-capability.json", self.home / "mixed-capability.done"
        adapter = self.home / "external.py"
        adapter.write_text("import subprocess,sys\ndef run(inputs,parameters,workspace):\n"
            " p=subprocess.run([sys.executable," + repr(str(self.fixture)) + "," + repr(str(c)) + ","
            + repr(str(gate_c)) + ",'normal'])\n return {'native_code':p.returncode}\n", encoding="utf-8")
        with jobs.transaction() as db:
            jobs.capabilities.register(db, jobs.files, {"id": "qa.pool", "version": "1",
                "python": sys.executable, "adapter": str(adapter), "max_files": 0})
        provider = jobs.dispatch("capability", operation="describe", capability_id="qa.pool")
        third = jobs.dispatch("capability", operation="queue", capability_id="qa.pool", job_id=str(uuid4()),
            revision=provider["revision"], inputs=[], parameters={})
        self.wait(lambda: a.exists() and b.exists())
        self.assertFalse(c.exists())
        self.assertEqual(jobs.rpc("read", third["id"])["state"], "queued")
        gate_a.touch()
        self.wait(c.exists)
        gate_b.touch(); gate_c.touch()
        self.wait(lambda: all(jobs.rpc("read", key)["state"] == "completed"
                              for key in (first, native["id"], third["id"])))
        self.assertEqual(jobs.rpc("read", native["id"])["exit_code"], 7)
        self.assertEqual(jobs.rpc("read", third["id"])["exit_code"], 0)

    def test_graceful_shutdown_drains_owned_jobs_without_claiming_more(self):
        self.policy(max_jobs=2, legacy_defaults=ZERO)
        first, a, gate_a = self.registered("shutdown_a")
        second, b, gate_b = self.registered("shutdown_b")
        third, c, gate_c = self.registered("shutdown_c")
        worker = self.start_worker()
        self.wait(lambda: a.exists() and b.exists())
        (self.root / "worker.stop").touch()
        gate_a.touch(); gate_b.touch()
        worker.wait(timeout=10)
        self.assertEqual(worker.returncode, 0)
        self.assertFalse(c.exists())
        self.assertEqual(jobs.rpc("read", third)["state"], "queued")
        with jobs.transaction() as db:
            status = jobs.resource_policy.status(db)
            self.assertEqual(status["claims"], [])
            self.assertFalse(status["worker"]["fresh"])
            self.assertTrue(status["worker"]["stopped"])

    def test_restart_and_untrusted_receipts_cannot_release_uncertain_capacity(self):
        generation = str(uuid4())
        key, marker, gate = self.registered("uncertain_owner")
        with jobs.transaction() as db:
            jobs.resource_policy.worker_start(db, generation, platform="windows" if jobs.WINDOWS else "linux")
        rows, failures = jobs.claim_jobs(generation, 1)
        self.assertEqual(failures, [])
        self.assertEqual([row["id"] for row in rows], [key])
        with jobs.transaction() as db:
            nonce = jobs.resource_policy.claim_nonce(db, key, generation)
            db.execute("UPDATE worker_launches SET launch_possible=1 WHERE id=?", (key,))
            jobs.resource_policy.worker_start(db, str(uuid4()), platform="windows" if jobs.WINDOWS else "linux")
        claim = {"job_id": key, "generation": generation, "cleanup_nonce": nonce}
        jobs.reconcile_cleanup(set())  # A new owner or an expired heartbeat is not cleanup proof.
        with jobs.transaction() as db:
            self.assertEqual(len(jobs.resource_policy.status(db)["claims"]), 1)
        path = jobs.cleanup_path(claim, create=True)
        for malformed in [dict(jobs.claim_document(claim), cleanup_nonce=str(uuid4())),
                          dict(jobs.claim_document(claim), protocol_version=2.0),
                          dict(jobs.claim_document(claim), cleanup_confirmed=1)]:
            # Create only new synthetic test files; the real reader never repairs ACLs.
            jobs.publish_private_packet(path, malformed)
            jobs.reconcile_cleanup(set())
            with jobs.transaction() as db:
                self.assertEqual(len(jobs.resource_policy.status(db)["claims"]), 1)
            path.unlink()
        jobs.write_cleanup(claim)
        jobs.reconcile_cleanup(set())
        with jobs.transaction() as db:
            self.assertEqual(jobs.resource_policy.status(db)["claims"], [])
        self.assertFalse(marker.exists())
        self.assertEqual(jobs.rpc("read", key)["state"], "unconfirmed")

    def test_prelaunch_pipe_failure_releases_only_the_never_started_claim(self):
        generation = str(uuid4())
        key, marker, gate = self.registered("pipe_allocation_failure")
        with jobs.transaction() as db:
            jobs.resource_policy.worker_start(db, generation, platform="windows" if jobs.WINDOWS else "linux")
        rows, failures = jobs.claim_jobs(generation, 1)
        self.assertEqual(failures, [])
        with patch.object(jobs.os, "pipe", side_effect=OSError("synthetic pipe allocation failure")), \
                patch.object(jobs.subprocess, "Popen") as create:
            jobs.run_one(rows[0], generation=generation)
            create.assert_not_called()
        self.assertEqual(jobs.rpc("read", key)["state"], "failed")
        self.assertFalse(marker.exists())
        with jobs.transaction() as db:
            self.assertEqual(jobs.resource_policy.status(db)["claims"], [])

    def test_submit_thread_start_failure_keeps_queued_callable_claim_until_drain(self):
        self.policy(max_jobs=3, legacy_defaults=ZERO)
        keys = [self.registered("submit_" + name)[0] for name in ("first", "uncertain", "unsent")]
        generation = str(uuid4())
        with jobs.transaction() as db:
            jobs.resource_policy.worker_start(db, generation, platform="windows" if jobs.WINDOWS else "linux")
        rows, failures = jobs.claim_jobs(generation, 3)
        self.assertEqual(failures, [])
        self.assertEqual([row["id"] for row in rows], keys)
        entered, release, uncertain_called = Event(), Event(), Event()
        original = jobs.run_one

        def controlled(row, *args):
            if row["id"] == keys[0]:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("The synthetic pool blocker was not released")
            else:
                uncertain_called.set()
                original(row, *args)

        active = {}
        pool = ThreadPoolExecutor(max_workers=2)
        with patch.object(jobs, "run_one", side_effect=controlled), patch.object(jobs.subprocess, "Popen") as create:
            try:
                jobs.submit_claimed(pool, rows[:1], active, None, generation)
                self.assertTrue(entered.wait(2))
                # Actual CPython submit enqueues the WorkItem, then attempts to
                # start thread two. The existing thread will later consume it.
                with patch.object(Thread, "start", side_effect=RuntimeError("synthetic thread creation failure")):
                    with self.assertRaisesRegex(RuntimeError, "synthetic thread creation failure"):
                        jobs.submit_claimed(pool, rows[1:], active, None, generation)
                self.assertEqual(set(active), {keys[0]})  # No future for item two.
                self.assertFalse(uncertain_called.is_set())
                with jobs.transaction() as db:
                    self.assertEqual({item["job_id"] for item in jobs.resource_policy.status(db)["claims"]}, set(keys))
                    self.assertEqual([jobs.job(db, key)["state"] for key in keys], ["running", "stopping", "stopping"])
                    self.assertEqual(db.execute("SELECT SUM(launch_possible) FROM worker_launches").fetchone()[0], 0)
            finally:
                release.set()
                pool.shutdown(wait=True)
            self.assertTrue(uncertain_called.is_set(), "The WorkItem really remained in the executor queue")
            create.assert_not_called()
        self.assertEqual(jobs.rpc("read", keys[1])["state"], "cancelled")
        # This is safe only now: no queued callable can pass a future launch gate.
        jobs.reconcile_cleanup(set())
        with jobs.transaction() as db:
            self.assertEqual(jobs.resource_policy.status(db)["claims"], [])

    def test_targeted_v2_read_binds_database_and_manifest_in_one_transaction(self):
        generation = str(uuid4())
        with jobs.transaction() as db:
            db.execute("INSERT OR REPLACE INTO worker_environment VALUES(1,?,?)",
                       (os.environ.get("PATH", ""), os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")))
            jobs.resource_policy.worker_start(db, generation, platform="windows" if jobs.WINDOWS else "linux")
            jobs.resource_policy.heartbeat(db, generation, observation={
                "observed_at": time.time(), "cpu_millis": 1000, "cpu_percent": None,
                "memory_total_bytes": 1024 ** 3, "memory_available_bytes": 1024 ** 3, "gpus": []})
        request = jobs.dispatch_process("execute", 2, job_id=str(uuid4()),
            argv=[sys.executable, "-c", "pass"], cwd=str(self.home), resources=ZERO)
        legacy = jobs.dispatch_process("execute", 1, job_id=str(uuid4()),
            argv=[sys.executable, "-c", "pass"], cwd=str(self.home))
        key, expected = request["id"], request["manifest"]["destination_state_id"]
        original_read = jobs.direct_execution.read

        def checked_read(db, job_id):
            self.assertTrue(db.in_transaction)
            self.assertEqual(jobs.resource_policy.state_id(db), expected)
            return original_read(db, job_id)

        with patch.object(jobs.resource_policy, "status", side_effect=AssertionError("Read must not require resource freshness")), \
                patch.object(jobs.direct_execution, "read", side_effect=checked_read):
            result = jobs.dispatch_process("read", 2, job_id=key, expected_state_id=expected)
        self.assertEqual(result["manifest"], request["manifest"])
        self.assertEqual(jobs.dispatch_process("read", 2, job_id=key)["manifest"], request["manifest"])
        self.assertEqual(jobs.dispatch_process("read", 1, job_id=legacy["id"])["manifest"], legacy["manifest"])
        with patch.object(jobs.direct_execution, "read", side_effect=AssertionError("Reject changed durable state first")):
            with self.assertRaises(ValueError):
                jobs.dispatch_process("read", 2, job_id=key, expected_state_id=str(uuid4()))
        with self.assertRaises(ValueError):
            jobs.dispatch_process("read", 1, job_id=legacy["id"], expected_state_id=expected)
        with self.assertRaises(ValueError):
            jobs.dispatch_process("read", 2, job_id=legacy["id"], expected_state_id=expected)
        with self.assertRaises(ValueError):
            jobs.dispatch_process("read", 2, job_id=key, expected_state_id=expected, extra=True)
        # A valid immutable manifest from a different durable state must fail
        # even when the receiver's current identity matches the caller's pin.
        with jobs.transaction() as db:
            db.execute("UPDATE resource_identity SET state_id=? WHERE id=1", (str(uuid4()),))
            foreign_key = str(uuid4())
            jobs.direct_execution.queue(db, "full", foreign_key, [sys.executable, "-c", "pass"],
                                        cwd=str(self.home), resources=ZERO, protocol_version=2)
            db.execute("UPDATE resource_identity SET state_id=? WHERE id=1", (expected,))
        with self.assertRaisesRegex(ValueError, "immutable destination state"):
            jobs.dispatch_process("read", 2, job_id=foreign_key, expected_state_id=expected)

    def test_compatible_job_beyond_first_busy_queue_page_can_run(self):
        self.policy(max_jobs=2, cpu_millis=1000, legacy_defaults=dict(ZERO, cpu_millis=1000))
        _, a, gate = self.registered("page_blocker")
        self.start_worker(); self.wait(lambda: a.exists() and self.worker_ready())
        queued = []
        marker = self.home / "later-compatible-job"
        with jobs.transaction() as db:
            for _ in range(70):
                key = str(uuid4()); queued.append(key)
                jobs.direct_execution.queue(db, "full", key, [sys.executable, "-c", "raise SystemExit(0)"],
                    cwd=str(self.home), resources=dict(ZERO, cpu_millis=1000), protocol_version=2)
            later = str(uuid4())
            jobs.direct_execution.queue(db, "full", later,
                [sys.executable, "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()"],
                cwd=str(self.home), resources=ZERO, protocol_version=2)
        self.wait(marker.exists)
        self.wait(lambda: jobs.rpc("read", later)["state"] == "completed")
        self.assertTrue(all(jobs.rpc("read", key)["state"] == "queued" for key in queued))

    def test_targeted_v2_stop_binds_database_and_manifest_without_metrics(self):
        self.start_worker(); self.wait(self.worker_ready)
        blocker, marker, release = self.registered("targeted_stop_blocker")
        self.wait(marker.exists)
        request = jobs.dispatch_process("execute", 2, job_id=str(uuid4()),
            argv=[sys.executable, "-c", "raise SystemExit(0)"], cwd=str(self.home), resources=ZERO)
        key, manifest = request["id"], request["manifest"]
        payload = {"expected_state_id": manifest["destination_state_id"], "expected_sha256": manifest["sha256"]}
        for changed in ({"expected_state_id": str(uuid4())}, {"expected_sha256": "f" * 64}):
            with self.assertRaises(ValueError):
                jobs.dispatch_process("stop", 2, job_id=key, **(payload | changed))
            self.assertEqual(jobs.rpc("read", key)["state"], "queued")
        with self.assertRaises(ValueError):
            jobs.dispatch_process("stop", 1, job_id=key, **payload)
        with patch.object(jobs.resource_policy, "status", side_effect=AssertionError("Stop must not require resource freshness")):
            stopped = jobs.dispatch_process("stop", 2, job_id=key, **payload)
        self.assertEqual(stopped["state"], "cancelled")
        release.touch()
        self.wait(lambda: jobs.rpc("read", blocker)["state"] == "completed")


if __name__ == "__main__":
    unittest.main()
