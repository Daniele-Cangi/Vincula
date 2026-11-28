# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controller enrollment metadata only: SQLite fixtures, no process/SSH/file payload."""
from copy import deepcopy
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

import large_copy_state as state
import large_transfer_plans as plans
import resource_policy


SCHEMA = resource_policy.SCHEMA + state.SCHEMA + """
CREATE TABLE policy(mode TEXT NOT NULL);
INSERT INTO policy VALUES('full');
CREATE TABLE quota_sentinel(id TEXT PRIMARY KEY, held INTEGER NOT NULL);
INSERT INTO quota_sentinel VALUES('owned-spool',4096);
"""


class LargeCopyStateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("BEGIN IMMEDIATE")
        self.now = time.time()
        self.authority, self.source, self.destination, self.transfer = [str(uuid4()) for _ in range(4)]
        self.generation = str(uuid4())
        self.state_id = resource_policy.state_id(self.db)
        resource_policy.worker_start(self.db, self.generation, "linux", now=self.now)
        state.worker_seen(self.db, self.generation, expected_authority_state_id=self.state_id, now=self.now)
        request = {"source": {"node_id": self.source, "root": "data", "path": "nested/input.bin"},
                   "destination": {"node_id": self.destination, "root": "results", "path": "output.bin"},
                   "max_bytes": 128 * 1024 * 1024, "ttl_seconds": 3600}
        self.manifest = plans.make_manifest(self.transfer, self.authority, request,
                                            str(uuid4()), str(uuid4()), self.now)
        self.epochs = {"source": 4, "destination": 7}

    def enroll(self, *, mode="full", manifest=None, endpoint_epochs=None, budget=8 * 1024 * 1024):
        return state.enroll(self.db, manifest or self.manifest, self.authority, self.state_id,
                            endpoint_epochs or self.epochs, budget, mode, now=self.now)

    def enable(self):
        state.set_policy(self.db, dict(state.DEFAULT_POLICY, enabled=True))

    def pins(self, value):
        return {"expected_controller_epoch": value["controller_epoch"], "expected_authority_state_id": self.state_id}

    @staticmethod
    def ack(proposal):
        return {"version": 1, "side": proposal["side"], "endpoint_id": proposal["endpoint_id"],
                "endpoint_state_id": proposal["endpoint_state_id"], "endpoint_epoch": proposal["expected_endpoint_epoch"],
                "binding": deepcopy(proposal["binding"]), "proposal_sha256": proposal["proposal_sha256"], "bound": True}

    def proposal(self, value, side="source"):
        return state.binding_request(self.db, value["controller_id"], side, **self.pins(value), now=self.now)

    def confirm(self, value, proposal):
        return state.confirm_binding(self.db, value["controller_id"], proposal["side"], self.ack(proposal),
                                     worker_generation=self.generation, **self.pins(value), now=self.now)

    def ready(self):
        self.enable()
        value = self.enroll()
        self.confirm(value, self.proposal(value))
        return self.confirm(value, self.proposal(value, "destination"))

    def evidence(self, stopped, generation=None):
        return {"controller_id": stopped["controller_id"], "controller_epoch": stopped["controller_epoch"],
                "previous_controller_epoch": stopped["controller_epoch"] - 1,
                "worker_generation": self.generation if generation is None else generation, "runtime_run_id": None}

    def stop(self, value, operation="stop"):
        return state.control(self.db, value["controller_id"], operation, **self.pins(value), now=self.now)

    def test_disabled_default_allows_intent_but_no_binding_or_launch(self):
        value = self.enroll()
        self.assertFalse(state.policy(self.db)["enabled"])
        self.assertEqual(value["phase"], "disabled")
        self.assertFalse(value["enrollment_ready"])
        self.assertFalse(value["launchable"])
        self.assertFalse(value["operational"])
        self.assertFalse(value["runtime_implemented"])
        with self.assertRaises(ValueError): self.proposal(value)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM large_copy_controllers").fetchone()[0], 1)

    def test_policy_closed_schema_revision_cas_and_pending_limit(self):
        revision = state.digest(state.policy(self.db))
        document = dict(state.DEFAULT_POLICY, max_pending_enrollments=1)
        state.set_policy(self.db, document, revision)
        with self.assertRaises(ValueError): state.set_policy(self.db, document, revision)
        for invalid in (dict(document, enabled=1), dict(document, max_pending_enrollments=True),
                        dict(document, max_pending_enrollments=129), dict(document, token="forbidden")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): state.set_policy(self.db, invalid)
        self.enroll()
        other = plans.make_manifest(str(uuid4()), self.authority, self.manifest["request"],
                                    str(uuid4()), str(uuid4()), self.now)
        with self.assertRaises(ValueError): self.enroll(manifest=other)

    def test_exact_retry_never_duplicates_or_overwrites_an_enrollment(self):
        value = self.enroll()
        self.assertEqual(self.enroll(), value)
        for change in ({"budget": 1}, {"endpoint_epochs": {"source": 5, "destination": 7}}):
            with self.subTest(change=change), self.assertRaises(ValueError): self.enroll(**change)
        manifest = deepcopy(self.manifest)
        manifest["request"]["source"]["path"] = "another.bin"
        with self.assertRaises(ValueError): self.enroll(manifest=manifest)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM large_copy_outbox").fetchone()[0], 2)

    def test_manifest_authority_state_and_closed_quantities_are_checked(self):
        with self.assertRaises(ValueError):
            state.enroll(self.db, self.manifest, str(uuid4()), self.state_id, self.epochs, 1, "full", self.now)
        with self.assertRaises(ValueError):
            state.enroll(self.db, self.manifest, self.authority, str(uuid4()), self.epochs, 1, "full", self.now)
        for epochs in ({"source": True, "destination": 7}, {"source": 4}, {"source": 4, "destination": 7, "pid": 123}):
            with self.subTest(epochs=epochs), self.assertRaises(ValueError): self.enroll(endpoint_epochs=epochs)
        with self.assertRaises(ValueError): self.enroll(budget=0)
        with self.assertRaises(ValueError): self.enroll(mode="ask")

    def test_no_implicit_commit_and_caller_rollback_removes_all_effects(self):
        self.db.execute("SAVEPOINT enrollment")
        value = self.enroll()
        self.assertTrue(self.db.in_transaction)
        self.db.execute("ROLLBACK TO enrollment")
        self.db.execute("RELEASE enrollment")
        with self.assertRaises(ValueError): state.status(self.db, value["controller_id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM large_copy_outbox").fetchone()[0], 0)
        self.db.rollback()
        with self.assertRaises(ValueError): state.policy(self.db)

    def test_ask_exact_approval_can_precede_optin_but_never_bypasses_it(self):
        self.db.execute("UPDATE policy SET mode='ask'")
        value = self.enroll(mode="ask")
        self.assertEqual(len(state.pending(self.db, self.now)), 1)
        with self.assertRaises(ValueError): self.proposal(value)
        with self.assertRaises(ValueError):
            state.decide(self.db, value["controller_id"], True, "a" * 64, **self.pins(value), now=self.now)
        value = state.decide(self.db, value["controller_id"], True, value["intent_sha256"], **self.pins(value), now=self.now)
        self.assertEqual(value["phase"], "disabled")
        with self.assertRaises(ValueError): self.proposal(value)
        self.enable()
        self.assertEqual(self.proposal(value)["binding"]["controller_id"], value["controller_id"])

    def test_ready_requires_both_exact_confirmations_and_is_never_operational(self):
        self.enable()
        value = self.enroll()
        first = self.proposal(value)
        value = self.confirm(value, first)
        self.assertEqual(value["phase"], "binding_partial")
        self.assertFalse(value["enrollment_ready"])
        value = self.confirm(value, self.proposal(value, "destination"))
        self.assertTrue(value["enrollment_ready"])
        self.assertEqual(value["phase"], "enrollment_ready")
        self.assertFalse(value["launchable"])
        self.assertFalse(value["operational"])
        self.assertEqual(self.confirm(value, first), value)

    def test_lost_binding_reply_reuses_exact_proposal_and_preserves_partial_ack(self):
        self.enable()
        value = self.enroll()
        source = self.proposal(value)
        partial = self.confirm(value, source)
        destination = self.proposal(value, "destination")
        self.assertEqual(self.proposal(value, "destination"), destination)
        self.assertEqual(self.confirm(partial, source), state.status(self.db, value["controller_id"], self.now))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM large_copy_outbox WHERE phase='confirmed'").fetchone()[0], 1)
        self.assertTrue(self.confirm(value, destination)["enrollment_ready"])

    def test_unsent_and_mismatched_binding_receipts_never_count(self):
        self.enable()
        value = self.enroll()
        proposal = state._proposal(state._row(self.db, value["controller_id"]), "source")
        with self.assertRaises(ValueError): self.confirm(value, proposal)
        proposal = self.proposal(value)
        for name, changed in (("bound", 1), ("version", True), ("endpoint_id", str(uuid4())),
                              ("endpoint_state_id", str(uuid4())), ("endpoint_epoch", 99), ("proposal_sha256", "f" * 64)):
            receipt = dict(self.ack(proposal), **{name: changed})
            with self.subTest(name=name), self.assertRaises(ValueError):
                state.confirm_binding(self.db, value["controller_id"], "source", receipt,
                                      worker_generation=self.generation, **self.pins(value), now=self.now)
        receipt = self.ack(proposal);receipt["binding"]["manifest_sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            state.confirm_binding(self.db, value["controller_id"], "source", receipt,
                                  worker_generation=self.generation, **self.pins(value), now=self.now)
        self.assertFalse(state.status(self.db, value["controller_id"], self.now)["enrollment_ready"])

    def test_full_to_ask_blocks_auto_consent_and_revocation_keeps_outbox(self):
        value = self.ready()
        self.db.execute("UPDATE policy SET mode='ask'")
        value = state.status(self.db, value["controller_id"], self.now)
        self.assertEqual(value["phase"], "awaiting_approval")
        with self.assertRaises(ValueError): self.proposal(value)
        value = state.decide(self.db, value["controller_id"], True, value["intent_sha256"], **self.pins(value), now=self.now)
        self.assertTrue(value["enrollment_ready"])
        revoked = self.stop(value, "revoke")
        self.assertEqual(revoked["phase"], "stopping")
        self.assertEqual(revoked["stop_reason"], "revoked")
        self.assertEqual(len(revoked["bindings"]), 2)
        self.assertTrue(all(x["phase"] == "confirmed" for x in revoked["bindings"].values()))
        self.assertFalse(revoked["enrollment_ready"])

    def test_worker_attestation_requires_current_generation_freshness_and_history(self):
        value = self.ready()
        self.assertFalse(state.status(self.db, value["controller_id"], self.now + 5)["enrollment_ready"])
        self.assertFalse(state.status(self.db, value["controller_id"], self.now - 1)["enrollment_ready"])
        resource_policy.worker_stopped(self.db, self.generation, now=self.now)
        self.assertFalse(state.status(self.db, value["controller_id"], self.now)["enrollment_ready"])
        with self.assertRaises(ValueError): state.worker_seen(self.db, self.generation, expected_authority_state_id=self.state_id, now=self.now)

    def test_new_worker_cannot_confirm_old_generation_outbox(self):
        self.enable()
        value = self.enroll()
        proposal = self.proposal(value)
        old = self.generation
        self.generation = str(uuid4())
        resource_policy.worker_start(self.db, self.generation, "linux", now=self.now)
        state.worker_seen(self.db, self.generation, expected_authority_state_id=self.state_id, now=self.now)
        with self.assertRaises(ValueError): self.confirm(value, proposal)
        with self.assertRaises(ValueError):
            state.worker_seen(self.db, old, expected_authority_state_id=self.state_id, now=self.now)
        self.assertEqual(state.status(self.db, value["controller_id"], self.now)["phase"], "awaiting_worker")

    def test_history_replacement_never_becomes_valid_by_echoing_a_pin(self):
        value = self.ready()
        other = str(uuid4())
        self.db.execute("UPDATE resource_identity SET state_id=?", (other,))
        self.assertEqual(state.status(self.db, value["controller_id"], self.now)["phase"], "history_mismatch")
        for pin in (self.state_id, other):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                state.control(self.db, value["controller_id"], "stop", expected_controller_epoch=1,
                              expected_authority_state_id=pin, now=self.now)
        stopped = state.emergency_stop(self.db, value["controller_id"], self.now)
        self.assertFalse(stopped["history_matches"])
        self.assertFalse(stopped["enrollment_ready"])
        with self.assertRaises(ValueError):
            state.confirm_cleanup(self.db, value["controller_id"], self.evidence(stopped),
                                  expected_controller_epoch=stopped["controller_epoch"], expected_authority_state_id=other)

    def test_stop_fences_pending_responses_and_never_releases_quota(self):
        self.enable()
        value = self.enroll()
        proposal = self.proposal(value)
        stopped = self.stop(value)
        self.assertEqual(stopped["controller_epoch"], 2)
        self.assertEqual(self.stop(stopped)["controller_epoch"], 2)
        with self.assertRaises(ValueError): self.confirm(value, proposal)
        with self.assertRaises(ValueError): self.proposal(stopped)
        self.assertEqual(self.db.execute("SELECT held FROM quota_sentinel").fetchone()[0], 4096)

    def test_cleanup_exact_types_original_generation_and_epoch_are_required(self):
        value = self.ready()
        stopped = self.stop(value)
        expected = self.evidence(stopped)
        for change in ({"previous_controller_epoch": True}, {"worker_generation": str(uuid4())},
                       {"controller_epoch": 1}, {"runtime_run_id": str(uuid4())}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                state.confirm_cleanup(self.db, stopped["controller_id"], dict(expected, **change), **self.pins(stopped), now=self.now)
        cleaned = state.confirm_cleanup(self.db, stopped["controller_id"], expected, **self.pins(stopped), now=self.now)
        self.assertEqual(cleaned["phase"], "stopped")
        self.assertEqual(self.db.execute("SELECT held FROM quota_sentinel").fetchone()[0], 4096)

    def test_resume_is_never_launched_only_and_preserves_outbox_history(self):
        self.db.execute("UPDATE policy SET mode='ask'")
        self.enable()
        value = self.enroll(mode="ask")
        value = state.decide(self.db, value["controller_id"], True, value["intent_sha256"], **self.pins(value), now=self.now)
        original_proposal = self.proposal(value)
        self.confirm(value, original_proposal)
        stopped = self.stop(value)
        with self.assertRaises(ValueError):
            state.control(self.db, value["controller_id"], "resume", endpoint_epochs=self.epochs, **self.pins(stopped), now=self.now)
        stopped = state.confirm_cleanup(self.db, value["controller_id"], self.evidence(stopped), **self.pins(stopped), now=self.now)
        resumed = state.control(self.db, value["controller_id"], "resume", endpoint_epochs={"source": 8, "destination": 9}, **self.pins(stopped), now=self.now)
        self.assertEqual(resumed["controller_epoch"], 3)
        self.assertNotEqual(resumed["intent_sha256"], value["intent_sha256"])
        self.assertEqual(resumed["phase"], "awaiting_approval")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM large_copy_outbox").fetchone()[0], 4)
        with self.assertRaisesRegex(ValueError, "epoch"): self.confirm(value, original_proposal)
        self.assertFalse(resumed["launchable"])

    def test_recorded_runtime_run_refuses_placeholder_cleanup_and_resume(self):
        value = self.ready()
        stopped = self.stop(value)
        self.db.execute("UPDATE large_copy_controllers SET runtime_run_id=?", (str(uuid4()),))
        with self.assertRaises(ValueError):
            state.confirm_cleanup(self.db, value["controller_id"], self.evidence(stopped), **self.pins(stopped))
        with self.assertRaises(ValueError):
            state.control(self.db, value["controller_id"], "resume", endpoint_epochs=self.epochs, **self.pins(stopped))

    def test_stop_retries_after_cleanup_preserve_fence_and_resume_without_new_proof(self):
        value = self.ready()
        stopped = self.stop(value)
        stopped = state.confirm_cleanup(self.db, value["controller_id"], self.evidence(stopped), **self.pins(stopped), now=self.now)
        self.assertEqual((stopped["controller_epoch"], stopped["cleanup_epoch"]), (2, 2))
        self.assertEqual(self.stop(stopped), stopped)
        self.assertEqual(state.emergency_stop(self.db, value["controller_id"], self.now), stopped)
        revoked = self.stop(stopped, "revoke")
        self.assertEqual((revoked["phase"], revoked["controller_epoch"], revoked["cleanup_epoch"]), ("stopped", 2, 2))
        self.assertEqual(revoked["stop_reason"], "revoked")
        row = state._row(self.db, value["controller_id"])
        self.assertIsNone(row["approved_sha256"])
        self.assertEqual((row["worker_generation"], row["stopped_from_epoch"]), (self.generation, 1))
        # No second cleanup confirmation or invented generation is supplied.
        resumed = state.control(self.db, value["controller_id"], "resume", endpoint_epochs={"source": 8, "destination": 9},
                                **self.pins(revoked), now=self.now)
        self.assertEqual(resumed["controller_epoch"], 3)
        self.assertEqual(resumed["phase"], "enrolling")
        self.assertFalse(resumed["launchable"])
        self.assertEqual(self.db.execute("SELECT held FROM quota_sentinel").fetchone()[0], 4096)

    def test_expired_intents_are_not_offered_as_pending_decisions(self):
        self.db.execute("UPDATE policy SET mode='ask'")
        value = self.enroll(mode="ask")
        expired = self.now + 3600
        self.assertTrue(state.status(self.db, value["controller_id"], expired)["expired"])
        self.assertFalse(state.status(self.db, value["controller_id"], expired)["approval_required"])
        self.assertEqual(state.pending(self.db, expired), [])
        with self.assertRaises(ValueError):
            state.decide(self.db, value["controller_id"], True, value["intent_sha256"], **self.pins(value), now=expired)
        stopped = state.emergency_stop(self.db, value["controller_id"], expired)
        self.assertEqual(stopped["phase"], "stopping")
        self.assertEqual(self.db.execute("SELECT held FROM quota_sentinel").fetchone()[0], 4096)

    def test_disabled_policy_and_emergency_stop_do_not_unbind_confirmed_endpoints(self):
        value = self.ready()
        state.set_policy(self.db, state.DEFAULT_POLICY)
        stopped = state.emergency_stop(self.db, value["controller_id"], self.now)
        self.assertEqual(stopped["phase"], "stopping")
        self.assertTrue(all(x["phase"] == "confirmed" for x in stopped["bindings"].values()))
        self.assertFalse(stopped["launchable"])
        self.assertEqual(self.db.execute("SELECT held FROM quota_sentinel").fetchone()[0], 4096)

    def test_two_connections_serialize_enrollment_cas_and_pending_admission(self):
        with TemporaryDirectory() as temporary:
            first = sqlite3.connect(temporary + "/metadata.sqlite", timeout=0)
            second = sqlite3.connect(temporary + "/metadata.sqlite", timeout=0)
            try:
                first.executescript(SCHEMA)
                first.execute("BEGIN IMMEDIATE")
                state.set_policy(first, dict(state.DEFAULT_POLICY, max_pending_enrollments=1))
                pin = resource_policy.state_id(first)
                arguments = (self.manifest, self.authority, pin, self.epochs, 4096, "full")
                original = state.enroll(first, *arguments, now=self.now)
                with self.assertRaises(sqlite3.OperationalError): second.execute("BEGIN IMMEDIATE")
                first.commit()
                second.execute("BEGIN IMMEDIATE")
                self.assertEqual(state.enroll(second, *arguments, now=self.now), original)
                other = plans.make_manifest(str(uuid4()), self.authority, self.manifest["request"], str(uuid4()), str(uuid4()), self.now)
                with self.assertRaises(ValueError): state.enroll(second, other, *arguments[1:], now=self.now)
                self.assertEqual(second.execute("SELECT COUNT(*) FROM large_copy_controllers").fetchone()[0], 1)
                self.assertEqual(second.execute("SELECT COUNT(*) FROM large_copy_outbox").fetchone()[0], 2)
                second.rollback()
            finally:
                first.close();second.close()


if __name__ == "__main__":
    unittest.main()
