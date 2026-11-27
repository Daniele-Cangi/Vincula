# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""App lifecycle with synthetic node replies. No desktop, SSH or credentials."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from queue import Queue
import sys
import subprocess
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch
from uuid import uuid4

import desktop_app as app
from test_desktop_coordinator import Network


class Output:
    def __init__(self):
        self.events = []
        self.queue = Queue()

    def send(self, state, message, complete=False):
        self.events.append((state, message, complete))


class AppPipeTests(unittest.TestCase):
    def fixture(self, source):
        # Keep both pipes open while waiting for exit: communicate() would close
        # stdin and hide a daemon holding CPython's BufferedReader lock.
        prefix = "import sys;sys.path.insert(0," + repr(str(Path(app.__file__).parent)) + ");"
        child = subprocess.Popen([sys.executable, "-B", "-W", "error", "-c", prefix + source],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.wait(timeout=4)
            errors = child.stderr.read()
            self.assertEqual(child.returncode, 0, errors.decode("utf-8", "replace"))
            self.assertNotIn(b"_enter_buffered_busy", errors)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_exit_with_gui_stdin_still_open_does_not_hold_a_buffered_lock(self):
        self.fixture("from desktop_app import watch_input;from threading import Event,Thread;"
                     "import time;Thread(target=watch_input,args=(Event(),),daemon=True).start();time.sleep(.15)")

    def test_exit_with_blocked_gui_stdout_does_not_hold_a_buffered_lock(self):
        self.fixture("from desktop_app import StatusOutput;from threading import Event;import time;"
                     "o=StatusOutput(Event());o.queue.put({'payload':'x'*1048576});time.sleep(.15)")


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ids = [str(uuid4()) for _ in range(3)]
        family = "windows" if sys.platform == "win32" else "linux"
        self.document = {"local": self.ids[0], "nodes": [
            {"id": node, "label": "Synthetic", "family": family if index == 0 else "windows" if index == 1 else "linux"}
            for index, node in enumerate(self.ids)], "desktop": {"primary": self.ids[1], "clipboard": True,
                "members": [{"node_id": node, "x": index, "y": 0} for index, node in enumerate(self.ids)]}}
        self.stopped, self.output = Event(), Output()
        self.calls, self.transports = [], []
        self.gate = asyncio.Event()
        self.release = asyncio.Event()

    def patches(self, stack, *, block_heartbeat=False, inspect_failure=False, delay_terminal=False):
        owner = self
        class Requests:
            def __init__(self, catalog):
                pass

            async def __call__(self, node, payload):
                owner.calls.append((node, deepcopy(payload)))
                action = payload["action"]
                if action == "inspect":
                    if inspect_failure:
                        raise ValueError("Synthetic unavailable")
                    return {"node_id": node, "helper_sha256": "b" * 64, "port": 24807, "revision": "c" * 64}
                if action == "provision":
                    return {"node_id": node, "session_id": payload["session_id"], "revision": "c" * 64,
                            "helper_sha256": "b" * 64, "pin": str(owner.ids.index(node) + 1) * 64}
                group = payload["group"]
                settings = str(Path.cwd() / (node + ".conf"))
                config = {"group": group, "node_id": node, "owner_consent": True,
                          "settings_file": settings, "state_file": settings + ".state", "binary_sha256": "d" * 64}
                return {"node_id": node, "command": {"name": "engine", "argv": [sys.executable,
                    "server" if node == group["primary"] else "client", "--settings", settings], "managed": config}}

        class Transport(Network):
            def __init__(self, catalog, session, revisions, pins):
                super().__init__(owner.ids, monotonic)
                self.closed, self.stops = False, {}
                owner.transports.append(self)
                if block_heartbeat:
                    self.gates[(owner.ids[0], "heartbeat")] = owner.gate, owner.release

            async def __call__(self, node, request):
                response = await super().__call__(node, request)
                state = response["session"]
                state["desktop_ready_remaining"] = 1.5 if state.get("desktop_ready") else 0
                if delay_terminal and request["action"] == "stop_session":
                    self.stops[node] = monotonic()
                    state.update(state="stopping", desktop_ready=False)
                elif delay_terminal and request["action"] == "status" and node in self.stops:
                    if monotonic() - self.stops[node] < 0.15:
                        state.update(state="stopping", desktop_ready=False)
                return response

            async def close(self):
                self.closed = True

        stack.enter_context(patch.object(app.nodes, "snapshot", return_value={"registry": self.document, "revision": "c" * 64}))
        stack.enter_context(patch.object(app.desktop_ssh, "implementation_digest", return_value="b" * 64))
        stack.enter_context(patch.object(app, "NodeRequests", Requests))
        stack.enter_context(patch.object(app.desktop_ssh, "DesktopSSHTransport", Transport))

    async def test_cancel_before_start_does_not_provision_or_contact_nodes(self):
        self.stopped.set()
        with ExitStack() as stack:
            self.patches(stack)
            await app.run(self.stopped, self.output)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.output.events[-1][2])
        self.assertFalse(self.transports)

    async def test_unavailable_runtime_does_not_provision_or_claim_ready(self):
        with ExitStack() as stack:
            self.patches(stack, inspect_failure=True)
            await app.run(self.stopped, self.output)
        self.assertEqual({payload["action"] for _, payload in self.calls}, {"inspect"})
        self.assertFalse(any(state == "ready" for state, _, _ in self.output.events))
        self.assertTrue(self.output.events[-1][2])
        self.assertEqual(self.output.events[-1][0], "failed")
        self.assertIn("Gruppo non disponibile", self.output.events[-1][1])

    async def test_stop_is_independent_of_pending_heartbeat_and_waits_for_terminal_cleanup(self):
        with ExitStack() as stack:
            self.patches(stack, block_heartbeat=True, delay_terminal=True)
            runner = asyncio.create_task(app.run(self.stopped, self.output))
            try:
                await asyncio.wait_for(self.gate.wait(), 2)
                before = monotonic()
                self.stopped.set()
                while not self.transports[0].stops:
                    await asyncio.sleep(0.005)
                    self.assertLess(monotonic() - before, 0.5)
                self.assertFalse(self.release.is_set(), "Stop must not wait for the pending heartbeat")
                self.release.set()
                await asyncio.wait_for(runner, 3)
            finally:
                self.stopped.set()
                self.release.set()
                await asyncio.wait_for(runner, 3)
        self.assertTrue(self.transports[0].closed)
        self.assertEqual(self.output.events[-1][0], "stopped")
        self.assertTrue(self.output.events[-1][2])
        self.assertFalse(any(payload["action"] in {"stop", "shutdown"} for _, payload in self.transports[0].calls))


if __name__ == "__main__":
    unittest.main()
