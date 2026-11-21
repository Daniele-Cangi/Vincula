# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise destination policy without touching the operator's catalog."""

from argparse import Namespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


class PermissionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for key, value in (("ROOT", root), ("DATABASE", root / "jobs.sqlite3")):
            change = patch.object(jobs, key, value)
            change.start()
            self.addCleanup(change.stop)
        jobs.initialize()
        self.register()

    def register(self):
        jobs.operator(Namespace(command="register", name="version", argv=[sys.executable, "--version"]))

    def test_decisions_and_mode_switches(self):
        first, second = str(uuid4()), str(uuid4())
        self.assertEqual(jobs.rpc("start", first, "version")["state"], "pending")
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")
        self.assertEqual(jobs.rpc("read", first)["state"], "pending")
        self.assertEqual(jobs.rpc("start", second, "version")["state"], "queued")
        with jobs.transaction() as db:
            jobs.set_mode(db, "ask")
            jobs.decide(db, first, False)
            jobs.decide(db, second, True)
        self.assertEqual(jobs.rpc("read", first)["state"], "cancelled")
        self.assertEqual(jobs.rpc("read", second)["state"], "queued")
        with jobs.transaction() as db:
            jobs.set_mode(db, "ask")
        self.assertEqual(jobs.rpc("read", second)["state"], "queued")
        self.assertEqual(jobs.rpc("start", first, "version")["state"], "cancelled")
        with self.assertRaises(ValueError):
            jobs.rpc("approve", first)
        self.register()
        self.assertEqual(jobs.rpc("read", second)["state"], "cancelled")

    def test_unregistered_and_id_conflict(self):
        key = str(uuid4())
        with self.assertRaises(ValueError):
            jobs.rpc("start", key, "unknown")
        jobs.rpc("start", key, "version")
        with self.assertRaises(ValueError):
            jobs.rpc("start", key, "other")
        self.assertEqual(jobs.rpc("stop", key)["state"], "cancelled")

    def test_stop_between_claim_and_launch(self):
        key = str(uuid4())
        jobs.rpc("start", key, "version")
        with jobs.transaction() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
            row = db.execute("SELECT id,argv FROM jobs WHERE id=?", (key,)).fetchone()
        self.assertEqual(jobs.rpc("stop", key)["state"], "stopping")
        with patch.object(jobs, "program_process") as launch:
            jobs.run_one(row)
            launch.assert_not_called()
        result = jobs.rpc("read", key)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(result["exit_code"])


if __name__ == "__main__":
    unittest.main()
