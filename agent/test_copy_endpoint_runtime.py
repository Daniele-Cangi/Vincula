# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Receiver binding facade and real bounded I/O in owned local TEMP only."""
import json
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4, uuid5, UUID

import jobs
import test_large_files_runtime as fixtures


@unittest.skipUnless(sys.platform in {"linux", "win32"}, "Native worker platforms")
class CopyEndpointRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.LargeFilesRuntimeTests("runTest")
        self.f = fixture
        try:
            fixture.setUp()
        finally:
            self._cleanups.extend(fixture._cleanups)
            fixture._cleanups.clear()
        fixture.start_worker()
        self.export, self.target, self.size, self.digest = fixture.request_pair(size=16385)
        with jobs.transaction() as db:
            jobs.copy_endpoint_bindings.set_policy(db, dict(jobs.copy_endpoint_bindings.DEFAULT_POLICY, enabled=True))
            spec = jobs.transfer_state.details(db, self.export["id"])["spec"]
        self.manifest = jobs.large_transfer_plans.make_manifest(spec["transfer_id"], spec["authority_node_id"],
            {"source": spec["source"], "destination": spec["destination"], "max_bytes": spec["max_bytes"], "ttl_seconds": 600},
            fixture.state_id, fixture.state_id, spec["expires_at"] - 600)
        self.authority_state = str(uuid4())
        self.binding = {}

    def call(self, operation, **fields):
        return jobs.dispatch("copy-binding", operation=operation, **fields)

    def configure(self, side):
        f = self.f
        family = "windows" if jobs.WINDOWS else "linux"
        node_id = self.manifest["request"][side]["node_id"]
        registry = f.home / "binding-nodes.json"
        registry.write_text(json.dumps({"local": node_id, "nodes": [
            {"id": value, "label": value, "family": family}
            for value in (f.source_node, f.target_node, f.authority)]}))
        (f.root / "large-copy-controller.json").write_text(json.dumps(
            {"version": 1, "node_id": node_id, "registry_path": str(registry)}))

    def proposal(self, side, controller_epoch=1):
        endpoint = self.export if side == "source" else self.target
        epoch = self.f.status(endpoint)["epoch"]
        value = {"version": 1, "side": side, "endpoint_id": endpoint["id"], "endpoint_state_id": self.f.state_id,
                 "expected_endpoint_epoch": epoch, "binding": {
                    "controller_id": str(uuid5(UUID(self.manifest["id"]), "large-copy:" + self.manifest["origin"])),
                    "controller_epoch": controller_epoch, "authority_node_id": self.manifest["origin"],
                    "authority_state_id": self.authority_state,
                    "manifest_sha256": jobs.large_transfer_plans.package(self.manifest)["sha256"]}}
        return dict(value, proposal_sha256=jobs.copy_endpoint_bindings.digest(value))

    def prepare(self, side, controller_epoch=1):
        self.configure(side)
        endpoint = self.export if side == "source" else self.target
        status = self.call("status", endpoint_id=endpoint["id"])
        return self.call("prepare", manifest=self.manifest, proposal=self.proposal(side, controller_epoch),
                         expected_binding_revision=status["binding_revision"])

    def bind(self, side, controller_epoch=1):
        intent = self.prepare(side, controller_epoch)
        ack = self.call("bind", endpoint_id=intent["endpoint_id"], expected_state_id=self.f.state_id,
                        expected_intent_sha256=intent["intent_sha256"],
                        expected_binding_revision=intent["binding_revision"])
        status = self.call("status", endpoint_id=intent["endpoint_id"])
        self.binding[side] = dict(binding_token=status["token"], binding_revision=status["binding_revision"])
        return intent, ack

    def rpc(self, operation, side, **fields):
        endpoint = self.export if side == "source" else self.target
        current = self.f.status(endpoint)
        return self.f.rpc(operation, current, **dict(self.binding.get(side, {}), **fields))

    def test_closed_wire_owner_controls_and_readable_preview(self):
        info = self.call("info")
        self.assertTrue(info["enforcement_implemented"])
        self.assertFalse(info["autonomous_copy_implemented"])
        intent = self.prepare("source")
        self.assertEqual(intent["preview"]["manifest"]["request"]["source"]["path"], "source.bin")
        self.assertEqual(intent["preview"]["proposal"]["binding"]["authority_state_id"], self.authority_state)
        for operation in ("worker_seen", "set_policy", "approve", "cleanup", "emergency_stop", "revoke"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.call(operation, endpoint_id=self.export["id"])
        with self.assertRaises(ValueError):
            self.call("prepare", manifest=self.manifest, proposal=self.proposal("source"),
                      expected_binding_revision=0, verified=True)

    def test_binding_ack_replay_and_old_clients_cannot_mutate_managed_endpoint(self):
        intent, ack = self.bind("source")
        self.assertTrue(ack["bound"])
        replay = self.call("bind", endpoint_id=self.export["id"], expected_state_id=self.f.state_id,
                           expected_intent_sha256=intent["intent_sha256"], expected_binding_revision=0)
        self.assertEqual(ack, replay)
        for operation, extra in (("reserve", {}), ("queue", {}), ("stop", {}),
                                 ("block-read", {"index": 0}), ("cleanup", {})):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.f.rpc(operation, self.export, **extra)
        self.assertEqual(self.f.status(self.export)["state"], "ready")
        self.assertTrue(self.f.rpc("blocks", self.export)[0]["blocks"])

    def test_binding_sealed_export_does_not_require_original_source(self):
        (self.f.source / "source.bin").unlink()
        self.bind("source")
        self.assertEqual(len(self.rpc("block-read", "source", index=0)[1]), self.size)

    def test_two_bound_endpoints_copy_publish_and_cleanup(self):
        self.bind("source"); self.bind("destination")
        reply, data = self.rpc("block-read", "source", index=0)
        self.rpc("block-write", "destination", index=0, expected=reply["block"], payload=data)
        self.rpc("queue", "destination")
        self.f.complete_attempt(self.target, "commit")
        status = self.f.status(self.target)
        self.assertEqual(status["state"], "published")
        self.assertEqual(status["receipt"]["sha256"], self.digest)
        self.assertEqual((self.f.target / "received.bin").stat().st_size, self.size)
        self.rpc("cleanup", "destination")
        self.assertEqual(self.f.status(self.target)["reserved_bytes"], 0)

    def test_stop_disabled_policy_and_resume_gap_does_not_reactivate_data(self):
        self.bind("source")
        with jobs.transaction() as db:
            jobs.copy_endpoint_bindings.set_policy(db, jobs.copy_endpoint_bindings.DEFAULT_POLICY)
        # Authenticated Stop must still work after feature policy is disabled.
        self.rpc("stop", "source")
        stopped = self.call("status", endpoint_id=self.export["id"])
        self.binding["source"]["binding_revision"] = stopped["binding_revision"]
        self.assertEqual(stopped["phase"], "stopped")
        with self.assertRaises(ValueError): self.rpc("resume", "source")
        with jobs.transaction() as db:
            jobs.copy_endpoint_bindings.set_policy(db, dict(jobs.copy_endpoint_bindings.DEFAULT_POLICY, enabled=True))
        self.rpc("resume", "source")
        for operation, fields in (("queue", {}), ("block-read", {"index": 0})):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.rpc(operation, "source", **fields)
        self.bind("source", controller_epoch=2)
        self.assertEqual(len(self.rpc("block-read", "source", index=0)[1]), self.size)

    def test_owner_stop_with_foreign_history_does_not_release_disk(self):
        self.bind("source")
        original = self.f.status(self.export)["reserved_bytes"]
        self.f.replace_history(self.export)
        with jobs.transaction() as db:
            result = jobs.copy_endpoint_bindings.emergency_stop(db, self.export["id"])
            self.assertFalse(result["history_matches"])
            self.assertEqual(jobs.transfer_state.status(db, self.export["id"])["reserved_bytes"], original)
        with self.assertRaises(ValueError): self.rpc("block-read", "source", index=0)

    def test_block_checkpoint_rechecks_captured_token_after_local_stop(self):
        self.bind("source"); self.bind("destination")
        reply, data = self.rpc("block-read", "source", index=0)
        original = jobs.transfer_storage.store_block

        def fenced_store(*args, **kwargs):
            checkpoint = kwargs["checkpoint"]
            def fence_then_checkpoint(block):
                with jobs.transaction() as db:
                    jobs.copy_endpoint_bindings.emergency_stop(db, self.target["id"])
                checkpoint(block)
            return original(*args, **dict(kwargs, checkpoint=fence_then_checkpoint))

        with patch.object(jobs.transfer_storage, "store_block", fenced_store), self.assertRaises(ValueError):
            self.rpc("block-write", "destination", index=0, expected=reply["block"], payload=data)
        status = self.f.status(self.target)
        self.assertEqual(status["verified_bytes"], 0)
        self.assertGreater(status["reserved_bytes"], 0)
        self.assertFalse((self.f.target / "received.bin").exists())

    def test_queued_job_keeps_original_binding_snapshot(self):
        self.bind("destination")
        # Do not launch this incomplete commit. Inspect the same receiver queue
        # and argument guard the worker uses, while excluding its claim loop.
        self.f.root.joinpath("worker.stop").touch()
        self.assertEqual(self.f.workers[0].wait(timeout=20), 0)
        with jobs.transaction() as db:
            generation = str(uuid4())
            jobs.resource_policy.worker_start(db, generation, "windows" if jobs.WINDOWS else "linux")
            jobs.transfer_state.worker_seen(db, generation)
            jobs.copy_endpoint_bindings.worker_seen(db, generation, expected_state_id=self.f.state_id)
        self.rpc("queue", "destination")
        with jobs.transaction() as db:
            row = db.execute("SELECT job_id FROM large_file_jobs WHERE endpoint_id=?", (self.target["id"],)).fetchone()
            captured = jobs.large_files._job_binding(db, row[0])
            self.assertEqual(captured, (self.binding["destination"]["binding_token"], self.binding["destination"]["binding_revision"]))
            # Simulate a changed binding revision without changing the file
            # epoch: arguments must reject the saved snapshot before spawning.
            db.execute("UPDATE copy_endpoint_bindings SET revision=revision+1 WHERE endpoint_id=?", (self.target["id"],))
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (row[0],))
            with self.assertRaisesRegex(ValueError, "revision"):
                jobs.large_files.arguments(jobs.LOCAL_RUNTIME, db, row[0])
            db.execute("UPDATE jobs SET state='queued' WHERE id=?", (row[0],))


if __name__ == "__main__":
    unittest.main()
