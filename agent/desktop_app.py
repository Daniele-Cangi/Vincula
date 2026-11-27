# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Foreground desktop group owner for the GTK and WinForms applications.

stdout contains bounded status JSON lines. A stop line or stdin EOF cancels the
group on independent channels; application work/transfer locks are never used.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from queue import Queue, Full
import subprocess
import sys
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_backend as backend
import desktop_coordinator
import desktop_node
import desktop_ssh
import desktop_supervisor
import node_registry
import node_ssh
import nodes


def registry(document):
    # Explicit registries need no MCP dependencies. Retain the original Windows
    # Ethernet alias while that operator-owned catalog is being migrated.
    legacy = nodes.validation_legacy()
    if sys.platform == "win32":
        config = Path(os.environ["LOCALAPPDATA"]) / "Linux Dock/ssh_config"
        legacy = node_registry.Registry("windows", (
            node_registry.Node("windows", "Windows", "windows"),
            node_registry.Node("ubuntu", "Ubuntu", "linux", route=node_registry.Route(
                "linux-dock", ("-F", str(config)), (config,), "Ethernet"))))
    elif any(row.get("legacy_route") for row in document["nodes"]):
        raise ValueError("Migrate the Linux desktop catalog to explicit SSH routes first.")
    return node_registry.from_document(document, legacy, node_ssh.EMPTY_CONFIG)


def fixed_node_command(family):
    if family == "linux":
        return 'python3 -IB "$HOME/.local/share/vincula-desktop/desktop_node.py"'
    if family != "windows":
        raise ValueError("Unsupported desktop platform.")
    source = ("import os,runpy,sys;from pathlib import Path;"
              "p=Path(os.environ['LOCALAPPDATA'])/'Vincula-Desktop'/'desktop_node.py';"
              "sys.path.insert(0,str(p.parent));sys.argv=[str(p)];runpy.run_path(str(p),run_name='__main__')")
    return 'py.exe -3 -IB -c "' + source + '"'


class NodeRequests:
    def __init__(self, catalog):
        self.catalog = catalog
        self.children = set()

    async def __call__(self, node_id, payload):
        node = self.catalog.get(node_id)
        if node_id == self.catalog.local_id:
            argv = [sys.executable, "-IB", str(Path(__file__).with_name("desktop_node.py"))]
        else:
            if node.route is None or not node.route.configured():
                raise ValueError("A group member has no usable SSH route.")
            argv = [str(node_ssh.executable("ssh")), "-T", *node_ssh.OPTIONS, *node.route.options,
                    node.route.destination, fixed_node_command(node.family)]
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=node_ssh.environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.children.add(process)
        try:
            raw = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
            if len(raw) > 65536:
                raise ValueError("Desktop node request too large.")
            # Installed peers emit a small bounded reply. A bounded reader also
            # rejects a broken peer instead of buffering arbitrary diagnostics.
            process.stdin.write(raw)
            await process.stdin.drain()
            process.stdin.close()
            async def bounded_output():
                output = bytearray()
                while True:
                    block = await process.stdout.read(min(8192, 65537 - len(output)))
                    if not block:
                        return bytes(output)
                    output.extend(block)
                    if len(output) > 65536:
                        raise ValueError("Desktop node response too large.")
            output = await asyncio.wait_for(bounded_output(), timeout=30)
            await asyncio.wait_for(process.wait(), timeout=3)
            reply = desktop_ssh.decode(output)
            if process.returncode or not isinstance(reply, dict) or reply.get("ok") is not True:
                raise ValueError("A desktop node is unavailable, occupied or needs installation.")
            if set(reply) != {"ok", "result"} or not isinstance(reply["result"], dict):
                raise ValueError("Invalid desktop node reply.")
            return reply["result"]
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self.children.discard(process)


class StatusOutput:
    """A stalled GUI reader must not block the owner's emergency-stop loop."""
    def __init__(self, stopped):
        self.stopped, self.queue = stopped, Queue(maxsize=4)
        Thread(target=self.write, name="desktop-status", daemon=True).start()

    def write(self):
        try:
            while True:
                line = self.queue.get()
                try:
                    if line is None:
                        return
                    raw = memoryview((json.dumps(line, ensure_ascii=True) + "\n").encode("ascii"))
                    while raw:
                        raw = raw[os.write(sys.stdout.fileno(), raw):]
                finally:
                    self.queue.task_done()
        except (OSError, ValueError):
            self.stopped.set()

    def send(self, state, message, complete=False):
        try:
            self.queue.put_nowait({"type": "status", "state": state, "message": message,
                                   "cleanup_complete": complete})
        except Full:
            self.stopped.set()


def watch_input(stopped):
    try:
        pending = bytearray()
        while True:
            block = os.read(sys.stdin.fileno(), 513)
            if not block:
                break
            pending.extend(block)
            while b"\n" in pending:
                index = pending.index(b"\n")
                if index > 512:
                    return
                line, pending = pending[:index], pending[index + 1:]
                if desktop_ssh.decode(line) == {"action": "stop"}:
                    return
            if len(pending) > 512:
                break
    except (OSError, ValueError, UnicodeError):
        pass
    finally:
        stopped.set()


async def run(stopped, output):
    controller = transport = None
    stop_task = None
    failure_message = None
    pending = set()
    try:
        before = nodes.snapshot(desktop_ssh.default_registry_path())
        catalog = registry(before["registry"])
        if catalog.desktop is None or catalog.get(catalog.desktop.primary).family != "windows":
            raise ValueError("Save a desktop group with Windows as its primary first.")
        members = sorted(member.node_id for member in catalog.desktop.members)
        if catalog.local_id not in members:
            raise ValueError("Include this computer in the selected desktop group.")
        session = str(uuid4())
        requests = NodeRequests(catalog)
        async def batch(payloads):
            if stopped.is_set():
                raise asyncio.CancelledError()
            tasks = [asyncio.create_task(requests(node, payloads[node])) for node in members]
            pending.update(tasks)
            try:
                while not all(task.done() for task in tasks):
                    if stopped.is_set():
                        raise asyncio.CancelledError()
                    await asyncio.sleep(0.05)
                results = await asyncio.gather(*tasks)
                return dict(zip(members, results))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                pending.difference_update(tasks)
        output.send("starting", "Verifica dei nodi e dei componenti desktop installati…")
        facts = await batch({node: {"action": "inspect", "node_id": node} for node in members})
        pin = desktop_ssh.implementation_digest()
        if any(row.get("node_id") != node or row.get("helper_sha256") != pin for node, row in facts.items()):
            raise ValueError("Desktop helpers differ from the installed release.")
        if len({row.get("port") for row in facts.values()}) != 1:
            raise ValueError("Desktop nodes must use the same authorized data port.")
        provisioned = await batch({node: {"action": "provision", "node_id": node, "session_id": session,
            "desktop": before["registry"]["desktop"], "expected_revision": facts[node]["revision"]} for node in members})
        if any(row.get("node_id") != node or row.get("session_id") != session or row.get("helper_sha256") != pin
               for node, row in provisioned.items()):
            raise ValueError("The provision receipt does not identify the selected node and release.")
        group = backend.GroupManifest(session, catalog.desktop.primary,
            tuple(backend.Screen(m.node_id, m.x, m.y) for m in catalog.desktop.members), catalog.desktop.clipboard,
            tuple((node, provisioned[node]["pin"]) for node in members),
            tuple((node, provisioned[node]["revision"]) for node in members))
        configured = await batch({node: {"action": "configure", "node_id": node,
                                         "group": group.to_document()} for node in members})
        revisions = dict(group.revisions)
        if stopped.is_set():
            raise asyncio.CancelledError()
        transport = desktop_ssh.DesktopSSHTransport(catalog, session, revisions, dict.fromkeys(members, pin))
        controller = desktop_coordinator.DesktopCoordinator(catalog, revisions,
            {node: [configured[node]["command"]] for node in members}, transport, session_id=session, timeout=2)
        if stopped.is_set():
            controller = None  # No coordinator operation has been scheduled.
            raise asyncio.CancelledError()
        async def emergency_stop():
            while not stopped.is_set():
                await asyncio.sleep(0.02)
            output.send("stopping", "Arresto del gruppo in corso…")
            report = await controller.stop()
            deadline = monotonic() + 6
            while not report["cleanup_complete"] and monotonic() < deadline:
                await asyncio.sleep(0.05)
                report = await controller.status()
            return report
        stop_task = asyncio.create_task(emergency_stop())
        startup = asyncio.create_task(controller.start())
        pending.add(startup)
        while not startup.done():
            if stopped.is_set():
                break
            await asyncio.sleep(0.05)
        if not stopped.is_set():
            await startup
            pending.discard(startup)
        while not stopped.is_set():
            report = await controller.heartbeat()
            if stopped.is_set():
                break
            if report["state"] in {"failed", "stopped", "stopping"}:
                raise ValueError("A desktop member lost its connection, backend grant or fresh attestation.")
            output.send("ready" if report["ready"] else "waiting_ready",
                        ("Gruppo attivo: mouse, tastiera e appunti verificati." if catalog.desktop.clipboard
                         else "Gruppo attivo: mouse e tastiera verificati.") if report["ready"]
                        else "In attesa dei nodi e dei consensi del desktop…")
            for _ in range(10):
                if stopped.is_set():
                    break
                await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        pass
    except (OSError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
        failure_message = "Gruppo non disponibile. Verifica layout, runtime desktop e collegamenti dei nodi."
        output.send("failed", failure_message)
    finally:
        output.send("stopping", "Arresto del gruppo in corso…")
        # Stop uses fresh purpose-bound channels, independently of a pending start
        # or heartbeat. Cancel those only after the scoped stop was attempted.
        complete = controller is None
        if controller is not None:
            stopped.set()
            report = await stop_task if stop_task is not None else await controller.stop()
            complete = report["cleanup_complete"]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if transport is not None:
            await transport.close()
        final_message = failure_message or "Gruppo fermato."
        if not complete:
            final_message = (failure_message + " " if failure_message else "") + \
                "Arresto non confermato su tutti i nodi; le lease restano limitate."
        output.send("failed" if failure_message or not complete else "stopped", final_message, complete)
        # Bounded drain; a GUI which stopped reading cannot prevent lease cleanup.
        for _ in range(10):
            if not output.queue.unfinished_tasks:
                break
            await asyncio.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run",))
    parser.parse_args()
    stopped = Event()
    output = StatusOutput(stopped)
    Thread(target=watch_input, args=(stopped,), name="desktop-stop", daemon=True).start()
    try:
        desktop_node.security.private_directory(desktop_node.home())
        with desktop_supervisor.owner_lock(desktop_node.home() / "coordinator"):
            asyncio.run(run(stopped, output))
    except (OSError, ValueError):
        output.send("failed", "Un altro coordinatore è attivo oppure il runtime desktop non è installato.", True)
        # No remote operation has been initiated before acquiring this lock.
        import time
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
