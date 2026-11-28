# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native-plan routing against three temporary destination SQLite profiles.

The transport is simulated; queue, approval, journal and state-pin checks are
the real receiver. No SSH connections or program launches occur in this file.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import RLock
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import execution_plans
import jobs
import node_registry
import server


class DestinationProfiles:
    def __init__(self, root, nodes):
        self.lock = RLock()
        self.dispatch = jobs.dispatch
        self.roots = {node: root / node for node in nodes}
        self.coordinator = root / "coordinator"
        self.offline, self.lost, self.calls = set(), set(), []
        self.before_request = None
        for location in [self.coordinator, *self.roots.values()]:
            with self.profile(location):
                jobs.initialize()
                with jobs.transaction() as db:
                    db.execute("UPDATE policy SET mode='full'")
                    jobs.direct_execution.set_enabled(db, True)
                    db.execute("INSERT INTO worker_environment VALUES(1,?,?)",
                               (str(Path(sys.executable).parent), ".EXE"))
                    generation = str(uuid4())
                    jobs.resource_policy.worker_start(db, generation, "windows" if jobs.WINDOWS else "linux")
                    jobs.resource_policy.heartbeat(db, generation, {
                        "observed_at": time.time(), "cpu_millis": 4000, "cpu_percent": 0,
                        "memory_total_bytes": 2**30, "memory_available_bytes": 2**30, "gpus": []})

    def profile(self, root):
        # Callers hold this fixture's RLock when dispatching from multiple threads.
        return patch.multiple(jobs, ROOT=root, DATABASE=root / "jobs.sqlite3")

    def local(self, action, **payload):
        with self.lock, self.profile(self.coordinator):
            return self.dispatch(action, **payload)

    def __call__(self, node, request):
        with self.lock:
            self.calls.append((node, deepcopy(request)))
            if node in self.offline:
                raise server.ToolError("Fixture transport unavailable.")
            if self.before_request is not None:
                self.before_request(node, request)
            with self.profile(self.roots[node]):
                try:
                    result = self.dispatch(**request)
                except (ValueError, TypeError) as error:
                    raise server.ToolError(str(error)) from None
            key = (node, request["action"], request.get("operation"))
            if key in self.lost:
                self.lost.remove(key)
                raise server.ToolError("Fixture acknowledgment lost after commit.")
            return result

    def sql(self, node, statement, parameters=()):
        with self.lock, closing(sqlite3.connect(self.roots[node] / "jobs.sqlite3")) as db, db:
            return db.execute(statement, parameters).fetchall()

    def count(self):
        return sum(self.sql(node, "SELECT COUNT(*) FROM jobs")[0][0] for node in self.roots)


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-execution-recovery-")
        self.addCleanup(temporary.cleanup)
        self.nodes = [str(uuid4()) for _ in range(3)]
        self.authority, self.left, self.right = self.nodes
        self.network = DestinationProfiles(Path(temporary.name), self.nodes)
        topology = node_registry.Registry(self.authority, [node_registry.Node(node, node,
            "windows" if jobs.WINDOWS else "linux",
            route=None if node == self.authority else node_registry.Route("fixture", ())) for node in self.nodes])
        for mocker in (patch.object(server, "destination_request", side_effect=self.network),
                       patch.object(server.jobs, "dispatch", side_effect=self.network.local),
                       patch.object(server, "local_node", return_value=self.authority),
                       patch.object(server, "registry", return_value=topology)):
            mocker.start()
            self.addCleanup(mocker.stop)
        self.key = uuid4()
        self.tasks = [server.ExecutionTask(candidates=[server.ExecutionCandidate(node_id=node,
            process=server.ExecutionInvocation(argv=[sys.executable, "-c", "print('prepared argv')"],
                resources=server.ProcessResources(cpu_millis=1000, memory_bytes=4096)))])
            for node in (self.left, self.right)]

    def submit(self):
        return asyncio.run(server.submit_execution_plan(self.key, self.authority, self.tasks))

    def report(self, operation="get_execution_plan"):
        return asyncio.run(getattr(server, operation)(self.key, self.authority))

    def assert_success(self, result):
        self.assertTrue(all(part.error is None and part.job is not None for part in result.parts), result)

    def test_full_native_jobs_and_replicas_precede_effects_without_registration(self):
        result = self.submit()
        self.assert_success(result)
        self.assertEqual([part.job.state for part in result.parts], ["queued", "queued"])
        self.assertEqual(self.network.count(), 2)
        for node in (self.left, self.right):
            calls = [request for peer, request in self.network.calls if peer == node]
            store = next(i for i, request in enumerate(calls) if request.get("operation") == "store")
            execute = next(i for i, request in enumerate(calls) if request.get("operation") == "execute")
            self.assertLess(store, execute)
            self.assertEqual(self.network.sql(node, "SELECT COUNT(*) FROM programs")[0][0], 0)

    def test_lost_execute_ack_and_repeated_submit_keep_original_job_ids(self):
        self.network.lost.add((self.left, "process", "execute"))
        first = self.submit()
        self.assertIn("acknowledgment lost", first.parts[0].error)
        self.assertEqual(self.network.count(), 2)
        second = self.submit()
        self.assert_success(second)
        self.assertEqual(first.manifest, second.manifest)
        self.assertEqual(self.network.count(), 2)

    def test_lost_replica_ack_defers_its_job_until_same_plan_is_retried(self):
        self.network.lost.add((self.left, "plan", "store"))
        first = self.submit()
        self.assertIn("acknowledgment lost", first.parts[0].error)
        self.assertEqual(self.network.count(), 1)
        self.assert_success(self.submit())
        self.assertEqual(self.network.count(), 2)

    def test_lost_authority_ack_creates_no_jobs_until_the_original_plan_is_found(self):
        self.network.lost.add((self.authority, "plan", "store"))
        with self.assertRaisesRegex(server.ToolError, "acknowledgment lost"):
            self.submit()
        self.assertEqual(self.network.count(), 0)
        record = self.network(self.authority, {"action": "plan", "operation": "get",
                                              "kind": "execution", "plan_id": str(self.key)})
        self.assertIsNotNone(record)
        result = self.submit()
        self.assert_success(result)
        self.assertEqual(result.manifest, record["manifest"])
        self.assertEqual(self.network.count(), 2)

    def test_another_origin_resumes_known_jobs_while_authority_is_offline(self):
        first = self.submit()
        self.network.offline.add(self.authority)
        self.network.coordinator = self.network.roots[self.right]
        with patch.object(server, "local_node", return_value=self.right):
            recovered = self.submit()
        self.assert_success(recovered)
        self.assertEqual(recovered.manifest, first.manifest)
        self.assertEqual(recovered.coordinator_node_id, self.right)
        self.assertEqual(self.network.count(), 2)

    def test_unknown_plan_requires_its_authority_before_creating_jobs(self):
        self.network.offline.add(self.authority)
        with self.assertRaisesRegex(server.ToolError, "unknown plan"):
            self.submit()
        self.assertEqual(self.network.count(), 0)

    def test_read_has_no_replication_or_execution_effects_and_stop_is_scoped(self):
        first = self.submit()
        self.network.calls.clear()
        read = self.report()
        self.assert_success(read)
        self.assertEqual(read.manifest, first.manifest)
        self.assertTrue(all(request.get("operation") in {"get", "read", None}
                            for _, request in self.network.calls))
        stopped = self.report("stop_execution_plan")
        self.assert_success(stopped)
        self.assertEqual([part.job.state for part in stopped.parts], ["cancelled", "cancelled"])
        self.assertEqual(self.network.count(), 2)

    def test_reset_destination_history_blocks_replay_read_and_stop(self):
        self.submit()
        self.network.sql(self.left, "UPDATE resource_identity SET state_id=?", (str(uuid4()),))
        for operation in (self.submit, self.report, lambda: self.report("stop_execution_plan")):
            result = operation()
            self.assertIn("durable state changed", result.parts[0].error)
            self.assertIsNone(result.parts[1].error)
        self.assertEqual(self.network.count(), 2)

    def test_read_pin_is_checked_in_the_receiver_transaction_after_observation(self):
        self.submit()
        def reset(node, request):
            if node == self.left and request.get("operation") == "read":
                self.network.sql(node, "UPDATE resource_identity SET state_id=?", (str(uuid4()),))
                self.network.before_request = None
        self.network.before_request = reset
        result = self.report()
        self.assertIsNone(result.parts[0].job)
        self.assertIsNotNone(result.parts[0].error)
        self.assertIsNone(result.parts[1].error)

    def test_ask_retains_exact_resource_manifest_and_never_auto_approves(self):
        self.network.sql(self.left, "UPDATE policy SET mode='ask'")
        result = self.submit()
        self.assert_success(result)
        job = result.parts[0].job
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.manifest["original"]["resources"], self.tasks[0].candidates[0].process.resources.model_dump())
        self.assertIsNone(self.network.sql(self.left,
            "SELECT approved_sha256 FROM direct_process_jobs WHERE id=?", (str(job.id),))[0][0])

    def test_resume_does_not_require_unused_candidate_on_the_new_origin(self):
        unused = deepcopy(self.tasks[1].candidates[0])
        unused.estimated_seconds = 1000
        self.tasks = [self.tasks[0]]
        self.tasks[0].candidates.append(unused)
        first = self.submit()
        self.assertEqual(first.parts[0].node_id, self.left)
        topology = node_registry.Registry(self.authority, [node_registry.Node(node, node,
            "windows" if jobs.WINDOWS else "linux",
            route=None if node == self.authority else node_registry.Route("fixture", ()))
            for node in (self.authority, self.left)])
        with patch.object(server, "registry", return_value=topology):
            second = self.submit()
            self.assert_success(second)
            self.assertEqual(second.manifest, first.manifest)
            self.key = uuid4()
            with self.assertRaisesRegex(server.ToolError, "Unknown registered node"):
                self.submit()
        self.assertEqual(self.network.count(), 1)

    def test_changed_request_or_authority_cannot_reuse_the_plan_handle(self):
        self.submit()
        self.tasks[0].candidates[0].process.argv.append("different")
        with self.assertRaisesRegex(server.ToolError, "different immutable"):
            self.submit()
        with self.assertRaisesRegex(server.ToolError, "different authority"):
            asyncio.run(server.get_execution_plan(self.key, self.right))
        self.assertEqual(self.network.count(), 2)

    def test_racing_proposals_adopt_one_authority_manifest_before_effects(self):
        request = {"tasks": [{"candidates": [candidate.model_dump() for task in self.tasks for candidate in task.candidates]}]}
        snapshots = {node: asyncio.run(server.node_resources(node)).model_dump(mode="json")
                     for node in (self.left, self.right)}
        for row in snapshots.values():
            row["request_round_trip_seconds"] = 0
        first = execution_plans.make_manifest(str(self.key), self.authority, request, snapshots)
        snapshots[first["plan"][0]["node_id"]]["worker"]["observation"]["cpu_percent"] = 100
        second = execution_plans.make_manifest(str(self.key), self.authority, request, snapshots)
        self.assertNotEqual(first, second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: asyncio.run(server.store_execution_authority(body)), [first, second]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.network.count(), 0)

    def test_slow_roundtrip_does_not_extend_destination_freshness(self):
        with patch.object(server, "monotonic", side_effect=[100, 106]):
            value = asyncio.run(server.node_resources(self.left))
        self.assertEqual(value.request_round_trip_seconds, 6)
        self.assertFalse(value.worker.fresh)
        self.assertFalse(value.worker.observation_fresh)
        self.assertEqual(value.worker.fresh_remaining_seconds, 0)

    def test_api_cost_estimates_accept_numbers_and_reject_boolean_or_string_coercion(self):
        from pydantic import ValidationError
        candidate = self.tasks[0].candidates[0].model_dump()
        for field in ("estimated_seconds", "setup_seconds"):
            for invalid in (True, "2.5"):
                with self.subTest(field=field, value=invalid), self.assertRaises(ValidationError):
                    server.ExecutionCandidate(**(candidate | {field: invalid}))
            self.assertEqual(getattr(server.ExecutionCandidate(**(candidate | {field: 2})), field), 2)


if __name__ == "__main__":
    unittest.main()
