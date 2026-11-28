# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise the Vincula MCP server over stdio from the current PC."""

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import Client, MCPError, StdioServerParameters

from server import Metrics, legacy_registry, registry


async def check_node(client, origin, definition, node, include_remotes):
    node_id = node["node_id"]
    is_local = node_id == origin
    configured = include_remotes and definition.route is not None and definition.route.configured()
    expected = "available" if is_local or configured else "not_configured"
    assert node["local"] == is_local and node["availability"] == expected, node
    assert node["label"] == definition.label and node["os_family"] == definition.family, node
    assert node["aliases"] == list(definition.aliases), node
    assert node["network"] == (definition.route.network if definition.route else None), node
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


async def check(topology, include_remotes: bool, env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=[str(Path(__file__).with_name("server.py").resolve())], env=env,
    )
    async with Client(params, read_timeout_seconds=20) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "list_nodes", "get_node_status", "list_jobs", "start_job", "read_job", "stop_job",
            "submit_batch", "get_batch",
            "discover_tools", "open_shell", "send_shell_command", "read_shell", "close_shell",
            "list_file_roots", "request_file", "get_file_request", "transfer_file",
            "discover_capabilities", "submit_capability", "get_capability_run",
            "get_process_execution_info", "execute_process", "read_process",
            "get_node_resources", "submit_execution_plan", "get_execution_plan", "stop_execution_plan",
        }
        assert {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint} == {
            "list_nodes", "get_node_status", "list_jobs", "read_job",
            "get_batch", "discover_tools", "read_shell", "list_file_roots",
            "discover_capabilities", "get_capability_run",
            "get_process_execution_info", "read_process",
            "get_node_resources", "get_execution_plan",
        }

        origin = topology.local_id
        inventory = (await client.call_tool("list_nodes")).structured_content
        assert [node["node_id"] for node in inventory["nodes"]] == [node.node_id for node in topology.nodes]
        for definition, node in zip(topology.nodes, inventory["nodes"]):
            await check_node(client, origin, definition, node, include_remotes)

        try:
            invalid = await client.call_tool("get_node_status", {"node_id": "other"})
        except MCPError:
            pass
        else:
            assert invalid.is_error, invalid


async def main() -> None:
    env = {
        name: value for name, value in os.environ.items()
        if name.startswith("VINCULA_") or name in {"SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "LOCALAPPDATA"}
    }
    await check(registry(), True, env)
    with TemporaryDirectory() as empty:
        home_var = "LOCALAPPDATA" if sys.platform == "win32" else "HOME"
        isolated = {name: value for name, value in env.items() if name != "VINCULA_NODE_REGISTRY"}
        await check(legacy_registry(), False, {**isolated, home_var: empty, "XDG_CONFIG_HOME": empty})


if __name__ == "__main__":
    asyncio.run(main())
