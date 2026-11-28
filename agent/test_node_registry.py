# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import io
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


class ServiceRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-service-catalog-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.local, self.first, self.second, self.spare = (str(uuid4()) for _ in range(4))
        self.empty_config = "NUL" if sys.platform == "win32" else "/dev/null"
        self.document = {"local": self.local, "nodes": [
            {"id": self.local, "label": "Controller", "family": "linux", "aliases": ["main"]},
            {"id": self.first, "label": "First receiver", "family": "windows", "aliases": ["peer-a"],
             "ssh": self.route("192.0.2.31", 2222, "Ethernet", "192.0.2.10")},
            {"id": self.second, "label": "Second receiver", "family": "linux", "aliases": ["peer-b"],
             "ssh": self.route("2001:db8::32", 22, "Wi-Fi")},
            {"id": self.spare, "label": "Future receiver", "family": "linux", "aliases": ["spare"]},
        ]}

    def route(self, host, port, network, bind_address=None):
        value = {"host": host, "user": "core-worker", "port": port,
                 "identity_file": str(self.root / "controller identity.pub"),
                 "known_hosts_file": str(self.root / "host pins"), "network": network}
        if bind_address is not None:
            value["bind_address"] = bind_address
        return value

    def parse(self, document=None, family="linux"):
        return node_registry.from_service_document(self.document if document is None else document, family, self.empty_config)

    def test_four_uuid_catalog_preserves_explicit_routes_and_unconfigured_peer(self):
        original = deepcopy(self.document)
        topology = self.parse()
        self.assertEqual(topology.local_id, self.local)
        self.assertEqual([node.node_id for node in topology.nodes], [self.local, self.first, self.second, self.spare])
        self.assertIsNone(topology.get(self.local).route)
        self.assertIsNone(topology.get("spare").route)
        first, second = topology.get("peer-a").route, topology.get("peer-b").route
        self.assertEqual(first.destination, "192.0.2.31")
        self.assertEqual(second.destination, "2001:db8::32")
        self.assertEqual((first.network, second.network), ("Ethernet", "Wi-Fi"))
        self.assertEqual(first.required_files, (self.root / "controller identity.pub", self.root / "host pins"))
        self.assertEqual(first.options[:4], ("-F", self.empty_config, "-i", str(self.root / "controller identity.pub")))
        for identity, route in ((self.first, first), (self.second, second)):
            self.assertIn(f"HostKeyAlias=vincula-{identity}", route.options)
            self.assertIn('UserKnownHostsFile="' + str(self.root / "host pins") + '"', route.options)
            self.assertIn("GlobalKnownHostsFile=" + self.empty_config, route.options)
            self.assertIn("HostKeyAlgorithms=ssh-ed25519", route.options)
            self.assertIn("UpdateHostKeys=no", route.options)
        self.assertIn("Port=2222", first.options)
        self.assertIn("BindAddress=192.0.2.10", first.options)
        self.assertIn("User=core-worker", first.options)
        self.assertEqual(self.document, original)

    def test_configuration_does_not_read_identity_known_hosts_or_attest_connections(self):
        with patch.object(Path, "open", side_effect=AssertionError("Only catalog data is supplied")), \
                patch.object(node_registry.Route, "configured", side_effect=AssertionError("Do not claim live readiness")):
            topology = self.parse()
        self.assertIsNotNone(topology.get(self.first).route)
        self.assertFalse((self.root / "controller identity.pub").exists())
        self.assertFalse((self.root / "host pins").exists())

    def test_local_family_uuid_and_local_route_are_checked(self):
        with self.assertRaisesRegex(ValueError, "family"): self.parse(family="windows")
        for family in ("unknown", None, []):
            with self.subTest(family=family), self.assertRaises(ValueError): self.parse(family=family)
        for local in ("main", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", str(uuid4()), None):
            with self.subTest(local=local), self.assertRaises(ValueError):
                self.parse(dict(self.document, local=local))
        document = deepcopy(self.document)
        document["nodes"][0]["ssh"] = self.route("192.0.2.99", 22, "test")
        with self.assertRaisesRegex(ValueError, "local node"): self.parse(document)

    def test_legacy_route_presence_is_rejected_instead_of_borrowing_any_catalog(self):
        for index, value in ((0, "ubuntu"), (1, "windows"), (2, None)):
            document = deepcopy(self.document)
            document["nodes"][index]["legacy_route"] = value
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "legacy_route"):
                self.parse(document)

    def test_present_routes_reuse_existing_validation(self):
        for change in ({"host": "bad;command"}, {"user": "-invalid"}, {"port": True},
                       {"identity_file": "relative"}, {"known_hosts_file": "relative"}, {"password": "forbidden"}):
            document = deepcopy(self.document)
            document["nodes"][1]["ssh"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError): self.parse(document)

    def test_optional_desktop_metadata_is_preserved_without_activating_it(self):
        document = deepcopy(self.document)
        document["desktop"] = {"primary": self.first, "clipboard": True,
                               "members": [{"node_id": self.local, "x": -1, "y": 0},
                                           {"node_id": self.first, "x": 0, "y": 0}]}
        topology = self.parse(document)
        self.assertEqual(topology.desktop.primary, self.first)
        self.assertEqual(topology.desktop.members[0].node_id, self.local)
        self.assertTrue(topology.desktop.clipboard)

    @unittest.skipUnless(sys.platform == "linux", "Managed explicit agent routes are Linux-only")
    def test_managed_public_identity_and_socket_configuration_are_preserved(self):
        document = deepcopy(self.document)
        socket = self.root / "service-owned runtime" / "agent.sock"
        document["nodes"][1]["ssh"]["identity_agent"] = str(socket)
        topology = self.parse(document)
        route = topology.get(self.first).route
        self.assertIn(f'IdentityAgent="{socket}"', route.options)
        self.assertEqual(route.required_files, (self.root / "controller identity.pub", self.root / "host pins"))
        self.assertFalse(socket.exists())

    def test_load_service_uses_explicit_file_and_accepts_utf8_bom(self):
        path = self.root / "service-catalog.json"
        document = deepcopy(self.document);document["nodes"][0]["label"] = "Contrôleur"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(document, ensure_ascii=False).encode("utf-8"))
        with patch.object(Path, "home", side_effect=AssertionError("No user default path")), \
                patch.object(node_registry, "load", side_effect=AssertionError("No legacy loader fallback")):
            topology = node_registry.load_service(str(path), "linux", self.empty_config)
        self.assertEqual(topology.get(self.local).label, "Contrôleur")
        self.assertIsNone(topology.get(self.spare).route)

    def test_missing_invalid_or_unset_service_file_never_falls_back(self):
        path = self.root / "service-catalog.json"
        with patch.object(node_registry, "load", side_effect=AssertionError("No fallback")):
            with self.assertRaises(FileNotFoundError): node_registry.load_service(path, "linux", self.empty_config)
            with self.assertRaises(ValueError): node_registry.load_service(None, "linux", self.empty_config)
            for content in (b"invalid JSON", b"\xff", b"[]", b'{"local":null,"nodes":[]}'):
                path.write_bytes(content)
                with self.subTest(content=repr(content)), self.assertRaises(ValueError):
                    node_registry.load_service(path, "linux", self.empty_config)

    def test_service_file_exact_limit_and_oversize_are_distinct(self):
        path = self.root / "service-catalog.json"
        raw = json.dumps(self.document).encode("utf-8")
        raw += b" " * (node_registry.MAX_SERVICE_DOCUMENT_BYTES - len(raw))
        path.write_bytes(raw)
        self.assertEqual(node_registry.load_service(path, "linux", self.empty_config).local_id, self.local)
        path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValueError, "one MiB"):
            node_registry.load_service(path, "linux", self.empty_config)

    def test_guard_covers_the_bounded_read_and_validation(self):
        events, sizes = [], []

        @contextmanager
        def guard():
            events.append("enter")
            try: yield
            finally: events.append("exit")

        class Source(io.BytesIO):
            def read(inner, size=-1):
                self.assertEqual(events, ["enter"])
                sizes.append(size)
                return super().read(size)

        stream = Source(json.dumps(self.document).encode("utf-8"))
        with patch.object(node_registry, "registry_guard", guard), patch.object(Path, "open", return_value=stream):
            topology = node_registry.load_service(self.root / "explicit.json", "linux", self.empty_config)
        self.assertEqual(events, ["enter", "exit"])
        self.assertEqual(sizes, [node_registry.MAX_SERVICE_DOCUMENT_BYTES + 1])
        self.assertEqual(topology.local_id, self.local)
        self.assertTrue(stream.closed)

    def test_existing_loaders_keep_their_legacy_behavior(self):
        legacy_route = node_registry.Route("legacy-peer", ("-o", "test-option"))
        legacy = node_registry.Registry("old-local", (
            node_registry.Node("old-local", "Old local", "linux"),
            node_registry.Node("windows", "Old peer", "windows", route=legacy_route)))
        self.assertIs(node_registry.load(None, legacy, self.empty_config), legacy)
        document = {"local": self.local, "nodes": [self.document["nodes"][0],
                    {"id": self.first, "label": "Explicit migration", "family": "windows", "legacy_route": "windows"}]}
        self.assertIs(node_registry.from_document(document, legacy, self.empty_config).get(self.first).route, legacy_route)
        with self.assertRaisesRegex(ValueError, "legacy_route"): self.parse(document)


if __name__ == "__main__":
    unittest.main()
