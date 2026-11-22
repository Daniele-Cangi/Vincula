# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch
from uuid import uuid4

import desktop_coordinator
import desktop_ssh as bridge
import desktop_supervisor
import node_registry
import nodes


class ProtocolTests(unittest.TestCase):
    def binding(self, emergency=False):
        return {"protocol": 1, "mode": "stop" if emergency else "control", "node_id": str(uuid4()),
                "session_id": str(uuid4()), "revision": "a" * 64, "helper_sha256": "b" * 64,
                "instance": str(uuid4())}

    def test_frames_are_bounded_utf8_exact_and_do_not_require_eof(self):
        data = {"text": "à 🎯" * 2000}
        framed = bridge.encode(data, bridge.MAX_MESSAGE)
        stream = BytesIO(framed + bridge.encode({"next": True}, bridge.MAX_MESSAGE))
        self.assertEqual(bridge.read_frame(stream, bridge.MAX_MESSAGE), data)
        self.assertEqual(bridge.read_frame(stream, bridge.MAX_MESSAGE), {"next": True})
        self.assertIsNone(bridge.read_frame(stream, bridge.MAX_MESSAGE))
        for malformed in (b"00010001\n", b"FFFFFFFF\n", b"00000004\n{}", b"bad"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                bridge.read_frame(BytesIO(malformed), bridge.MAX_MESSAGE)
        with self.assertRaises(ValueError):
            bridge.encode({"data": "x" * bridge.MAX_MESSAGE}, bridge.MAX_MESSAGE)

    def test_duplicate_fields_nonfinite_and_invalid_utf8_are_rejected(self):
        for raw in (b'{"action":"status","action":"shutdown"}', b'{"x":NaN}', b'"\xff"', b'\xef\xbb\xbf{}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                bridge.decode(raw)

    def test_no_global_stop_shutdown_tokens_aliases_or_cross_session_requests(self):
        binding = self.binding()
        for payload in ({"action": "shutdown"}, {"action": "stop"}, {"action": "status", "token": "secret"},
                        {"action": "commit", "session_id": str(uuid4())},
                        {"action": "heartbeat", "session_id": binding["session_id"].replace("-", "")},
                        {"action": "stop_session", "session_id": binding["session_id"]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bridge.validate_request(payload, binding)
        for payload in ({"action": "status"}, {"action": "stop"},
                        {"action": "stop_session", "session_id": str(uuid4())},
                        {"action": "stop_session", "session_id": binding["session_id"], "extra": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bridge.validate_request(payload, binding, "stop")

    def test_binding_requires_exact_protocol_pin_and_canonical_identities(self):
        for changes in ({"protocol": True}, {"mode": "stop"}, {"node_id": "ubuntu"},
                        {"helper_sha256": "remote says trust me"}, {"instance": "old"}, {"extra": "x"}):
            binding = self.binding() | changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bridge.validate_binding(binding, "control")

    def test_remote_bootstrap_never_embeds_request_paths_or_powershell(self):
        for family in ("linux", "windows"):
            for mode in ("control", "stop", "status"):
                command = bridge.fixed_remote_command(family, mode)
                self.assertNotIn("PowerShell", command)
                self.assertNotIn("EncodedCommand", command)
                self.assertIn("-IB", command)
                self.assertIn({"control": "stdio", "stop": "stop-stdio", "status": "status-stdio"}[mode], command)
        self.assertTrue(bridge.fixed_remote_command("windows").startswith('py.exe -3 -IB -c "'))

    @unittest.skipUnless(sys.platform == "win32", "Native Windows delete-sharing contract")
    def test_windows_reader_allows_atomic_catalog_publication_while_held(self):
        with TemporaryDirectory(prefix="vincula-reader-publication-") as directory:
            path = Path(directory) / "nodes.json"
            node = str(uuid4())
            document = {"local": node, "nodes": [{"id": node, "label": "Before", "family": "windows"}]}
            original = json.dumps(document).encode()
            path.write_bytes(original)
            revision = nodes.snapshot(path)["revision"]
            request = {"action": "update", "node_id": node, "changes": {"label": "After"},
                       "expected_revision": revision}
            with bridge.metadata_reader(path) as reader:
                updated = nodes.execute(request, path)
                self.assertEqual(reader.read(len(original) + 1), original)
                self.assertEqual(updated["registry"]["nodes"][0]["label"], "After")
            self.assertEqual(json.loads(path.read_bytes())["nodes"][0]["label"], "After")


class LocalProcesses(bridge.DesktopSSHTransport):
    """Only the wire helper is local; all local endpoint authentication stays real."""

    def __init__(self, *args, fixtures, **kwargs):
        self.fixtures, self.spawned, self.control_wrapper = fixtures, [], None
        super().__init__(*args, **kwargs)

    async def _spawn(self, node_id, mode):
        fixture = self.fixtures[node_id]
        if self.control_wrapper is not None and mode == "control":
            argv = [sys.executable, "-IB", str(self.control_wrapper)]
        else:
            argv = [sys.executable, "-IB", bridge.__file__]
        argv += [{"control": "stdio", "stop": "stop-stdio", "status": "status-stdio"}[mode], "--directory", str(fixture["control"]),
                 "--registry", str(fixture["registry"])]
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=bridge.MAX_REPLY + 9)
        self.spawned.append((node_id, mode, process))
        return process


class SSHBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory(prefix="vincula-desktop-bridge-")
        self.root = Path(self.temp.name)
        self.ids = [str(uuid4()) for _ in range(3)]
        self.session_id = str(uuid4())
        self.fixtures, self.daemons, self.transports = {}, [], []
        self.addAsyncCleanup(self.cleanup)
        family = "windows" if sys.platform == "win32" else "linux"
        self.revisions = {}
        for index, node in enumerate(self.ids):
            base = self.root / str(index)
            base.mkdir()
            control, registry_path = base / "control", base / "nodes.json"
            document = {"local": node, "nodes": [{"id": identity, "label": "Synthetic node", "family": family}
                         for identity in self.ids], "desktop": {"primary": self.ids[0], "clipboard": True,
                         "members": [{"node_id": identity, "x": offset, "y": 0}
                                     for offset, identity in enumerate(self.ids)]}}
            raw = (json.dumps(document, indent=2) + "\n").encode()
            registry_path.write_bytes(raw)
            self.revisions[node] = sha256(raw).hexdigest()
            self.fixtures[node] = {"control": control, "registry": registry_path, "document": document}
            daemon = subprocess.Popen([sys.executable, "-B", desktop_supervisor.__file__, "serve",
                    "--directory", str(control), "--lease-seconds", "12", "--guardian-lease", "0.7"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.daemons.append(daemon)
            reply = await asyncio.to_thread(daemon.stdout.readline)
            self.assertTrue(json.loads(reply)["ok"])
        self.registry = nodes.validate(self.fixtures[self.ids[0]]["document"])
        self.pins = dict.fromkeys(self.ids, bridge.implementation_digest())
        self.commands = {node: [{"name": "synthetic", "argv": [str(Path(sys.executable).resolve()),
                         "-c", "import time;time.sleep(30)"]}] for node in self.ids}

    async def cleanup(self):
        for transport in self.transports:
            await transport.close()
            self.assertTrue(all(process.returncode is not None for _, _, process in transport.spawned))
        for node, daemon in zip(self.ids, self.daemons):
            if daemon.poll() is None:
                try:
                    await asyncio.to_thread(desktop_supervisor.request, self.fixtures[node]["control"], {"action": "shutdown"})
                    await asyncio.to_thread(daemon.wait, 5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    daemon.kill()
                    await asyncio.to_thread(daemon.wait, 3)
            daemon.stdout.close()
            daemon.stderr.close()
        self.temp.cleanup()
        self.assertFalse(self.root.exists())

    def transport(self, **kwargs):
        options = dict(registry=self.registry, session_id=self.session_id, revisions=self.revisions,
                       helper_pins=self.pins, fixtures=self.fixtures, timeout=4)
        options.update(kwargs)
        transport = LocalProcesses(**options)
        self.transports.append(transport)
        return transport

    def plan(self, node, **changes):
        return {"session_id": self.session_id, "revision": self.revisions[node], "local_id": node,
                "primary": self.ids[0], "members": self.ids, "commands": self.commands[node]} | changes

    async def state(self, node):
        reply = await asyncio.to_thread(desktop_supervisor.request, self.fixtures[node]["control"], {"action": "status"})
        return reply["session"]

    async def wait_state(self, node, states, timeout=5):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            state = await self.state(node)
            if state["state"] in states:
                return state
            await asyncio.sleep(0.02)
        self.fail("Synthetic supervisor did not reach expected state.")

    async def test_three_real_helpers_and_authenticated_supervisors_never_claim_desktop_ready(self):
        transport = self.transport()
        coordinator = desktop_coordinator.DesktopCoordinator(self.registry, self.revisions, self.commands,
                transport, session_id=self.session_id, timeout=4)
        report = await coordinator.start()
        self.assertFalse(report["ready"])
        self.assertEqual(report["state"], "waiting_ready")
        self.assertEqual(len(transport.spawned), 3)
        for node in self.ids:
            self.assertEqual((await self.state(node))["session_id"], self.session_id)
        await coordinator.stop()
        for node in self.ids:
            await self.wait_state(node, {"stopped"})
        self.assertEqual(sum(mode == "stop" for _, mode, _ in transport.spawned), 3)
        for _ in range(30):
            if (await coordinator.status())["cleanup_complete"]:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(coordinator.report()["cleanup_complete"])

    async def test_wrong_bundle_pin_is_rejected_before_prepare_has_effects(self):
        transport = self.transport(helper_pins=dict.fromkeys(self.ids, "f" * 64))
        payload = {"action": "prepare", "plan": self.plan(self.ids[0])}
        with self.assertRaises(bridge.BridgeError):
            await transport(self.ids[0], payload)
        self.assertEqual((await self.state(self.ids[0]))["state"], "idle")
        self.assertFalse((self.fixtures[self.ids[0]]["control"] / self.session_id).exists())

    async def test_wrong_catalog_revision_identity_and_group_are_rejected_before_effects(self):
        node = self.ids[0]
        transport = self.transport(revisions=self.revisions | {node: "f" * 64})
        payload = {"action": "prepare", "plan": self.plan(node, revision="f" * 64)}
        with self.assertRaises(bridge.BridgeError):
            await transport(node, payload)
        self.assertEqual((await self.state(node))["state"], "idle")
        transport = self.transport()
        payload = {"action": "prepare", "plan": self.plan(node, primary=self.ids[1])}
        with self.assertRaises(bridge.BridgeError):
            await transport(node, payload)
        self.assertEqual((await self.state(node))["state"], "idle")

    async def test_global_status_never_leaks_another_session_or_its_logs(self):
        node = self.ids[0]
        other = self.plan(node, session_id=str(uuid4()))
        await asyncio.to_thread(desktop_supervisor.request, self.fixtures[node]["control"], {"action": "prepare", "plan": other})
        transport = self.transport()
        with self.assertRaises(bridge.BridgeError) as caught:
            await transport(node, {"action": "status"})
        self.assertNotIn(other["session_id"], str(caught.exception))
        self.assertEqual((await self.state(node))["session_id"], other["session_id"])
        self.assertEqual((await self.state(node))["state"], "prepared")

    async def test_authenticated_endpoint_rejection_does_not_return_a_fake_success(self):
        node = self.ids[0]
        endpoint_path = self.fixtures[node]["control"] / "endpoint.json"
        endpoint = json.loads(endpoint_path.read_text())
        original = deepcopy(endpoint)
        endpoint["token"] = "deliberately-wrong-private-test-token"
        desktop_supervisor.atomic_json(endpoint_path, endpoint)
        transport = self.transport()
        payload = {"action": "prepare", "plan": self.plan(node)}
        with self.assertRaises(bridge.BridgeError) as caught:
            await transport(node, payload)
        self.assertNotIn(endpoint["token"], str(caught.exception))
        desktop_supervisor.atomic_json(endpoint_path, original)

    async def test_origin_eof_stops_only_its_owned_session_and_retires_control(self):
        node = self.ids[0]
        transport = self.transport()
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        await transport(node, {"action": "commit", "session_id": self.session_id})
        await self.wait_state(node, {"running"})
        await transport.close()
        await self.wait_state(node, {"stopped"})
        with self.assertRaises(bridge.BridgeError):
            await transport(node, {"action": "heartbeat", "session_id": self.session_id})

    async def test_old_channel_eof_and_emergency_cannot_stop_a_replacement_session(self):
        node = self.ids[0]
        transport = self.transport()
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        await asyncio.to_thread(desktop_supervisor.request, self.fixtures[node]["control"],
                                {"action": "stop_session", "session_id": self.session_id})
        await self.wait_state(node, {"stopped"})
        replacement = self.plan(node, session_id=str(uuid4()))
        await asyncio.to_thread(desktop_supervisor.request, self.fixtures[node]["control"], {"action": "prepare", "plan": replacement})
        with self.assertRaises(bridge.BridgeError):
            await transport(node, {"action": "stop_session", "session_id": self.session_id})
        await transport.close()
        self.assertEqual((await self.state(node))["session_id"], replacement["session_id"])
        self.assertEqual((await self.state(node))["state"], "prepared")

    async def test_concurrent_first_binds_never_overwrite_an_instance_pinned_during_io(self):
        node = self.ids[0]
        transport = self.transport()
        entered = {mode: asyncio.Event() for mode in ("control", "stop")}
        release = {mode: asyncio.Event() for mode in entered}
        instances = {mode: str(uuid4()) for mode in entered}

        class FakeChannel:
            def __init__(self, mode, binding):
                self.mode, self.binding, self.closed = mode, binding, False

            async def exchange(self, binding):
                entered[self.mode].set()
                await release[self.mode].wait()
                return {"ok": True, "binding": binding | {"instance": instances[self.mode]}}

            async def close(self):
                self.closed = True

        async def spawn(_node, mode):
            return mode

        transport._spawn = spawn
        with patch.object(bridge, "Channel", FakeChannel):
            old = asyncio.create_task(transport._open(node, "control"))
            emergency = asyncio.create_task(transport._open(node, "stop"))
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
                release["stop"].set()
                await emergency
                self.assertEqual(transport.bindings[node]["instance"], instances["stop"])
                release["control"].set()
                with self.assertRaises(bridge.BridgeError):
                    await old
                self.assertEqual(transport.bindings[node]["instance"], instances["stop"])
            finally:
                for event in release.values():
                    event.set()
                await asyncio.gather(old, emergency, return_exceptions=True)

    async def test_close_joins_pending_spawn_and_late_helper_receives_no_binding(self):
        node = self.ids[0]
        transport = self.transport()
        spawned = asyncio.Event()
        original_spawn = transport._spawn

        async def delayed_spawn(identity, mode):
            process = await original_spawn(identity, mode)
            spawned.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return process  # Deterministic completion at the close/spawn boundary.

        transport._spawn = delayed_spawn
        pending = asyncio.create_task(transport(node, {"action": "prepare", "plan": self.plan(node)}))
        try:
            await asyncio.wait_for(spawned.wait(), 2)
            await asyncio.wait_for(transport.close(), 3)
            with self.assertRaises(bridge.BridgeError):
                await pending
            self.assertTrue(all(process.returncode is not None for _, _, process in transport.spawned))
            self.assertFalse(transport.opening)
            self.assertEqual((await self.state(node))["state"], "idle")
            self.assertFalse((self.fixtures[node]["control"] / self.session_id).exists())
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_external_cancellation_propagates_when_close_cancels_a_pending_open(self):
        node = self.ids[0]
        transport = self.transport()
        entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def pending_open(_node, _mode):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()

        transport._open_channel = pending_open
        payload = {"action": "prepare", "plan": self.plan(node)}
        pending = asyncio.create_task(transport(node, payload))
        closer = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            closer = asyncio.create_task(transport.close())
            await asyncio.wait_for(closing.wait(), 2)
            pending.cancel()  # Caller cancellation after the independent close.
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(closer, 2)
            self.assertFalse(transport.opening)
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if closer is not None:
                await closer

    async def test_changed_supervisor_instance_is_never_automatically_adopted(self):
        node = self.ids[0]
        transport = self.transport(instances={node: str(uuid4())})
        payload = {"action": "prepare", "plan": self.plan(node)}
        with self.assertRaises(bridge.BridgeError):
            await transport(node, payload)
        self.assertEqual((await self.state(node))["state"], "idle")

    async def test_endpoint_instance_change_after_bind_blocks_commit_before_effects(self):
        node = self.ids[0]
        transport = self.transport()
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        endpoint_path = self.fixtures[node]["control"] / "endpoint.json"
        original = json.loads(endpoint_path.read_text())
        desktop_supervisor.atomic_json(endpoint_path, original | {"instance": str(uuid4())})
        try:
            with self.assertRaises(bridge.BridgeError):
                await transport(node, {"action": "commit", "session_id": self.session_id})
            self.assertEqual((await self.state(node))["state"], "prepared")
        finally:
            desktop_supervisor.atomic_json(endpoint_path, original)
        reply = await transport(node, {"action": "stop_session", "session_id": self.session_id})
        self.assertIn(reply["session"]["state"], {"stopping", "stopped"})

    @unittest.skipIf(sys.platform == "win32", "Windows DACL drift is covered by the endpoint's native suite")
    async def test_fresh_emergency_helper_preserves_authenticated_stop_after_token_acl_drift(self):
        node = self.ids[0]
        transport = self.transport()
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        endpoint_path = self.fixtures[node]["control"] / "endpoint.json"
        endpoint_path.chmod(0o644)
        try:
            reply = await transport(node, {"action": "stop_session", "session_id": self.session_id})
            self.assertIn(reply["session"]["state"], {"stopping", "stopped"})
            self.assertEqual(endpoint_path.stat().st_mode & 0o777, 0o644)
        finally:
            endpoint_path.chmod(0o600)
        await self.wait_state(node, {"stopped"})

    async def test_catalog_change_blocks_heartbeat_but_preserves_scoped_emergency_stop(self):
        node = self.ids[0]
        transport = self.transport()
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        document = deepcopy(self.fixtures[node]["document"])
        document["nodes"][0]["label"] = "Renamed while connected"
        self.fixtures[node]["registry"].write_text(json.dumps(document))
        with self.assertRaises(bridge.BridgeError):
            await transport(node, {"action": "heartbeat", "session_id": self.session_id})
        reply = await transport(node, {"action": "stop_session", "session_id": self.session_id})
        self.assertEqual(reply["session"]["state"], "stopped")

    async def test_published_bom_catalog_preserves_fresh_stop_and_terminal_status(self):
        node = self.ids[0]
        transport = self.transport()
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        await transport(node, {"action": "commit", "session_id": self.session_id})
        await self.wait_state(node, {"running"})
        document = deepcopy(self.fixtures[node]["document"])
        document["nodes"][0]["label"] = "Valid BOM catalog published while active"
        stage = self.fixtures[node]["registry"].with_name("bom-candidate.json")
        stage.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode("utf-8"))
        stage.replace(self.fixtures[node]["registry"])
        self.assertNotEqual(nodes.snapshot(self.fixtures[node]["registry"])["revision"], self.revisions[node])
        reply = await transport(node, {"action": "stop_session", "session_id": self.session_id})
        self.assertIn(reply["session"]["state"], {"stopping", "stopped"})
        await self.wait_state(node, {"stopped"})
        reply = await transport(node, {"action": "status"})
        self.assertEqual(reply["session"]["state"], "stopped")
        self.assertEqual(reply["session"]["session_id"], self.session_id)
        self.assertEqual(reply["session"]["revision"], self.revisions[node])
        self.assertEqual(reply["session"]["instance"], transport.bindings[node]["instance"])

    async def test_emergency_channel_bypasses_a_blocked_control_rpc_and_no_mutation_retries(self):
        node = self.ids[0]
        entered = self.root / "entered"
        wrapper = self.root / "blocked-helper.py"
        wrapper.write_text("import sys,time\nfrom pathlib import Path\n"
                          f"sys.path.insert(0,{str(Path(bridge.__file__).parent)!r})\n"
                          "import desktop_ssh as b\noriginal=b.Helper.call\n"
                          "def blocked(self,payload):\n"
                          " result=original(self,payload)\n"
                          " if payload['action']=='heartbeat':\n"
                          f"  Path({str(entered)!r}).write_text('entered')\n"
                          "  time.sleep(30)\n return result\n"
                          "b.Helper.call=blocked\nraise SystemExit(b.main())\n")
        transport = self.transport(timeout=3)
        transport.control_wrapper = wrapper
        await transport(node, {"action": "prepare", "plan": self.plan(node)})
        blocked = asyncio.create_task(transport(node, {"action": "heartbeat", "session_id": self.session_id}))
        try:
            deadline = monotonic() + 3
            while not entered.exists() and monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(entered.exists())
            started = monotonic()
            reply = await transport(node, {"action": "stop_session", "session_id": self.session_id})
            self.assertIn(reply["session"]["state"], {"stopping", "stopped"})
            self.assertLess(monotonic() - started, 2)
            await self.wait_state(node, {"stopped"})
        finally:
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
        self.assertEqual(sum(mode == "control" for _, mode, _ in transport.spawned), 1)
        with self.assertRaises(bridge.BridgeError):
            await transport(node, {"action": "commit", "session_id": self.session_id})

    async def test_default_local_node_path_uses_real_helper_without_ssh(self):
        node = self.ids[0]
        transport = bridge.DesktopSSHTransport(self.registry, self.session_id, self.revisions, self.pins,
                    local_directory=self.fixtures[node]["control"], local_registry_path=self.fixtures[node]["registry"])
        self.addAsyncCleanup(transport.close)
        reply = await transport(node, {"action": "prepare", "plan": self.plan(node)})
        self.assertEqual(reply["session"]["local_id"], node)
        self.assertNotIn("components", reply["session"])


if __name__ == "__main__":
    unittest.main()
