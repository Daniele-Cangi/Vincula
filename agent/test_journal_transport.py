# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4, uuid5

import jobs
import node_registry
import node_ssh
import server


def large_capability_manifest():
    run = uuid4()
    origin = str(uuid4())
    requested, parts = [], []
    for position in range(16):
        node = str(uuid4())
        files = [{"node_id": origin, "root": "source", "path": "é" * 100 + str(index) + ".txt"}
                 for index in range(4)]
        parameters = {"message": "journal-content-" + "y" * 440}
        requested.append({"node_id": node, "files": files, "parameters": parameters})
        parts.append({"node_id": node, "job_id": str(uuid5(run, f"capability:{origin}:{node}")),
                      "revision": "a" * 64, "inbox": "inbox", "parameters": parameters,
                      "inputs": [{"index": position * 4 + index, "source": source}
                                 for index, source in enumerate(files)]})
    return {"version": 1, "kind": "capability", "id": str(run), "origin": origin,
            "request": {"capability_id": "example.text", "parts": requested},
            "plan": {"capability_id": "example.text", "parts": parts}}


class JournalTransportTests(unittest.TestCase):
    def setUp(self):
        self.route = node_registry.Route("peer", ())
        self.windows = node_registry.Node(str(uuid4()), "Windows", "windows", route=self.route)

    def test_large_manifest_uses_stdin_with_a_bounded_constant_windows_command(self):
        body = large_capability_manifest()
        packaged = jobs.plan_journal.package(body)
        self.assertGreater(len(json.dumps(body, ensure_ascii=True).encode()), 64 * 1024)
        request = {"action": "plan", "operation": "store", "manifest": body}
        reply = {"result": {"state": "stored", "sha256": packaged["sha256"]}}
        with patch.object(node_ssh, "json_command", return_value=reply) as execute:
            self.assertEqual(server.remote_request(self.windows, request), reply)
        args, options = execute.call_args
        raw = options["input_data"]
        self.assertGreater(len(raw), 32767)
        self.assertEqual(json.loads(raw), request)
        self.assertLess(len(args[1]), 12000)
        command = base64.b64decode(args[1].split()[-1]).decode("utf-16-le")
        self.assertIn("journal-stdin", command)
        self.assertNotIn("journal-content-", command)
        self.assertEqual(options["output_limit"], jobs.plan_journal.MAX_RESPONSE_BYTES)

    def test_small_rpc_keeps_its_previous_argument_protocol(self):
        with patch.object(node_ssh, "json_command", return_value={"result": {}}) as execute:
            server.remote_request(self.windows, {"action": "list"})
        args, options = execute.call_args
        self.assertEqual(options, {})
        command = base64.b64decode(args[1].split()[-1]).decode("utf-16-le")
        self.assertIn(" rpc '", command)
        self.assertNotIn("journal-stdin", command)

    def test_regular_file_stdin_and_no_n_option_for_input(self):
        payload = b'{"action":"plan","operation":"get"}'

        def run(route, program, flags, args, output, **options):
            self.assertEqual(flags, ("-T",))
            self.assertEqual(options["stdin"].read(), payload)
            output.write(b'{"result":null}')

        with patch.object(node_ssh, "run", side_effect=run):
            self.assertEqual(node_ssh.json_command(self.route, "journal", input_data=payload), {"result": None})

    def test_timeout_never_accepts_a_partial_replication_reply(self):
        def interrupted(route, program, flags, args, output, **options):
            output.write(b'{"result":{"state":"stored"}}')
            raise subprocess.TimeoutExpired("ssh", 12)

        with patch.object(node_ssh, "run", side_effect=interrupted), self.assertRaises(subprocess.TimeoutExpired):
            node_ssh.json_command(self.route, "journal", input_data=b"{}")
        with patch.object(server, "registered_node", return_value=self.windows), \
                patch.object(server, "local_node", return_value="ubuntu"), \
                patch.object(node_ssh, "json_command", side_effect=subprocess.TimeoutExpired("ssh", 12)), \
                self.assertRaisesRegex(server.ToolError, "unavailable"):
            server.destination_request(self.windows.node_id, {"action": "plan", "operation": "store",
                                                           "manifest": large_capability_manifest()})

    def test_transport_rejects_oversized_or_malformed_output(self):
        with self.assertRaisesRegex(ValueError, "bounded journal"):
            node_ssh.json_command(self.route, "journal", input_data=b"x" * (512 * 1024 + 1))

        def malformed(route, program, flags, args, output, **options):
            output.write(b"{")

        with patch.object(node_ssh, "run", side_effect=malformed), self.assertRaises(ValueError):
            node_ssh.json_command(self.route, "journal", input_data=b"{}")


class JournalStdinTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        LOCALAPPDATA=str(self.home / "AppData"))
        self.state = self.home / ("AppData/Vincula-Jobs" if sys.platform == "win32" else ".local/share/vincula-jobs")
        helper = Path(jobs.__file__).resolve()
        subprocess.run([sys.executable, str(helper), "init"], env=self.env, capture_output=True, check=True)
        self.helper = self.state / "jobs.py"

    def invoke(self, raw):
        result = subprocess.run([sys.executable, "-IB", str(self.helper), "journal-stdin"], input=raw,
                                capture_output=True, env=self.env, timeout=10, check=True)
        return json.loads(result.stdout)

    def test_large_native_stdin_round_trip(self):
        body = large_capability_manifest()
        record = jobs.plan_journal.package(body)
        request = {"action": "plan", "operation": "store", "manifest": body}
        receipt = self.invoke(json.dumps(request).encode())
        self.assertEqual(receipt, {"result": {"state": "stored", "sha256": record["sha256"]}})
        found = self.invoke(json.dumps({"action": "plan", "operation": "get", "kind": "capability",
                                       "plan_id": body["id"]}).encode())
        self.assertEqual(found, {"result": record})

    def test_invalid_size_json_or_nonjournal_action_leaves_no_partial_state(self):
        body = large_capability_manifest()
        invalid = deepcopy(body)
        invalid["plan"]["parts"][0]["revision"] = "invalid"
        payloads = [b"{", b" " * (jobs.plan_journal.MAX_REQUEST_BYTES + 1),
                    json.dumps({"action": "list"}).encode(),
                    json.dumps({"action": "plan", "operation": "store", "manifest": invalid}).encode()]
        for raw in payloads:
            self.assertIn("error", self.invoke(raw))
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM plan_journal").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell stdin bridge")
    def test_windows_powershell_bridge_forwards_large_payload_without_argument_expansion(self):
        body = large_capability_manifest()
        expected = jobs.plan_journal.package(body)
        script = server.WINDOWS_JOURNAL_STDIN.replace("__JOURNAL_LIMIT__", str(jobs.plan_journal.MAX_REQUEST_BYTES))
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        request = json.dumps({"action": "plan", "operation": "store", "manifest": body}, ensure_ascii=False).encode()
        result = subprocess.run([str(powershell), *server.windows_ssh._powershell_arguments(script)],
                                input=request, capture_output=True, env=self.env, timeout=20, check=True)
        self.assertEqual(json.loads(result.stdout), {"result": {"state": "stored", "sha256": expected["sha256"]}})


if __name__ == "__main__":
    unittest.main()
