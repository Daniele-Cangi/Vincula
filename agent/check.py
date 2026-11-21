# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise the Vincula MCP server over stdio from the current PC."""

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import Client, MCPError, StdioServerParameters

from server import Metrics, local_node


async def check(peer_configured: bool, env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=[str(Path(__file__).with_name("server.py").resolve())], env=env,
    )
    async with Client(params, read_timeout_seconds=20) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "list_nodes", "get_node_status", "list_jobs", "start_job", "read_job", "stop_job",
        }
        assert {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint} == {
            "list_nodes", "get_node_status", "list_jobs", "read_job",
        }

        origin = local_node()
        inventory = (await client.call_tool("list_nodes")).structured_content
        assert [node["node_id"] for node in inventory["nodes"]] == ["ubuntu", "windows"]
        for node in inventory["nodes"]:
            node_id = node["node_id"]
            is_local = node_id == origin
            expected = "available" if is_local or peer_configured else "not_configured"
            assert node == {"node_id": node_id, "local": is_local, "availability": expected}, node
            reply = await client.call_tool("get_node_status", {"node_id": node_id})
            info = dict(reply.structured_content)
            metrics = info.pop("metrics")
            assert not reply.is_error and info == node, reply
            if expected == "available":
                status = Metrics.model_validate(metrics)
                assert status.os and status.memory_available_bytes <= status.memory_total_bytes
                print(f"{origin} -> {node_id}: {status.os}, uptime/CPU/RAM validated")
            else:
                assert metrics is None
                print(f"{origin} -> {node_id}: missing SSH config reported")

        try:
            invalid = await client.call_tool("get_node_status", {"node_id": "other"})
        except MCPError:
            pass
        else:
            assert invalid.is_error, invalid


async def main() -> None:
    env = {
        name: value for name, value in os.environ.items()
        if name.startswith("VINCULA_") or name == "SSH_AUTH_SOCK"
    }
    await check(True, env)
    with TemporaryDirectory() as empty:
        home_var = "LOCALAPPDATA" if local_node() == "windows" else "HOME"
        await check(False, {**env, home_var: empty})


if __name__ == "__main__":
    asyncio.run(main())
