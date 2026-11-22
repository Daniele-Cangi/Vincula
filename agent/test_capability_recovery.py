# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from copy import deepcopy
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4, uuid5

import capability_workflow
import jobs
import node_registry
import server
from test_plan_recovery import WorkerNetwork


class CapabilityNetwork(WorkerNetwork):
    def __init__(self, root, nodes):
        super().__init__(root, nodes)
        provider = {"id": "example.text", "version": "1", "revision": "a" * 32, "inbox": "inbox",
                    "contract": {"max_files": 1, "max_input_bytes": 32, "extensions": [".txt"],
                                 "parameters": {"count": {"type": "integer", "required": True}}}}
        self.providers = {node: deepcopy(provider) for node in nodes}
        self.capability_jobs = {node: {} for node in nodes}
        self.file_requests = {}
        self.mutations = []

    def __call__(self, node, request):
        if request["action"] in {"plan", "batch"}:
            return super().__call__(node, request)
        self.calls.append((node, deepcopy(request)))
        if node in self.offline:
            raise server.ToolError("Job service unavailable.")
        if request["action"] == "files":
            if request["operation"] != "inspect-transfer":
                raise AssertionError("Read-only recovery attempted to mutate a transfer")
            key = (node, request["request_id"])
            if key not in self.file_requests:
                raise server.ToolError("Unknown file request.")
            return deepcopy(self.file_requests[key])
        if request["action"] != "capability":
            raise AssertionError(request)
        operation = request["operation"]
        if operation == "describe":
            return deepcopy(self.providers[node])
        if operation == "read":
            return deepcopy(self.capability_jobs[node].get(request["job_id"]))
        if operation != "queue":
            raise AssertionError(request)
        self.mutations.append(("queue", node, deepcopy(request)))
        key = request["job_id"]
        if key not in self.capability_jobs[node]:
            self.capability_jobs[node][key] = {
                "id": key, "program": request["capability_id"], "state": "pending", "exit_code": None,
                "created": 1, "finished": None, "stdout": {"text": "", "truncated": False},
                "stderr": {"text": "", "truncated": False},
            }
        return deepcopy(self.capability_jobs[node][key])

    def transfer(self, transfer_id, source, destination):
        self.mutations.append(("transfer", str(transfer_id)))
        requests = []
        for endpoint, operation in [(source, "export"), (destination, "import")]:
            key = str(uuid5(transfer_id, operation))
            value = {"id": key, "state": "pending", "operation": operation,
                     "root": endpoint["root"], "path": endpoint["path"], "result": None}
            self.file_requests.setdefault((endpoint["node_id"], key), value)
            requests.append(deepcopy(self.file_requests[(endpoint["node_id"], key)]))
        return {"transfer_id": str(transfer_id), "source": requests[0], "destination": requests[1]}


class CapabilityRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin, self.first, self.second = (str(uuid4()) for _ in range(3))
        self.nodes = [self.origin, self.first, self.second]
        state = self.root / "origin"
        state.mkdir()
        changed = patch.multiple(jobs, ROOT=state, DATABASE=state / "jobs.sqlite3")
        changed.start()
        self.addCleanup(changed.stop)
        jobs.initialize()
        self.network = CapabilityNetwork(self.root, self.nodes)
        for name, value in [("destination_request", self.network),
                            ("transfer_capability_input", self.network.transfer)]:
            changed = patch.object(server, name, side_effect=value)
            changed.start()
            self.addCleanup(changed.stop)
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
        self.parts = [server.CapabilityPart(node_id=node, parameters={"count": 2}, files=[
            server.FileEndpoint(node_id=source, root="source", path="sample.txt")])
            for node, source in ((self.first, self.second), (self.second, self.first))]

    def submit(self):
        return server.CapabilityReport.model_validate(asyncio.run(
            server.submit_capability(self.key, "example.text", self.parts)))

    def recover(self):
        state = self.root / "reader"
        state.mkdir(exist_ok=True)
        with patch.multiple(jobs, ROOT=state, DATABASE=state / "jobs.sqlite3"), \
                patch.object(server, "local_node", return_value=self.first):
            jobs.initialize()
            return server.CapabilityReport.model_validate(asyncio.run(server.get_capability_run(self.key)))

    def assert_read_only(self):
        allowed = {"plan": {"get"}, "capability": {"read", "describe"}, "files": {"inspect-transfer"}}
        self.assertTrue(all(request["operation"] in allowed[request["action"]]
                            for _, request in self.network.calls))

    def test_journal_accepts_revisions_from_a_real_registered_provider(self):
        adapter = self.root / "registered_adapter.py"
        adapter.write_text("def run(inputs, parameters, workspace):\n    return 42\n", encoding="utf-8")
        with jobs.transaction() as db:
            jobs.capabilities.register(db, jobs.files, {
                "id": "example.real", "version": "1", "python": sys.executable,
                "adapter": str(adapter), "max_files": 0,
            })
        provider = jobs.dispatch("capability", operation="describe", capability_id="example.real")
        self.assertEqual(len(provider["revision"]), 32)
        request = {"capability_id": "example.real", "parts": [
            {"node_id": self.first, "files": [], "parameters": {}}
        ]}
        capability_workflow.make_plan(self.key, request, {self.first: provider}, self.origin)
        body = capability_workflow.manifest(self.key, self.origin)
        receipt = jobs.dispatch("plan", operation="store", manifest=body)
        self.assertEqual(receipt["state"], "stored")
        saved = jobs.dispatch("plan", operation="get", kind="capability", plan_id=str(self.key))
        self.assertEqual(saved["manifest"]["plan"]["parts"][0]["revision"], provider["revision"])

    def test_pending_input_approvals_and_original_transfer_ids_survive_origin_loss(self):
        initial = self.submit()
        self.assertTrue(all(part.waiting_transfers for part in initial.parts))
        body = capability_workflow.manifest(self.key, self.origin)
        self.assertEqual(body["plan"]["parts"][0]["revision"], "a" * 32)
        self.assertEqual([part["inputs"][0]["index"] for part in body["plan"]["parts"]], [0, 1])
        self.network.offline.add(self.origin)
        before = deepcopy(self.network.mutations)
        self.network.calls.clear()
        recovered = self.recover()
        self.assertEqual([part.job_id for part in recovered.parts], [part.job_id for part in initial.parts])
        for original, read in zip(initial.parts, recovered.parts):
            self.assertEqual(read.waiting_transfers, original.waiting_transfers)
            self.assertIn("awaiting approval", read.error)
        self.assertEqual(self.network.mutations, before)
        self.assert_read_only()

    def test_transferred_inputs_are_not_queued_or_transferred_again_by_read(self):
        initial = self.submit()
        for part in initial.parts:
            transfer = part.waiting_transfers[0]
            key = (part.node_id, str(transfer.destination.id))
            self.network.file_requests[key].update(state="completed", result={"size": 4, "sha256": "b" * 64})
        self.network.offline.add(self.origin)
        before = deepcopy(self.network.mutations)
        self.network.calls.clear()
        recovered = self.recover()
        self.assertTrue(all(not part.waiting_transfers and "not been queued" in part.error for part in recovered.parts))
        self.assertEqual(self.network.mutations, before)
        self.assert_read_only()

    def test_revoked_provider_before_queue_is_reported_without_transfer_effects(self):
        self.submit()
        self.network.providers[self.first] = None
        self.network.offline.add(self.origin)
        before = deepcopy(self.network.mutations)
        self.network.calls.clear()
        recovered = self.recover()
        self.assertIn("changed or was revoked", recovered.parts[0].error)
        self.assertIn("awaiting approval", recovered.parts[1].error)
        self.assertEqual(self.network.mutations, before)
        self.assert_read_only()

    def test_existing_pending_cancelled_and_completed_jobs_remain_readable(self):
        initial = self.submit()
        for part in initial.parts:
            self.network.capability_jobs[part.node_id][str(part.job_id)] = {
                "id": str(part.job_id), "program": "example.text", "state": "pending", "exit_code": None,
                "created": 3, "finished": None, "stdout": {"text": "", "truncated": False},
                "stderr": {"text": "", "truncated": False},
            }
        self.network.offline.add(self.origin)
        self.network.calls.clear()
        before = deepcopy(self.network.mutations)
        pending = self.recover()
        self.assertTrue(all(part.job.state == "pending" and part.job.created == 3 for part in pending.parts))
        first = self.network.capability_jobs[self.first][str(initial.parts[0].job_id)]
        first.update(state="cancelled", finished=5)
        self.network.providers[self.first] = None
        second = self.network.capability_jobs[self.second][str(initial.parts[1].job_id)]
        second.update(state="completed", exit_code=0, finished=5, stdout={"text": '{"value":42}', "truncated": False})
        final = self.recover()
        self.assertEqual(final.parts[0].job.state, "cancelled")
        self.assertEqual(final.parts[1].output.value, 42)
        self.assertEqual(self.network.mutations, before)
        self.assert_read_only()

    def test_offline_worker_does_not_hide_the_other_parts(self):
        initial = self.submit()
        key = str(initial.parts[1].job_id)
        self.network.capability_jobs[self.second][key] = {
            "id": key, "program": "example.text", "state": "pending", "exit_code": None,
            "created": 3, "finished": None, "stdout": {"text": "", "truncated": False},
            "stderr": {"text": "", "truncated": False},
        }
        self.network.offline.update({self.origin, self.first})
        self.network.calls.clear()
        before = deepcopy(self.network.mutations)
        recovered = self.recover()
        self.assertIn("unavailable", recovered.parts[0].error)
        self.assertEqual(recovered.parts[1].job.state, "pending")
        self.assertEqual(self.network.mutations, before)

    def test_conflicts_block_publication_effects_and_read_only_recovery(self):
        request = {"capability_id": "example.text", "parts": [part.model_dump(mode="json") for part in self.parts]}
        providers = {node: self.network.providers[node] for node in (self.first, self.second)}
        capability_workflow.make_plan(self.key, request, providers, self.origin)
        alternate = capability_workflow.manifest(self.key, self.origin)
        alternate["origin"] = self.second
        self.network(self.second, {"action": "plan", "operation": "store", "manifest": alternate})
        with self.assertRaisesRegex(server.ToolError, "conflicting"):
            self.submit()
        self.assertEqual(self.network.mutations, [])
        self.network.offline.add(self.origin)
        self.network.calls.clear()
        with self.assertRaisesRegex(server.ToolError, "Conflicting execution"):
            self.recover()
        self.assertTrue(all(request["operation"] == "get" for _, request in self.network.calls))

    def test_transfer_inspection_rejects_a_read_request_without_materializing_it(self):
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "sample.txt").write_text("readable")
        key = str(uuid4())
        with jobs.transaction() as db:
            jobs.files.grant(db, "shared", str(shared), True)
        jobs.dispatch("files", operation="request", request_id=key, operation_kind="read",
                      root="shared", relative="sample.txt")
        with jobs.transaction() as db:
            jobs.files.decide(db, key, True)
        with patch.object(jobs.files, "read_text", side_effect=AssertionError("read materialized")), \
                self.assertRaisesRegex(ValueError, "not a transfer"):
            jobs.dispatch("files", operation="inspect-transfer", request_id=key)
        with jobs.transaction() as db:
            row = db.execute("SELECT state,result FROM file_requests WHERE id=?", (key,)).fetchone()
        self.assertEqual(tuple(row), ("approved", None))

    def test_recovered_transfer_is_bound_to_the_original_endpoint(self):
        initial = self.submit()
        receipt = initial.parts[0].waiting_transfers[0]
        self.network.file_requests[(self.first, str(receipt.destination.id))]["path"] = "other.txt"
        self.network.offline.add(self.origin)
        before = deepcopy(self.network.mutations)
        self.network.calls.clear()
        recovered = self.recover()
        self.assertIn("does not match", recovered.parts[0].error)
        self.assertEqual(self.network.mutations, before)
        self.assert_read_only()

    def test_corrupt_capability_replica_is_reported_before_any_job_or_file_inspection(self):
        self.submit()
        with closing(sqlite3.connect(self.root / (self.first + ".sqlite3"))) as db, db:
            db.execute("UPDATE plan_journal SET sha256=?", ("0" * 64,))
        self.network.offline.add(self.origin)
        self.network.calls.clear()
        before = deepcopy(self.network.mutations)
        with self.assertRaisesRegex(server.ToolError, "Invalid plan journal reply"):
            self.recover()
        self.assertEqual(self.network.mutations, before)
        self.assertTrue(all(request["action"] == "plan" and request["operation"] == "get"
                            for _, request in self.network.calls))

    def test_legacy_local_capability_plan_is_readable_without_publishing_it(self):
        request = {"capability_id": "example.text", "parts": [part.model_dump(mode="json") for part in self.parts]}
        providers = {node: self.network.providers[node] for node in (self.first, self.second)}
        plan = capability_workflow.make_plan(self.key, request, providers, self.origin)
        for part in plan["parts"]:
            self.network.capability_jobs[part["node_id"]][part["job_id"]] = {
                "id": part["job_id"], "program": "example.text", "state": "pending", "exit_code": None,
                "created": 1, "finished": None, "stdout": {"text": "", "truncated": False},
                "stderr": {"text": "", "truncated": False},
            }
        self.network.calls.clear()
        report = server.CapabilityReport.model_validate(asyncio.run(server.get_capability_run(self.key)))
        self.assertTrue(all(part.job.state == "pending" for part in report.parts))
        self.assertEqual(self.network.mutations, [])
        self.assertTrue(all(request["action"] == "capability" and request["operation"] == "read"
                            for _, request in self.network.calls))
        with jobs.transaction() as db:
            self.assertIsNone(jobs.plan_journal.get(db, "capability", str(self.key)))


if __name__ == "__main__":
    unittest.main()
