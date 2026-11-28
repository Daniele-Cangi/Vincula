# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resource-aware native manifests and approvals, using temporary SQLite state."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

import direct_execution as direct
import resource_policy as resources


class ResourceProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="vincula-native-resources-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.db = self.database()
        self.generation = self.start(self.db)
        self.demand = {"cpu_millis": 1000, "memory_bytes": 4096, "gpus": []}

    def database(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('full');
            CREATE TABLE jobs(id TEXT PRIMARY KEY,program TEXT,argv TEXT,state TEXT,exit_code INTEGER,
                              created REAL,finished REAL);
            CREATE TABLE worker_environment(id INTEGER PRIMARY KEY,path TEXT,pathext TEXT);
        """ + direct.SCHEMA + resources.SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO worker_environment VALUES(1,?,?)", (str(Path(sys.executable).parent), ".EXE"))
        direct.set_enabled(db, True)
        return db

    def start(self, db):
        generation = str(uuid4())
        resources.worker_start(db, generation, "windows" if sys.platform == "win32" else "linux")
        resources.heartbeat(db, generation, {"observed_at": time.time(), "cpu_millis": 4000,
            "cpu_percent": 0, "memory_total_bytes": 1_000_000, "memory_available_bytes": 900_000, "gpus": []})
        return generation

    def queue(self, *, db=None, key=None, access="full", **changes):
        db = self.db if db is None else db
        key = str(uuid4()) if key is None else key
        kwargs = dict(argv=[sys.executable, "-c", "print('resource')"], cwd=str(self.home),
                      protocol_version=2, resources=deepcopy(self.demand))
        kwargs.update(changes)
        return key, direct.queue(db, access, key, **kwargs)

    def claimed(self, key):
        self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
        self.assertTrue(resources.try_claim(self.db, key, self.generation))

    def test_full_has_no_approval_roundtrip_but_needs_the_owned_resource_claim(self):
        key, manifest = self.queue()
        self.assertEqual(self.db.execute("SELECT state,program FROM jobs WHERE id=?", (key,)).fetchone()[:],
                         ("queued", "@process-v2"))
        self.assertEqual(manifest["destination_state_id"], resources.state_id(self.db))
        self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
        with self.assertRaisesRegex(ValueError, "matching owned admission"):
            direct.launch_spec(self.db, key, {})
        self.assertTrue(resources.try_claim(self.db, key, self.generation))
        spec = direct.launch_spec(self.db, key, {"BASE": "value"})
        self.assertEqual(set(spec), {"argv", "cwd", "env", "stdin_text", "timeout_seconds"})
        self.assertEqual(spec["env"], {"BASE": "value"})
        direct.record_outcome(self.db, key, "completed", 17, "exit", True)
        self.assertEqual(direct.result(self.db, key)["outcome"]["program_exit_code"], 17)
        self.assertEqual(direct.result(self.db, key)["outcome"]["runtime_protocol_version"], 2)

    def test_ask_digest_binds_resources_and_destination_before_any_launch(self):
        self.db.execute("UPDATE policy SET mode='ask'")
        key, manifest = self.queue(access="ask")
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE id=?", (key,)).fetchone()[0], "pending")
        altered = deepcopy(manifest)
        altered["original"]["resources"]["cpu_millis"] += 1
        with self.assertRaisesRegex(ValueError, "exact process manifest"):
            direct.approve(self.db, key, direct.digest(altered))
        direct.approve(self.db, key, manifest["sha256"])
        self.claimed(key)
        self.assertEqual(direct.launch_spec(self.db, key, {})["argv"], manifest["resolved"]["argv"])

    def test_retry_preserves_original_resolution_and_rejects_changed_resources_or_state(self):
        state = resources.state_id(self.db)
        key, manifest = self.queue(expected_state_id=state)
        self.db.execute("DELETE FROM resource_worker")
        direct.set_enabled(self.db, False)
        self.assertEqual(self.queue(key=key, expected_state_id=state)[1], manifest)
        with self.assertRaisesRegex(ValueError, "different process request"):
            self.queue(key=key, resources={"cpu_millis": 1001, "memory_bytes": 4096, "gpus": []})
        with self.assertRaisesRegex(ValueError, "durable state changed"):
            self.queue(key=key, expected_state_id=str(uuid4()))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_origin_resume_cannot_enqueue_after_destination_database_was_replaced(self):
        original_state = resources.state_id(self.db)
        other = self.database()
        self.start(other)
        self.assertNotEqual(resources.state_id(other), original_state)
        with self.assertRaisesRegex(ValueError, "durable state changed"):
            self.queue(db=other, expected_state_id=original_state)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_new_helper_without_live_worker_cannot_queue_a_resource_process(self):
        self.db.execute("DELETE FROM resource_worker")
        with self.assertRaisesRegex(ValueError, "worker_unavailable"):
            self.queue()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_damaged_resource_ledger_is_not_silently_treated_as_legacy(self):
        key, _ = self.queue()
        self.claimed(key)
        self.db.execute("DELETE FROM resource_requests WHERE id=?", (key,))
        with self.assertRaisesRegex(ValueError, "matching owned admission"):
            direct.launch_spec(self.db, key, {})

    def test_changed_resources_with_recomputed_checksum_still_need_matching_authorization(self):
        key, original = self.queue()
        self.claimed(key)
        changed = deepcopy(original)
        for part in ("original", "resolved"):
            changed[part]["resources"]["memory_bytes"] += 1
        changed["sha256"] = direct.digest(changed)
        self.db.execute("UPDATE direct_process_jobs SET manifest=? WHERE id=?", (json.dumps(changed), key))
        with self.assertRaisesRegex(ValueError, "no matching destination authorization"):
            direct.launch_spec(self.db, key, {})

    def test_portable_plan_validation_preserves_windows_paths_for_the_destination(self):
        values = dict(argv=["python.exe"], cwd="C:\\Inputs", resources=self.demand, protocol_version=2)
        self.assertEqual(direct.request(**values, _platform="portable")["cwd"], "C:\\Inputs")
        self.assertEqual(direct.request(**values, _platform="windows")["cwd"], "C:\\Inputs")
        with self.assertRaisesRegex(ValueError, "absolute"):
            direct.request(**values, _platform="linux")
        values.update(env={"Path": "one", "PATH": "two"})
        direct.request(**values, _platform="portable")
        with self.assertRaisesRegex(ValueError, "unique"):
            direct.request(**values, _platform="windows")

    def test_protocol_one_rejects_resource_fields_and_keeps_its_existing_wire_shape(self):
        with self.assertRaisesRegex(ValueError, "version 2"):
            direct.request(["python"], resources=self.demand)
        value = direct.request(["python"])
        self.assertEqual(set(value), {"argv", "cwd", "env", "stdin_text", "timeout_seconds"})


if __name__ == "__main__":
    unittest.main()
