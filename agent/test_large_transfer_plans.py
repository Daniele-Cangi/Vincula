# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Immutable transfer handles and journal recovery; no live nodes."""
from copy import deepcopy
from contextlib import closing
import sqlite3
import unittest
from uuid import uuid4, uuid5, UUID
import large_transfer_plans as plans
import plan_journal


class LargeTransferPlansTests(unittest.TestCase):
    def setUp(self):
        self.source, self.destination, self.authority, self.key, self.first, self.second = [str(uuid4()) for _ in range(6)]
        self.request = {"source": {"node_id": self.source, "root": "data", "path": "nested/CON"},
                        "destination": {"node_id": self.destination, "root": "results", "path": "résultats.bin"},
                        "max_bytes": 1024**3, "ttl_seconds": 86400}

    def make(self):
        return plans.make_manifest(self.key, self.authority, self.request, self.first, self.second, 1000)

    def test_metadata_preserves_destination_path_without_coordinator_os_rules(self):
        value = self.make()
        self.assertEqual(value["request"], self.request)
        self.assertEqual(value["plan"]["expires_at"], 87400)
        self.request["source"]["path"] = "changed"
        self.assertEqual(value["request"]["source"]["path"], "nested/CON")

    def test_endpoint_identity_includes_authority_and_role(self):
        value = self.make()
        export = value["plan"]["source_id"]
        self.assertEqual(export, str(uuid5(UUID(self.key), f"large-file:{self.authority}:export")))
        self.assertNotEqual(export, value["plan"]["destination_id"])
        self.assertNotEqual(export, plans.endpoint_id(self.key, self.destination, "export"))
        self.assertNotEqual(export, str(uuid5(UUID(self.key), f"execution:{self.source}:0")))

    def test_content_intent_and_state_pins_are_distinct_on_both_endpoints(self):
        value = self.make()
        source = plans.endpoint_spec(value, "export")
        destination = plans.endpoint_spec(value, "import", "a" * 64)
        self.assertEqual(source["expected_state_id"], self.first)
        self.assertEqual(destination["expected_state_id"], self.second)
        self.assertIsNone(source["source_sha256"])
        self.assertEqual(destination["source_sha256"], "a" * 64)
        self.assertEqual(source["source"], destination["source"])
        self.assertEqual(source["expires_at"], destination["expires_at"])

    def test_invalid_paths_and_private_staging_names_never_enter_plan(self):
        for path in ("/file", "../file", "a//b", "a/./b", "a/../b", "a\\b", "C:/data", "a ", "a.",
                     "a/.VINCULA-IMPORT-large-x", "a\0b", "x" * 241, "é" * 121, "a?b"):
            request = deepcopy(self.request)
            request["destination"]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                plans.normalize_request(request)

    def test_invalid_budgets_ttls_fields_and_implicit_nodes_are_rejected(self):
        for field, values in (("max_bytes", (0, -1, True, 1024**4 + 1, 1.5)),
                              ("ttl_seconds", (0, 59, True, 30 * 86400 + 1, float("nan")))):
            for invalid in values:
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    plans.normalize_request(self.request | {field: invalid})
        for endpoint in (self.request["source"] | {"host": "untrusted"}, self.request["source"] | {"node_id": "ubuntu"},
                         self.request["source"] | {"root": "../data"}, self.request["source"] | {"node_id": self.destination}):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                plans.normalize_request(self.request | {"source": endpoint})

    def test_changed_assignment_state_and_deadline_fail_validation(self):
        original = self.make()
        changes = [lambda x: x.update(version=True), lambda x: x.update(kind="batch"),
                   lambda x: x.update(origin=self.source), lambda x: x.update(extra=True),
                   lambda x: x["plan"].update(source_id=str(uuid4())),
                   lambda x: x["plan"].update(source_state_id="legacy"),
                   lambda x: x["plan"].update(block_size=1048576),
                   lambda x: x["plan"].update(expires_at=90000),
                   lambda x: x["plan"].update(created_at=float("inf"))]
        for change in changes:
            candidate = deepcopy(original)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                plans.validate_manifest(candidate)

    def test_recovery_preserves_authority_and_rejects_conflicts_or_corruption(self):
        original = self.make()
        record = plans.package(original)
        self.assertEqual(plans.select_replica(self.key, [None, record, deepcopy(record)]), original)
        other = plans.make_manifest(self.key, self.authority, self.request, str(uuid4()), self.second, 1000)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            plans.select_replica(self.key, [record, plans.package(other)])
        for bad in (record | {"sha256": "0" * 64}, record | {"extra": True}, record | {"sha256": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                plans.select_replica(self.key, [bad])
        with self.assertRaises(ValueError):
            plans.select_replica(str(uuid4()), [record])

    def test_journal_stores_immutably_and_checks_content_digest(self):
        with closing(sqlite3.connect(":memory:")) as db, db:
            db.executescript(plan_journal.SCHEMA)
            original = self.make()
            self.assertEqual(plan_journal.store(db, original)["state"], "stored")
            self.assertEqual(plan_journal.get(db, "large-file", self.key), plans.package(original))
            changed = plans.make_manifest(self.key, self.authority, self.request, str(uuid4()), self.second, 1000)
            self.assertEqual(plan_journal.store(db, changed)["state"], "conflict")
            self.assertEqual(plan_journal.get(db, "large-file", self.key), plans.package(original))
            db.execute("UPDATE plan_journal SET sha256=?", ("0" * 64,))
            self.assertEqual(plan_journal.get(db, "large-file", self.key), {"state": "invalid"})


if __name__ == "__main__":
    unittest.main()
