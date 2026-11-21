# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from argparse import Namespace
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import batches
import jobs
import server


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in {"ROOT": root, "DATABASE": root / "jobs.sqlite3"}.items():
            change = patch.object(jobs, name, value)
            change.start()
            self.addCleanup(change.stop)
        jobs.initialize()

    def test_balanced_persistent_plan(self):
        key = uuid4()
        request = [{"program": "shared", "node_id": None}] * 3
        request = [*request, {"program": "specific", "node_id": "ubuntu"}]
        plan = batches.make_plan(key, "ubuntu", request, {"windows": ["shared"], "ubuntu": ["shared"]})
        self.assertEqual([row["node_id"] for row in plan], ["windows", "ubuntu", "windows", "ubuntu"])
        self.assertEqual(len({row["job_id"] for row in plan}), 4)
        self.assertEqual(batches.save(key, request, plan), plan)
        alternative = batches.make_plan(key, "ubuntu", request, {"ubuntu": ["shared"]})
        self.assertEqual(batches.save(key, request, alternative), plan)
        with self.assertRaisesRegex(ValueError, "different task list"):
            batches.load(key, request[:-1])

    def test_partial_rejection_and_retry_keep_original_jobs(self):
        jobs.operator(Namespace(command="register", name="allowed", argv=[sys.executable, "--version"]))
        allowed, unknown = str(uuid4()), str(uuid4())
        items = [{"job_id": allowed, "program": "allowed"}, {"job_id": unknown, "program": "unknown"}]
        reply = jobs.dispatch("batch", operation="start", items=items)
        self.assertEqual(reply[0]["job"]["state"], "pending")
        self.assertIn("not authorized", reply[1]["error"])
        self.assertEqual(jobs.dispatch("batch", operation="start", items=items), reply)
        self.assertEqual(jobs.dispatch("batch", operation="status", items=items)[0], reply[0])
        with self.assertRaisesRegex(ValueError, "Invalid batch"):
            jobs.dispatch("batch", operation="stop", items=items)

    def test_report_preserves_results_when_a_node_stops_responding(self):
        key = uuid4()
        request = [{"program": "shared", "node_id": node} for node in ("windows", "ubuntu")]
        plan = batches.make_plan(key, "ubuntu", request, {})

        def destination(node, request):
            self.assertEqual(request["operation"], "status")
            if node == "windows":
                raise server.ToolError("Job service unavailable.")
            item, = request["items"]
            return [{"job": {"id": item["job_id"], "program": item["program"], "state": "completed",
                             "exit_code": 0, "created": 1, "finished": 2}}]

        with patch.object(server, "destination_request", side_effect=destination):
            report = asyncio.run(server.batch_report(key, plan, "status"))
        self.assertEqual([task.node_id for task in report.tasks], ["windows", "ubuntu"])
        self.assertIsNone(report.tasks[0].job)
        self.assertIn("unavailable", report.tasks[0].error)
        self.assertEqual(report.tasks[1].job.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
