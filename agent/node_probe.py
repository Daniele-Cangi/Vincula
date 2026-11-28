# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bounded, authenticated observations of a registered node, without job RPC.

Installed helper compatibility is an observation of files, never an attestation
of a running worker. SQLite policy reads are logically read-only; SQLite may
use its ordinary WAL shared-memory bookkeeping on an existing WAL database.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import stat
import subprocess
import sys
from tempfile import TemporaryFile
from time import monotonic, sleep
from uuid import UUID

import direct_execution
from node_enrollment import EnrollmentConflict
import node_registry
import node_ssh
import nodes


MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_DIAGNOSTICS_BYTES = 4 * 1024
MAX_PIN_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 512 * 1024
PROBE_TIMEOUT = 12
HELPERS = ("jobs.py", "vincula_files.py", "vincula_capabilities.py", "plan_journal.py",
           "wire_framing.py", "direct_execution.py", "resource_policy.py", "worker_resources.py", "execution_plans.py",
           "transfer_state.py", "transfer_storage.py", "transfer_wire.py", "large_transfer_plans.py", "large_files.py",
           "node_registry.py", "large_copy_state.py", "large_copy_runtime.py")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_INVALID_POLICY = "Invalid policy observation."

# Executed directly by an isolated stdlib Python interpreter. It reads helper
# bytes only; no installed Vincula code, adapter or shell profile is imported.
_PROBE_SOURCE = r'''
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import UUID

HELPERS = ("jobs.py", "vincula_files.py", "vincula_capabilities.py", "plan_journal.py",
           "wire_framing.py", "direct_execution.py", "resource_policy.py", "worker_resources.py", "execution_plans.py",
           "transfer_state.py", "transfer_storage.py", "transfer_wire.py", "large_transfer_plans.py", "large_files.py",
           "node_registry.py", "large_copy_state.py", "large_copy_runtime.py")

def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result

def bounded_file(path, limit):
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("limit")
    return value

def catalog(source, path):
    result = {"source": source, "state": "invalid", "node_id": None}
    try:
        document = json.loads(bounded_file(path, 256 * 1024).decode("utf-8-sig"), object_pairs_hook=unique_fields)
        node_id = document["local"]
        if not isinstance(node_id, str) or str(UUID(node_id)) != node_id:
            return result
        members = document["nodes"]
        if not isinstance(members, list) or not 1 <= len(members) <= 64:
            return result
        local = [entry for entry in members if isinstance(entry, dict) and entry.get("id") == node_id]
        family = "windows" if sys.platform == "win32" else "linux"
        if len(local) != 1 or local[0].get("family") != family or "ssh" in local[0] or "legacy_route" in local[0]:
            return result
        return {"source": source, "state": "observed", "node_id": node_id}
    except FileNotFoundError:
        result["state"] = "missing"
    except OSError:
        result["state"] = "unavailable"
    except (ValueError, TypeError, KeyError, RecursionError):
        pass
    return result

def policy(root, compatible):
    if not compatible:
        return {"state": "unchecked", "reason": "helpers_unverified"}
    deadline = time.monotonic() + 1
    try:
        # mode=ro cannot create a database or modify its policy. query_only also
        # prevents accidental SQL writes; no initialize/migrate/dispatch runs.
        with closing(sqlite3.connect((root / "jobs.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            db.execute("BEGIN")
            modes = db.execute("SELECT CASE WHEN mode='ask' THEN 'ask' WHEN mode='full' THEN 'full' END FROM policy LIMIT 2").fetchall()
            flags = db.execute("SELECT CASE WHEN typeof(enabled)='integer' AND enabled IN (0,1) THEN enabled END FROM direct_execution_policy WHERE id=1 LIMIT 2").fetchall()
            if time.monotonic() >= deadline:
                return {"state": "unchecked", "reason": "policy_unavailable"}
            if len(modes) != 1 or modes[0][0] not in ("ask", "full") or len(flags) != 1 or flags[0][0] not in (0, 1):
                return {"state": "unchecked", "reason": "policy_invalid"}
            return {"state": "observed", "mode": modes[0][0], "native_enabled": bool(flags[0][0])}
    except (OSError, sqlite3.Error, ValueError):
        return {"state": "unchecked", "reason": "policy_unavailable"}

def main(request):
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        config, root = base / "Vincula", base / "Vincula-Jobs"
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "vincula"
        root = home / ".local/share/vincula-jobs"
    catalogs = [catalog("core", config / "core/nodes.json"), catalog("desktop", config / "nodes.json")]
    if request["local_catalog"] is not None:
        catalogs.append(catalog("selected", Path(request["local_catalog"])))
    hashes = {}
    for name in HELPERS:
        try:
            hashes[name] = hashlib.sha256(bounded_file(root / name, 512 * 1024)).hexdigest()
        except (OSError, ValueError):
            hashes[name] = None
    compatible = all(isinstance(request["helpers"].get(name), str)
                     and hashes[name] == request["helpers"][name] for name in HELPERS)
    response = {"format": "vincula-node-probe", "version": 1, "nonce": request["nonce"],
                "catalogs": catalogs, "helpers": hashes, "policy": policy(root, compatible)}
    raw = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise ValueError("response limit")
    print(raw)
'''


class _TransportFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@lru_cache(maxsize=1)
def _peek_named_pipe():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.PeekNamedPipe
    function.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    function.restype = wintypes.BOOL
    return function


def _read_available(stream, count):
    if os.name == "nt":
        import ctypes
        import msvcrt
        available = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(stream.fileno())
        if not _peek_named_pipe()(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):  # ERROR_BROKEN_PIPE, ERROR_NO_DATA
                return b""
            raise OSError(error, "Cannot inspect the probe pipe.")
        if not available.value:
            return None
        return os.read(stream.fileno(), min(count, available.value))
    try:
        return os.read(stream.fileno(), count)
    except BlockingIOError:
        return None


def _drain_available(streams):
    activity = False
    # EOF removes entries from streams, so iterate over an independent snapshot.
    for stream, (buffer, limit, reason) in streams.copy().items():
        chunk = _read_available(stream, min(4096, limit - len(buffer) + 1))
        if chunk is None:
            continue
        activity = True
        if not chunk:
            stream.close()
            del streams[stream]
            continue
        if len(buffer) + len(chunk) > limit:
            raise _TransportFailure(reason)
        buffer.extend(chunk)
    return activity


def _reap_owned(process, deadline):
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=max(0.001, deadline - monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        raise _TransportFailure("termination_unconfirmed") from None


def _capture(argv, source, *, env):
    """Bound collection/cleanup and reap only our own Popen.

    PROBE_TIMEOUT budgets pipe collection and cleanup, excluding registry waits.
    OS process creation/termination calls are not a hard real-time guarantee.
    An unconfirmed reap is an explicit failure, never a successful observation.
    """
    if not isinstance(source, bytes) or len(source) > MAX_REQUEST_BYTES:
        raise ValueError("The fixed node probe exceeds its input limit.")
    started = monotonic()
    deadline = started + PROBE_TIMEOUT
    # Leave part of the same total deadline to kill and reap an owned process.
    work_deadline = deadline - min(0.25, PROBE_TIMEOUT / 4)
    with TemporaryFile() as input_file:
        input_file.write(source)
        input_file.seek(0)
        process = subprocess.Popen(argv, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0, env=env,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        streams = {process.stdout: (bytearray(), MAX_RESPONSE_BYTES, "response_too_large"),
                   process.stderr: (bytearray(), MAX_DIAGNOSTICS_BYTES, "diagnostics_too_large")}
        output = streams[process.stdout][0]
        try:
            if os.name != "nt":
                for stream in streams:
                    os.set_blocking(stream.fileno(), False)
            while streams:
                activity = _drain_available(streams)
                if monotonic() >= work_deadline:
                    raise _TransportFailure("timeout")
                if not activity:
                    sleep(min(0.005, max(0, work_deadline - monotonic())))
            try:
                status = process.wait(timeout=max(0, work_deadline - monotonic()))
            except subprocess.TimeoutExpired:
                raise _TransportFailure("timeout") from None
            return status, bytes(output)
        finally:
            try:
                _reap_owned(process, deadline)
            finally:
                for stream in streams:
                    stream.close()


def _check_windows_file(path):
    text = str(path).replace("/", "\\")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    if (text.startswith("\\\\.\\") or re.match(r"^\\\\\?\\(?:GLOBALROOT|PIPE)(?:\\|$)", text, re.IGNORECASE)
            or any(part.rstrip(" .").split(".", 1)[0].upper() in reserved for part in PureWindowsPath(text).parts)):
        raise ValueError("The public probe file must not be a Windows device.")
    leaf = path.lstat()
    if (getattr(leaf, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            and not stat.S_ISLNK(leaf.st_mode)):
        raise ValueError("The public probe file has an unsupported reparse point.")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("The public probe file must be a regular file.")


def _bounded_digest(path, limit):
    # A configured public pin must not turn the snapshot into a blocking FIFO
    # read. Check the opened descriptor, using nonblocking opens where offered.
    if os.name == "nt":
        _check_windows_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("The public probe file must be a regular file.")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("The public probe file exceeds its limit.")
    return sha256(value).hexdigest()


def _source_hashes():
    hashes = {}
    for name in HELPERS:
        try:
            hashes[name] = _bounded_digest(Path(__file__).with_name(name), MAX_SOURCE_BYTES)
        except (OSError, ValueError):
            hashes[name] = None
    return hashes


def _unique_fields(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("Duplicate node probe fields.")
        document[key] = value
    return document


def _invalid_number(value):
    raise ValueError("Invalid node probe number.")


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _check_catalogs(catalogs, local):
    sources = {"core", "desktop", "selected"} if local else {"core", "desktop"}
    if not isinstance(catalogs, list) or len(catalogs) != len(sources):
        raise ValueError("Invalid node identity observations.")
    observed_sources = set()
    for catalog in catalogs:
        if (not isinstance(catalog, dict) or set(catalog) != {"source", "state", "node_id"}
                or not isinstance(catalog["source"], str) or catalog["source"] not in sources
                or catalog["source"] in observed_sources
                or catalog["state"] not in ("observed", "missing", "invalid", "unavailable")):
            raise ValueError("Invalid node identity observations.")
        observed_sources.add(catalog["source"])
        if catalog["state"] == "observed":
            if not _uuid(catalog["node_id"]):
                raise ValueError("Invalid persistent node UUID.")
        elif catalog["node_id"] is not None:
            raise ValueError("Invalid missing node identity.")


def _check_hashes(hashes):
    if not isinstance(hashes, dict) or set(hashes) != set(HELPERS) or any(
            digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest))
            for digest in hashes.values()):
        raise ValueError("Invalid helper observations.")


def _check_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError(_INVALID_POLICY)
    if policy.get("state") == "observed":
        if (set(policy) != {"state", "mode", "native_enabled"} or policy["mode"] not in ("ask", "full")
                or type(policy["native_enabled"]) is not bool):
            raise ValueError(_INVALID_POLICY)
    elif (set(policy) != {"state", "reason"} or policy["state"] != "unchecked"
          or policy["reason"] not in ("helpers_unverified", "policy_unavailable", "policy_invalid")):
        raise ValueError(_INVALID_POLICY)


def _payload(raw, nonce, *, local):
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Invalid node probe response size.")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_invalid_number)
    if (not isinstance(value, dict) or set(value) != {"format", "version", "nonce", "catalogs", "helpers", "policy"}
            or value["format"] != "vincula-node-probe" or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["nonce"], str) or not value["nonce"].isascii()
            or not compare_digest(value["nonce"], nonce)):
        raise ValueError("Invalid node probe envelope.")
    _check_catalogs(value["catalogs"], local)
    _check_hashes(value["helpers"])
    _check_policy(value["policy"])
    return value


def _observations(value, node_id, hashes):
    catalogs = value["catalogs"]
    identities = {entry["node_id"] for entry in catalogs if entry["state"] == "observed"}
    errors = any(entry["state"] in ("invalid", "unavailable") for entry in catalogs)
    if len(identities) > 1 or identities and errors:
        identity = {"state": "ambiguous", "reason": "catalogs_disagree_or_unreadable"}
    elif not identities:
        identity = {"state": "unavailable", "reason": "persistent_identity_unavailable"}
    else:
        identity = {"state": "match" if identities == {node_id} else "mismatch"}
    compatible = all(isinstance(hashes[name], str) and value["helpers"][name] == hashes[name] for name in HELPERS)
    if compatible:
        helper = {"state": "compatible_on_disk", "protocol_version": direct_execution.PROTOCOL_VERSION}
    else:
        helper = {"state": "missing" if all(digest is None for digest in value["helpers"].values()) else "unverified",
                  "reason": "installed_helpers_not_verified"}
    policy = value["policy"] if compatible else {"state": "unchecked", "reason": "helpers_unverified"}
    return {"identity": identity, "helper": helper, "policy": policy}


@dataclass
class _Selection:
    snapshot: dict
    entry: dict
    route: object
    pin_state: str
    pin_digest: str | None
    route_digest: str


def _selection(path, node_id):
    # Windows shares the editor's mutex. POSIX reads atomic registry snapshots;
    # the final revision/pin comparison detects edits, without creating locks.
    with node_registry.registry_guard():
        snapshot = nodes.snapshot(path)
        if snapshot["registry"] is None:
            raise ValueError("Register this computer before checking a peer.")
        registry = nodes.validate(snapshot["registry"])
        node = registry.get(node_id)
        entry = deepcopy(next(entry for entry in snapshot["registry"]["nodes"] if entry["id"] == node_id))
        pin_state, pin_digest = "not_applicable", None
        if "ssh" in entry:
            try:
                pin_digest = _bounded_digest(node.route.required_files[1], MAX_PIN_BYTES)
                pin_state = "observed"
            except (OSError, ValueError):
                pin_state = "unavailable"
        raw = json.dumps({"entry": entry, "pin_state": pin_state, "pin_digest": pin_digest},
                         sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return _Selection(snapshot, entry, node.route, pin_state, pin_digest, sha256(raw).hexdigest())


def _source(nonce, hashes, local_catalog):
    request = {"nonce": nonce, "helpers": hashes, "local_catalog": str(local_catalog) if local_catalog else None}
    value = (_PROBE_SOURCE + "\nmain(" + repr(request) + ")\n").encode("utf-8")
    if len(value) > MAX_REQUEST_BYTES:
        raise ValueError("The fixed node probe exceeds its input limit.")
    return value


def _failed_checks(checks, reason, local):
    if local:
        for name in ("identity", "helper", "policy"):
            checks[name] = {"state": "unchecked", "reason": reason}
    else:
        checks["ssh"] = {"state": "failed", "reason": reason}
    return checks


def _remote_launcher(family):
    return "py.exe -3 -IBS -" if family == "windows" else "python3 -IBS -"


def _probe(selection, path, node_id):
    local = node_id == selection.snapshot["registry"]["local"]
    checks = {"ssh": {"state": "unchecked"}, "identity": {"state": "unchecked"},
              "helper": {"state": "unchecked"}, "policy": {"state": "unchecked"},
              "worker": {"state": "unverified"}, "desktop": {"state": "unchecked"}}
    if local:
        checks["ssh"]["reason"] = "local_node"
        argv = [sys.executable, "-IBS", "-"]
    elif "legacy_route" in selection.entry:
        checks["ssh"]["reason"] = "legacy_route_requires_explicit"
        return checks
    elif "ssh" not in selection.entry:
        checks["ssh"]["reason"] = "explicit_route_missing"
        return checks
    elif selection.pin_state != "observed":
        checks["ssh"]["reason"] = "host_pin_unavailable"
        return checks
    elif not selection.route.configured():
        checks["ssh"]["reason"] = "local_identity_unavailable"
        return checks
    else:
        command = _remote_launcher(selection.entry["family"])
        argv = [node_ssh.executable("ssh"), "-T", *node_ssh.OPTIONS, *selection.route.options,
                selection.route.destination, command]
    nonce = secrets.token_hex(16)
    hashes = _source_hashes()
    source = _source(nonce, hashes, path if local else None)
    try:
        status, raw = _capture(argv, source, env=node_ssh.environment())
        if status != 0:
            raise _TransportFailure("probe_failed")
        value = _payload(raw, nonce, local=local)
    except _TransportFailure as error:
        return _failed_checks(checks, error.reason, local)
    except (OSError, subprocess.SubprocessError):
        return _failed_checks(checks, "probe_unavailable", local)
    except (ValueError, TypeError, KeyError, RecursionError):
        return _failed_checks(checks, "invalid_response", local)
    else:
        if not local:
            checks["ssh"] = {"state": "authenticated"}
        checks.update(_observations(value, node_id, hashes))
    return checks


def check_node(path, *, node_id, expected_revision):
    """Observe a canonical node against an unchanged local revision and pin."""
    try:
        valid_id = _uuid(node_id)
    except (ValueError, AttributeError):
        valid_id = False
    if not valid_id:
        raise ValueError("Select a canonical registered node UUID.")
    if not isinstance(expected_revision, str) or not _DIGEST.fullmatch(expected_revision):
        raise ValueError("Specify the registry revision returned by a previous read.")
    path = Path(path).resolve()
    started = monotonic()
    before = _selection(path, node_id)
    if before.snapshot["revision"] != expected_revision:
        raise EnrollmentConflict("The node registry changed. Reload it before checking this node.")
    checks = _probe(before, path, node_id)
    try:
        after = _selection(path, node_id)
    except (OSError, ValueError, KeyError, StopIteration):
        raise EnrollmentConflict("The node configuration changed while the check was running.") from None
    if after.snapshot["revision"] != expected_revision or after.route_digest != before.route_digest:
        raise EnrollmentConflict("The node configuration or host-key pin changed while the check was running.")
    report = {"node_id": node_id, "origin_id": before.snapshot["registry"]["local"],
              "revision": expected_revision, "route_digest": before.route_digest,
              "host_pin_sha256": before.pin_digest,
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "elapsed_seconds": round(monotonic() - started, 3), "checks": checks}
    return {**after.snapshot, "node_check": report}
