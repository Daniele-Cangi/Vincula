# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real stdio discovery and owner policy, with a disposable native worker."""
import asyncio
import json
import sys
import time
import unittest
from mcp import Client, StdioServerParameters
import test_process_mcp as fixtures


class LargeTransferMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_discovery_owner_opt_in_and_live_file_worker_attestation(self):
        asyncio.get_running_loop().set_debug(False)
        harness = fixtures.ProcessMCPTests()
        try:
            harness.setUp()
        finally:
            # Its async unittest runner is not active here. Execute the native
            # fixture's synchronous cleanups through this test's runner.
            for callback, args, kwargs in harness._cleanups:
                self.addCleanup(callback, *args, **kwargs)
            harness._cleanups.clear()
        params = StdioServerParameters(command=sys.executable,
            args=[str(harness.source.with_name("server.py"))], env=harness.env)
        async with Client(params, read_timeout_seconds=20) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            new = {"get_large_file_info", "start_large_transfer", "advance_large_transfer", "get_large_transfer",
                   "stop_large_transfer", "resume_large_transfer", "cleanup_large_transfer"}
            self.assertEqual(len(tools), 34)
            self.assertTrue(new <= tools.keys())
            self.assertEqual({name for name in new if tools[name].annotations and tools[name].annotations.read_only_hint},
                             {"get_large_file_info", "get_large_transfer"})
            deadline = time.monotonic() + 10
            while True:
                reply = await client.call_tool("get_large_file_info", {"node_id": harness.node})
                self.assertFalse(reply.is_error, reply.content)
                info = reply.structured_content
                if info["file_worker_fresh"]:
                    break
                self.assertLess(time.monotonic(), deadline, info)
                await asyncio.sleep(0.05)
            self.assertEqual(info["version"], 2)
            self.assertFalse(info["policy"]["enabled"])
            self.assertEqual(info["mode"], "ask")
            policy = harness.home / "large-files-policy.json"
            policy.write_text(json.dumps(info["policy"] | {"enabled": True}), encoding="utf-8")
            harness.cli("large-files-policy", str(policy))
            harness.cli("mode", "full")
            updated = await client.call_tool("get_large_file_info", {"node_id": harness.node})
            self.assertFalse(updated.is_error, updated.content)
            self.assertTrue(updated.structured_content["policy"]["enabled"])
            self.assertEqual(updated.structured_content["mode"], "full")
            self.assertEqual(updated.structured_content["state_id"], info["state_id"])
            self.assertNotEqual(updated.structured_content["policy_revision"], info["policy_revision"])
            invalid = await client.call_tool("get_large_transfer", {"transfer_id": "../../bad", "authority_node_id": harness.node})
            self.assertTrue(invalid.is_error)


if __name__ == "__main__":
    unittest.main()
