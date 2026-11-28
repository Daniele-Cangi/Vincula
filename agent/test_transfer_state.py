# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""SQL metadata only: no file payloads, subprocesses, network or real grants."""
from hashlib import sha256
import sqlite3
import tempfile
import time
import unittest
from uuid import uuid4

import resource_policy as resources
import transfer_state as state


class TransferStateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(resources.SCHEMA + state.SCHEMA +
                              "CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('full');")
        self.db.execute("BEGIN IMMEDIATE")
        self.now = time.time()
        self.generation = str(uuid4())
        resources.worker_start(self.db, self.generation, "linux", now=self.now)
        state.worker_seen(self.db, self.generation, now=self.now)
        self.settings = dict(state.DEFAULT_POLICY, enabled=True, margin_bytes=0)
        state.set_policy(self.db, self.settings)
        self.transfer, self.authority, self.source, self.destination = [str(uuid4()) for _ in range(4)]
        self.grant = {"directory": "/private/granted", "writable": True}

    def spec(self, role="export", size=9, transfer=None, source_digest=None):
        return {"transfer_id": transfer or self.transfer, "authority_node_id": self.authority,
                "source": {"node_id": self.source, "root": "documents", "path": "data/source.bin"},
                "destination": {"node_id": self.destination, "root": "incoming", "path": "target.bin"},
                "role": role, "expected_state_id": resources.state_id(self.db),
                "max_bytes": max(1, size), "expires_at": self.now + 3600,
                "source_sha256": source_digest if role == "import" else None}

    @staticmethod
    def identity(size=9):
        return {"dev": 42, "ino": 73, "size": size, "mtime_ns": 10, "ctime_ns": 11}

    def create(self, role="export", size=9, transfer=None, source_digest=None, mode="full"):
        spec = self.spec(role, size, transfer, source_digest)
        return state.request(self.db, spec, self.grant, self.identity(size) if role == "export" else None,
                             [], mode, now=self.now)

    @staticmethod
    def volumes(role="export", free=state.MAX_FILE_BYTES * 8, same=False):
        return [{"role": "spool", "id": "volume-A", "free_bytes": free}] + ([] if role == "export" else
                [{"role": "target", "id": "volume-A" if same else "volume-B", "free_bytes": free}])

    def reserve(self, endpoint, role="export", **kwargs):
        return state.reserve(self.db, endpoint["id"], endpoint["epoch"], self.volumes(role, **kwargs), now=self.now)

    def approve(self, endpoint):
        return state.decide(self.db, endpoint["id"], True, endpoint["approval_sha256"], now=self.now)

    def prepared(self, size=9):
        endpoint = self.create(size=size)
        self.assertTrue(self.reserve(endpoint)["reserved"])
        attempt = str(uuid4())
        state.begin_attempt(self.db, endpoint["id"], 1, attempt, "prepare", now=self.now)
        records = []
        for index in range((size + state.BLOCK_SIZE - 1) // state.BLOCK_SIZE):
            count = min(state.BLOCK_SIZE, size - index * state.BLOCK_SIZE)
            records.append(state.checkpoint(self.db, endpoint["id"], 1, attempt, index, count, f"{index + 1:064x}"))
        block_digest = sha256("".join(f"{r['index']}:{r['size']}:{r['sha256']}\n" for r in records).encode("ascii")).hexdigest()
        header = {"version": 2, "export_id": endpoint["id"], "request_sha256": endpoint["request_sha256"],
                  "size": size, "sha256": sha256(b"").hexdigest() if size == 0 else "f" * 64,
                  "block_size": state.BLOCK_SIZE, "block_count": len(records), "blocks_sha256": block_digest}
        state.source_ready(self.db, endpoint["id"], 1, attempt, header)
        return endpoint, attempt, header, records

    def receiving(self, mode="full"):
        source, attempt, header, records = self.prepared()
        if mode == "ask":
            self.db.execute("UPDATE policy SET mode='ask'")
        endpoint = self.create("import", source_digest=source["request_sha256"], mode=mode)
        if mode == "ask": endpoint = self.approve(endpoint)
        self.assertTrue(self.reserve(endpoint, "import")["reserved"])
        endpoint = state.bind_manifest(self.db, endpoint["id"], header, mode, now=self.now)
        if mode == "ask": endpoint = self.approve(endpoint)
        return endpoint, header, records

    def test_default_disabled_explicit_policy_and_revision_cas(self):
        self.db.execute("DELETE FROM large_file_policy")
        self.assertFalse(state.info(self.db, self.now)["policy"]["enabled"])
        spec = self.spec()
        with self.assertRaises(ValueError):
            state.request(self.db, spec, self.grant, self.identity(), [], "full", now=self.now)
        revision = state.digest(state.policy(self.db))
        state.set_policy(self.db, self.settings, revision)
        with self.assertRaises(ValueError): state.set_policy(self.db, self.settings, revision)

    def test_policy_limits_reject_bool_extra_fields_and_over_one_tib(self):
        for change in ({"max_file_bytes": state.MAX_FILE_BYTES + 1}, {"max_active_endpoints": 17},
                       {"max_reserved_bytes": True}, {"enabled": 1}, {"extra": 1}):
            with self.subTest(change=change):
                document = dict(self.settings, **change)
                with self.assertRaises(ValueError): state.set_policy(self.db, document)

    def test_caller_transaction_is_not_committed_and_rollback_removes_request(self):
        endpoint = self.create()
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError): state.status(self.db, endpoint["id"])
        self.db.rollback()
        with self.assertRaises(ValueError): state.policy(self.db)

    def test_canonical_endpoint_uuid_is_role_specific_and_stable(self):
        exported = state.endpoint_id(self.transfer, self.authority, "export")
        self.assertEqual(exported, state.endpoint_id(self.transfer, self.authority, "export"))
        self.assertNotEqual(exported, state.endpoint_id(self.transfer, self.authority, "import"))

    def test_request_closed_schema_relative_paths_and_state_identity(self):
        for change in ({"expected_state_id": str(uuid4())}, {"expires_at": self.now},
                       {"max_bytes": 0}, {"password": "forbidden"}, {"transfer_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}):
            with self.subTest(change=change):
                spec = dict(self.spec(), **change)
                with self.assertRaises(ValueError):
                    state.request(self.db, spec, self.grant, self.identity(), [], "full", now=self.now)
        for path in ("../escape", "/absolute", "a//b", "a/./b", "a\\b", "C:/a", "a\n"):
            spec = self.spec();spec["source"]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError): state.validate_spec(spec)

    def test_request_idempotency_binds_private_grant_and_source_identity(self):
        endpoint = self.create()
        self.assertEqual(endpoint, self.create())
        for grant, identity in ((dict(self.grant, directory="/another"), self.identity()),
                                (self.grant, dict(self.identity(), ino=74))):
            with self.assertRaises(ValueError):
                state.request(self.db, self.spec(), grant, identity, [], "full", now=self.now)
        public = state.canonical(endpoint)
        self.assertNotIn("/private/granted", public)
        self.assertNotIn("mtime_ns", public)
        self.assertEqual(state.details(self.db, endpoint["id"])["grant_snapshot"], self.grant)

    def test_ask_exact_intent_before_reservation_and_no_automatic_approval(self):
        self.db.execute("UPDATE policy SET mode='ask'")
        endpoint = self.create(mode="ask")
        self.assertEqual(endpoint["state"], "pending")
        self.assertEqual(len(state.pending(self.db)), 1)
        with self.assertRaises(ValueError): self.reserve(endpoint)
        with self.assertRaises(ValueError): state.decide(self.db, endpoint["id"], True, "a" * 64)
        endpoint = self.approve(endpoint)
        self.assertTrue(self.reserve(endpoint)["reserved"])
        with self.assertRaises(ValueError):
            state.guard(self.db, endpoint["id"], 1, dict(self.grant, writable=False))

    def test_full_to_ask_downgrade_requires_manual_exact_approval(self):
        endpoint = self.create()
        self.reserve(endpoint)
        self.db.execute("UPDATE policy SET mode='ask'")
        endpoint = state.status(self.db, endpoint["id"])
        self.assertEqual(endpoint["approval_stage"], "intent")
        with self.assertRaises(ValueError): state.guard(self.db, endpoint["id"], 1)
        self.approve(endpoint)
        state.guard(self.db, endpoint["id"], 1)

    def test_pending_limit_includes_full_unreserved_endpoints(self):
        state.set_policy(self.db, dict(self.settings, max_pending_endpoints=1))
        self.create()
        with self.assertRaises(ValueError): self.create(transfer=str(uuid4()))

    def test_fresh_file_worker_attestation_not_helper_installation(self):
        endpoint = self.create()
        self.db.execute("DELETE FROM large_file_workers")
        self.assertEqual(self.reserve(endpoint)["reason"], "file_worker_unavailable")
        state.worker_seen(self.db, self.generation, self.now)
        self.assertTrue(state.info(self.db, self.now)["file_worker_fresh"])
        self.assertFalse(state.info(self.db, self.now + 5)["file_worker_fresh"])
        self.assertFalse(state.info(self.db, self.now - 1)["file_worker_fresh"])
        new_generation = str(uuid4())
        resources.worker_start(self.db, new_generation, "linux", now=self.now)
        self.assertFalse(state.info(self.db, self.now)["file_worker_fresh"])
        with self.assertRaises(ValueError): state.worker_seen(self.db, self.generation, self.now)

    def test_worker_stopped_immediately_invalidates_file_feature(self):
        resources.worker_stopped(self.db, self.generation, now=self.now)
        self.assertFalse(state.info(self.db, self.now)["file_worker_fresh"])

    def test_volume_and_global_reservations_count_import_twice_on_same_volume(self):
        endpoint = self.create("import", source_digest="a" * 64)
        self.assertEqual(self.reserve(endpoint, "import", free=17, same=True)["reason"], "volume_space")
        self.assertTrue(self.reserve(endpoint, "import", free=18, same=True)["reserved"])
        self.assertEqual(state.status(self.db, endpoint["id"])["reserved_bytes"], 18)
        again = self.create(size=1, transfer=str(uuid4()))
        self.assertEqual(self.reserve(again, free=18)["reason"], "volume_space")
        state.set_policy(self.db, dict(self.settings, max_reserved_bytes=18))
        self.assertEqual(self.reserve(again)["reason"], "disk_budget")

    def test_reservation_idempotency_pins_volume_id_and_does_not_require_new_space(self):
        endpoint = self.create()
        self.assertTrue(self.reserve(endpoint)["reserved"])
        self.assertTrue(self.reserve(endpoint, free=0)["reserved"])
        volumes = [{"role": "spool", "id": "new-volume", "free_bytes": 999}]
        with self.assertRaises(ValueError): state.reserve(self.db, endpoint["id"], 1, volumes)

    def test_active_endpoint_limit_and_margin_are_atomic(self):
        state.set_policy(self.db, dict(self.settings, max_active_endpoints=1, margin_bytes=5))
        endpoint = self.create()
        self.assertEqual(self.reserve(endpoint, free=13)["reason"], "volume_space")
        self.assertTrue(self.reserve(endpoint, free=14)["reserved"])
        other = self.create(transfer=str(uuid4()))
        self.assertEqual(self.reserve(other)["reason"], "endpoint_limit")

    def test_guard_forbids_unreserved_io(self):
        endpoint = self.create()
        with self.assertRaises(ValueError): state.guard(self.db, endpoint["id"], 1)

    def test_large_metadata_over_legacy_limit_and_bounded_pagination(self):
        size = 17 * state.BLOCK_SIZE + 3
        endpoint, attempt, header, records = self.prepared(size)
        self.assertGreater(header["size"], 64 * 1024 * 1024)
        first = state.block_page(self.db, endpoint["id"], limit=7)
        self.assertEqual(len(first["blocks"]), 7)
        self.assertEqual(first["next_offset"], 7)
        final = state.block_page(self.db, endpoint["id"], 14, limit=7)
        self.assertEqual(final["blocks"], records[14:])
        self.assertIsNone(final["next_offset"])
        self.assertEqual(state.status(self.db, endpoint["id"])["verified_bytes"], size)

    def test_empty_file_uses_budget_one_and_empty_header_digests(self):
        endpoint, attempt, header, records = self.prepared(0)
        self.assertEqual(records, [])
        self.assertEqual(endpoint["spec"]["max_bytes"], 1)
        self.assertEqual(header["sha256"], sha256(b"").hexdigest())

    def test_checkpoint_is_immutable_and_requires_attempt_owner(self):
        endpoint, attempt, header, records = self.prepared()
        record = records[0]
        self.assertEqual(state.checkpoint(self.db, endpoint["id"], 1, attempt, **record), record)
        with self.assertRaises(ValueError): state.checkpoint(self.db, endpoint["id"], 1, str(uuid4()), **record)
        with self.assertRaises(ValueError): state.checkpoint(self.db, endpoint["id"], 1, attempt, 0, 9, "e" * 64)
        with self.assertRaises(ValueError): state.checkpoint(self.db, endpoint["id"], 1, attempt, 1, 9, "e" * 64)
        self.assertEqual(state.status(self.db, endpoint["id"])["verified_blocks"], 1)

    def test_source_ready_rejects_incomplete_or_different_manifest(self):
        endpoint, attempt, header, _ = self.prepared()
        self.assertEqual(state.source_ready(self.db, endpoint["id"], 1, attempt, header), header)
        changed = dict(header, sha256="d" * 64)
        with self.assertRaises(ValueError): state.source_ready(self.db, endpoint["id"], 1, attempt, changed)
        self.db.execute("UPDATE large_file_endpoints SET header=NULL WHERE id=?", (endpoint["id"],))
        self.db.execute("DELETE FROM large_file_blocks WHERE endpoint_id=?", (endpoint["id"],))
        with self.assertRaises(ValueError): state.source_ready(self.db, endpoint["id"], 1, attempt, header)

    def test_import_requires_separate_exact_content_approval_and_replays_pending(self):
        source, attempt, header, records = self.prepared()
        self.db.execute("UPDATE policy SET mode='ask'")
        endpoint = self.create("import", source_digest=source["request_sha256"], mode="ask")
        with self.assertRaises(ValueError): state.bind_manifest(self.db, endpoint["id"], header, "ask")
        endpoint = self.approve(endpoint);self.reserve(endpoint, "import")
        endpoint = state.bind_manifest(self.db, endpoint["id"], header, "ask")
        self.assertEqual(endpoint["approval_stage"], "content")
        self.assertEqual(state.bind_manifest(self.db, endpoint["id"], header, "ask"), endpoint)
        with self.assertRaises(ValueError): state.checkpoint(self.db, endpoint["id"], 1, None, **records[0])
        endpoint = self.approve(endpoint)
        state.checkpoint(self.db, endpoint["id"], 1, None, **records[0])

    def test_import_header_binds_export_request_digest_not_content_hash(self):
        source, attempt, header, records = self.prepared()
        endpoint = self.create("import", source_digest=header["sha256"])
        self.reserve(endpoint, "import")
        with self.assertRaises(ValueError): state.bind_manifest(self.db, endpoint["id"], header, "full")

    def test_published_receipt_redacts_target_identity_and_has_no_future_approval(self):
        endpoint, header, records = self.receiving()
        for record in records: state.checkpoint(self.db, endpoint["id"], 1, None, **record)
        attempt = str(uuid4())
        state.begin_attempt(self.db, endpoint["id"], 1, attempt, "commit")
        state.commit_publication(self.db, endpoint["id"], 1, attempt)
        receipt = dict(header, import_id=endpoint["id"], published=True, target_identity={"dev": 42, "ino": 99})
        result = state.published(self.db, endpoint["id"], 1, attempt, receipt)
        self.assertNotIn("target_identity", result["receipt"])
        self.assertEqual(state.details(self.db, endpoint["id"])["receipt"], receipt)
        state.finish_attempt(self.db, attempt, True, "completed")
        self.db.execute("UPDATE policy SET mode='ask'")
        self.assertIsNone(state.status(self.db, endpoint["id"])["approval_stage"])
        self.assertEqual(state.status(self.db, endpoint["id"])["state"], "published")

    def publication(self):
        endpoint, header, records = self.receiving()
        for record in records: state.checkpoint(self.db, endpoint["id"], 1, None, **record)
        attempt = str(uuid4())
        state.begin_attempt(self.db, endpoint["id"], 1, attempt, "commit")
        receipt = dict(header, import_id=endpoint["id"], published=True, target_identity={"dev": 42, "ino": 99})
        return endpoint, attempt, receipt

    def test_stop_before_publication_permit_prevents_publication(self):
        endpoint, attempt, receipt = self.publication()
        state.stop(self.db, endpoint["id"])
        with self.assertRaises(ValueError): state.commit_publication(self.db, endpoint["id"], 1, attempt)
        with self.assertRaises(ValueError): state.published(self.db, endpoint["id"], 1, attempt, receipt)
        result = state.status(self.db, endpoint["id"])
        self.assertFalse(result["publication_committed"])
        self.assertIsNone(result["receipt"])
        self.assertEqual(result["reserved_bytes"], 18)

    def test_stop_after_permit_records_only_original_late_receipt(self):
        endpoint, attempt, receipt = self.publication()
        permit = state.commit_publication(self.db, endpoint["id"], 1, attempt)
        self.assertTrue(permit["publication_committed"])
        self.assertEqual(permit["publication_epoch"], 1)
        stopped = state.stop(self.db, endpoint["id"])
        with self.assertRaises(ValueError): state.guard(self.db, endpoint["id"], stopped["epoch"])
        result = state.published(self.db, endpoint["id"], 1, attempt, receipt)
        self.assertEqual(result["state"], "published")
        self.assertEqual(result["epoch"], stopped["epoch"])
        self.assertEqual(result["reserved_bytes"], 18)
        self.assertEqual(state.published(self.db, endpoint["id"], 1, attempt, receipt), result)
        with self.assertRaises(ValueError): state.published(self.db, endpoint["id"], 1, str(uuid4()), receipt)
        with self.assertRaises(ValueError): state.published(self.db, endpoint["id"], stopped["epoch"], attempt, receipt)

    def test_revoke_after_permit_does_not_erase_possible_publication_or_free_disk(self):
        endpoint, attempt, receipt = self.publication()
        state.commit_publication(self.db, endpoint["id"], 1, attempt)
        state.revoke_root(self.db, "incoming")
        self.db.execute("UPDATE policy SET mode='ask'")
        state.set_policy(self.db, dict(self.settings, enabled=False))
        result = state.published(self.db, endpoint["id"], 1, attempt, receipt)
        self.assertEqual(result["state"], "published")
        self.assertEqual(result["reserved_bytes"], 18)
        self.assertIsNone(result["approval_stage"])
        with self.assertRaises(ValueError): state.guard(self.db, endpoint["id"], result["epoch"])

    def test_publication_permit_binds_exact_attempt_header_and_complete_blocks(self):
        endpoint, attempt, receipt = self.publication()
        with self.assertRaises(ValueError): state.commit_publication(self.db, endpoint["id"], 1, str(uuid4()))
        with self.assertRaises(ValueError): state.published(self.db, endpoint["id"], 1, attempt, receipt)
        self.db.execute("DELETE FROM large_file_blocks WHERE endpoint_id=?", (endpoint["id"],))
        with self.assertRaises(ValueError): state.commit_publication(self.db, endpoint["id"], 1, attempt)
        state.checkpoint(self.db, endpoint["id"], 1, attempt, 0, 9, "1".zfill(64))
        state.commit_publication(self.db, endpoint["id"], 1, attempt)
        original_header = receipt.copy()
        for key in ("import_id", "published", "target_identity"): original_header.pop(key)
        changed = dict(original_header, sha256="e" * 64)
        self.db.execute("UPDATE large_file_endpoints SET header=? WHERE id=?", (state.canonical(changed), endpoint["id"]))
        with self.assertRaises(ValueError): state.published(self.db, endpoint["id"], 1, attempt, dict(receipt, sha256="e" * 64))

    def test_resume_new_permit_preserves_old_attempt_evidence(self):
        endpoint, attempt, receipt = self.publication()
        state.commit_publication(self.db, endpoint["id"], 1, attempt)
        state.stop(self.db, endpoint["id"])
        state.finish_attempt(self.db, attempt, True, "stopped")
        resumed = state.resume(self.db, endpoint["id"], 2, "full")
        next_attempt = str(uuid4())
        state.begin_attempt(self.db, endpoint["id"], resumed["epoch"], next_attempt, "commit")
        result = state.commit_publication(self.db, endpoint["id"], resumed["epoch"], next_attempt)
        self.assertEqual(result["publication_epoch"], 3)
        rows = self.db.execute("SELECT epoch,attempt_id FROM large_file_publications WHERE endpoint_id=? ORDER BY epoch", (endpoint["id"],)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, attempt), (3, next_attempt)])
        self.assertEqual(state.commit_publication(self.db, endpoint["id"], 1, attempt)["publication_epoch"], 3)
        state.published(self.db, endpoint["id"], 3, next_attempt, receipt)
        self.assertEqual(state.status(self.db, endpoint["id"])["reserved_bytes"], 18)

    def test_publication_receipt_cannot_change_immutable_identity(self):
        endpoint, attempt, receipt = self.publication()
        state.commit_publication(self.db, endpoint["id"], 1, attempt)
        state.published(self.db, endpoint["id"], 1, attempt, receipt)
        with self.assertRaises(ValueError):
            state.published(self.db, endpoint["id"], 1, attempt, dict(receipt, target_identity={"dev": 42, "ino": 100}))

    def test_publication_permit_is_in_callers_transaction_and_rolls_back(self):
        endpoint, attempt, receipt = self.publication()
        self.db.execute("SAVEPOINT before_publication")
        state.commit_publication(self.db, endpoint["id"], 1, attempt)
        self.assertTrue(self.db.in_transaction)
        self.db.execute("ROLLBACK TO before_publication")
        self.db.execute("RELEASE before_publication")
        self.assertFalse(state.status(self.db, endpoint["id"])["publication_committed"])
        with self.assertRaises(ValueError): state.published(self.db, endpoint["id"], 1, attempt, receipt)
        self.assertEqual(state.status(self.db, endpoint["id"])["reserved_bytes"], 18)

    def test_publication_barrier_obeys_current_ask_approval_and_revocation(self):
        endpoint, attempt, receipt = self.publication()
        self.db.execute("UPDATE policy SET mode='ask'")
        with self.assertRaises(ValueError): state.commit_publication(self.db, endpoint["id"], 1, attempt)
        endpoint = self.approve(state.status(self.db, endpoint["id"]))
        endpoint = self.approve(endpoint)
        state.revoke_root(self.db, "incoming")
        with self.assertRaises(ValueError): state.commit_publication(self.db, endpoint["id"], endpoint["epoch"], attempt)
        self.assertFalse(state.status(self.db, endpoint["id"])["publication_committed"])

    def test_stop_rejects_old_io_and_resume_waits_for_scoped_cleanup(self):
        endpoint = self.create();self.reserve(endpoint)
        attempt = str(uuid4())
        state.begin_attempt(self.db, endpoint["id"], 1, attempt, "prepare")
        stopped = state.stop(self.db, endpoint["id"])
        self.assertEqual(stopped["epoch"], 2)
        self.assertEqual(stopped["reserved_bytes"], 9)
        with self.assertRaises(ValueError): state.checkpoint(self.db, endpoint["id"], 1, attempt, 0, 9, "a" * 64)
        with self.assertRaises(ValueError): state.resume(self.db, endpoint["id"], 2, "full")
        state.finish_attempt(self.db, attempt, False, "unconfirmed")
        with self.assertRaises(ValueError): state.cleanup(self.db, endpoint["id"], 2, True, True)
        state.reconcile_attempt_cleanup(self.db, attempt)
        resumed = state.resume(self.db, endpoint["id"], 2, "full")
        self.assertEqual(resumed["epoch"], 3)
        self.assertEqual(resumed["reserved_bytes"], 9)
        with self.assertRaises(ValueError): state.checkpoint(self.db, endpoint["id"], 3, attempt, 0, 9, "a" * 64)

    def test_reconcile_does_not_invent_outcome_or_release_disk(self):
        endpoint = self.create();self.reserve(endpoint)
        attempt = str(uuid4());state.begin_attempt(self.db, endpoint["id"], 1, attempt, "prepare")
        with self.assertRaises(ValueError): state.reconcile_attempt_cleanup(self.db, attempt)
        state.finish_attempt(self.db, attempt, False, "unconfirmed")
        with self.assertRaises(ValueError): state.finish_attempt(self.db, attempt, True, "unconfirmed")
        state.reconcile_attempt_cleanup(self.db, attempt)
        row = self.db.execute("SELECT outcome,cleanup_confirmed FROM large_file_attempts WHERE id=?", (attempt,)).fetchone()
        self.assertEqual(tuple(row), ("unconfirmed", 1))
        self.assertEqual(state.status(self.db, endpoint["id"])["reserved_bytes"], 9)
        with self.assertRaises(ValueError): state.finish_attempt(self.db, attempt, True, "completed")

    def test_resume_keeps_verified_blocks_and_rejects_state_history_change(self):
        endpoint, attempt, header, records = self.prepared()
        state.finish_attempt(self.db, attempt, True, "completed")
        stopped = state.stop(self.db, endpoint["id"])
        self.db.execute("UPDATE resource_identity SET state_id=?", (str(uuid4()),))
        with self.assertRaises(ValueError): state.resume(self.db, endpoint["id"], stopped["epoch"], "full")
        self.db.execute("UPDATE resource_identity SET state_id=?", (endpoint["spec"]["expected_state_id"],))
        resumed = state.resume(self.db, endpoint["id"], stopped["epoch"], "full")
        self.assertEqual(state.get_block(self.db, endpoint["id"], 0), records[0])
        self.assertEqual(resumed["state"], "ready")

    def test_expiry_and_grant_revocation_never_free_disk_by_themselves(self):
        endpoint = self.create();self.reserve(endpoint)
        with self.assertRaises(ValueError): state.guard(self.db, endpoint["id"], 1, now=self.now + 4000)
        self.assertEqual(state.status(self.db, endpoint["id"], now=self.now + 4000)["reserved_bytes"], 9)
        state.revoke_root(self.db, "documents")
        revoked = state.status(self.db, endpoint["id"])
        self.assertEqual(revoked["state"], "revoked")
        self.assertEqual(revoked["reserved_bytes"], 9)
        with self.assertRaises(ValueError): state.guard(self.db, endpoint["id"], revoked["epoch"])

    def test_cleanup_requires_process_and_physical_confirmation_and_is_durable(self):
        endpoint = self.create();self.reserve(endpoint)
        stopped = state.stop(self.db, endpoint["id"])
        for process, deleted in ((False, True), (True, False), (1, True)):
            with self.subTest(process=process, deleted=deleted), self.assertRaises(ValueError):
                state.cleanup(self.db, endpoint["id"], stopped["epoch"], process, deleted)
        result = state.cleanup(self.db, endpoint["id"], stopped["epoch"], True, True)
        self.assertEqual(result["reserved_bytes"], 0)
        self.assertEqual(result["state"], "cleaned")
        self.assertIsNone(result["approval_stage"])
        self.assertEqual(state.cleanup(self.db, endpoint["id"], result["epoch"], True, True), result)

    def test_attempt_uuid_cannot_bind_another_endpoint_or_role(self):
        first = self.create();self.reserve(first)
        second = self.create(transfer=str(uuid4()));self.reserve(second)
        attempt = str(uuid4());state.begin_attempt(self.db, first["id"], 1, attempt, "prepare")
        with self.assertRaises(ValueError): state.begin_attempt(self.db, second["id"], 1, attempt, "prepare")
        with self.assertRaises(ValueError): state.begin_attempt(self.db, second["id"], 1, str(uuid4()), "commit")

    def test_two_connections_serialize_reservations_and_never_overbook(self):
        with tempfile.TemporaryDirectory() as directory:
            first = sqlite3.connect(directory + "/metadata.sqlite", timeout=0)
            second = sqlite3.connect(directory + "/metadata.sqlite", timeout=0)
            try:
                first.executescript(resources.SCHEMA + state.SCHEMA +
                                    "CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('full');")
                first.execute("BEGIN IMMEDIATE")
                generation = str(uuid4())
                resources.worker_start(first, generation, "linux", now=self.now)
                state.worker_seen(first, generation, now=self.now)
                state.set_policy(first, dict(self.settings, max_active_endpoints=1))
                spec = dict(self.spec(), expected_state_id=resources.state_id(first))
                endpoint = state.request(first, spec, self.grant, self.identity(), [], "full", self.now)
                self.assertTrue(state.reserve(first, endpoint["id"], 1, self.volumes(), self.now)["reserved"])
                with self.assertRaises(sqlite3.OperationalError): second.execute("BEGIN IMMEDIATE")
                first.commit()
                second.execute("BEGIN IMMEDIATE")
                spec = dict(spec, transfer_id=str(uuid4()))
                other = state.request(second, spec, self.grant, self.identity(), [], "full", self.now)
                self.assertEqual(state.reserve(second, other["id"], 1, self.volumes(), self.now)["reason"], "endpoint_limit")
                second.rollback()
            finally:
                first.close();second.close()


    def test_stopped_full_to_ask_requires_each_exact_approval_without_resuming(self):
        for role in ("export", "import"):
            with self.subTest(role=role):
                self.db.execute("SAVEPOINT role")
                try:
                    endpoint = self.create() if role == "export" else self.receiving()[0]
                    if role == "export":self.reserve(endpoint)
                    stopped = state.stop(self.db, endpoint["id"], self.now)
                    claims = [tuple(row) for row in self.db.execute("SELECT * FROM large_file_claims")]
                    self.db.execute("UPDATE policy SET mode='ask'")
                    stages = ("intent",) if role == "export" else ("intent", "content")
                    for stage in stages:
                        current = state.status(self.db, endpoint["id"], self.now)
                        self.assertEqual(current["approval_stage"], stage)
                        with self.assertRaisesRegex(ValueError, "exact current"):
                            state.decide(self.db, endpoint["id"], True, "0" * 64, self.now)
                        approved = state.decide(self.db, endpoint["id"], True, current["approval_sha256"], self.now)
                        self.assertEqual((approved["state"], approved["epoch"]), ("stopped", stopped["epoch"]))
                        with self.assertRaises(ValueError):
                            state.guard(self.db, endpoint["id"], stopped["epoch"], now=self.now)
                    self.assertIsNone(approved["approval_stage"])
                    self.assertEqual([tuple(row) for row in self.db.execute("SELECT * FROM large_file_claims")], claims)
                    with self.assertRaises(ValueError):
                        state.decide(self.db, endpoint["id"], True, current["approval_sha256"], self.now)
                finally:
                    self.db.execute("ROLLBACK TO role");self.db.execute("RELEASE role")

    def test_stopped_reapproval_rejects_policy_expiry_history_and_stale_content_digest(self):
        for role in ("export", "import"):
            with self.subTest(role=role):
                self.db.execute("SAVEPOINT role")
                try:
                    endpoint = self.create() if role == "export" else self.receiving()[0]
                    stopped = state.stop(self.db, endpoint["id"], self.now)
                    self.db.execute("UPDATE policy SET mode='ask'")
                    current = state.status(self.db, endpoint["id"], self.now)
                    for condition in ("policy", "expiry", "history"):
                        self.db.execute("SAVEPOINT rejected")
                        try:
                            if condition == "policy":state.set_policy(self.db, dict(self.settings, enabled=False))
                            if condition == "history":self.db.execute("UPDATE resource_identity SET state_id=?", (str(uuid4()),))
                            now = current["spec"]["expires_at"] if condition == "expiry" else self.now
                            with self.subTest(condition=condition), self.assertRaises(ValueError):
                                state.decide(self.db, endpoint["id"], True, current["approval_sha256"], now)
                            self.assertEqual(state.details(self.db, endpoint["id"])["epoch"], stopped["epoch"])
                        finally:
                            self.db.execute("ROLLBACK TO rejected");self.db.execute("RELEASE rejected")
                    state.decide(self.db, endpoint["id"], True, current["approval_sha256"], self.now)
                    if role == "import":
                        with self.assertRaisesRegex(ValueError, "exact current"):
                            state.decide(self.db, endpoint["id"], True, current["approval_sha256"], self.now)
                finally:
                    self.db.execute("ROLLBACK TO role");self.db.execute("RELEASE role")

    def test_inflight_stopping_and_terminal_endpoints_cannot_be_reapproved(self):
        for role in ("export", "import"):
            with self.subTest(role=role):
                self.db.execute("SAVEPOINT role")
                try:
                    endpoint = self.create() if role == "export" else self.receiving()[0]
                    if role == "export":self.reserve(endpoint)
                    attempt = str(uuid4())
                    state.begin_attempt(self.db, endpoint["id"], 1, attempt, "prepare" if role == "export" else "commit", self.now)
                    stopped = state.stop(self.db, endpoint["id"], self.now)
                    self.assertEqual(stopped["state"], "stopping")
                    self.db.execute("UPDATE policy SET mode='ask'")
                    current = state.status(self.db, endpoint["id"], self.now)
                    with self.assertRaises(ValueError):
                        state.decide(self.db, endpoint["id"], True, current["approval_sha256"], self.now)
                    state.finish_attempt(self.db, attempt, True, "stopped")
                    denied = state.decide(self.db, endpoint["id"], False, now=self.now)
                    self.assertEqual(denied["state"], "denied")
                    with self.assertRaises(ValueError):
                        state.decide(self.db, endpoint["id"], True, current["approval_sha256"], self.now)
                finally:
                    self.db.execute("ROLLBACK TO role");self.db.execute("RELEASE role")


if __name__ == "__main__":
    unittest.main()
