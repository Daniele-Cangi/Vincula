# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

import desktop_coordinator as coordinator
import desktop_supervisor
import node_registry


def topology():
    ids = [str(uuid4()) for _ in range(3)]
    registry = node_registry.Registry(ids[0], [node_registry.Node(node, "Synthetic node", "linux") for node in ids])
    registry.desktop = node_registry.Desktop(ids[0], tuple(node_registry.DesktopMember(node, index, 0)
                                                          for index, node in enumerate(ids)), True)
    revisions = {node: str(index + 1) * 64 for index, node in enumerate(ids)}
    commands = {node: [{"name": "synthetic", "argv": [sys.executable, "-c", "import time;time.sleep(30)"]}] for node in ids}
    return ids, registry, revisions, commands


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Network:
    """Future readiness is synthetic here; this does not attest actual desktop input."""

    def __init__(self, ids, clock):
        self.clock, self.ids = clock, ids
        self.instances = {node: str(uuid4()) for node in ids}
        self.plans, self.states, self.deadlines = {}, {}, {}
        self.ready = dict.fromkeys(ids, True)
        self.calls, self.offline, self.failures, self.gates, self.corrupt = [], set(), {}, {}, {}

    def status(self, node):
        plan = self.plans.get(node)
        if plan is None:
            return {"instance": self.instances[node], "session_id": None, "state": "idle", "desktop_ready": False}
        if self.states[node] in coordinator.ACTIVE and self.clock() >= self.deadlines[node]:
            self.states[node] = "expired"
        active = self.states[node] in coordinator.ACTIVE
        return {key: deepcopy(plan[key]) for key in ("session_id", "revision", "local_id", "primary", "members")} | {
            "instance": self.instances[node], "state": self.states[node],
            "desktop_ready": self.states[node] == "running" and self.ready[node],
            "lease_remaining": max(0, self.deadlines[node] - self.clock()) if active else None}

    async def __call__(self, node, request):
        action = request["action"]
        self.calls.append((node, deepcopy(request)))
        if node in self.offline:
            raise OSError("synthetic unavailable")
        failure = self.failures.get((node, action))
        if failure == "reject":
            return {"ok": False, "error": "Synthetic approval or operation rejected."}
        if action == "prepare":
            self.plans[node] = deepcopy(request["plan"])
            self.states[node], self.deadlines[node] = "prepared", self.clock() + 5
        elif action == "stop_session":
            if self.plans.get(node, {}).get("session_id") != request["session_id"]:
                return {"ok": False, "error": "The session ID does not match."}
            self.states[node] = "stopped"
        elif action != "status":
            current = self.status(node)
            if current.get("session_id") != request["session_id"] or current["state"] not in coordinator.ACTIVE:
                return {"ok": False, "error": "Session mismatch or expired lease."}
            if action == "commit":
                if self.states[node] != "prepared":
                    return {"ok": False, "error": "Not prepared."}
                self.states[node] = "running"
            elif action == "heartbeat":
                self.deadlines[node] = self.clock() + 5
            else:
                raise AssertionError("No global stop/shutdown belongs in the coordinator.")
        response = {"ok": True, "session": self.status(node)}
        if (node, action) in self.gates:
            entered, release = self.gates[(node, action)]
            entered.set()
            await release.wait()
        if failure == "lost_ack":
            raise asyncio.TimeoutError()
        response["session"].update(self.corrupt.get((node, action), {}))
        return response


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ids, self.registry, self.revisions, self.commands = topology()
        self.clock = Clock()
        self.network = Network(self.ids, self.clock)

    def controller(self, **options):
        return coordinator.DesktopCoordinator(self.registry, self.revisions, self.commands, self.network,
                                             clock=self.clock, timeout=0.1, **options)

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("The controlled async operation did not reach its barrier.")

    def assert_scoped_cleanup(self, controller):
        stops = [(node, request) for node, request in self.network.calls if request["action"] == "stop_session"]
        self.assertEqual({node for node, _ in stops}, set(self.ids))
        self.assertTrue(all(request == {"action": "stop_session", "session_id": controller.session_id}
                            for _, request in stops))
        self.assertFalse(any(request["action"] in {"stop", "shutdown"} for _, request in self.network.calls))

    async def test_prepare_barrier_fresh_status_and_effective_readiness(self):
        controller = self.controller()
        self.network.ready[self.ids[-1]] = False
        report = await controller.start()
        self.assertEqual(report["state"], "waiting_ready")
        actions = [request["action"] for _, request in self.network.calls]
        self.assertEqual(actions[:3], ["prepare"] * 3)
        self.assertEqual(actions[3:6], ["commit"] * 3)
        self.assertEqual(actions[6:9], ["status"] * 3)
        self.network.ready[self.ids[-1]] = True
        self.assertTrue((await controller.status())["ready"])
        with self.assertRaises(ValueError):
            await controller.start()

    async def test_cached_report_stops_claiming_ready_when_known_lease_expires(self):
        controller = self.controller()
        self.assertTrue((await controller.start())["ready"])
        before = deepcopy(self.network.calls)
        self.clock.now += 5
        report = controller.report()
        self.assertFalse(report["ready"])
        self.assertEqual(report["state"], "waiting_ready")
        self.assertFalse(controller._failed, "A cache read must not perform async failure/cleanup.")
        self.assertEqual(self.network.calls, before)

    async def test_cached_report_is_not_ready_during_partially_completed_status(self):
        controller = self.controller()
        await controller.start()
        node, blocked = self.ids[:2]
        self.network.ready[node] = False
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(blocked, "status")] = entered, release
        refreshing = asyncio.create_task(controller.status())
        try:
            await entered.wait()
            await self.until(lambda: controller._records[node]["status"]["desktop_ready"] is False)
            report = controller.report()
            self.assertFalse(report["ready"])
            self.assertEqual(report["state"], "waiting_ready")
            self.assertFalse(controller._failed)
            self.assertFalse(any(request["action"] == "stop_session" for _, request in self.network.calls))
        finally:
            release.set()
        self.assertEqual((await refreshing)["state"], "failed")

    async def test_status_calls_cannot_overtake_one_peers_older_readiness_reply(self):
        node = self.ids[0]
        self.network.ready[node] = False
        controller = self.controller()
        self.assertEqual((await controller.start())["state"], "waiting_ready")
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(node, "status")] = entered, release
        older = asyncio.create_task(controller.status())
        await entered.wait()
        calls_before = sum(peer == node and request["action"] == "status" for peer, request in self.network.calls)
        self.network.ready[node] = True
        newer = asyncio.create_task(controller.status())
        try:
            await self.until(lambda: sum(peer == self.ids[1] and request["action"] == "status"
                                        for peer, request in self.network.calls) >= 3)
            self.assertEqual(sum(peer == node and request["action"] == "status" for peer, request in self.network.calls), calls_before)
        finally:
            release.set()
        self.assertNotEqual((await older)["state"], "failed")
        self.assertTrue((await newer)["ready"])
        self.assertFalse(any(request["action"] == "stop_session" for _, request in self.network.calls))

    async def test_old_status_cannot_overwrite_a_later_confirmed_heartbeat(self):
        node = self.ids[0]
        controller = self.controller()
        await controller.start()
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(node, "status")] = entered, release
        refreshing = asyncio.create_task(controller.status())
        await entered.wait()
        self.clock.now += 1
        renewing = asyncio.create_task(controller.heartbeat())
        try:
            await self.until(lambda: all(self.network.deadlines[peer] == 106 for peer in self.ids[1:]))
            self.assertEqual(self.network.deadlines[node], 105)
            self.assertFalse(any(peer == node and request["action"] == "heartbeat" for peer, request in self.network.calls))
        finally:
            release.set()
        self.assertNotEqual((await refreshing)["state"], "failed")
        self.assertTrue((await renewing)["ready"])
        self.assertEqual(controller._records[node]["lease_deadline"], 106)
        self.clock.now = 105.1
        self.assertTrue(controller.report()["ready"])
        self.assertFalse(any(request["action"] == "stop_session" for _, request in self.network.calls))

    async def test_rejected_prepare_and_lost_commit_ack_rollback_every_peer(self):
        for action, failure in (("prepare", "reject"), ("commit", "lost_ack")):
            self.network = Network(self.ids, self.clock)
            self.network.failures[(self.ids[1], action)] = failure
            controller = self.controller()
            report = await controller.start()
            self.assertEqual(report["state"], "failed")
            self.assertEqual(report["nodes"][self.ids[1]]["error"]["phase"], action)
            self.assert_scoped_cleanup(controller)
            if action == "prepare":
                self.assertFalse(any(request["action"] == "commit" for _, request in self.network.calls))
            self.assertTrue(all(state == "stopped" for state in self.network.states.values()))

    async def test_offline_peer_does_not_short_circuit_cleanup_or_hide_errors(self):
        self.network.offline.add(self.ids[1])
        controller = self.controller()
        report = await controller.start()
        self.assertEqual(report["state"], "failed")
        self.assertFalse(report["cleanup_complete"])
        self.assertIsNotNone(report["nodes"][self.ids[1]]["stop_error"])
        self.assert_scoped_cleanup(controller)
        self.assertTrue(all(state == "stopped" for state in self.network.states.values()))

    async def test_heartbeat_failure_expiry_and_readiness_loss_never_resume(self):
        for failure in ("expiry", "offline", "readiness"):
            self.network = Network(self.ids, self.clock)
            controller = self.controller()
            self.assertTrue((await controller.start())["ready"])
            if failure == "expiry":
                self.clock.now += 5
                report = await controller.heartbeat()
            elif failure == "offline":
                self.network.offline.add(self.ids[0])
                report = await controller.heartbeat()
            else:
                self.network.ready[self.ids[0]] = False
                report = await controller.status()
            self.assertEqual(report["state"], "failed")
            self.assert_scoped_cleanup(controller)
            self.network.offline.clear()
            self.network.ready = dict.fromkeys(self.ids, True)
            self.assertFalse((await controller.status())["ready"])
            with self.assertRaises(ValueError):
                await controller.heartbeat()

    async def test_session_instance_revision_and_local_identity_are_bound_to_acks(self):
        for field, value in (("session_id", str(uuid4())), ("instance", str(uuid4())),
                             ("revision", "f" * 64), ("local_id", str(uuid4()))):
            self.network = Network(self.ids, self.clock)
            self.network.corrupt[(self.ids[1], "commit")] = {field: value}
            controller = self.controller()
            self.assertEqual((await controller.start())["state"], "failed")
            self.assert_scoped_cleanup(controller)

    async def test_expired_delayed_reply_cannot_report_ready(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(self.ids[1], "status")] = entered, release
        controller = self.controller()
        starting = asyncio.create_task(controller.start())
        await entered.wait()
        self.clock.now += 6
        release.set()
        self.assertEqual((await starting)["state"], "failed")
        self.assert_scoped_cleanup(controller)

    async def test_heartbeat_and_stop_are_independent_of_blocked_startup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(self.ids[1], "prepare")] = entered, release
        controller = self.controller()
        starting = asyncio.create_task(controller.start())
        await entered.wait()
        await self.until(lambda: sum(record["prepared"] for record in controller._records.values()) == 2)
        await controller.heartbeat()
        self.assertTrue(any(request["action"] == "heartbeat" for _, request in self.network.calls))
        stopped = await controller.stop()
        self.assertEqual(stopped["state"], "stopped")
        release.set()
        self.assertFalse((await starting)["ready"])
        self.assertFalse(any(request["action"] == "commit" for _, request in self.network.calls))

    async def test_timeout_or_task_cancel_rolls_back_without_retrying_start(self):
        for cancel in (False, True):
            self.network = Network(self.ids, self.clock)
            entered, release = asyncio.Event(), asyncio.Event()
            self.network.gates[(self.ids[1], "prepare")] = entered, release
            controller = self.controller()
            task = asyncio.create_task(controller.start())
            await entered.wait()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                self.assertEqual((await task)["state"], "failed")
            self.assert_scoped_cleanup(controller)
            self.assertEqual(sum(request["action"] == "prepare" for _, request in self.network.calls), 3)
            self.assertFalse(any(request["action"] == "commit" for _, request in self.network.calls))

    async def test_old_origin_stop_cannot_stop_a_new_session(self):
        controller = self.controller()
        await controller.start()
        node = self.ids[0]
        other = str(uuid4())
        self.network.plans[node]["session_id"] = other
        self.network.states[node] = "prepared"
        report = await controller.stop()
        self.assertFalse(report["ready"])
        self.assertIsNotNone(report["nodes"][node]["stop_error"])
        self.assertEqual(self.network.plans[node]["session_id"], other)
        self.assertEqual(self.network.states[node], "prepared")
        self.assert_scoped_cleanup(controller)

    async def test_stop_does_not_wait_for_heartbeat_or_adopt_its_late_active_ack(self):
        controller = self.controller()
        await controller.start()
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(self.ids[0], "heartbeat")] = entered, release
        renewing = asyncio.create_task(controller.heartbeat())
        await entered.wait()
        self.assertEqual((await controller.stop())["state"], "stopped")
        release.set()
        report = await renewing
        self.assertEqual(report["state"], "stopped")
        self.assertFalse(report["ready"])
        self.assertTrue(all(record["status"]["state"] == "stopped" for record in report["nodes"].values()))

    async def test_stop_bypasses_status_lock_and_queued_heartbeat_on_that_peer(self):
        node = self.ids[0]
        controller = self.controller()
        await controller.start()
        entered, release = asyncio.Event(), asyncio.Event()
        self.network.gates[(node, "status")] = entered, release
        refreshing = asyncio.create_task(controller.status())
        await entered.wait()
        renewing = asyncio.create_task(controller.heartbeat())
        try:
            await self.until(lambda: all(any(peer == other and request["action"] == "heartbeat"
                                            for peer, request in self.network.calls) for other in self.ids[1:]))
            self.assertFalse(any(peer == node and request["action"] == "heartbeat" for peer, request in self.network.calls))
            self.assertEqual((await controller.stop())["state"], "stopped")
            self.assertFalse(refreshing.done())
            self.assertFalse(renewing.done())
        finally:
            release.set()
        self.assertEqual((await refreshing)["state"], "stopped")
        self.assertEqual((await renewing)["state"], "stopped")

    async def test_control_budget_includes_waiting_for_one_peer_lock(self):
        node = self.ids[0]
        controller = self.controller()
        await controller.start()
        lock = controller._node_locks[node]
        await lock.acquire()
        try:
            report = await controller.heartbeat()
            self.assertEqual(report["state"], "failed")
            self.assertIn("queue timeout", report["nodes"][node]["error"]["message"])
            self.assertFalse(any(peer == node and request["action"] == "heartbeat" for peer, request in self.network.calls))
            self.assert_scoped_cleanup(controller)
        finally:
            lock.release()

    async def test_plan_is_frozen_and_remote_platform_paths_are_not_probed(self):
        self.commands[self.ids[1]] = [{"name": "input", "argv": [r"C:\external\engine.exe"], "cwd": r"C:\external"}]
        controller = self.controller()
        planned = controller.prepared_plan()
        planned["nodes"][self.ids[1]]["commands"].clear()
        self.commands[self.ids[0]].clear()
        self.assertTrue(controller.prepared_plan()["nodes"][self.ids[0]]["commands"])
        self.assertTrue(controller.prepared_plan()["nodes"][self.ids[1]]["commands"])
        self.assertEqual(self.network.calls, [])


class RealSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, expire=False):
        ids, registry, revisions, commands = topology()
        with TemporaryDirectory(prefix="vincula-coordinator-") as temporary, ExitStack() as cleanup:
            supervisors = {}
            for node in ids:
                supervisor = desktop_supervisor.Supervisor(Path(temporary) / node, lease_seconds=1.5, guardian_lease=0.7)
                supervisors[node] = supervisor
                cleanup.callback(supervisor.close)
            calls = []

            async def local(node, request):
                calls.append((node, deepcopy(request)))
                supervisor = supervisors[node]
                action = request["action"]
                try:
                    if action == "prepare":
                        result = supervisor.prepare(request["plan"])
                    elif action == "status":
                        result = supervisor.status()
                    elif action == "stop_session":
                        result = supervisor.stop(request["session_id"])
                    else:
                        result = getattr(supervisor, action)(request["session_id"])
                    return {"ok": True, "session": result}
                except ValueError as error:
                    return {"ok": False, "error": str(error)}

            controller = coordinator.DesktopCoordinator(registry, revisions, commands, local, timeout=1)
            report = await controller.start()
            self.assertFalse(report["ready"], "Real Supervisor.desktop_ready is always False in this increment.")
            for _ in range(250):
                report = await controller.status()
                self.assertFalse(report["ready"])
                if all(value["state"] == "running" for value in (record["status"] for record in report["nodes"].values())):
                    break
                await controller.heartbeat()
                await asyncio.sleep(0.02)
            self.assertTrue(all(supervisor.status()["state"] == "running" for supervisor in supervisors.values()))
            self.assertEqual([request["action"] for _, request in calls[:3]], ["prepare"] * 3)
            if expire:
                await asyncio.sleep(1.6)
                for _ in range(100):
                    if all(supervisor.status()["state"] == "expired" for supervisor in supervisors.values()):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(all(supervisor.status()["state"] == "expired" for supervisor in supervisors.values()))
                self.assertEqual((await controller.status())["state"], "failed")
            else:
                await controller.stop()
                for _ in range(40):
                    if (await controller.status())["cleanup_complete"]:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(controller.report()["state"], "stopped")
                node = ids[0]
                replacement = deepcopy(controller.prepared_plan()["nodes"][node])
                replacement["session_id"] = str(uuid4())
                supervisors[node].prepare(replacement)
                await controller.stop()
                self.assertEqual(supervisors[node].status()["state"], "prepared")
                self.assertFalse(supervisors[node].stop_event.is_set())
            self.assertFalse(any(request["action"] in {"stop", "shutdown"} for _, request in calls))

    async def test_three_real_supervisors_never_infer_ready_and_stop_is_scoped(self):
        await self.exercise()

    async def test_origin_loss_expires_three_real_supervisors_without_resume(self):
        await self.exercise(expire=True)


if __name__ == "__main__":
    unittest.main()
