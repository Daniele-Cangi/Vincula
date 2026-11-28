# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""SQL-only ownership fixtures; no payload files, processes, SSH or OS grants."""
from copy import deepcopy
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

import copy_endpoint_bindings as binding
import large_copy_state as controller
import large_transfer_plans as plans
import resource_policy as resources
import transfer_state as files


SCHEMA = resources.SCHEMA + files.SCHEMA + binding.SCHEMA + """
CREATE TABLE policy(mode TEXT NOT NULL);
INSERT INTO policy VALUES('full');
CREATE TABLE file_roots(alias TEXT PRIMARY KEY,directory TEXT NOT NULL,writable INTEGER NOT NULL);
INSERT INTO file_roots VALUES('documents','/private/owned',1);
INSERT INTO file_roots VALUES('incoming','/private/owned',1);
"""


class CopyEndpointBindingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("BEGIN IMMEDIATE")
        self.now = time.time()
        self.authority, self.source, self.destination, self.transfer = [str(uuid4()) for _ in range(4)]
        self.authority_state, self.generation = str(uuid4()), str(uuid4())
        self.state_id = resources.state_id(self.db)
        resources.worker_start(self.db, self.generation, "linux", now=self.now)
        files.worker_seen(self.db, self.generation, now=self.now)
        binding.worker_seen(self.db, self.generation, expected_state_id=self.state_id, now=self.now)
        files.set_policy(self.db, dict(files.DEFAULT_POLICY, enabled=True, margin_bytes=0))
        self.grant = {"directory": "/private/owned", "writable": True}
        self.identity = {"dev": 12, "ino": 34, "size": 9, "mtime_ns": 56, "ctime_ns": 78}
        request = {"source": {"node_id": self.source, "root": "documents", "path": "input/data.bin"},
                   "destination": {"node_id": self.destination, "root": "incoming", "path": "output.bin"},
                   "max_bytes": 64, "ttl_seconds": 3600}
        self.manifest = plans.make_manifest(self.transfer, self.authority, request, self.state_id, str(uuid4()), self.now)
        self.endpoint = self.create()
        self.key = self.endpoint["id"]

    def create(self, *, manifest=None, role="export"):
        spec = plans.endpoint_spec(manifest or self.manifest, role, "a" * 64 if role == "import" else None)
        return files.request(self.db, spec, self.grant, self.identity if role == "export" else None, [], "full", self.now)

    def enable(self):
        return binding.set_policy(self.db, dict(binding.DEFAULT_POLICY, enabled=True))

    def proposal(self, *, manifest=None, controller_epoch=1, endpoint_epoch=None, authority_state=None, side="source"):
        manifest = manifest or self.manifest
        row = {"id": controller.controller_id(manifest["id"], manifest["origin"]),
               "definition": {"manifest": manifest, "manifest_sha256": plans.package(manifest)["sha256"],
                              "local_node_id": manifest["origin"], "authority_state_id": authority_state or self.authority_state},
               "intent": {"controller_epoch": controller_epoch, "endpoint_epochs": {
                   "source": endpoint_epoch or files.details(self.db, self.key)["epoch"], "destination": 1}}}
        return controller._proposal(row, side)

    def prepare(self, *, revision=0, proposal=None, manifest=None, local_node=None):
        return binding.prepare(self.db, manifest or self.manifest, proposal or self.proposal(),
                               local_node_id=local_node or self.source, expected_binding_revision=revision, now=self.now)

    def bind(self, value, *, revision=None):
        return binding.bind(self.db, value["endpoint_id"], expected_intent_sha256=value["intent_sha256"],
                            expected_binding_revision=value["preview"]["expected_binding_revision"] if revision is None else revision,
                            now=self.now)

    def active(self):
        self.enable()
        value = self.prepare()
        self.bind(value)
        return binding.status(self.db, self.key, now=self.now)

    def guard(self, value, *, control=False, token=None, epoch=None, revision=None, now=None):
        method = binding.guard_control if control else binding.guard_data
        return method(self.db, self.key, value["endpoint_epoch"] if epoch is None else epoch,
                      value["token"] if token is None else token,
                      expected_binding_revision=value["binding_revision"] if revision is None else revision,
                      now=self.now if now is None else now)

    def reserve(self):
        return files.reserve(self.db, self.key, files.details(self.db, self.key)["epoch"],
                             [{"role": "spool", "id": "own-volume", "free_bytes": 1000000}], now=self.now)

    def approve(self, value, *, allowed=True):
        return binding.decide(self.db, self.key, allowed, expected_intent_sha256=value["intent_sha256"],
                              expected_binding_revision=value["binding_revision"], now=self.now)

    def test_disabled_default_is_independent_of_file_and_metadata_policies(self):
        value = self.prepare()
        self.assertFalse(binding.info(self.db, self.now)["policy"]["enabled"])
        self.assertFalse(value["managed"])
        self.assertEqual(value["preview"]["manifest"], self.manifest)
        self.assertEqual(value["preview"]["manifest"]["request"]["source"]["path"], "input/data.bin")
        self.assertFalse(value["runtime_implemented"])
        with self.assertRaises(ValueError):
            self.bind(value)
        self.assertIsNone(binding.guard_data(self.db, self.key, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_endpoint_bindings").fetchone()[0], 0)

    def test_policy_closed_schema_bounds_and_revision_cas(self):
        old = binding.digest(binding.policy(self.db))
        self.enable()
        with self.assertRaises(ValueError):
            binding.set_policy(self.db, binding.DEFAULT_POLICY, old)
        for change in ({"enabled": 1}, {"max_pending_bindings": True}, {"max_pending_bindings": 129}, {"extra": False}):
            document = dict(binding.DEFAULT_POLICY, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                binding.set_policy(self.db, document)

    def test_ack_is_exact_existing_controller_proposal_and_replay_is_idempotent(self):
        self.enable()
        value = self.prepare()
        self.assertEqual(self.prepare(), value)
        proposal = value["preview"]["proposal"]
        expected = {"version": 1, "side": "source", "endpoint_id": self.key, "endpoint_state_id": self.state_id,
                    "endpoint_epoch": 1, "binding": proposal["binding"], "proposal_sha256": proposal["proposal_sha256"], "bound": True}
        self.assertEqual(self.bind(value), expected)
        self.assertEqual(self.bind(value), expected)
        active = binding.status(self.db, self.key, now=self.now)
        self.assertEqual(active["binding_revision"], 1)
        self.assertTrue(self.guard(active)["managed"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_binding_events").fetchone()[0], 1)

    def test_unmanaged_none_compatible_but_managed_requires_exact_token_revision(self):
        self.assertIsNone(binding.guard_control(self.db, self.key, 1))
        with self.assertRaises(ValueError):
            binding.guard_data(self.db, self.key, 1, "a" * 64)
        value = self.active()
        with self.assertRaises(ValueError):
            binding.guard_data(self.db, self.key, 1)
        for change in ({"token": "0" * 64}, {"revision": 0}, {"epoch": 2}, {"revision": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.guard(value, **change)

    def test_pending_successor_never_overwrites_active_consensus_or_guard(self):
        active = self.active()
        self.db.execute("UPDATE policy SET mode='ask'")
        files.decide(self.db, self.key, True, self.endpoint["request_sha256"], self.now)
        self.approve(active)
        before = binding.status(self.db, self.key, now=self.now)
        next_proposal = self.proposal(controller_epoch=2)
        pending = self.prepare(revision=1, proposal=next_proposal)
        self.assertTrue(pending["approval_required"])
        self.assertEqual(binding.status(self.db, self.key, now=self.now), before)
        self.assertTrue(self.guard(before)["managed"])
        self.approve(pending, allowed=False)
        self.assertEqual(binding.status(self.db, self.key, now=self.now), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_binding_intents").fetchone()[0], 2)

    def test_ask_approval_is_exact_and_full_to_ask_blocks_before_io(self):
        active = self.active()
        self.db.execute("UPDATE policy SET mode='ask'")
        with self.assertRaises(ValueError):
            self.guard(active)
        files.decide(self.db, self.key, True, self.endpoint["request_sha256"], self.now)
        with self.assertRaises(ValueError):
            self.guard(active)
        fields = {"expected_intent_sha256": "f" * 64, "expected_binding_revision": 1, "now": self.now}
        with self.assertRaises(ValueError):
            binding.decide(self.db, self.key, True, **fields)
        self.approve(active)
        self.assertTrue(self.guard(active)["managed"])
        self.assertEqual(binding.pending(self.db, self.now), [])

    def test_initial_ask_approval_does_not_bypass_disabled_policy(self):
        self.db.execute("UPDATE policy SET mode='ask'")
        value = self.prepare()
        self.assertEqual(len(binding.pending(self.db, self.now)), 1)
        value = self.approve(value)
        with self.assertRaises(ValueError):
            self.bind(value)
        self.enable()
        files.decide(self.db, self.key, True, self.endpoint["request_sha256"], self.now)
        self.assertTrue(self.bind(value)["bound"])

    def test_manifest_request_controller_and_exact_proposal_types_are_checked(self):
        for change in ({"controller_id": str(uuid4())}, {"authority_node_id": str(uuid4())},
                       {"manifest_sha256": "a" * 64}, {"controller_epoch": True}):
            proposal = self.proposal()
            proposal["binding"].update(change)
            proposal["proposal_sha256"] = binding.digest({k: v for k, v in proposal.items() if k != "proposal_sha256"})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(proposal=proposal)
        for field, value in (("version", True), ("expected_endpoint_epoch", True), ("endpoint_id", str(uuid4())),
                             ("endpoint_state_id", str(uuid4())), ("proposal_sha256", "a" * 64)):
            proposal = self.proposal();proposal[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare(proposal=proposal)
        manifest = deepcopy(self.manifest);manifest["request"]["source"]["path"] = "other.bin"
        proposal = self.proposal(manifest=manifest)
        with self.assertRaises(ValueError):
            self.prepare(manifest=manifest, proposal=proposal)
        with self.assertRaises(ValueError):
            self.prepare(local_node=self.destination)

    def test_import_role_preserves_existing_exact_source_request_digest(self):
        manifest = plans.make_manifest(str(uuid4()), self.authority, self.manifest["request"], str(uuid4()), self.state_id, self.now)
        endpoint = self.create(manifest=manifest, role="import")
        proposal = self.proposal(manifest=manifest, side="destination")
        self.enable()
        value = self.prepare(manifest=manifest, proposal=proposal, local_node=self.destination)
        self.assertTrue(self.bind(value)["bound"])
        self.assertEqual(files.details(self.db, endpoint["id"])["spec"]["source_sha256"], "a" * 64)

    def test_first_bound_identity_authority_history_and_manifest_remain_pinned(self):
        active = self.active()
        stopped = binding.emergency_stop(self.db, self.key, self.now)
        files.resume(self.db, self.key, stopped["endpoint_epoch"], "full", self.now)
        proposal = self.proposal(controller_epoch=2, authority_state=str(uuid4()))
        with self.assertRaises(ValueError):
            self.prepare(revision=2, proposal=proposal)
        self.assertEqual(binding.status(self.db, self.key, now=self.now)["active_intent_sha256"], active["active_intent_sha256"])

    def test_current_state_id_is_verified_not_echoed_and_stop_still_available(self):
        active = self.active()
        self.reserve()
        self.db.execute("UPDATE resource_identity SET state_id=?", (str(uuid4()),))
        with self.assertRaises(ValueError):
            self.guard(active)
        with self.assertRaises(ValueError):
            self.guard(active, control=True)
        with self.assertRaises(ValueError):
            binding.guard_fence(self.db, self.key, 1, active["token"], expected_binding_revision=1)
        stopped = binding.emergency_stop(self.db, self.key, self.now)
        self.assertFalse(stopped["history_matches"])
        self.assertEqual(stopped["phase"], "stopped")
        self.assertIsNone(self.db.execute("SELECT released FROM large_file_claims WHERE endpoint_id=?", (self.key,)).fetchone()[0])

    def test_old_helper_or_stale_stopped_or_replaced_worker_cannot_attest(self):
        self.enable();value = self.prepare()
        self.db.execute("DELETE FROM copy_binding_workers")
        with self.assertRaises(ValueError):
            self.bind(value)
        with self.assertRaises(ValueError):
            binding.worker_seen(self.db, self.generation, expected_state_id=str(uuid4()), now=self.now)
        binding.worker_seen(self.db, self.generation, expected_state_id=self.state_id, now=self.now)
        with self.assertRaises(ValueError):
            binding.bind(self.db, self.key, expected_intent_sha256=value["intent_sha256"], expected_binding_revision=0, now=self.now + 6)
        fresh = str(uuid4());resources.worker_start(self.db, fresh, "linux", now=self.now)
        with self.assertRaises(ValueError):
            self.bind(value)
        with self.assertRaises(ValueError):
            binding.worker_seen(self.db, self.generation, expected_state_id=self.state_id, now=self.now)
        binding.worker_seen(self.db, fresh, expected_state_id=self.state_id, now=self.now)
        resources.worker_stopped(self.db, fresh, now=self.now)
        self.assertFalse(binding.info(self.db, self.now)["worker"]["fresh"])
        with self.assertRaises(ValueError):
            self.bind(value)

    def test_policy_revocation_and_grant_change_block_data_without_unbinding(self):
        active = self.active()
        binding.set_policy(self.db, binding.DEFAULT_POLICY)
        with self.assertRaises(ValueError):
            self.guard(active)
        self.enable()
        self.db.execute("UPDATE file_roots SET writable=0 WHERE alias='documents'")
        with self.assertRaises(ValueError):
            self.guard(active)
        with self.assertRaises(ValueError):
            self.guard(active, control=True)
        self.assertTrue(binding.status(self.db, self.key, now=self.now)["managed"])
        self.assertEqual(binding.emergency_stop(self.db, self.key, self.now)["phase"], "stopped")

    def test_uncertain_attempt_blocks_bind_and_control_even_after_timeout(self):
        self.enable();value = self.prepare();self.reserve()
        attempt = str(uuid4())
        files.begin_attempt(self.db, self.key, 1, attempt, "prepare", self.now)
        with self.assertRaises(ValueError):
            self.bind(value)
        files.finish_attempt(self.db, attempt, False, "unconfirmed")
        with self.assertRaises(ValueError):
            self.bind(value)
        files.reconcile_attempt_cleanup(self.db, attempt)
        files.resume(self.db, self.key, 1, "full", self.now)
        value = self.prepare(proposal=self.proposal())
        self.bind(value)
        next_attempt = str(uuid4())
        files.begin_attempt(self.db, self.key, 2, next_attempt, "prepare", self.now)
        stopped = binding.emergency_stop(self.db, self.key, self.now)
        with self.assertRaises(ValueError):
            self.guard(stopped, control=True)
        self.assertEqual(stopped["stop_job_ids"], [next_attempt])
        self.assertIsNone(self.db.execute("SELECT released FROM large_file_claims WHERE endpoint_id=?", (self.key,)).fetchone()[0])

    def test_emergency_stop_preserves_quota_and_repeat_revision_without_token_or_freshness(self):
        active = self.active();self.reserve()
        self.db.execute("DELETE FROM copy_binding_workers")
        binding.set_policy(self.db, binding.DEFAULT_POLICY)
        stopped = binding.emergency_stop(self.db, self.key, self.now)
        self.assertEqual((stopped["binding_revision"], stopped["endpoint_epoch"]), (2, 2))
        self.assertEqual(binding.emergency_stop(self.db, self.key, self.now), stopped)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_binding_events").fetchone()[0], 2)
        self.assertEqual(stopped["active_intent_sha256"], active["active_intent_sha256"])
        self.assertIsNone(self.db.execute("SELECT released FROM large_file_claims WHERE endpoint_id=?", (self.key,)).fetchone()[0])

    def test_revocation_persists_revision_and_requires_exact_reapproval_for_control(self):
        active = self.active()
        revoked = binding.revoke(self.db, self.key, expected_binding_revision=1, now=self.now)
        self.assertEqual((revoked["binding_revision"], revoked["phase"]), (2, "revoked"))
        self.assertEqual(binding.revoke(self.db, self.key, expected_binding_revision=2, now=self.now), revoked)
        with self.assertRaises(ValueError):
            self.guard(revoked, control=True)
        with self.assertRaises(ValueError):
            binding.revoke(self.db, self.key, expected_binding_revision=1, now=self.now)
        self.approve(revoked)
        self.assertTrue(self.guard(revoked, control=True)["managed"])
        with self.assertRaises(ValueError):
            self.guard(active)

    def test_fence_authenticates_stop_cleanup_even_when_io_permission_is_unavailable(self):
        active = self.active();self.reserve()
        attempt = str(uuid4())
        files.begin_attempt(self.db, self.key, 1, attempt, "prepare", self.now)
        revoked = binding.revoke(self.db, self.key, expected_binding_revision=1, now=self.now)
        self.db.execute("DELETE FROM copy_binding_workers")
        binding.set_policy(self.db, binding.DEFAULT_POLICY)
        self.db.execute("UPDATE policy SET mode='ask'")
        current = revoked["endpoint_epoch"]
        self.assertTrue(binding.guard_fence(self.db, self.key, current, active["token"], expected_binding_revision=2)["managed"])
        self.assertEqual(revoked["stop_job_ids"], [attempt])
        for token, revision, epoch in ((None, 2, current), (active["token"], 1, current),
                                       (active["token"], 2, 1), ("a" * 64, 2, current)):
            with self.subTest(token=token), self.assertRaises(ValueError):
                binding.guard_fence(self.db, self.key, epoch, token, expected_binding_revision=revision)
        with self.assertRaises(ValueError):
            self.guard(revoked, control=True, now=self.now + 3600)
        self.assertIsNone(self.db.execute("SELECT released FROM large_file_claims WHERE endpoint_id=?", (self.key,)).fetchone()[0])

    def test_control_resume_gap_keeps_data_fenced_until_new_cas_and_old_token_fails(self):
        active = self.active()
        stopped = binding.emergency_stop(self.db, self.key, self.now)
        self.assertTrue(self.guard(stopped, control=True)["managed"])
        files.resume(self.db, self.key, stopped["endpoint_epoch"], "full", self.now)
        resumed = binding.status(self.db, self.key, now=self.now)
        self.assertTrue(self.guard(resumed, control=True)["managed"])
        with self.assertRaises(ValueError):
            self.guard(resumed)
        with self.assertRaises(ValueError):
            self.bind(active)
        value = self.prepare(revision=2, proposal=self.proposal(controller_epoch=2))
        ack = self.bind(value)
        self.assertEqual(ack["endpoint_epoch"], 3)
        new = binding.status(self.db, self.key, now=self.now)
        self.assertEqual(new["binding_revision"], 3)
        self.assertTrue(self.guard(new)["managed"])
        with self.assertRaises(ValueError):
            self.guard(new, token=active["token"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_binding_intents").fetchone()[0], 2)

    def test_successor_requires_increased_controller_epoch_and_endpoint_fence(self):
        active = self.active()
        value = self.prepare(revision=1, proposal=self.proposal(controller_epoch=2))
        with self.assertRaises(ValueError):
            self.bind(value)
        stopped = binding.emergency_stop(self.db, self.key, self.now)
        files.resume(self.db, self.key, stopped["endpoint_epoch"], "full", self.now)
        old_controller = self.prepare(revision=2, proposal=self.proposal(controller_epoch=1))
        with self.assertRaises(ValueError):
            self.bind(old_controller)
        self.assertEqual(binding.status(self.db, self.key, now=self.now)["token"], active["token"])

    def test_expired_intents_are_not_pending_and_never_bind_but_stop_works(self):
        self.enable();self.db.execute("UPDATE policy SET mode='ask'")
        value = self.prepare();expired = self.now + 3600
        self.assertEqual(binding.pending(self.db, expired), [])
        with self.assertRaises(ValueError):
            binding.decide(self.db, self.key, True, expected_intent_sha256=value["intent_sha256"], expected_binding_revision=0, now=expired)
        self.assertEqual(binding.emergency_stop(self.db, self.key, expired)["phase"], "unmanaged")

    def test_caller_rollback_removes_intent_binding_and_revision_events(self):
        self.enable();self.db.execute("SAVEPOINT own_binding")
        value = self.prepare();self.bind(value)
        self.db.execute("ROLLBACK TO own_binding");self.db.execute("RELEASE own_binding")
        self.assertFalse(binding.status(self.db, self.key, now=self.now)["managed"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_binding_intents").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_binding_events").fetchone()[0], 0)
        self.db.rollback()
        with self.assertRaises(ValueError):
            binding.policy(self.db)

    def test_two_connections_conflicting_cas_preserves_winning_binding(self):
        # Only the disposable SQLite database is file backed; no file payload I/O.
        with TemporaryDirectory(prefix="vincula-binding-cas-") as directory:
            path = directory + "/metadata.sqlite3"
            # Copy only synthetic SQL state into two independent connections.
            dump = "\n".join(self.db.iterdump())
            first = sqlite3.connect(":memory:")
            first.executescript(dump)
            # Publish an isolated snapshot for two independent SQLite handles.
            disk = sqlite3.connect(path)
            first.backup(disk);disk.close();first.close()
            first = sqlite3.connect(path);second = sqlite3.connect(path)
            try:
                first.execute("BEGIN IMMEDIATE")
                binding.set_policy(first, dict(binding.DEFAULT_POLICY, enabled=True))
                proposal = self.proposal()
                a = binding.prepare(first, self.manifest, proposal, local_node_id=self.source, expected_binding_revision=0, now=self.now)
                proposal2 = self.proposal(controller_epoch=2)
                b = binding.prepare(first, self.manifest, proposal2, local_node_id=self.source, expected_binding_revision=0, now=self.now)
                first.commit()
                first.execute("BEGIN IMMEDIATE")
                binding.bind(first, self.key, expected_intent_sha256=a["intent_sha256"], expected_binding_revision=0, now=self.now)
                first.commit()
                second.execute("BEGIN IMMEDIATE")
                with self.assertRaises(ValueError):
                    binding.bind(second, self.key, expected_intent_sha256=b["intent_sha256"], expected_binding_revision=0, now=self.now)
                self.assertEqual(binding.status(second, self.key, now=self.now)["active_intent_sha256"], a["intent_sha256"])
                self.assertEqual(second.execute("SELECT COUNT(*) FROM copy_binding_events").fetchone()[0], 1)
                second.rollback()
            finally:
                first.close();second.close()


if __name__ == "__main__":
    unittest.main()
