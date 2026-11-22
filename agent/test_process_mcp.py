# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Real MCP stdio execution, approvals and stop in a disposable HOME."""

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

from mcp import Client, StdioServerParameters

import jobs


class ProcessMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-process-mcp-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        LOCALAPPDATA=str(self.home / "AppData"), XDG_CONFIG_HOME=str(self.home / ".config"))
        self.env.pop("VINCULA_NODE_REGISTRY", None)
        self.source = Path(jobs.__file__).resolve()
        self.cli("init")
        self.root = self.home / ("AppData/Vincula-Jobs" if jobs.WINDOWS else ".local/share/vincula-jobs")
        self.node = "windows" if jobs.WINDOWS else "ubuntu"
        self.worker = subprocess.Popen([sys.executable, str(self.source), "worker"], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_worker)
        deadline = time.monotonic() + 8
        while True:
            with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db:
                if db.execute("SELECT 1 FROM worker_environment").fetchone():
                    break
            if time.monotonic() > deadline:
                self.fail("The private worker did not start.")
            time.sleep(0.025)

    def cli(self, *args):
        answer = subprocess.run([sys.executable, str(self.source), *args], env=self.env,
                                capture_output=True, timeout=10)
        self.assertEqual(answer.returncode, 0, answer.stderr.decode("utf-8", "replace"))
        return answer.stdout

    def stop_worker(self):
        if self.worker.poll() is None:
            self.worker.terminate()
            self.worker.wait(timeout=10)

    async def call(self, client, tool, **values):
        reply = await client.call_tool(tool, dict(node_id=self.node, **values))
        self.assertFalse(reply.is_error, reply.content)
        return reply.structured_content

    async def terminal(self, client, key, state):
        deadline = time.monotonic() + 12
        while True:
            value = await self.call(client, "read_process", job_id=key)
            if value["state"] == state:
                return value
            if time.monotonic() > deadline:
                self.fail(str(value))
            await asyncio.sleep(0.05)

    async def test_native_tools_ask_full_retry_timeout_stop_and_owner_opt_in(self):
        params = StdioServerParameters(command=sys.executable, args=[str(self.source.with_name("server.py"))], env=self.env)
        async with Client(params, read_timeout_seconds=20) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            self.assertTrue({"get_process_execution_info", "execute_process", "read_process"} <= tools)
            info = await self.call(client, "get_process_execution_info")
            self.assertFalse(info["enabled"])
            self.cli("mode", "full")
            request = {"job_id": str(uuid4()), "argv": [sys.executable, "-c", "print('disabled')"]}
            blocked = await client.call_tool("execute_process", dict(node_id=self.node, **request))
            self.assertTrue(blocked.is_error)
            self.cli("process-access", "enable")
            self.cli("mode", "ask")
            request = {"job_id": str(uuid4()), "argv": [sys.executable, "-c",
                       "import sys; sys.stdout.reconfigure(encoding='utf-8'); "
                       "print(sys.argv[1]); print(sys.stdin.buffer.read().decode('utf-8')); sys.exit(7)", "é🙂"],
                       "cwd": str(self.home), "stdin_text": "é🙂 input"}
            pending = await self.call(client, "execute_process", **request)
            self.assertEqual(pending["state"], "pending")
            preview = json.loads(self.cli("pending"))
            self.assertEqual(json.loads(preview["argv"]), pending["manifest"])
            self.cli("approve", request["job_id"], "--sha256", pending["manifest"]["sha256"])
            completed = await self.terminal(client, request["job_id"], "completed")
            self.assertEqual(completed["exit_code"], 7)
            self.assertEqual(completed["stdout"]["text"].splitlines(), ["é🙂", "é🙂 input"])
            self.assertEqual((await self.call(client, "execute_process", **request))["outcome"], completed["outcome"])
            changed = await client.call_tool("execute_process", dict(node_id=self.node, **dict(request, stdin_text="changed")))
            self.assertTrue(changed.is_error)
            self.cli("mode", "full")
            full = await self.call(client, "execute_process", job_id=str(uuid4()),
                                   argv=[sys.executable, "-c", "print('new arguments, no registration')"])
            self.assertEqual((await self.terminal(client, full["id"], "completed"))["exit_code"], 0)
            timed = await self.call(client, "execute_process", job_id=str(uuid4()),
                                    argv=[sys.executable, "-c", "import time; time.sleep(120)"], timeout_seconds=1)
            self.assertEqual((await self.terminal(client, timed["id"], "failed"))["outcome"]["reason"], "timeout")
            stopped = await self.call(client, "execute_process", job_id=str(uuid4()),
                                      argv=[sys.executable, "-c", "import time; print('READY',flush=True); time.sleep(120)"])
            deadline = time.monotonic() + 8
            while "READY" not in (await self.call(client, "read_process", job_id=stopped["id"]))["stdout"]["text"]:
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.05)
            await self.call(client, "stop_job", job_id=stopped["id"])
            self.assertEqual((await self.terminal(client, stopped["id"], "cancelled"))["outcome"]["reason"], "stop")
            catalog = await self.call(client, "list_jobs")
            self.assertEqual(catalog["programs"], [])


if __name__ == "__main__":
    unittest.main()
