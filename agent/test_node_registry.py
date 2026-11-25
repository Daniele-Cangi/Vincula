# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
import unittest
import sys
from unittest.mock import patch
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError

import capability_workflow
import jobs
import node_registry
import nodes
import server


class NodeRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.local, self.windows, self.kali = (str(uuid4()) for _ in range(3))
        route = node_registry.Route("peer", ())
        self.topology = node_registry.Registry(self.local, (
            node_registry.Node(self.local, "ASUS", "linux", ("ubuntu", "tuf")),
            node_registry.Node(self.windows, "Lenovo", "windows", ("windows",), route),
            node_registry.Node(self.kali, "Third PC", "linux", ("kali",), route),
        ))
        registry = patch.object(server, "registry", return_value=self.topology)
        registry.start()
        self.addCleanup(registry.stop)
        state = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        state.start()
        self.addCleanup(state.stop)
        jobs.initialize()

    def test_same_linux_family_has_distinct_identities_and_failed_node_is_isolated(self):
        metrics = server.Metrics(os="test", os_version=None, uptime_seconds=1, cpu_percent=None,
                                 memory_total_bytes=1, memory_available_bytes=1)

        def status(node, is_local):
            self.assertEqual(is_local, node.node_id == self.local)
            if node.node_id == self.kali:
                raise TimeoutExpired("SSH", 1)
            return metrics

        with patch.object(server, "read_metrics", side_effect=status):
            nodes = asyncio.run(server.list_nodes()).nodes
            self.assertEqual([node.node_id for node in nodes], [self.local, self.windows, self.kali])
            self.assertEqual([node.availability for node in nodes], ["available", "available", "unavailable"])
            self.assertEqual(asyncio.run(server.get_node_status("ubuntu")).node_id, self.local)
        with self.assertRaisesRegex(ToolError, "Unknown registered node"):
            server.registered_node("unknown")

    def test_registry_pins_host_key_to_id_and_preserves_legacy_aliases(self):
        config = self.root / "nodes.json"
        identity, known = self.root / "identity", self.root / "hosts"
        ssh = {"host": "192.0.2.30", "user": "worker", "identity_file": str(identity),
               "known_hosts_file": str(known), "network": "Wi-Fi"}
        document = {"local": self.local, "nodes": [
            {"id": self.local, "label": "ASUS", "family": "linux", "aliases": ["ubuntu"]},
            {"id": self.kali, "label": "Kali", "family": "linux", "aliases": ["kali"], "ssh": ssh},
        ]}
        # Pinning and validation are independent of the platform the test suite runs on.
        legacy = node_registry.Registry("ubuntu", (node_registry.Node("ubuntu", "Ubuntu", "linux"),))
        config.write_text(json.dumps(document))
        topology = node_registry.load(config, legacy, server.node_ssh.EMPTY_CONFIG)
        self.assertEqual(topology.get("ubuntu").node_id, self.local)
        self.assertIn(f"HostKeyAlias=vincula-{self.kali}", topology.get("kali").route.options)
        self.assertFalse(topology.get("kali").route.configured())
        document["nodes"][1]["aliases"] = ["ubuntu"]
        config.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            node_registry.load(config, legacy, server.node_ssh.EMPTY_CONFIG)

    def test_relay_verifies_export_before_writing_and_cleans_up(self):
        source, destination = (self.topology.get(node) for node in ("windows", "kali"))
        payload = b"relay payload"
        expected = {"size": len(payload), "sha256": sha256(payload).hexdigest()}
        calls = []

        def copy(node, remote, path, *, sending):
            calls.append((node.node_id, remote, sending))
            if sending:
                self.assertEqual(path.read_bytes(), payload)
            else:
                path.write_bytes(payload)

        with patch.object(server, "sftp_copy", side_effect=copy):
            server.copy_staged(source, destination, "from", "to", expected)
            self.assertEqual(calls, [(self.windows, "from", False), (self.kali, "to", True)])
            calls.clear()
            with self.assertRaisesRegex(ValueError, "approved export"):
                server.copy_staged(source, destination, "from", "to", dict(expected, sha256="0" * 64))
            self.assertEqual(calls, [(self.windows, "from", False)])
        self.assertEqual(list(self.root.glob("relay-*")), [])

    @unittest.skipUnless(sys.platform == "linux", "Linux controller identity agent")
    def test_explicit_agent_selects_public_file_without_requiring_socket_as_file(self):
        public, known = self.root / "identity.pub", self.root / "hosts"
        public.write_text("public fixture"); known.write_text("host fixture")
        socket = self.root / "private service dir" / "agent.sock"
        route = node_registry.ssh_route({"host": "192.0.2.30", "user": "worker",
                                        "identity_file": str(public), "known_hosts_file": str(known),
                                        "identity_agent": str(socket)}, "/dev/null", self.kali)
        self.assertEqual(route.required_files, (public, known))
        self.assertTrue(route.configured())
        self.assertFalse(socket.exists())
        self.assertIn(f'IdentityAgent="{socket}"', route.options)
        self.assertEqual(route.options[route.options.index("-i") + 1], str(public))
        self.assertIn("IdentitiesOnly=yes", server.node_ssh.OPTIONS)
        self.assertIn("ForwardAgent=no", server.node_ssh.OPTIONS)

    @unittest.skipUnless(sys.platform == "linux", "Linux literal socket paths")
    def test_explicit_agent_rejects_nonliteral_socket_and_private_identity(self):
        values = {"host": "192.0.2.30", "user": "worker", "identity_file": str(self.root / "identity.pub"),
                  "known_hosts_file": str(self.root / "hosts")}
        for socket in (None, "relative.sock", "/run/$AGENT", "/run/%u/agent.sock", "/run/~/agent.sock", "/run/a\nb", "/run/a\tb"):
            with self.subTest(socket=socket), self.assertRaises(ValueError):
                node_registry.ssh_route(dict(values, identity_agent=socket), "/dev/null", self.kali)
        values["identity_file"] = str(self.root / "private-key")
        with self.assertRaisesRegex(ValueError, r"\.pub"):
            node_registry.ssh_route(dict(values, identity_agent="/run/service/agent.sock"), "/dev/null", self.kali)
        values["identity_file"] = str(self.root / "bad\tidentity.pub")
        with self.assertRaises(ValueError):
            node_registry.ssh_route(dict(values, identity_agent="/run/service/agent.sock"), "/dev/null", self.kali)
        self.assertIsNotNone(node_registry.ssh_route(values, "/dev/null", self.kali))

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("ssh"), "Native OpenSSH configuration parser")
    def test_native_ssh_config_round_trips_explicit_socket_without_network(self):
        public, known = self.root / "identity.pub", self.root / "hosts"
        public.write_text("public fixture"); known.write_text("host fixture")
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "LANG": "C", "SSH_AUTH_SOCK": "/wrong/desktop/agent.sock"}
        for name in ("service with spaces", "service\\with spaces"):
            socket = self.root / name / "agent.sock"
            route = node_registry.ssh_route({"host": "192.0.2.30", "user": "worker",
                                            "identity_file": str(public), "known_hosts_file": str(known),
                                            "identity_agent": str(socket)}, "/dev/null", self.kali)
            with self.subTest(socket=str(socket)):
                result = subprocess.run([shutil.which("ssh"), "-G", *server.node_ssh.OPTIONS, *route.options, route.destination],
                                        env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                settings = {key: value for key, separator, value in (line.partition(" ") for line in result.stdout.splitlines()) if separator}
                self.assertEqual(settings.get("identityagent"), str(socket))
                self.assertEqual(settings.get("identitiesonly"), "yes")
                self.assertEqual(settings.get("forwardagent"), "no")

    def test_windows_legacy_route_unchanged_and_linux_agent_route_rejected(self):
        values = {"host": "192.0.2.30", "user": "worker", "identity_file": str(self.root / "identity.pub"),
                  "known_hosts_file": str(self.root / "hosts")}
        with patch.object(node_registry.sys, "platform", "win32"):
            legacy = node_registry.ssh_route(values, "NUL", self.kali)
            self.assertFalse(any("IdentityAgent=" in value for value in legacy.options))
            with self.assertRaisesRegex(ValueError, "Linux controller"):
                node_registry.ssh_route(dict(values, identity_agent=str(self.root / "agent.sock")), "NUL", self.kali)

    def test_capability_plans_support_three_nodes_and_resolve_aliases(self):
        request = {"capability_id": "example.run", "parts": [
            {"node_id": node, "files": [{"node_id": "ubuntu", "root": "test", "path": f"{node}.txt"}],
             "parameters": {}} for node in ("tuf", "windows", "kali")
        ]}
        provider = {"version": "1", "revision": uuid4().hex, "inbox": "test",
                    "contract": {"max_files": 1, "extensions": [], "parameters": {}}}
        providers = {node: provider for node in (self.local, self.windows, self.kali)}
        uid = uuid4()
        plan = capability_workflow.make_plan(uid, request, providers, self.local, self.topology.get)
        self.assertEqual([part["node_id"] for part in plan["parts"]], [self.local, self.windows, self.kali])
        self.assertEqual([part["inputs"][0]["index"] for part in plan["parts"]], [0, 1, 2])
        self.assertEqual([part["inputs"][0]["source"]["node_id"] for part in plan["parts"]], [self.local] * 3)
        self.assertEqual(capability_workflow.load(uid, request), plan)
        request["parts"].append(dict(request["parts"][0], node_id="ubuntu"))
        with self.assertRaisesRegex(ValueError, "at most one"):
            capability_workflow.make_plan(uuid4(), request, providers, self.local, self.topology.get)

    def test_old_two_node_transfer_rejects_two_aliases_for_same_machine(self):
        named = server.FileEndpoint(node_id="ubuntu", root="shared", path="source.bin")
        canonical = server.FileEndpoint(node_id=self.local, root="shared", path="destination.bin")
        with self.assertRaisesRegex(ValueError, "different registered nodes"):
            server.execute_transfer(uuid4(), named, canonical)


class LiveRegistryTests(unittest.TestCase):
    def test_saved_node_changes_are_visible_to_the_running_server(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nodes.json"
            local, peer = str(uuid4()), str(uuid4())
            family = "windows" if sys.platform == "win32" else "linux"
            nodes.execute({"action": "add", "local": True,
                           "node": {"id": local, "label": "Local", "family": family}}, path)
            metrics = server.Metrics(os="test", os_version=None, uptime_seconds=1, cpu_percent=None,
                                     memory_total_bytes=1, memory_available_bytes=1)
            with patch.dict(os.environ, {"VINCULA_NODE_REGISTRY": str(path)}), \
                    patch.object(server, "read_metrics", return_value=metrics):
                self.assertEqual([node.node_id for node in asyncio.run(server.list_nodes()).nodes], [local])
                nodes.execute({"action": "add", "node": {"id": peer, "label": "Peer", "family": "linux"}}, path)
                inventory = asyncio.run(server.list_nodes()).nodes
                self.assertEqual([node.node_id for node in inventory], [local, peer])
                self.assertEqual(inventory[1].availability, "not_configured")
                nodes.execute({"action": "update", "node_id": peer, "changes": {"label": "Renamed"}}, path)
                self.assertEqual(asyncio.run(server.get_node_status(peer)).label, "Renamed")
                nodes.execute({"action": "remove", "node_id": peer}, path)
                self.assertEqual([node.node_id for node in asyncio.run(server.list_nodes()).nodes], [local])


if __name__ == "__main__":
    unittest.main()
