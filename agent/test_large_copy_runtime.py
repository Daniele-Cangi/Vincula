# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed metadata facade, with TEMP catalogs/SQLite and no transport."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


class LargeCopyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "receiver"
        self.addCleanup(patch.stopall)
        patch.object(jobs, "ROOT", self.root).start()
        patch.object(jobs, "DATABASE", self.root / "jobs.sqlite3").start()
        jobs.initialize()
        self.runtime = jobs.LOCAL_RUNTIME
        self.facade, self.state = jobs.large_copy_runtime, jobs.large_copy_state
        self.authority, self.source, self.destination, self.transfer = [str(uuid4()) for _ in range(4)]
        self.config = self.root / self.facade.SETTINGS_NAME
        self.registry = Path(self.temporary.name) / "service-nodes.json"
        family = "windows" if jobs.WINDOWS else "linux"
        self.document = {"local": self.authority, "nodes": [
            {"id": self.authority, "label": "Authority", "family": family},
            {"id": self.source, "label": "Source registered", "family": "windows"},
            {"id": self.destination, "label": "Target registered", "family": "linux"}]}
        self.registry.write_text(json.dumps(self.document))
        self.settings = {"version": 1, "node_id": self.authority, "registry_path": str(self.registry)}
        with jobs.transaction() as db:
            self.state_id = jobs.resource_policy.state_id(db)
            request = {"source": {"node_id": self.source, "root": "data", "path": "input.bin"},
                       "destination": {"node_id": self.destination, "root": "result", "path": "output.bin"},
                       "max_bytes": 128 * 1024 * 1024, "ttl_seconds": 3600}
            self.manifest = jobs.large_transfer_plans.make_manifest(self.transfer, self.authority, request,
                str(uuid4()), str(uuid4()), time.time())
            saved = jobs.plan_journal.store(db, self.manifest)
            self.manifest_sha = saved["sha256"]

    def configure(self):
        self.config.write_text(json.dumps(self.settings))

    def call(self, operation, **fields):
        return jobs.dispatch("copy-metadata", operation=operation, **fields)

    def prepare(self, **overrides):
        value = dict(transfer_id=self.transfer, expected_authority_state_id=self.state_id,
                     expected_manifest_sha256=self.manifest_sha,
                     proposed_endpoint_epochs={"source": 1, "destination": 1}, relay_budget_bytes=8 * 1024 * 1024)
        return self.call("prepare", **dict(value, **overrides))

    def pins(self, value):
        return dict(controller_id=value["controller_id"], expected_controller_epoch=value["controller_epoch"],
                    expected_authority_state_id=self.state_id)

    def test_missing_config_is_explicit_even_with_user_registry_environment(self):
        with patch.dict("os.environ", {"VINCULA_NODE_REGISTRY": str(self.registry)}):
            result = self.call("info")
        self.assertEqual(result["configuration"], "configuration_missing")
        self.assertFalse(result["operational"])
        self.assertFalse(result["launchable"])
        self.assertFalse(result["runtime_implemented"])
        self.assertFalse(result["policy"]["enabled"])
        self.assertEqual(result["policy_revision"], self.state.digest(result["policy"]))
        self.assertNotIn(str(self.registry), json.dumps(result))

    def test_prepare_requires_local_config_and_exact_authority_manifest(self):
        with self.assertRaises(FileNotFoundError): self.prepare()
        self.configure()
        for change in ({"expected_manifest_sha256": "0" * 64},
                       {"expected_authority_state_id": str(uuid4())}, {"transfer_id": str(uuid4())}):
            with self.subTest(change=change), self.assertRaises(ValueError): self.prepare(**change)
        value = self.prepare()
        self.assertEqual(value["phase"], "disabled")
        self.assertTrue(value["approval_required"])
        self.assertEqual(self.prepare()["controller_id"], value["controller_id"])
        self.assertFalse(value["enrollment_ready"])
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_copy_controllers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_copy_outbox WHERE phase='pending'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_file_claims").fetchone()[0], 0)

    def test_registered_offline_nodes_do_not_become_verified_routes(self):
        self.configure()
        result = self.call("info")
        self.assertEqual(set(result["registered_nodes"]), {self.authority, self.source, self.destination})
        self.assertFalse(result["provider_verified"])
        self.assertFalse(result["transport_verified"])
        self.prepare()
        self.document["nodes"] = self.document["nodes"][:-1]
        self.registry.write_text(json.dumps(self.document))
        with self.assertRaises(ValueError): self.prepare()

    def test_foreign_local_id_and_invalid_config_never_use_fallback(self):
        self.configure()
        for value in (dict(self.settings, node_id=str(uuid4())), dict(self.settings, version=True),
                      dict(self.settings, registry_path="relative.json"), dict(self.settings, extra="ignored?")):
            self.config.write_text(json.dumps(value))
            with self.subTest(value=value):
                self.assertEqual(self.call("info")["configuration"], "configuration_invalid")
                with self.assertRaises(ValueError): self.prepare()
        self.config.write_bytes(b" " * (self.facade.MAX_SETTINGS_BYTES + 1))
        self.assertEqual(self.call("info")["configuration"], "configuration_invalid")

    def test_wire_cannot_approve_attest_confirm_bindings_or_claim_cleanup(self):
        self.configure()
        value = self.prepare()
        for operation in ("decide", "set_policy", "worker_seen", "confirm_binding", "confirm_cleanup", "resume", "run"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.call(operation, controller_id=value["controller_id"])
        for extra in ({"mode": "full"}, {"local_node_id": self.authority}, {"verified": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError): self.prepare(**extra)
        with jobs.transaction() as db:
            self.assertEqual(len(self.state.pending(db)), 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM large_copy_decisions").fetchone()[0], 0)

    def test_stop_proves_only_never_dispatched_metadata_without_new_config(self):
        self.configure()
        value = self.prepare()
        self.config.unlink()
        stopped = self.call("stop", **self.pins(value))
        self.assertEqual(stopped["phase"], "stopped")
        self.assertEqual(stopped["cleanup_epoch"], stopped["controller_epoch"])
        self.assertFalse(stopped["operational"])
        repeated = self.call("stop", **self.pins(stopped))
        self.assertEqual(repeated["controller_epoch"], stopped["controller_epoch"])
        self.assertEqual(self.call("read", controller_id=value["controller_id"])["phase"], "stopped")

    def test_dispatched_outbox_never_accepts_wire_cleanup_confirmation(self):
        self.configure()
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")
            self.state.set_policy(db, dict(self.state.DEFAULT_POLICY, enabled=True))
        value = self.prepare()
        with jobs.transaction() as db:
            generation = str(uuid4())
            jobs.resource_policy.worker_start(db, generation, "windows" if jobs.WINDOWS else "linux")
            self.state.worker_seen(db, generation, expected_authority_state_id=self.state_id)
            self.state.binding_request(db, value["controller_id"], "source",
                expected_controller_epoch=value["controller_epoch"], expected_authority_state_id=self.state_id)
        stopped = self.call("stop", **self.pins(value))
        self.assertEqual(stopped["phase"], "stopping")
        self.assertIsNone(stopped["cleanup_epoch"])
        with self.assertRaises(ValueError): self.call("stop", **dict(self.pins(stopped), cleanup_confirmed=True))

    def test_incomplete_outbox_is_not_proof_of_no_dispatch(self):
        self.configure()
        value = self.prepare()
        with jobs.transaction() as db:
            db.execute("DELETE FROM large_copy_outbox WHERE controller_id=? AND side='source'", (value["controller_id"],))
        stopped = self.call("stop", **self.pins(value))
        self.assertEqual(stopped["phase"], "stopping")
        self.assertIsNone(stopped["cleanup_epoch"])

    def test_catalog_reads_are_outside_sqlite_transactions(self):
        self.configure()
        original = self.facade.catalog
        original_tx = jobs.transaction
        inside = []
        @contextmanager
        def tracked():
            inside.append(True)
            try:
                with original_tx() as db: yield db
            finally: inside.pop()
        def checked(runtime):
            self.assertFalse(inside)
            return original(runtime)
        with patch.object(jobs, "transaction", tracked), patch.object(self.facade, "catalog", checked):
            self.call("info")
            self.prepare()

    def test_helpers_are_installed_together_and_init_preserves_existing_data(self):
        for name in ("node_registry.py", "large_copy_state.py", "large_copy_runtime.py"):
            self.assertEqual((self.root / name).read_bytes(), Path(__file__).with_name(name).read_bytes())
        self.configure()
        value = self.prepare()
        jobs.initialize()
        reread = self.call("read", controller_id=value["controller_id"])
        self.assertEqual(reread["intent_sha256"], value["intent_sha256"])
        self.assertEqual(reread["authority_state_id"], self.state_id)

    def test_installed_isolated_cli_keeps_approval_local_and_does_not_launch(self):
        self.configure()
        value = self.prepare()
        code = ("import importlib.util,sys;from pathlib import Path;"
                "root=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location('installed_copy_qa',root/'jobs.py');"
                "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
                "module.ROOT=root;module.DATABASE=root/'jobs.sqlite3';sys.argv=['jobs.py',*sys.argv[2:]];module.main()")
        def cli(*args):
            result = subprocess.run([sys.executable, "-I", "-B", "-W", "error", "-c", code, str(self.root), *args],
                capture_output=True, text=True, timeout=15, check=True)
            return json.loads(result.stdout)
        pending = cli("large-copy-pending")
        self.assertEqual(pending["intent_sha256"], value["intent_sha256"])
        self.assertEqual(pending["manifest"], self.manifest)
        self.assertEqual(pending["manifest"]["request"]["source"]["path"], "input.bin")
        self.assertEqual(pending["manifest"]["request"]["destination"]["path"], "output.bin")
        self.assertEqual(pending["intent"]["relay_budget_bytes"], 8 * 1024 * 1024)
        approved = cli("large-copy-approve", value["controller_id"], "--sha256", value["intent_sha256"],
                       "--epoch", "1", "--state-id", self.state_id)
        self.assertFalse(approved["approval_required"])
        policy = self.root / "metadata-policy.json"
        policy.write_text(json.dumps(dict(self.state.DEFAULT_POLICY, enabled=True)))
        revision = self.call("info")["policy_revision"]
        self.assertTrue(cli("large-copy-policy", str(policy), "--expected-revision", revision)["policy"]["enabled"])
        query = self.root / "metadata-query.json"
        query.write_text(json.dumps({"operation": "read", "controller_id": value["controller_id"]}))
        result = cli("large-copy-metadata", str(query))
        self.assertEqual(result["phase"], "awaiting_worker")
        self.assertFalse(result["operational"])
        cli("large-copy-emergency-stop", value["controller_id"])
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM resource_claims WHERE released IS NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
