# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private, process-bound Deskflow snapshots; logs never establish readiness."""

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import signal
import stat
import subprocess
import sys
from threading import Event, RLock, Thread
from time import monotonic, sleep

from desktop_backend import GroupManifest

MAX_FRAME = 65536
SNAPSHOT_INTERVAL = 0.5
SNAPSHOT_TIMEOUT = 2.0
DIGEST = re.compile(r"[0-9a-f]{64}")


def runtime_environment(environment=None):
    """Keep real desktop/session facts; isolate this approved runtime's loaders."""
    result = dict(os.environ if environment is None else environment)
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                 "QT_QPA_PLATFORM", "QT_QPA_PLATFORMTHEME", "QT_STYLE_OVERRIDE", "OPENSSL_CONF", "OPENSSL_MODULES",
                 "OPENSSL_ENGINES", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "GIT_ASKPASS"):
        result.pop(name, None)
    if sys.platform == "win32":
        result["QT_QPA_PLATFORM"] = "windows"
    elif result.get("XDG_SESSION_TYPE") == "wayland" and result.get("WAYLAND_DISPLAY"):
        result["QT_QPA_PLATFORM"] = "wayland"
    elif result.get("DISPLAY"):
        result["QT_QPA_PLATFORM"] = "xcb"
    elif result.get("WAYLAND_DISPLAY"):
        result["QT_QPA_PLATFORM"] = "wayland"
    return result


def require(condition, message):
    if not condition:
        raise ValueError(message)


def absolute(value):
    return (isinstance(value, str) and 0 < len(value) <= 8192
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()))


def validate_configuration(value):
    fields = {"group", "node_id", "settings_file", "state_file", "binary_sha256", "owner_consent"}
    require(isinstance(value, dict) and set(value) == fields, "Invalid managed engine fields.")
    group = GroupManifest.from_document(value["group"])
    members = {screen.node_id for screen in group.screens}
    require(value["node_id"] in members and value["owner_consent"] is True,
            "An authorized group must include this desktop.")
    require(absolute(value["settings_file"]) and absolute(value["state_file"])
            and value["settings_file"] != value["state_file"], "Use distinct absolute private settings/state files.")
    require(isinstance(value["binary_sha256"], str) and DIGEST.fullmatch(value["binary_sha256"]),
            "The managed executable needs its approved installation digest.")
    return deepcopy(value)


def bootstrap(configuration, nonce):
    config = validate_configuration(configuration)
    require(isinstance(nonce, str) and DIGEST.fullmatch(nonce), "Invalid engine instance nonce.")
    group = GroupManifest.from_document(config["group"])
    return {"type": "bootstrap", "protocol": 1, "session_id": group.session_id,
            "node_id": config["node_id"], "primary": group.primary, "engine_nonce": nonce,
            "manifest_sha256": group.digest, "settings_file": config["settings_file"],
            "state_file": config["state_file"], "clipboard": group.clipboard,
            "members": sorted(screen.node_id for screen in group.screens),
            "tls_pins": dict(group.tls_pins), "owner_consent": True}


def validate_argv(configuration, argv):
    expected = bootstrap(configuration, "0" * 64)
    role = "server" if expected["node_id"] == expected["primary"] else "client"
    require(isinstance(argv, list) and len(argv) == 4 and absolute(argv[0])
            and argv[1:] == [role, "--settings", expected["settings_file"]],
            "Managed argv must select only its role and bound settings file.")


def bind_to_plan(configuration, plan):
    config = validate_configuration(configuration)
    group = GroupManifest.from_document(config["group"])
    require(group.session_id == plan["session_id"] and config["node_id"] == plan["local_id"]
            and group.primary == plan["primary"]
            and {s.node_id for s in group.screens} == set(plan["members"])
            and dict(group.revisions)[config["node_id"]] == plan["revision"],
            "Managed configuration differs from its prepared session.")
    return group


def artifact_approval(binary_sha256, executable):
    """The caller's hash is only a selector; approval comes from local installation."""
    import desktop_security

    configured = os.environ.get("VINCULA_DESKTOP_ARTIFACT_ROOT")
    if configured:
        root = Path(configured)
        require(root.is_absolute(), "The operator's artifact root must be absolute.")
    elif sys.platform == "win32":
        root = Path(os.environ["LOCALAPPDATA"]) / "Vincula-Desktop/engines/approved"
    else:
        root = Path.home() / ".local/share/vincula-desktop/engines/approved"
    # Verification only: do not create an approval store as an execution effect.
    require(root.is_dir(), "Install an approved managed desktop artifact first.")
    desktop_security.private_directory(root)
    path = root / (binary_sha256 + ".json")
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and not getattr(before, "st_file_attributes", 0) & 0x400, "Artifact approval must be a non-linked regular file.")
    desktop_security.private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), "Artifact approval changed before reading.")
        raw = source.read(16385)
    require(len(raw) <= 16384, "Oversized local artifact approval.")
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) == {"protocol", "binary_sha256", "executable", "artifact_manifest_sha256"}
            and type(value["protocol"]) is int and value["protocol"] == 1
            and value["binary_sha256"] == binary_sha256 and value["executable"] == str(executable)
            and isinstance(value["artifact_manifest_sha256"], str) and DIGEST.fullmatch(value["artifact_manifest_sha256"]),
            "The managed executable has no matching local installation approval.")
    return value


def frame(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    require(0 < len(raw) <= MAX_FRAME, "Managed frame exceeds 64 KiB.")
    return len(raw).to_bytes(4, "big") + raw


def write_frame(descriptor, value):
    pending = memoryview(frame(value))
    while pending:
        written = os.write(descriptor, pending)
        if not written:
            raise OSError("The private engine pipe stopped accepting data.")
        pending = pending[written:]


def read_frame(descriptor):
    def exact(count):
        result = bytearray()
        while len(result) < count:
            block = os.read(descriptor, count - len(result))
            if not block:
                raise EOFError("The private engine pipe closed.")
            result.extend(block)
        return result

    length = int.from_bytes(exact(4), "big")
    require(0 < length <= MAX_FRAME, "Invalid managed frame length.")

    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "Duplicate managed frame field.")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite managed frame value.")

    value = json.loads(exact(length).decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant)
    require(isinstance(value, dict), "A managed frame must be an object.")
    return value


def validate_snapshot(value, expected, challenge, previous_sequence):
    fields = {"type", "protocol", "session_id", "node_id", "primary", "engine_nonce", "manifest_sha256",
              "challenge", "sequence", "revoked", "reason", "role", "input_ready", "clipboard_ready",
              "peers", "local_pin_sha256"}
    # The primary is in the bootstrap; it need not be repeated by the engine.
    fields.remove("primary")
    require(isinstance(value, dict) and set(value) == fields, "Invalid engine snapshot schema.")
    require(value["type"] == "snapshot" and type(value["protocol"]) is int and value["protocol"] == 1,
            "Unsupported engine snapshot protocol.")
    for name in ("session_id", "node_id", "engine_nonce", "manifest_sha256"):
        require(value[name] == expected[name], "Engine snapshot belongs to another process or session.")
    require(value["challenge"] == challenge, "Engine snapshot is not the outstanding challenge.")
    require(type(value["sequence"]) is int and previous_sequence < value["sequence"] <= (1 << 64) - 1,
            "Engine snapshot sequence did not advance.")
    require(all(type(value[name]) is bool for name in ("revoked", "input_ready", "clipboard_ready")),
            "Engine capability facts must be booleans.")
    require(isinstance(value["reason"], str) and len(value["reason"]) <= 512 and "\0" not in value["reason"],
            "Invalid engine state reason.")
    primary, node_id = expected["primary"], expected["node_id"]
    require(value["role"] == ("primary" if node_id == primary else "client"), "Engine role mismatch.")
    require(value["local_pin_sha256"] == expected["tls_pins"][node_id], "Local TLS identity differs from this session.")
    peers = value["peers"]
    expected_peers = set(expected["members"]) - {node_id} if node_id == primary else {primary}
    require(isinstance(peers, list) and len(peers) <= len(expected_peers), "Invalid managed peer list.")
    seen = set()
    for peer in peers:
        require(isinstance(peer, dict) and set(peer) == {"node_id", "pin_sha256", "handshake"}, "Invalid peer snapshot fields.")
        identifier = peer["node_id"]
        require(isinstance(identifier, str) and identifier in expected_peers and identifier not in seen,
                "Duplicate or unexpected managed peer.")
        require(peer["pin_sha256"] == expected["tls_pins"][identifier] and type(peer["handshake"]) is bool,
                "A peer TLS identity does not match its node UUID.")
        if peer["handshake"]:
            seen.add(identifier)
        else:
            # A pending handshake cannot be reported twice under the same UUID.
            require(not any(p is not peer and p.get("node_id") == identifier for p in peers), "Duplicate pending peer.")
    ready = (not value["revoked"] and value["input_ready"] and seen == expected_peers
             and (not expected["clipboard"] or value["clipboard_ready"]))
    return ready


class SnapshotTracker:
    """One outstanding challenge, bounded freshness and terminal loss of readiness."""

    def __init__(self, expected, clock=monotonic):
        self.expected, self.clock = deepcopy(expected), clock
        self.sequence = 0
        self.pending = None
        self.requested = None
        self.observed = None
        self.snapshot = None
        self.failure = None
        self.was_ready = False
        self.lock = RLock()

    def request(self):
        with self.lock:
            require(self.failure is None and self.pending is None, "An engine challenge is already pending or revoked.")
            self.pending, self.requested = secrets.token_hex(32), self.clock()
            return {"type": "snapshot", "challenge": self.pending}

    def invalidate(self, reason):
        with self.lock:
            self.failure = self.failure or reason

    def accept(self, value):
        with self.lock:
            require(self.failure is None and self.pending is not None, "Unexpected engine snapshot.")
            require(self.clock() - self.requested < SNAPSHOT_TIMEOUT, "Engine snapshot missed its challenge deadline.")
            ready = validate_snapshot(value, self.expected, self.pending, self.sequence)
            self.sequence, self.observed = value["sequence"], self.requested
            self.pending, self.snapshot = None, deepcopy(value)
            if value["revoked"] or self.was_ready and not ready:
                self.invalidate("Engine session was revoked or lost readiness.")
            self.was_ready |= ready
            return ready and self.failure is None

    def report(self):
        with self.lock:
            now = self.clock()
            if ((self.pending is not None and now - self.requested >= SNAPSHOT_TIMEOUT)
                    or self.was_ready and now - self.observed >= SNAPSHOT_TIMEOUT):
                self.invalidate("Engine snapshot freshness expired.")
            ready = (self.failure is None and self.snapshot is not None and self.observed is not None
                     and now - self.observed < SNAPSHOT_TIMEOUT
                     and validate_snapshot(self.snapshot, self.expected, self.snapshot["challenge"], self.snapshot["sequence"] - 1))
            return {"ready": bool(ready), "failure": self.failure, "observed_monotonic": self.observed,
                    "snapshot": deepcopy(self.snapshot)}


class ManagedEngine:
    """Own both private pipe ends; nothing trusts a PID, log line or global IPC."""

    def __init__(self, configuration, argv, *, cwd=None, log=None, clock=monotonic, stop_event=None):
        self.configuration = validate_configuration(configuration)
        self.expected = bootstrap(self.configuration, secrets.token_hex(32))
        self.tracker = SnapshotTracker(self.expected, clock)
        self.argv, self.cwd, self.log, self.clock = list(argv), cwd, log, clock
        self.stopped = stop_event if stop_event is not None else Event()
        self.process = None
        self.reader = self.writer = None
        self.started = None

    def _pin_binary(self):
        self.check_cancelled()
        validate_argv(self.configuration, self.argv)
        require(self.argv and Path(self.argv[0]).is_absolute(), "Managed engine needs an absolute executable.")
        require(not any(arg.startswith("--vincula-") for arg in self.argv[1:]),
                "Private engine handles are assigned only by its guardian.")
        path = Path(self.argv[0])
        artifact_approval(self.configuration["binary_sha256"], path)
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and not getattr(info, "st_file_attributes", 0) & 0x400, "Managed executable must be a regular non-linked file.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            pinned = os.fstat(descriptor)
            require((info.st_dev, info.st_ino) == (pinned.st_dev, pinned.st_ino)
                    and 0 < pinned.st_size <= 128 * 1024 * 1024, "Managed executable changed or exceeds its size limit.")
            digest = hashlib.sha256()
            count = 0
            while True:
                self.check_cancelled()
                block = os.read(descriptor, 65536)
                if not block:
                    break
                count += len(block)
                require(count <= pinned.st_size, "Managed executable grew during verification.")
                digest.update(block)
            require(count == pinned.st_size and digest.hexdigest() == self.configuration["binary_sha256"],
                    "Managed executable differs from its approved pin.")
            return descriptor, pinned
        except BaseException:
            os.close(descriptor)
            raise

    def start(self):
        import desktop_security

        self.check_cancelled()
        settings, state = Path(self.configuration["settings_file"]), Path(self.configuration["state_file"])
        require(settings.is_absolute() and state.is_absolute() and settings.parent == state.parent,
                "Managed settings/state must share one destination-local private profile.")
        desktop_security.private_directory(settings.parent)
        for path in (settings, state):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not getattr(info, "st_file_attributes", 0) & 0x400,
                        "Managed settings/state must be regular non-linked files.")
        desktop_security.private_file(settings)
        if state.exists():
            desktop_security.private_file(state)
        descriptor, pinned = self._pin_binary()
        engine_input = writer = reader = engine_output = None
        inherited = []
        try:
            engine_input, writer = os.pipe()
            reader, engine_output = os.pipe()
            options = {"start_new_session": sys.platform != "win32"}
            if sys.platform == "win32":
                import msvcrt
                incoming, outgoing = msvcrt.get_osfhandle(engine_input), msvcrt.get_osfhandle(engine_output)
                inherited = [incoming, outgoing]
                for handle in inherited:
                    os.set_handle_inheritable(handle, True)
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {"handle_list": inherited}
                options.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                incoming, outgoing = engine_input, engine_output
                options["pass_fds"] = (engine_input, engine_output)
            argv = self.argv + ["--vincula-managed-in", str(incoming), "--vincula-managed-out", str(outgoing)]
            self.check_cancelled()
            self.process = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                            stdout=self.log, stderr=subprocess.STDOUT, close_fds=True,
                                            env=runtime_environment(), **options)
            self.reader, self.writer = reader, writer
            self.check_cancelled()
            after = Path(self.argv[0]).lstat()
            require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    == (pinned.st_dev, pinned.st_ino, pinned.st_size, pinned.st_mtime_ns),
                    "Managed executable was replaced during startup.")
            self.started = self.clock()
            Thread(target=self._read, name="managed-engine-read", daemon=True).start()
            Thread(target=self._write, name="managed-engine-challenge", daemon=True).start()
            return self.process
        except BaseException:
            self.stopped.set()
            for pipe in (reader, writer):
                if pipe is not None:
                    os.close(pipe)
            self.reader = self.writer = None
            if self.process is not None:
                if sys.platform == "win32":
                    # The owning guardian's Job Object also contains descendants.
                    self.process.kill()
                else:
                    # This original leader has not been polled/reaped. Keep its
                    # group ID reserved until descendants have received SIGKILL.
                    for sig, delay in ((signal.SIGTERM, 0.15), (signal.SIGKILL, 0)):
                        try:
                            os.killpg(self.process.pid, sig)
                        except ProcessLookupError:
                            pass
                        if delay:
                            sleep(delay)
                self.process.wait(timeout=3)
            raise
        finally:
            for handle in inherited:
                os.set_handle_inheritable(handle, False)
            for pipe in (engine_input, engine_output):
                if pipe is not None:
                    os.close(pipe)
            os.close(descriptor)

    def check_cancelled(self):
        if self.stopped.is_set():
            raise OSError("Managed engine startup was cancelled.")

    def _read(self):
        descriptor = self.reader
        try:
            while not self.stopped.is_set():
                self.tracker.accept(read_frame(descriptor))
        except (OSError, EOFError, ValueError, RecursionError, UnicodeError):
            if not self.stopped.is_set():
                self.tracker.invalidate("The private engine snapshot channel failed.")

    def _write(self):
        # A broken child that never reads cannot block the guardian's stop loop.
        descriptor = self.writer
        try:
            self.check_cancelled()
            write_frame(descriptor, self.expected)
            next_request = self.clock()
            while not self.stopped.wait(0.03):
                with self.tracker.lock:
                    can_request = self.tracker.pending is None and self.tracker.failure is None
                if can_request and self.clock() >= next_request:
                    request = self.tracker.request()
                    write_frame(descriptor, request)
                    next_request = self.clock() + SNAPSHOT_INTERVAL
        except (OSError, ValueError):
            if not self.stopped.is_set():
                self.tracker.invalidate("The private engine request channel failed.")

    def report(self):
        if self.started is not None and not self.tracker.sequence and self.clock() - self.started >= 2 * SNAPSHOT_TIMEOUT:
            self.tracker.invalidate("The managed engine did not answer its initial challenge.")
        return self.tracker.report()

    def close(self):
        self.stopped.set()
        # Closing the pipe revokes the owned engine. The caller still waits or
        # kills its original child/process group; no saved-PID adoption occurs.
        for name in ("writer", "reader"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                setattr(self, name, None)
                try:
                    os.close(descriptor)
                except OSError:
                    pass
