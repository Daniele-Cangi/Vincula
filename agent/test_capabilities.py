# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import capability_workflow
import jobs


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.file = self.inbox / "source.txt"
        self.file.write_bytes(b"sample")
        self.adapter = self.root / "my_adapter.py"
        self.adapter.write_text(
            "def run(inputs, parameters, workspace):\n"
            "    print('adapter log')\n"
            "    return [inputs[0]['path'].read_text(), parameters['count']]\n",
            encoding="utf-8",
        )
        self.manifest = {
            "id": "example.text", "version": "1", "python": sys.executable, "adapter": str(self.adapter),
            "inbox": "inbox", "max_files": 1, "max_input_bytes": 64, "extensions": [".txt"],
            "parameters": {"count": {"type": "integer", "required": True}},
        }
        with jobs.transaction() as db:
            jobs.files.grant(db, "inbox", str(self.inbox), True)
            jobs.capabilities.register(db, jobs.files, self.manifest)
        self.provider = jobs.dispatch("capability", operation="describe", capability_id="example.text")

    def queue(self, **overrides):
        request = {"job_id": str(uuid4()), "capability_id": self.provider["id"], "revision": self.provider["revision"],
                   "inputs": [{"index": 0, "root": "inbox", "path": "source.txt"}], "parameters": {"count": 2}}
        return jobs.dispatch("capability", operation="queue", **(request | overrides))

    def launcher(self, key):
        with jobs.transaction() as db:
            jobs.decide(db, key, True)
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
            return jobs.capabilities.arguments(db, key, self.root)

    def test_pending_runner_rejects_and_approved_job_executes_stored_adapter(self):
        queued = self.queue()
        key = queued["id"]
        self.assertEqual(queued["state"], "pending")
        launcher = [sys.executable, "-I", "-B", str(self.root / "vincula_capabilities.py"), key]
        blocked = subprocess.run(launcher, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("approved capability job", blocked.stderr)

        self.adapter.unlink()
        self.assertEqual(self.launcher(key), launcher)
        completed = subprocess.run(launcher, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(completed.stdout), {"value": ["sample", 2]})
        self.assertIn("adapter log", completed.stderr)

    def test_contract_rejects_invalid_files_parameters_and_replays(self):
        with self.assertRaisesRegex(ValueError, "Invalid value"):
            self.queue(parameters={"count": True})
        with self.assertRaisesRegex(ValueError, "extension"):
            self.queue(inputs=[{"index": 0, "root": "inbox", "path": "source.exe"}])
        key = str(uuid4())
        original = self.queue(job_id=key)
        replay = self.queue(job_id=key)
        self.assertEqual(replay, original)
        with self.assertRaisesRegex(ValueError, "different capability part"):
            self.queue(job_id=key, parameters={"count": 3})

    def test_snapshots_reject_changed_grants_and_transferred_contents(self):
        staged = {"index": 0, "root": "inbox", "path": "source.txt", "sha256": sha256(b"sample").hexdigest()}
        digest_job, root_job = self.queue(inputs=[staged]), self.queue()
        digest_cmd, root_cmd = self.launcher(digest_job["id"]), self.launcher(root_job["id"])
        self.file.write_bytes(b"changed")
        result = subprocess.run(digest_cmd, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verified transfer", result.stderr)
        with jobs.transaction() as db:
            jobs.files.grant(db, "inbox", str(self.root), True)
        result = subprocess.run(root_cmd, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reconfigured", result.stderr)

    def test_registry_discovers_new_adapter_and_cancels_revoked_jobs_independently(self):
        queued = self.queue()
        with jobs.transaction() as db:
            jobs.capabilities.register(db, jobs.files, dict(self.manifest, id="example.no_files", max_files=0))
        catalog = jobs.dispatch("capability", operation="describe")
        self.assertEqual([cap["id"] for cap in catalog], ["example.no_files", "example.text"])
        with jobs.transaction() as db:
            jobs.capabilities.revoke(db, "example.text")
        self.assertEqual(jobs.rpc("status", queued["id"])["state"], "cancelled")
        self.assertEqual(len(jobs.dispatch("capability", operation="describe")), 1)

    def test_upgrade_preserves_completed_history_and_current_capability_jobs(self):
        current = self.queue()["id"]
        queued, completed = str(uuid4()), str(uuid4())
        with jobs.transaction() as db:
            db.executemany("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,'old.specialized','[]',?,0)",
                           [(queued, "queued"), (completed, "completed")])
        jobs.initialize()
        self.assertEqual(jobs.rpc("status", queued)["state"], "cancelled")
        self.assertEqual(jobs.rpc("status", completed)["state"], "completed")
        self.assertEqual(jobs.rpc("status", current)["state"], "pending")

    def test_plans_match_contract_and_preserve_identity(self):
        run_id = uuid4()
        parts = [{"node_id": node, "files": [], "parameters": {"count": 2}} for node in ("windows", "ubuntu")]
        request = {"capability_id": "example.text", "parts": parts}
        providers = {"windows": self.provider, "ubuntu": dict(self.provider, version="incompatible")}
        with self.assertRaisesRegex(ValueError, "same capability version"):
            capability_workflow.make_plan(run_id, request, providers, "ubuntu")
        providers["ubuntu"] = self.provider
        first = capability_workflow.make_plan(run_id, request, providers, "ubuntu")
        self.assertEqual(capability_workflow.load(run_id, request), first)
        with self.assertRaisesRegex(ValueError, "different capability plan"):
            capability_workflow.load(run_id, dict(request, parts=parts[:1]))

    def test_malformed_result_is_isolated_and_json_null_is_valid(self):
        plan = {"capability_id": "example.text", "parts": [
            {"node_id": node, "job_id": str(uuid4())} for node in ("windows", "ubuntu")
        ]}

        def dispatch(node, _request):
            output = '{"value": NaN}' if node == "windows" else '{"value": null}'
            return {"state": "completed", "exit_code": 0, "stdout": {"text": output, "truncated": False}}

        result = asyncio.run(capability_workflow.reconcile(uuid4(), plan, dispatch))
        self.assertIn("invalid JSON", result["parts"][0]["error"])
        self.assertEqual(result["parts"][1]["output"], {"value": None})


if __name__ == "__main__":
    unittest.main()
