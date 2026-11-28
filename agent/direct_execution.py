# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Opt-in destination-side manifests for native noninteractive process execution."""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import time
from uuid import UUID

import resource_policy

PROTOCOL_VERSION = 1
RESOURCE_PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOLS = (PROTOCOL_VERSION, RESOURCE_PROTOCOL_VERSION)
PROGRAM = "@process-v1"
PROGRAM_V2 = "@process-v2"
# JSON null is deliberately not argv. Legacy workers fail before launching anything.
LEGACY_ARGV = "null"
MAX_REQUEST_BYTES = 256 * 1024
MAX_STDIN_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TRANSPORT_BYTES = MAX_REQUEST_BYTES + 4096
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600
WINDOWS = sys.platform == "win32"
SCHEMA = """
CREATE TABLE IF NOT EXISTS direct_execution_policy(
    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
);
INSERT OR IGNORE INTO direct_execution_policy VALUES(1,0);
CREATE TABLE IF NOT EXISTS direct_process_jobs(
    id TEXT PRIMARY KEY, manifest TEXT NOT NULL, approved_sha256 TEXT, approval_mode TEXT, outcome TEXT
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def support(db, protocol_version=PROTOCOL_VERSION):
    if type(protocol_version) is not int or protocol_version not in SUPPORTED_PROTOCOLS:
        raise ValueError("Unsupported direct process protocol version.")
    return {"protocol_version": protocol_version,
            "enabled": bool(db.execute("SELECT enabled FROM direct_execution_policy WHERE id=1").fetchone()[0]),
            "interactive": False, "max_request_bytes": MAX_REQUEST_BYTES,
            "max_stdin_bytes": MAX_STDIN_BYTES, "max_response_bytes": MAX_RESPONSE_BYTES, "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "max_timeout_seconds": MAX_TIMEOUT_SECONDS}


def set_enabled(db, enabled):
    if type(enabled) is not bool:
        raise ValueError("Direct execution access must be enabled or disabled explicitly.")
    db.execute("UPDATE direct_execution_policy SET enabled=? WHERE id=1", (int(enabled),))
    if not enabled:
        db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE state IN ('pending','queued') "
                   "AND id IN (SELECT id FROM direct_process_jobs)", (time.time(),))


def text(value, label, empty=True, allow_nul=False):
    if not isinstance(value, str) or ("\x00" in value and not allow_nul) or (not empty and not value):
        raise ValueError(f"{label} must be {'a nonempty' if not empty else 'a'} string without NUL bytes.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{label} must contain valid UTF-8 text.") from None
    return value


def request(argv, cwd=None, env=None, stdin_text=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, *,
            resources=None, protocol_version=PROTOCOL_VERSION, _check_size=True, _platform=None):
    if type(protocol_version) is not int or protocol_version not in SUPPORTED_PROTOCOLS:
        raise ValueError("Unsupported direct process protocol version.")
    if protocol_version == PROTOCOL_VERSION and resources is not None:
        raise ValueError("Declared resources require process protocol version 2.")
    if _platform not in (None, "linux", "windows", "portable"):
        raise ValueError("Unsupported process path platform.")
    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a nonempty list of strings.")
    for index, value in enumerate(argv):
        text(value, f"argv[{index}]", empty=index != 0)
    if cwd is not None:
        text(cwd, "cwd", empty=False)
        absolute = (PureWindowsPath(cwd).is_absolute() if _platform == "windows" else
                    PurePosixPath(cwd).is_absolute() if _platform == "linux" else
                    (PureWindowsPath(cwd).is_absolute() or PurePosixPath(cwd).is_absolute())
                    if _platform == "portable" else Path(cwd).is_absolute())
        if not absolute:
            raise ValueError("cwd must be an absolute destination path.")
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("env must be an object of explicit string overrides.")
        seen = set()
        for name, value in env.items():
            text(name, "Environment variable name", empty=False)
            text(value, f"Environment variable {name}")
            fold_case = WINDOWS if _platform is None else _platform == "windows"
            key = name.casefold() if fold_case else name
            if "=" in name or key in seen:
                raise ValueError("Environment names must be unique and must not contain '='.")
            seen.add(key)
    if stdin_text is not None:
        text(stdin_text, "stdin_text", allow_nul=True)
        if len(stdin_text.encode("utf-8")) > MAX_STDIN_BYTES:
            raise ValueError("stdin_text exceeds 64 KiB of UTF-8.")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}.")
    value = {"argv": argv, "cwd": cwd, "env": env, "stdin_text": stdin_text,
             "timeout_seconds": timeout_seconds}
    if protocol_version == RESOURCE_PROTOCOL_VERSION:
        value["resources"] = resource_policy.validate_requirements(resources)
    if _check_size and len(encode(value).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("The process request exceeds 256 KiB of UTF-8 JSON.")
    # Detach mutable lists and dictionaries from caller-owned objects.
    return json.loads(encode(value))


def executable(name, worker_path, worker_pathext):
    candidate = Path(name)
    if candidate.is_absolute():
        choices = [candidate]
    elif candidate.name != name or (WINDOWS and ("/" in name or "\\" in name)):
        raise ValueError("Use an absolute executable path or a bare name from the worker PATH.")
    else:
        suffixes = [""]
        if WINDOWS:
            extensions = [suffix for suffix in worker_pathext.split(";") if suffix]
            if candidate.suffix.casefold() not in {suffix.casefold() for suffix in extensions}:
                suffixes += extensions
        choices = [Path(folder) / (name + suffix)
                   for folder in worker_path.split(os.pathsep) if Path(folder).is_absolute()
                   for suffix in suffixes]
    for path in choices:
        if path.is_file() and (WINDOWS or os.access(path, os.X_OK)):
            resolved = path.resolve()
            if WINDOWS and resolved.suffix.casefold() in {".bat", ".cmd"}:
                raise ValueError("Batch scripts require an explicit cmd.exe interpreter in argv.")
            return str(resolved)
    raise ValueError("The executable is unavailable on the destination worker PATH.")


def digest(manifest):
    return sha256(encode({key: value for key, value in manifest.items() if key != "sha256"}).encode("utf-8")).hexdigest()


def absolute_path(value, platform):
    if platform == "windows":
        return PureWindowsPath(value).is_absolute()
    if platform == "linux":
        return PurePosixPath(value).is_absolute()
    return Path(value).is_absolute()


def queue(db, access, job_id, argv, cwd=None, env=None, stdin_text=None,
          timeout_seconds=DEFAULT_TIMEOUT_SECONDS, *, home=None, resources=None,
          protocol_version=PROTOCOL_VERSION, expected_state_id=None):
    job_id = str(UUID(job_id))
    original = request(argv, cwd, env, stdin_text, timeout_seconds,
                       resources=resources, protocol_version=protocol_version)
    if expected_state_id is not None:
        if protocol_version != RESOURCE_PROTOCOL_VERSION or str(UUID(expected_state_id)) != expected_state_id:
            raise ValueError("Use a canonical resource destination state UUID with protocol 2.")
        if resource_policy.state_id(db) != expected_state_id:
            raise ValueError("The destination durable state changed; do not replay this execution plan.")
    existing = db.execute("SELECT manifest FROM direct_process_jobs WHERE id=?", (job_id,)).fetchone()
    if existing:
        saved = checked_manifest(existing[0])
        if (saved["protocol_version"] != protocol_version or encode(saved["original"]) != encode(original)
                or expected_state_id is not None and saved.get("destination_state_id") != expected_state_id):
            raise ValueError("Job ID already used for a different process request.")
        return saved
    if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        raise ValueError("Job ID already used outside direct execution.")
    if not support(db)["enabled"]:
        raise ValueError("Direct execution is disabled. The node owner must explicitly enable it locally.")
    if access not in {"ask", "full"}:
        raise ValueError("Unknown destination access mode.")
    destination_state = None
    if protocol_version == RESOURCE_PROTOCOL_VERSION:
        snapshot = resource_policy.status(db)
        destination_state = snapshot["state_id"]
        if expected_state_id is not None and expected_state_id != destination_state:
            raise ValueError("The destination durable state changed; do not replay this execution plan.")
        eligibility = resource_policy.evaluate(snapshot, original["resources"])
        if not eligibility["eligible"]:
            raise ValueError("Resource-aware process unavailable: " + eligibility["reason"])
    saved_env = db.execute("SELECT path,pathext FROM worker_environment WHERE id=1").fetchone()
    if saved_env is None:
        raise ValueError("Start the destination worker before resolving an executable.")
    resolved_cwd = Path(cwd) if cwd is not None else Path.home() if home is None else Path(home)
    if not resolved_cwd.is_absolute() or not resolved_cwd.is_dir():
        raise ValueError("cwd must identify an existing absolute destination directory.")
    resolved = dict(original, argv=[executable(argv[0], *saved_env), *argv[1:]],
                    cwd=str(resolved_cwd.resolve()), env=env or {})
    manifest = {"protocol_version": protocol_version, "kind": "process", "original": original,
                "resolved": resolved}
    if protocol_version == RESOURCE_PROTOCOL_VERSION:
        manifest["destination_state_id"] = destination_state
    manifest["sha256"] = digest(manifest)
    db.execute("INSERT INTO direct_process_jobs VALUES(?,?,?,?,NULL)",
               (job_id, encode(manifest), manifest["sha256"] if access == "full" else None,
                "full" if access == "full" else None))
    db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,?,?)",
               (job_id, PROGRAM_V2 if protocol_version == RESOURCE_PROTOCOL_VERSION else PROGRAM,
                LEGACY_ARGV, "queued" if access == "full" else "pending", time.time()))
    if protocol_version == RESOURCE_PROTOCOL_VERSION:
        resource_policy.set_request(db, job_id, original["resources"])
    return manifest


def checked_manifest(raw, *, _platform=None):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid or unsupported immutable process manifest.")
    version = value.get("protocol_version")
    fields = {"protocol_version", "kind", "original", "resolved", "sha256"}
    if version == RESOURCE_PROTOCOL_VERSION:
        fields.add("destination_state_id")
    if (value.keys() != fields or type(version) is not int or version not in SUPPORTED_PROTOCOLS
            or value["kind"] != "process" or value["sha256"] != digest(value)):
        raise ValueError("Invalid or unsupported immutable process manifest.")
    if version == RESOURCE_PROTOCOL_VERSION:
        state_id = value["destination_state_id"]
        if not isinstance(state_id, str) or str(UUID(state_id)) != state_id:
            raise ValueError("Invalid immutable resource destination state.")
    for field in ("original", "resolved"):
        entry = value[field]
        expected = {"argv", "cwd", "env", "stdin_text", "timeout_seconds"}
        if version == RESOURCE_PROTOCOL_VERSION:
            expected.add("resources")
        if not isinstance(entry, dict) or entry.keys() != expected:
            raise ValueError("Invalid immutable process request.")
        request(**entry, protocol_version=version, _check_size=field == "original", _platform=_platform)
    original, resolved = value["original"], value["resolved"]
    if (not absolute_path(resolved["argv"][0], _platform) or not isinstance(resolved["cwd"], str)
            or resolved["argv"][1:] != original["argv"][1:]
            or resolved["env"] != (original["env"] or {})
            or resolved["stdin_text"] != original["stdin_text"]
            or resolved["timeout_seconds"] != original["timeout_seconds"]
            or version == RESOURCE_PROTOCOL_VERSION and resolved["resources"] != original["resources"]):
        raise ValueError("The resolved process differs from its approved request.")
    return value


def read(db, job_id):
    row = db.execute("SELECT j.program,j.argv,d.manifest FROM direct_process_jobs d JOIN jobs j ON j.id=d.id "
                     "WHERE d.id=?", (str(UUID(job_id)),)).fetchone()
    if row is None:
        raise ValueError("Unknown direct process ID on this node.")
    manifest = checked_manifest(row[2])
    marker = PROGRAM_V2 if manifest["protocol_version"] == RESOURCE_PROTOCOL_VERSION else PROGRAM
    if row[0] != marker or row[1] != LEGACY_ARGV:
        raise ValueError("The direct process job marker is invalid.")
    return manifest


def approve(db, job_id, expected_sha256):
    manifest = read(db, job_id)
    if expected_sha256 != manifest["sha256"]:
        raise ValueError("Review the exact process manifest and provide its SHA-256 to approve it.")
    if not support(db)["enabled"]:
        raise ValueError("Direct execution is disabled on this node.")
    changed = db.execute("UPDATE jobs SET state='queued' WHERE id=? AND state='pending'", (job_id,))
    if not changed.rowcount:
        raise ValueError("Process job is not awaiting approval.")
    db.execute("UPDATE direct_process_jobs SET approved_sha256=?,approval_mode='ask' WHERE id=?",
               (expected_sha256, job_id))


def launch_spec(db, job_id, base_env):
    manifest = read(db, job_id)
    state = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
    if state != "running" or not support(db)["enabled"]:
        raise ValueError("The enabled destination worker must launch the approved process job.")
    approval = db.execute("SELECT approved_sha256,approval_mode FROM direct_process_jobs WHERE id=?",
                          (job_id,)).fetchone()
    access = db.execute("SELECT mode FROM policy").fetchone()[0]
    if (approval[0] != manifest["sha256"] or approval[1] not in {"ask", "full"}
            or (access == "ask" and approval[1] != "ask")):
        raise ValueError("The immutable process request has no matching destination authorization.")
    if manifest["protocol_version"] == RESOURCE_PROTOCOL_VERSION:
        snapshot = resource_policy.status(db)
        declared = manifest["original"]["resources"]
        worker = snapshot["worker"]
        if (snapshot["state_id"] != manifest["destination_state_id"]
                or resource_policy.request_for_job(db, job_id) != declared or worker is None
                or not any(claim["job_id"] == job_id and claim["generation"] == worker["generation"]
                           and claim["requirements"] == declared and not claim["legacy"]
                           for claim in snapshot["claims"])):
            raise ValueError("The resource-aware process has no matching owned admission claim.")
    # Resource declarations are enforced by admission; the internal launcher
    # continues to receive the existing five-field execution specification.
    spec = {key: value for key, value in manifest["resolved"].items() if key != "resources"}
    spec["env"] = dict(base_env)
    if WINDOWS:
        overrides = {key.casefold() for key in manifest["resolved"]["env"]}
        spec["env"] = {key: value for key, value in spec["env"].items() if key.casefold() not in overrides}
    spec["env"].update(manifest["resolved"]["env"])
    return spec


def record_outcome(db, job_id, state, code, reason, started):
    # Recording a failed launch must also work when manifest validation itself
    # failed. The job marker selects the wire generation; read() still rejects
    # malformed manifests before exposing a usable process result.
    row = db.execute("SELECT program FROM jobs WHERE id=?", (job_id,)).fetchone()
    version = RESOURCE_PROTOCOL_VERSION if row and row[0] == PROGRAM_V2 else PROTOCOL_VERSION
    value = {"runtime_protocol_version": version, "state": state,
             "program_exit_code": code, "reason": reason, "started": started}
    db.execute("UPDATE direct_process_jobs SET outcome=? WHERE id=?", (encode(value), job_id))


def result(db, job_id):
    manifest = read(db, job_id)
    row = db.execute("SELECT outcome FROM direct_process_jobs WHERE id=?", (job_id,)).fetchone()
    return {"manifest": manifest, "outcome": json.loads(row[0]) if row[0] is not None else None}
