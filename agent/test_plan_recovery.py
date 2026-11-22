# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock
import unittest
from unittest.mock import patch
from uuid import uuid4

import batches
import jobs
import node_registry
import server


def manifest():
    key = uuid4()
    request = [{"program": "shared", "node_id": "windows"}]
    return {"version": 1, "kind": "batch", "id": str(key), "origin": "ubuntu", "request": request,
            "plan": batches.make_plan(key, "ubuntu", request, {})}


class PlanJournalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(jobs.plan_journal.SCHEMA)

    def test_immutable_and_idempotent_storage(self):
        original = manifest()
        first = jobs.plan_journal.store(self.db, original)
        self.assertEqual(first, jobs.plan_journal.store(self.db, deepcopy(original)))
        changed = deepcopy(original)
        changed["origin"] = "kali"
        self.assertEqual(jobs.plan_journal.store(self.db, changed)["state"], "conflict")
        record = jobs.plan_journal.get(self.db, "batch", original["id"])
        self.assertEqual(record["manifest"], original)
        self.assertEqual(record["sha256"], first["sha256"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM plan_journal").fetchone()[0], 1)

    def test_validation_digest_and_payload_limits(self):
        original = manifest()
        variants = [dict(original, version=True), dict(original, kind="capability"), dict(original, id="bad"),
                    dict(original, plan=[]), dict(original, extra="field")]
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                jobs.plan_journal.store(self.db, invalid)
        with patch.object(jobs.plan_journal, "MAX_BYTES", 100), self.assertRaisesRegex(ValueError, "256 KiB"):
            jobs.plan_journal.package(original)
        record = jobs.plan_journal.package(original)
        record["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            jobs.plan_journal.unpack(record)

    def test_corrupt_stored_record_is_reported_as_invalid(self):
        original = manifest()
        jobs.plan_journal.store(self.db, original)
        self.db.execute("UPDATE plan_journal SET sha256=?", ("0" * 64,))
        self.assertEqual(jobs.plan_journal.get(self.db, "batch", original["id"]), {"state": "invalid"})
        self.assertEqual(jobs.plan_journal.store(self.db, original), {"state": "invalid"})

    def test_concurrent_writers_keep_one_immutable_manifest(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "journal.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript(jobs.plan_journal.SCHEMA)
            first = manifest()
            second = dict(first, origin="kali")

            def save(body):
                with closing(sqlite3.connect(database)) as db, db:
                    return jobs.plan_journal.store(db, body)

            with ThreadPoolExecutor() as pool:
                receipts = list(pool.map(save, [first, second]))
            self.assertEqual(sorted(row["state"] for row in receipts), ["conflict", "stored"])
            self.assertEqual(receipts[0]["sha256"], receipts[1]["sha256"])


class WorkerNetwork:
    """Separate SQLite journals and durable job records behind the SSH dispatcher boundary."""

    def __init__(self, root, nodes):
        self.root, self.nodes = root, nodes
        self.offline, self.lost_replies, self.lost_starts = set(), set(), set()
        self.calls, self.jobs = [], {node: {} for node in nodes}
        self.created = 0
        self.lock = Lock()
        for node in nodes:
            with closing(sqlite3.connect(root / (node + ".sqlite3"))) as db, db:
                db.executescript(jobs.plan_journal.SCHEMA)

    def __call__(self, node, request):
        with self.lock:
            self.calls.append((node, deepcopy(request)))
        if node in self.offline:
            raise server.ToolError("Job service unavailable.")
        if request["action"] == "plan":
            fields = {key: value for key, value in request.items() if key != "action"}
            with closing(sqlite3.connect(self.root / (node + ".sqlite3"))) as db, db:
                result = jobs.plan_journal.dispatch(db, **fields)
            if request["operation"] == "store" and node in self.lost_replies:
                self.lost_replies.remove(node)
                raise server.ToolError("Replica acknowledgment lost.")
            return result
        if request["action"] != "batch":
            raise AssertionError(request)
        replies = []
        with self.lock:
            for item in request["items"]:
                key = item["job_id"]
                if request["operation"] == "start" and key not in self.jobs[node]:
                    self.created += 1
                    self.jobs[node][key] = dict(item, id=key, state="pending", exit_code=None,
                                               created=self.created, finished=None)
                    self.jobs[node][key].pop("job_id")
                saved = self.jobs[node].get(key)
                replies.append({"job": deepcopy(saved)} if saved else {"error": "Unknown job ID on this node."})
        if request["operation"] == "start" and node in self.lost_starts:
            self.lost_starts.remove(node)
            raise server.ToolError("Job acknowledgment lost.")
        return replies


class BatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin, self.windows, self.kali = (str(uuid4()) for _ in range(3))
        self.nodes = [self.origin, self.windows, self.kali]
        state = self.root / "origin"
        state.mkdir()
        changed = patch.multiple(jobs, ROOT=state, DATABASE=state / "jobs.sqlite3")
        changed.start()
        self.addCleanup(changed.stop)
        jobs.initialize()
        self.network = WorkerNetwork(self.root, self.nodes)
        destination = patch.object(server, "destination_request", side_effect=self.network)
        destination.start()
        self.addCleanup(destination.stop)
        local = patch.object(server, "local_node", return_value=self.origin)
        local.start()
        self.addCleanup(local.stop)
        topology = node_registry.Registry(self.origin, [
            node_registry.Node(node, node, "linux", route=None if node == self.origin else node_registry.Route("peer", ()))
            for node in self.nodes
        ])
        registry = patch.object(server, "registry", return_value=topology)
        registry.start()
        self.addCleanup(registry.stop)
        self.key = uuid4()
        self.tasks = [server.BatchTaskRequest(program="shared", node_id=node)
                      for node in (self.windows, self.kali)]

    def submit(self):
        return asyncio.run(server.submit_batch(self.key, self.tasks))

    def recover(self):
        root = self.root / "reader"
        root.mkdir(exist_ok=True)
        with patch.multiple(jobs, ROOT=root, DATABASE=root / "jobs.sqlite3"), \
                patch.object(server, "local_node", return_value=self.windows):
            jobs.initialize()
            return asyncio.run(server.get_batch(self.key))

    def test_read_from_another_origin_preserves_jobs_and_pending_approval(self):
        first = self.submit()
        original_jobs = deepcopy(self.network.jobs)
        self.assertTrue(all(task.job.state == "pending" for task in first.tasks))
        self.network.offline.add(self.origin)
        self.network.calls.clear()
        recovered = self.recover()
        self.assertEqual(recovered.origin, self.origin)
        self.assertEqual(recovered.model_dump(), first.model_dump())
        self.assertEqual(self.network.jobs, original_jobs)
        self.assertTrue(all(request["operation"] in {"get", "status"} for _, request in self.network.calls))
        for node in (self.windows, self.kali):
            for job in self.network.jobs[node].values():
                job.update(state="completed", exit_code=0, finished=10)
        completed = self.recover()
        self.assertEqual([task.job.created for task in completed.tasks], [task.job.created for task in first.tasks])
        self.assertTrue(all(task.job.state == "completed" and task.job.exit_code == 0 for task in completed.tasks))
        self.assertEqual(self.network.created, 2)

    def test_plan_is_replicated_before_each_job_start(self):
        self.submit()
        for node in (self.windows, self.kali):
            operations = [request["operation"] for peer, request in self.network.calls if peer == node]
            self.assertEqual(operations, ["store", "start"])

    def test_lost_replica_reply_and_submission_retry_create_each_job_once(self):
        self.network.lost_replies.add(self.windows)
        first = self.submit()
        self.assertIsNone(first.tasks[0].job)
        self.assertIn("acknowledgment lost", first.tasks[0].error)
        self.assertEqual(self.network.created, 1)
        second = self.submit()
        third = self.submit()
        self.assertEqual(second.model_dump(), third.model_dump())
        self.assertEqual(second.tasks[1].job.created, first.tasks[1].job.created)
        self.assertEqual(self.network.created, 2)

    def test_offline_destination_does_not_start_without_its_replica(self):
        self.network.offline.add(self.kali)
        result = self.submit()
        self.assertEqual(result.tasks[0].job.state, "pending")
        self.assertIsNone(result.tasks[1].job)
        self.assertIn("unavailable", result.tasks[1].error)
        self.assertFalse(any(node == self.kali and request["operation"] == "start"
                             for node, request in self.network.calls))

    def test_lost_start_reply_is_recovered_without_another_start(self):
        self.network.lost_starts.add(self.windows)
        first = self.submit()
        self.assertIsNone(first.tasks[0].job)
        self.assertIn("Job acknowledgment lost", first.tasks[0].error)
        self.network.offline.add(self.origin)
        self.network.calls.clear()
        recovered = self.recover()
        self.assertEqual([task.job_id for task in recovered.tasks], [task.job_id for task in first.tasks])
        self.assertTrue(all(task.job.state == "pending" for task in recovered.tasks))
        self.assertEqual(self.network.created, 2)
        self.assertTrue(all(request["operation"] in {"get", "status"} for _, request in self.network.calls))

    def test_conflicting_manifest_blocks_start_and_recovery(self):
        requested = [task.model_dump() for task in self.tasks]
        plan = batches.make_plan(self.key, self.kali, requested, {})
        alternate = {"version": 1, "kind": "batch", "id": str(self.key), "origin": self.kali,
                     "request": requested, "plan": plan}
        self.network(self.kali, {"action": "plan", "operation": "store", "manifest": alternate})
        self.network.calls.clear()
        with self.assertRaisesRegex(server.ToolError, "conflicting"):
            self.submit()
        self.assertEqual(self.network.created, 0)
        self.network.offline.add(self.origin)
        self.network.calls.clear()
        with self.assertRaisesRegex(server.ToolError, "Conflicting batch"):
            self.recover()
        self.assertTrue(all(request["operation"] == "get" for _, request in self.network.calls))

    def test_publication_without_start_is_recoverable_without_starting_jobs(self):
        request = [task.model_dump() for task in self.tasks]
        plan = batches.make_plan(self.key, self.origin, request, {})
        batches.save(self.key, request, plan)
        body = batches.manifest(self.key, self.origin)
        asyncio.run(server.publish_plan(body))
        self.network.offline.add(self.origin)
        recovered = self.recover()
        self.assertEqual([str(task.job_id) for task in recovered.tasks], [task["job_id"] for task in plan])
        self.assertTrue(all(task.job is None and "Unknown job ID" in task.error for task in recovered.tasks))
        self.assertEqual(self.network.created, 0)

    def test_legacy_local_plan_is_readable_without_journal_migration(self):
        request = [task.model_dump() for task in self.tasks]
        plan = batches.make_plan(self.key, self.origin, request, {})
        batches.save(self.key, request, plan)
        for node in (self.windows, self.kali):
            items = [{"job_id": task["job_id"], "program": task["program"]}
                     for task in plan if task["node_id"] == node]
            self.network(node, {"action": "batch", "operation": "start", "items": items})
        self.network.calls.clear()
        result = asyncio.run(server.get_batch(self.key))
        self.assertTrue(all(task.job is not None for task in result.tasks))
        self.assertTrue(all(request["operation"] == "status" for _, request in self.network.calls))
        with jobs.transaction() as db:
            self.assertIsNone(jobs.plan_journal.get(db, "batch", str(self.key)))

    def test_replica_blocks_a_new_local_plan_with_the_same_uuid(self):
        request = [task.model_dump() for task in self.tasks]
        plan = batches.make_plan(self.key, self.origin, request, {})
        body = {"version": 1, "kind": "batch", "id": str(self.key), "origin": self.kali,
                "request": request, "plan": plan}
        with jobs.transaction() as db:
            jobs.plan_journal.store(db, body)
        with self.assertRaisesRegex(ValueError, "replicated plan"):
            batches.save(self.key, request, plan)

    def test_malformed_or_misdirected_replica_is_not_silently_selected(self):
        first = manifest()
        record = jobs.plan_journal.package(first)
        with self.assertRaisesRegex(ValueError, "another batch UUID"):
            batches.select_replica(self.key, [None, record])
        valid_request = [task.model_dump() for task in self.tasks]
        valid_plan = batches.make_plan(self.key, self.origin, valid_request, {})
        valid = jobs.plan_journal.package({"version": 1, "kind": "batch", "id": str(self.key),
                                           "origin": self.origin, "request": valid_request, "plan": valid_plan})
        malformed = dict(valid, sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            batches.select_replica(self.key, [valid, malformed])


if __name__ == "__main__":
    unittest.main()
