# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Session-scoped desktop coordination, with an injected authenticated async transport."""

import asyncio
from copy import deepcopy
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from time import monotonic
from uuid import UUID, uuid4

import desktop_layout

ACTIVE = {"prepared", "starting", "running"}
TERMINAL = {"stopped", "expired", "failed"}
STATES = ACTIVE | TERMINAL | {"idle", "stopping"}
DIGEST = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use canonical UUIDs for desktop session and node identities.")
    return value


def absolute(value):
    return isinstance(value, str) and "\0" not in value and (
        PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())


def command_list(commands):
    # Paths belong to the destination OS. Never probe them on the coordinator.
    if not isinstance(commands, list) or not 1 <= len(commands) <= 8:
        raise ValueError("Each node needs between one and eight authorized components.")
    names = set()
    for item in commands:
        if not isinstance(item, dict) or not {"name", "argv"} <= item.keys() <= {"name", "argv", "cwd"}:
            raise ValueError("Invalid desktop component fields.")
        name, argv = item["name"], item["argv"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise ValueError("Desktop component names must be distinct simple names.")
        names.add(name)
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
                or not all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 8192 for arg in argv)
                or not absolute(argv[0]) or "cwd" in item and not absolute(item["cwd"])):
            raise ValueError("Invalid authorized desktop component argv or working directory.")


def build_plan(registry, revisions, commands, session_id=None):
    """Compile the catalog's group; executable validation remains destination-local."""
    compiled = desktop_layout.compile_layout(registry)
    members = sorted(canonical_uuid(member.node_id) for member in registry.desktop.members)
    primary = canonical_uuid(registry.desktop.primary)
    session_id = canonical_uuid(str(uuid4()) if session_id is None else session_id)
    if not isinstance(revisions, dict) or not isinstance(commands, dict) or set(revisions) != set(members) or set(commands) != set(members):
        raise ValueError("Supply revisions and authorized commands for exactly the desktop members.")
    plans = {}
    for node in members:
        if registry.get(node).node_id != node:
            raise ValueError("Desktop members must be canonical registered node identities.")
        revision = revisions[node]
        if not isinstance(revision, str) or not DIGEST.fullmatch(revision):
            raise ValueError("Each node needs its exact local registry revision.")
        command_list(commands[node])
        plans[node] = {"session_id": session_id, "revision": revision, "local_id": node,
                       "primary": primary, "members": members, "commands": commands[node]}
        if len(json.dumps({"action": "prepare", "plan": plans[node]}, ensure_ascii=True).encode()) > 65536 - 512:
            raise ValueError("A desktop plan exceeds the bounded control protocol.")
    return deepcopy({"session_id": session_id, "primary": primary, "members": members,
                     "layout": compiled["layout"], "nodes": plans})


class DesktopCoordinator:
    """One explicit, non-resumable session. No background task renews leases."""

    def __init__(self, registry, revisions, commands, transport, *, session_id=None, timeout=2, clock=monotonic):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Choose a finite, positive control timeout up to 60 seconds.")
        self._plan = build_plan(registry, revisions, commands, session_id)
        self.transport, self.timeout, self.clock = transport, timeout, clock
        self._started = self._stopping = self._failed = self._was_ready = False
        self._phase = "new"
        self._cleanup_lock = asyncio.Lock()
        self._node_locks = {node: asyncio.Lock() for node in self._plan["members"]}
        self._records = {node: {"prepared": False, "committed": False, "instance": None,
                         "status": None, "error": None, "stop_error": None, "terminal": False,
                         "lease_deadline": None} for node in self._plan["members"]}

    @property
    def session_id(self):
        return self._plan["session_id"]

    def prepared_plan(self):
        return deepcopy(self._plan)

    def _cache_ready(self):
        now = self.clock()
        return all(record["committed"] and record["error"] is None
                   and record["status"] is not None and record["status"]["state"] == "running"
                   and record["status"]["desktop_ready"] is True
                   and record["lease_deadline"] is not None and record["lease_deadline"] > now
                   for record in self._records.values())

    def report(self):
        nodes = {node: {key: deepcopy(value) for key, value in record.items() if key != "lease_deadline"}
                 for node, record in self._records.items()}
        complete = all(record["terminal"] for record in self._records.values())
        if self._failed:
            state = "failed"
        elif self._stopping:
            state = "stopped" if complete else "stopping"
        else:
            state = self._phase
            if state == "ready" and not self._cache_ready():
                state = "waiting_ready"
        return {"session_id": self.session_id, "primary": self._plan["primary"],
                "members": list(self._plan["members"]), "state": state,
                "ready": state == "ready", "cleanup_complete": complete, "nodes": nodes}

    def _validate(self, node, action, status, started):
        plan, record = self._plan["nodes"][node], self._records[node]
        if not isinstance(status, dict) or status.get("session_id") != self.session_id:
            raise ValueError("The reply belongs to a different desktop session.")
        instance = canonical_uuid(status.get("instance"))
        if record["instance"] is not None and instance != record["instance"]:
            raise ValueError("The destination supervisor instance changed; automatic adoption is forbidden.")
        if (status.get("local_id") != node or status.get("revision") != plan["revision"]
                or status.get("primary") != plan["primary"] or not isinstance(status.get("members"), list)
                or len(status["members"]) != len(plan["members"]) or set(status["members"]) != set(plan["members"])):
            raise ValueError("The reply does not attest the prepared node, revision and desktop group.")
        state, ready = status.get("state"), status.get("desktop_ready")
        if state not in STATES or type(ready) is not bool:
            raise ValueError("Invalid desktop state or readiness attestation.")
        if record["terminal"] and state in ACTIVE:
            raise ValueError("A terminal desktop session cannot become active again; this reply may be stale.")
        allowed = {"prepare": {"prepared"}, "commit": {"starting", "running"},
                   "heartbeat": ACTIVE, "status": STATES, "stop_session": TERMINAL | {"stopping"}}
        if state not in allowed[action]:
            raise ValueError("The destination did not accept " + action + ": " + state)
        deadline = None
        if state in ACTIVE:
            remaining = status.get("lease_remaining")
            if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0:
                raise ValueError("The destination session lease is not live.")
            # Start time is conservative: reply delay never extends perceived ownership.
            deadline = started + remaining
            if deadline <= self.clock():
                raise ValueError("The control reply arrived too late to confirm a live lease.")
        return deepcopy(status), instance, deadline

    async def _call(self, node, action, timeout):
        request = {"action": action}
        if action == "prepare":
            request["plan"] = deepcopy(self._plan["nodes"][node])
        elif action != "status":
            request["session_id"] = self.session_id
        started = self.clock()
        try:
            reply = await asyncio.wait_for(self.transport(node, request), timeout=timeout)
            if not isinstance(reply, dict) or reply.get("ok") is not True or reply.keys() != {"ok", "session"}:
                detail = reply.get("error") if isinstance(reply, dict) else None
                raise ValueError(str(detail)[:1000] if detail else "Invalid or rejected supervisor reply.")
            return self._validate(node, action, reply.get("session"), started), None
        except asyncio.TimeoutError:
            return None, "Control timeout; the request may already have taken effect."
        except ValueError as error:
            return None, str(error)
        except Exception as error:
            return None, "Control transport failed (" + type(error).__name__ + "); effects may be unconfirmed."

    async def _fanout(self, action, targets=None):
        async def exchange(node, timeout):
            if action in {"prepare", "commit", "heartbeat"} and (self._stopping or self._failed):
                return False
            result, error = await self._call(node, action, timeout)
            record = self._records[node]
            if error:
                record["stop_error" if action == "stop_session" else "error"] = {"phase": action, "message": error}
                return False
            status, instance, deadline = result
            record.update(status=status, instance=instance, lease_deadline=deadline)
            record["terminal"] |= status["state"] in TERMINAL
            if action == "prepare":
                record["prepared"] = True
            elif action == "commit":
                record["committed"] = True
            elif action == "stop_session":
                record["stop_error"] = None
            return True

        async def contact(node):
            if action == "stop_session":
                return await exchange(node, self.timeout)
            lock = self._node_locks[node]
            loop = asyncio.get_running_loop()
            budget_end = loop.time() + self.timeout
            acquired = False
            try:
                await asyncio.wait_for(lock.acquire(), timeout=self.timeout)
                acquired = True
                remaining = budget_end - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                # Serialize the whole exchange, not just assignment or request
                # launch order: a later RPC cannot overtake this node's reply.
                return await exchange(node, remaining)
            except asyncio.TimeoutError:
                # No remote snapshot is applied for an operation that never
                # acquired its turn. The queue timeout remains an explicit error.
                self._records[node]["error"] = {"phase": action, "message": "Control queue timeout; the request was not sent."}
                return False
            finally:
                if acquired:
                    lock.release()
        return all(await asyncio.gather(*(contact(node) for node in (self._records if targets is None else targets))))

    async def _cleanup(self):
        async with self._cleanup_lock:
            # Never short-circuit on an offline peer and never use global stop/shutdown.
            await self._fanout("stop_session")

    async def _fail(self):
        self._failed = True
        await self._cleanup()

    def _live_targets(self):
        return [node for node, record in self._records.items() if record["prepared"] and not record["terminal"]]

    async def _evaluate(self):
        if self._failed or self._stopping or self._phase in {"preparing", "committing"}:
            return
        for node, record in self._records.items():
            status = record["status"]
            if status is None or status["state"] not in {"starting", "running"} or record["lease_deadline"] is None or record["lease_deadline"] <= self.clock():
                record["error"] = {"phase": "status", "message": "The committed desktop session is no longer live."}
                await self._fail()
                return
        ready = self._cache_ready()
        if self._was_ready and not ready:
            for record in self._records.values():
                if record["status"]["state"] != "running" or record["status"]["desktop_ready"] is not True:
                    record["error"] = {"phase": "status", "message": "Desktop readiness was lost; a new explicit session is required."}
            await self._fail()
        else:
            self._phase = "ready" if ready else "waiting_ready"
            self._was_ready |= ready

    async def start(self):
        if self._started or self._stopping:
            raise ValueError("A desktop session can be started only once; create a new explicit session.")
        self._started = True
        try:
            self._phase = "preparing"
            prepared = await self._fanout("prepare")
            if self._stopping or self._failed:
                return self.report()
            if not prepared:
                await self._fail()
                return self.report()
            self._phase = "committing"
            committed = await self._fanout("commit")
            if self._stopping or self._failed:
                return self.report()
            if not committed:
                await self._fail()
                return self.report()
            self._phase = "waiting_ready"
            return await self.status()
        except asyncio.CancelledError:
            self._failed = True
            await asyncio.shield(self._cleanup())
            raise

    async def heartbeat(self):
        if not self._started or self._stopping or self._failed:
            raise ValueError("This session cannot renew leases or resume automatically.")
        # A separate caller task can renew confirmed prepares while startup is blocked.
        valid = await self._fanout("heartbeat", self._live_targets())
        if not valid and not self._stopping and not self._failed:
            await self._fail()
        await self._evaluate()
        return self.report()

    async def status(self):
        if not self._started:
            return self.report()
        targets = self._live_targets() if self._phase in {"preparing", "committing"} else list(self._records)
        valid = await self._fanout("status", targets)
        if not valid and not self._failed and not self._stopping:
            await self._fail()
        await self._evaluate()
        return self.report()

    async def stop(self):
        self._stopping = True
        await self._cleanup()
        return self.report()
