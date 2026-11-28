# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs
import node_registry
import node_ssh
import server


class ProcessTransportTests(unittest.TestCase):
    def test_one_mib_response_bound_accepts_complete_json_and_rejects_oversize(self):
        route = node_registry.Route("peer", ())
        limit = 1024 * 1024
        complete = b'{"result":"' + b"x" * (limit - 20) + b'"}'

        def response(route, program, flags, args, output, **options):
            output.write(complete)

        with patch.object(node_ssh, "run", side_effect=response):
            self.assertEqual(len(node_ssh.json_command(route, "fixed", output_limit=limit)["result"]), limit - 20)

        def oversized(route, program, flags, args, output, **options):
            output.write(complete + b" " * 32)

        with patch.object(node_ssh, "run", side_effect=oversized), self.assertRaisesRegex(ValueError, "response exceeds"):
            node_ssh.json_command(route, "fixed", output_limit=limit)

    def test_all_process_operations_use_constant_stdin_on_both_platforms(self):
        for family in ("linux", "windows"):
            node = node_registry.Node(str(uuid4()), family, family, route=node_registry.Route("peer", ()))
            commands = []
            for operation in ("info", "read", "execute"):
                request = {"action": "process", "operation": operation, "protocol_version": 1}
                if operation != "info":
                    request["job_id"] = str(uuid4())
                if operation == "execute":
                    request["argv"] = ["python", "é🙂"]
                    request["stdin_text"] = "é🙂" * 9000
                with patch.object(node_ssh, "json_command", return_value={"result": {}}) as execute:
                    server.remote_request(node, request)
                args, options = execute.call_args
                data = options["input_data"]
                self.assertEqual(int(data[:8], 16), len(data) - 9)
                self.assertEqual(data[8:9], b"\n")
                data = data[9:]
                self.assertEqual(json.loads(data), request)
                self.assertEqual(options["output_limit"], 1024 * 1024)
                self.assertLess(len(args[1]), 8192)
                command = args[1]
                self.assertIn("process-stdin", command)
                self.assertNotIn("é🙂", command)
                if family == "windows":
                    self.assertIn("py.exe -3 -IB -c", command)
                    self.assertIn("os.environ['LOCALAPPDATA']", command)
                    self.assertNotIn("%LOCALAPPDATA%", command)
                commands.append(args[1])
            self.assertEqual(commands, [commands[0]] * 3)


class ProcessStdinTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-process-stdin-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        LOCALAPPDATA=str(self.home / "AppData"))
        self.root = self.home / ("AppData/Vincula-Jobs" if jobs.WINDOWS else ".local/share/vincula-jobs")
        subprocess.run([sys.executable, str(Path(jobs.__file__).resolve()), "init"], env=self.env,
                       capture_output=True, timeout=10, check=True)
        self.helper = self.root / "jobs.py"
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute("UPDATE direct_execution_policy SET enabled=1")
            db.execute("INSERT INTO worker_environment VALUES(1,?,?)",
                       (str(Path(sys.executable).parent), ".EXE;.COM;.BAT;.CMD"))

    def invoke(self, raw):
        raw = f"{len(raw):08x}\n".encode() + raw
        answer = subprocess.run([sys.executable, "-IB", str(self.helper), "process-stdin"],
                                input=raw, env=self.env, capture_output=True, timeout=10, check=True)
        return answer.stdout

    def request(self):
        return {"action": "process", "operation": "execute", "protocol_version": 1,
                "job_id": str(uuid4()), "argv": [sys.executable, "é🙂" * 22000],
                "cwd": str(self.home), "stdin_text": "é🙂" * 8000, "env": {"PROBE": "é🙂"}}

    def test_large_utf8_manifest_roundtrip_and_rejection_leave_original_unchanged(self):
        request = self.request()
        raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(raw), 32767)
        output = self.invoke(raw)
        self.assertGreater(len(output), 256 * 1024)
        response = json.loads(output)
        self.assertEqual(response["result"]["manifest"]["original"]["argv"], request["argv"])
        self.assertEqual(response["result"]["manifest"]["original"]["stdin_text"], request["stdin_text"])
        read = {"action": "process", "operation": "read", "protocol_version": 1, "job_id": request["job_id"]}
        self.assertEqual(json.loads(self.invoke(json.dumps(read).encode())), response)
        invalid = dict(request, argv=[sys.executable, "different"])
        self.assertIn("error", json.loads(self.invoke(json.dumps(invalid).encode())))
        self.assertEqual(json.loads(self.invoke(json.dumps(read).encode())), response)

    def test_wrong_protocol_nonprocess_action_and_oversize_are_rejected_without_jobs(self):
        for raw in (b"{", b" " * (jobs.direct_execution.MAX_TRANSPORT_BYTES + 1), b'{"action":"list"}',
                    b'{"action":"process","operation":"info","protocol_version":3}'):
            self.assertIn("error", json.loads(self.invoke(raw)))
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_version_two_frame_is_supported_but_cannot_queue_without_its_live_worker(self):
        info = {"action": "process", "operation": "info", "protocol_version": 2}
        self.assertEqual(json.loads(self.invoke(json.dumps(info).encode()))["result"]["protocol_version"], 2)
        request = dict(info, operation="execute", job_id=str(uuid4()),
                       argv=[sys.executable, "-c", "pass"], cwd=str(self.home),
                       resources={"cpu_millis": 100, "memory_bytes": 4096, "gpus": []})
        result = json.loads(self.invoke(json.dumps(request).encode()))
        self.assertIn("worker_unavailable", result["error"])
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_complete_frame_finishes_without_waiting_for_stdin_eof(self):
        body = b'{"action":"process","operation":"info","protocol_version":1}'
        with subprocess.Popen([sys.executable, "-IB", str(self.helper), "process-stdin"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env) as process:
            try:
                process.stdin.write(jobs.wire_framing.pack(body, jobs.direct_execution.MAX_TRANSPORT_BYTES))
                process.stdin.flush()
                process.wait(timeout=5)
                self.assertIn("result", json.loads(process.stdout.read()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows UTF-8 helper command")
    def test_windows_native_command_preserves_accents_emoji_and_large_read_output(self):
        node = node_registry.Node(str(uuid4()), "Windows", "windows", route=node_registry.Route("peer", ()))

        def native(route, command, **options):
            self.assertLess(len(command), 8192)
            result = subprocess.run(command, input=options["input_data"],
                                    capture_output=True, env=self.env, timeout=20, check=True)
            return json.loads(result.stdout)

        request = self.request()
        with patch.object(node_ssh, "json_command", side_effect=native):
            queued = server.remote_request(node, request)
            read = server.remote_request(node, {"action": "process", "operation": "read", "protocol_version": 1,
                                                "job_id": request["job_id"]})
            info = server.remote_request(node, {"action": "process", "operation": "info", "protocol_version": 1})
        self.assertEqual(queued, read)
        self.assertEqual(read["result"]["manifest"]["original"]["argv"], request["argv"])
        self.assertTrue(info["result"]["enabled"])


if __name__ == "__main__":
    unittest.main()
