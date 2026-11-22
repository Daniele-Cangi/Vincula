# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bounded, session-scoped desktop control over pre-authorized OpenSSH routes.

This module neither installs its destination helper nor starts a supervisor.
The caller must supply trusted release pins and an explicitly authorized group.
"""

import argparse
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

# The explicitly installed bundle is the sole local import directory under -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_supervisor
import node_ssh
import node_registry
import nodes
import wire_framing


PROTOCOL = 1
MAX_MESSAGE = 65536 - 512  # The local endpoint adds its private authentication token.
MAX_REPLY = 65536
DIGEST = re.compile(r"[0-9a-f]{64}")
BUNDLE_FILES = ("desktop_ssh.py", "desktop_supervisor.py", "desktop_security.py",
                "desktop_process.py", "node_ssh.py", "nodes.py", "node_registry.py",
                "desktop_layout.py", "wire_framing.py")
SESSION_FIELDS = {"instance", "session_id", "revision", "local_id", "primary", "members",
                  "state", "desktop_ready", "lease_remaining"}
BIND_FIELDS = {"protocol", "mode", "node_id", "session_id", "revision", "helper_sha256", "instance"}
GENERIC_ERROR = "Desktop control rejected or unconfirmed; no automatic retry is permitted."


class BridgeError(ValueError):
    """A failed control exchange does not prove that remote effects did not occur."""


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use canonical desktop session and node UUIDs.")
    return value


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("A trusted SHA256 pin is required.")
    return value


def implementation_digest(directory=None):
    """Pin the entire local helper dependency bundle, not a remote-reported value."""
    directory = Path(__file__).resolve().parent if directory is None else Path(directory)
    files = {}
    for name in BUNDLE_FILES:
        raw = bounded_file(directory / name, 1024 * 1024)
        files[name] = sha256(raw).hexdigest()
    return sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bounded_file(path, maximum):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Desktop local metadata exceeds its limit.")
    return raw


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate desktop protocol fields.")
        result[key] = value
    return result


def decode(raw):
    def invalid_constant(_):
        raise ValueError("Non-finite JSON values are forbidden.")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object,
                      parse_constant=invalid_constant)


def encode(value, maximum):
    return wire_framing.pack(json.dumps(value, ensure_ascii=True, allow_nan=False,
                                       separators=(",", ":")).encode("utf-8"), maximum)


def read_frame(stream, maximum):
    first = stream.read(1)
    if not first:
        return None
    header = first + wire_framing.exact(stream, wire_framing.HEADER_BYTES - 1)
    if not wire_framing.HEADER.fullmatch(header):
        raise ValueError("Invalid desktop frame.")
    size = int(header[:8], 16)
    if size > maximum:
        raise ValueError("Desktop frame exceeds its limit.")
    return decode(wire_framing.exact(stream, size))


def default_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Vincula-Desktop/control"
    return Path.home() / ".local/share/vincula-desktop/control"


def default_registry_path():
    # Match nodes.registry_path without acquiring the edit mutex just to locate
    # the file. Emergency stop must not queue behind a GUI registry writer.
    override = os.environ.get("VINCULA_NODE_REGISTRY")
    if override:
        return registry_argument_path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Vincula/nodes.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vincula/nodes.json"


def registry_argument_path(path):
    # Path.resolve can briefly open Windows registry targets without delete
    # sharing. The common writer mutex is intentionally not used by emergency
    # stop, so locating its metadata must be lexical on Windows too.
    return Path(os.path.abspath(path)) if sys.platform == "win32" else Path(path).resolve()


def fixed_remote_command(family, mode="control"):
    if mode not in {"control", "stop", "status"}:
        raise ValueError("Invalid desktop channel purpose.")
    entrypoint = {"control": "stdio", "stop": "stop-stdio", "status": "status-stdio"}[mode]
    if family == "linux":
        return 'python3 -IB "$HOME/.local/share/vincula-desktop/desktop_ssh.py" ' + entrypoint
    if family != "windows":
        raise ValueError("Unsupported desktop node OS family.")
    # A native Python bootstrap avoids the PowerShell/OpenSSH stdin block limit.
    # Nothing in this constant source originates in a request or shell argument.
    source = ("import os,runpy,sys;from pathlib import Path;"
              "p=Path(os.environ['LOCALAPPDATA'])/'Vincula-Desktop'/'desktop_ssh.py';"
              "sys.path.insert(0,str(p.parent));"
              "sys.argv=[str(p),'" + entrypoint + "'];runpy.run_path(str(p),run_name='__main__')")
    return 'py.exe -3 -IB -c "' + source + '"'


def validate_binding(value, mode):
    if not isinstance(value, dict) or set(value) != BIND_FIELDS:
        raise ValueError("Invalid desktop binding fields.")
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL:
        raise ValueError("Incompatible desktop bridge protocol.")
    if mode not in {"control", "stop", "status"} or value["mode"] != mode:
        raise ValueError("Desktop control channels cannot change purpose.")
    for name in ("node_id", "session_id"):
        canonical_uuid(value[name])
    if value["instance"] is not None:
        canonical_uuid(value["instance"])
    digest(value["revision"])
    digest(value["helper_sha256"])
    return deepcopy(value)


def validate_request(payload, binding, mode="control"):
    allowed = {"control": {"prepare": {"plan"}, "commit": {"session_id"}, "heartbeat": {"session_id"}, "status": set()},
               "stop": {"stop_session": {"session_id"}}, "status": {"status": set()}}[mode]
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
        raise ValueError("Invalid desktop control request.")
    action = payload["action"]
    if action not in allowed or set(payload) != {"action"} | allowed[action]:
        raise ValueError("Forbidden desktop control action or fields.")
    if action == "prepare":
        plan = payload["plan"]
        if (not isinstance(plan, dict) or plan.get("session_id") != binding["session_id"]
                or plan.get("local_id") != binding["node_id"] or plan.get("revision") != binding["revision"]):
            raise ValueError("Prepare does not match the desktop channel binding.")
        # Destination-local validation is also performed by Supervisor.prepare.
        # Do not probe destination paths in the origin-side validator.
    elif action != "status" and canonical_uuid(payload["session_id"]) != binding["session_id"]:
        raise ValueError("Desktop control belongs to another session.")
    encode(payload, MAX_MESSAGE)
    return deepcopy(payload)


def scoped_reply(reply, binding):
    if not isinstance(reply, dict) or reply.get("ok") is not True or set(reply) != {"ok", "session"}:
        raise BridgeError(GENERIC_ERROR)
    status = reply["session"]
    if (not isinstance(status, dict) or not SESSION_FIELDS <= status.keys()
            or status["instance"] != binding["instance"] or status["session_id"] != binding["session_id"]
            or status["local_id"] != binding["node_id"] or status["revision"] != binding["revision"]):
        raise BridgeError(GENERIC_ERROR)
    result = {"ok": True, "session": {key: deepcopy(status[key]) for key in SESSION_FIELDS}}
    encode(result, MAX_REPLY)
    return result


class Helper:
    """Destination-local adapter. Endpoint credentials never enter its wire format."""

    def __init__(self, directory, registry_path, mode="control"):
        self.directory, self.registry_path = Path(directory), Path(registry_path)
        self.mode, self.binding = mode, None

    def catalog(self, binding):
        # All Windows readers, including the bounded precheck, participate in
        # the same cross-session mutex as atomic catalog publication.
        with node_registry.registry_guard():
            bounded_file(self.registry_path, 1024 * 1024)
            snapshot = nodes.snapshot(self.registry_path)
        registry = snapshot["registry"]
        if registry is None or registry["local"] != binding["node_id"]:
            raise ValueError("The desktop route does not identify this computer.")
        if snapshot["revision"] != binding["revision"]:
            raise ValueError("The destination registry changed.")
        return registry

    def bind(self, payload):
        binding = validate_binding(payload, self.mode)
        if binding["helper_sha256"] != implementation_digest():
            raise ValueError("The destination desktop helper is not the pinned release.")
        if self.mode == "control":
            registry = self.catalog(binding)
            instance = self.endpoint_instance(binding, registry)
        else:
            # An owned prepare cannot have been sent before a successful control
            # bind pinned the instance. Fresh emergency channels never reopen the
            # mutable catalog or queue behind its editor, even for status.
            if binding["instance"] is None:
                raise ValueError("Emergency control requires the already-pinned supervisor instance.")
            endpoint = decode(bounded_file(self.directory / "endpoint.json", MAX_REPLY))
            instance = canonical_uuid(endpoint["instance"])
        if binding["instance"] is not None and binding["instance"] != instance:
            raise ValueError("The destination supervisor instance changed.")
        binding["instance"] = instance
        self.binding = binding
        return {"ok": True, "binding": deepcopy(binding)}

    def endpoint_instance(self, binding, registry):
        group = registry.get("desktop")
        if group is None or not any(member["node_id"] == binding["node_id"] for member in group["members"]):
            raise ValueError("Configure and authorize the local desktop group first.")
        reply = desktop_supervisor.request(self.directory, {"action": "status"})
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise BridgeError(GENERIC_ERROR)
        status = reply["session"]
        instance = canonical_uuid(status["instance"])
        if status["state"] != "idle" and status["local_id"] != binding["node_id"]:
            raise ValueError("The endpoint does not attest this local node.")
        if status["state"] in desktop_supervisor.ACTIVE and status["session_id"] != binding["session_id"]:
            raise ValueError("A different desktop session owns this endpoint.")
        return instance

    def check_instance(self):
        endpoint = decode(bounded_file(self.directory / "endpoint.json", MAX_REPLY))
        if not isinstance(endpoint, dict) or endpoint.get("instance") != self.binding["instance"]:
            raise BridgeError(GENERIC_ERROR)

    def call(self, payload):
        if self.binding is None:
            raise ValueError("Bind the desktop channel before using it.")
        payload = validate_request(payload, self.binding, self.mode)
        if self.mode == "control":
            registry = self.catalog(self.binding)
            if payload["action"] == "prepare":
                group, plan = registry["desktop"], payload["plan"]
                if (plan.get("primary") != group["primary"] or not isinstance(plan.get("members"), list)
                        or sorted(plan["members"]) != sorted(member["node_id"] for member in group["members"])):
                    raise ValueError("The plan does not match the destination's authorized desktop group.")
            # request authenticates through the private local endpoint. Never expose
            # its token, errors, paths, logs or an unrelated session in the reply.
        self.check_instance()
        return scoped_reply(desktop_supervisor.request(self.directory, payload), self.binding)

    def lost_origin(self):
        if self.binding is not None and self.mode == "control":
            try:
                self.call_stop()
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Best effort only; an unconfirmed stop relies on the existing lease.

    def call_stop(self):
        self.check_instance()
        return scoped_reply(desktop_supervisor.request(self.directory, {
            "action": "stop_session", "session_id": self.binding["session_id"]}), self.binding)


def serve(input_stream, output_stream, directory, registry_path, mode="control"):
    helper = Helper(directory, registry_path, mode)
    try:
        binding = read_frame(input_stream, MAX_MESSAGE)
        if binding is None:
            return 1
        output_stream.write(encode(helper.bind(binding), MAX_REPLY))
        output_stream.flush()
        while True:
            payload = read_frame(input_stream, MAX_MESSAGE)
            if payload is None:
                return 0
            reply = helper.call(payload)
            output_stream.write(encode(reply, MAX_REPLY))
            output_stream.flush()
            if mode != "control":
                return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        try:
            output_stream.write(encode({"ok": False, "error": GENERIC_ERROR}, MAX_REPLY))
            output_stream.flush()
        except (OSError, ValueError):
            pass
        return 1
    finally:
        helper.lost_origin()


async def read_async_frame(stream, maximum):
    header = await stream.readexactly(wire_framing.HEADER_BYTES)
    if not wire_framing.HEADER.fullmatch(header) or int(header[:8], 16) > maximum:
        raise BridgeError(GENERIC_ERROR)
    return decode(await stream.readexactly(int(header[:8], 16)))


class Channel:
    def __init__(self, process, binding):
        self.process, self.binding = process, binding
        self.closed = False
        self.closing = None

    async def exchange(self, payload):
        if self.closed:
            raise BridgeError(GENERIC_ERROR)
        self.process.stdin.write(encode(payload, MAX_MESSAGE))
        await self.process.stdin.drain()
        return await read_async_frame(self.process.stdout, MAX_REPLY)

    async def close(self):
        if self.closing is None:
            self.closed = True
            self.closing = asyncio.create_task(self._close())
        await asyncio.shield(self.closing)

    async def _close(self):
        self.process.stdin.close()  # Let the destination observe EOF before escalation.
        try:
            await asyncio.wait_for(self.process.wait(), 0.3)
        except asyncio.TimeoutError:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 0.5)
            except asyncio.TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(self.process.wait(), 0.5)


class DesktopSSHTransport:
    """One non-resumable coordinator session, one control SSH channel per peer.

    Create only after trusted local desktop setup. The MCP job ask/full setting is
    not consulted and does not authorize a prepare command on this transport.
    """

    def __init__(self, registry, session_id, revisions, helper_pins, *, instances=None, timeout=6,
                 local_directory=None, local_registry_path=None):
        self.registry, self.session_id = registry, canonical_uuid(session_id)
        if registry.desktop is None:
            raise ValueError("Configure the explicit desktop group first.")
        members = {canonical_uuid(member.node_id) for member in registry.desktop.members}
        if set(revisions) != members or set(helper_pins) != members:
            raise ValueError("Supply trusted revisions and helper pins for exactly the desktop members.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Use a finite positive desktop transport timeout up to 60 seconds.")
        self.timeout = timeout
        self.bindings = {}
        for node in members:
            if registry.get(node).node_id != node:
                raise ValueError("Use registered canonical node identities.")
            instance = None if instances is None else instances.get(node)
            if instance is not None:
                canonical_uuid(instance)
            self.bindings[node] = {"protocol": PROTOCOL, "mode": "control", "node_id": node,
                                   "session_id": self.session_id, "revision": digest(revisions[node]),
                                   "helper_sha256": digest(helper_pins[node]), "instance": instance}
        self.local_directory = default_directory() if local_directory is None else Path(local_directory).resolve()
        self.local_registry_path = default_registry_path() if local_registry_path is None else registry_argument_path(local_registry_path)
        self.channels, self.retired = {}, set()
        self.owned = set()
        self.opening = set()
        self.locks = {node: asyncio.Lock() for node in members}
        self.closed = False
        self.closing = None

    async def _spawn(self, node_id, mode):
        node = self.registry.get(node_id)
        if node.node_id == self.registry.local_id:
            argv = [sys.executable, "-IB", str(Path(__file__).resolve()),
                    {"control": "stdio", "stop": "stop-stdio", "status": "status-stdio"}[mode], "--directory", str(self.local_directory),
                    "--registry", str(self.local_registry_path)]
        else:
            if node.route is None or not node.route.configured():
                raise ValueError("The desktop node has no configured pinned SSH route.")
            argv = [str(node_ssh.executable("ssh")), "-T", *node_ssh.OPTIONS, *node.route.options,
                    node.route.destination, fixed_remote_command(node.family, mode)]
        return await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=node_ssh.environment(), limit=MAX_REPLY + wire_framing.HEADER_BYTES,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)

    async def _open(self, node_id, mode):
        if self.closed:
            raise BridgeError(GENERIC_ERROR)
        task = asyncio.create_task(self._open_channel(node_id, mode))
        self.opening.add(task)
        try:
            # An owned child cancelled by close is an operational failure. A
            # caller cancellation cancels gather itself and propagates naturally,
            # including when close and external cancellation happen together.
            outcome, = await asyncio.gather(task, return_exceptions=True)
            if isinstance(outcome, asyncio.CancelledError):
                raise BridgeError(GENERIC_ERROR) from None
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            self.opening.discard(task)

    async def _open_channel(self, node_id, mode):
        binding = deepcopy(self.bindings[node_id])
        binding["mode"] = mode
        process = await self._spawn(node_id, mode)
        channel = Channel(process, binding)
        self.owned.add(channel)
        try:
            if self.closed or mode == "control" and node_id in self.retired:
                raise BridgeError(GENERIC_ERROR)
            reply = await channel.exchange(binding)
            if not isinstance(reply, dict) or set(reply) != {"ok", "binding"} or reply["ok"] is not True:
                raise BridgeError(GENERIC_ERROR)
            attested = validate_binding(reply["binding"], mode)
            for field in BIND_FIELDS - {"instance"}:
                if attested[field] != binding[field]:
                    raise BridgeError(GENERIC_ERROR)
            if binding["instance"] is not None and attested["instance"] != binding["instance"]:
                raise BridgeError(GENERIC_ERROR)
            canonical_uuid(attested["instance"])
            # A parallel emergency bind may have established the first pin
            # while this handshake awaited I/O. Never overwrite that live pin.
            current_instance = self.bindings[node_id]["instance"]
            if current_instance is not None and attested["instance"] != current_instance:
                raise BridgeError(GENERIC_ERROR)
            channel.binding = attested
            self.bindings[node_id]["instance"] = attested["instance"]
            return channel
        except BaseException:
            await channel.close()
            raise

    async def _control_channel(self, node_id):
        if node_id in self.retired or self.closed:
            raise BridgeError(GENERIC_ERROR)
        channel = self.channels.get(node_id)
        if channel is None:
            channel = await self._open(node_id, "control")
            self.channels[node_id] = channel
        if node_id in self.retired or self.closed:
            raise BridgeError(GENERIC_ERROR)
        return channel

    async def _exchange(self, node_id, payload, mode):
        channel = None
        try:
            if mode != "control":
                channel = await self._open(node_id, mode)
            else:
                channel = await self._control_channel(node_id)
            reply = await channel.exchange(payload)
            return scoped_reply(reply, channel.binding)
        except BaseException:
            if mode == "control":
                self.retired.add(node_id)
            if channel is not None:
                await channel.close()
            raise
        finally:
            if mode != "control" and channel is not None:
                await channel.close()

    async def __call__(self, node_id, payload):
        if self.closed:
            raise BridgeError(GENERIC_ERROR)
        if node_id not in self.bindings:
            raise ValueError("Select a canonical member of this desktop session.")
        action = payload.get("action") if isinstance(payload, dict) else None
        mode = "control"
        if action == "stop_session":
            mode = "stop"
        elif action == "status" and node_id in self.retired:
            mode = "status"
        payload = validate_request(payload, self.bindings[node_id], mode)
        if mode == "stop":
            self.retired.add(node_id)
        async def exchange():
            if mode != "control":
                return await self._exchange(node_id, payload, mode)
            async with self.locks[node_id]:
                return await self._exchange(node_id, payload, "control")
        try:
            return await asyncio.wait_for(exchange(), self.timeout)
        except asyncio.TimeoutError:
            # Python3.10 uses a distinct asyncio timeout type; 3.11+ aliases the
            # built-in OSError subclass. Keep its handling separate and ordered.
            raise BridgeError(GENERIC_ERROR) from None
        except (OSError, ValueError, TypeError, KeyError, asyncio.IncompleteReadError):
            raise BridgeError(GENERIC_ERROR) from None

    async def close(self):
        if self.closing is None:
            self.closed = True
            self.closing = asyncio.create_task(self._close())
        await asyncio.shield(self.closing)

    async def _close(self):
        pending = list(self.opening)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*(channel.close() for channel in self.owned))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stdio", "stop-stdio", "status-stdio"))
    parser.add_argument("--directory", type=Path, default=default_directory())
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    args = parser.parse_args()
    return serve(sys.stdin.buffer, sys.stdout.buffer, args.directory, args.registry,
                 mode={"stdio": "control", "stop-stdio": "stop", "status-stdio": "status"}[args.mode])


if __name__ == "__main__":
    raise SystemExit(main())
