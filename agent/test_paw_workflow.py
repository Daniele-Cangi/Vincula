# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs
import paw_workflow


class PawWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        folder = self.root / "inbox"
        folder.mkdir()
        (folder / "input.eml").write_text("sample")
        with jobs.transaction() as db:
            jobs.files.grant(db, "inbox", str(folder), True)
            db.execute("INSERT INTO paw_provider VALUES(1,?,?,?,?)",
                       ("revision-one", sys.executable, "2.0.0", "inbox"))

    def test_plans_pin_revision_and_reject_changed_input(self):
        identifier = uuid4()
        files = [{"node_id": "ubuntu", "root": "inbox", "path": "input.eml"}]
        request = [{"node_id": "windows", "files": files}]
        provider = {"revision": "test", "inbox": "inbox", "paw_version": "2.0.0"}
        paw_workflow.validate_request(request)
        first = paw_workflow.make_plan(identifier, request, {"windows": provider}, "ubuntu")
        newer = dict(provider, revision="changed")
        self.assertEqual(paw_workflow.make_plan(identifier, request, {"windows": newer}, "ubuntu"), first)
        with self.assertRaisesRegex(ValueError, "different plan"):
            paw_workflow.load(identifier, [{"node_id": "ubuntu", "files": files}])
        with self.assertRaisesRegex(ValueError, "distinct"):
            paw_workflow.validate_request([{"node_id": "ubuntu", "files": files * 2}])

    def test_rebinding_a_root_prevents_launch_after_approval(self):
        identifier = str(uuid4())
        inputs = [{"index": 0, "root": "inbox", "path": "input.eml"}]
        request = jobs.dispatch("paw", operation="queue", job_id=identifier, revision="revision-one", inputs=inputs)
        self.assertEqual(request["state"], "pending")
        replacement = self.root / "replacement"
        replacement.mkdir()
        with jobs.transaction() as db:
            jobs.decide(db, identifier, True)
            jobs.files.grant(db, "inbox", str(replacement), True)
            with self.assertRaisesRegex(ValueError, "reconfigured"):
                jobs.paw.arguments(db, jobs.files, identifier, self.root)


if __name__ == "__main__":
    unittest.main()
