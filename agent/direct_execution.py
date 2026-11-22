# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Opt-in destination-side manifests for native noninteractive process execution."""

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

PROTOCOL_VERSION = 1
PROGRAM = "@process-v1"
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


def support(db):
    return {"protocol_version": PROTOCOL_VERSION,
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


def request(argv, cwd=None, env=None, stdin_text=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, *, _check_size=True):
    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a nonempty list of strings.")
    for index, value in enumerate(argv):
        text(value, f"argv[{index}]", empty=index != 0)
    if cwd is not None:
        text(cwd, "cwd", empty=False)
        if not Path(cwd).is_absolute():
            raise ValueError("cwd must be an absolute destination path.")
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("env must be an object of explicit string overrides.")
        seen = set()
        for name, value in env.items():
            text(name, "Environment variable name", empty=False)
            text(value, f"Environment variable {name}")
            key = name.casefold() if WINDOWS else name
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


def queue(db, access, job_id, argv, cwd=None, env=None, stdin_text=None,
          timeout_seconds=DEFAULT_TIMEOUT_SECONDS, *, home=None):
    job_id = str(UUID(job_id))
    original = request(argv, cwd, env, stdin_text, timeout_seconds)
    existing = db.execute("SELECT manifest FROM direct_process_jobs WHERE id=?", (job_id,)).fetchone()
    if existing:
        saved = checked_manifest(existing[0])
        if encode(saved["original"]) != encode(original):
            raise ValueError("Job ID already used for a different process request.")
        return saved
    if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        raise ValueError("Job ID already used outside direct execution.")
    if not support(db)["enabled"]:
        raise ValueError("Direct execution is disabled. The node owner must explicitly enable it locally.")
    if access not in {"ask", "full"}:
        raise ValueError("Unknown destination access mode.")
    saved_env = db.execute("SELECT path,pathext FROM worker_environment WHERE id=1").fetchone()
    if saved_env is None:
        raise ValueError("Start the destination worker before resolving an executable.")
    resolved_cwd = Path(cwd) if cwd is not None else Path.home() if home is None else Path(home)
    if not resolved_cwd.is_absolute() or not resolved_cwd.is_dir():
        raise ValueError("cwd must identify an existing absolute destination directory.")
    resolved = dict(original, argv=[executable(argv[0], *saved_env), *argv[1:]],
                    cwd=str(resolved_cwd.resolve()), env=env or {})
    manifest = {"protocol_version": PROTOCOL_VERSION, "kind": "process", "original": original,
                "resolved": resolved}
    manifest["sha256"] = digest(manifest)
    db.execute("INSERT INTO direct_process_jobs VALUES(?,?,?,?,NULL)",
               (job_id, encode(manifest), manifest["sha256"] if access == "full" else None,
                "full" if access == "full" else None))
    db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,?,?)",
               (job_id, PROGRAM, LEGACY_ARGV, "queued" if access == "full" else "pending", time.time()))
    return manifest


def checked_manifest(raw):
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.keys() != {"protocol_version", "kind", "original", "resolved", "sha256"}
            or type(value["protocol_version"]) is not int or value["protocol_version"] != PROTOCOL_VERSION
            or value["kind"] != "process" or value["sha256"] != digest(value)):
        raise ValueError("Invalid or unsupported immutable process manifest.")
    for field in ("original", "resolved"):
        entry = value[field]
        if not isinstance(entry, dict) or entry.keys() != {"argv", "cwd", "env", "stdin_text", "timeout_seconds"}:
            raise ValueError("Invalid immutable process request.")
        request(**entry, _check_size=field == "original")
    original, resolved = value["original"], value["resolved"]
    if (not Path(resolved["argv"][0]).is_absolute() or not isinstance(resolved["cwd"], str)
            or resolved["argv"][1:] != original["argv"][1:]
            or resolved["env"] != (original["env"] or {})
            or resolved["stdin_text"] != original["stdin_text"]
            or resolved["timeout_seconds"] != original["timeout_seconds"]):
        raise ValueError("The resolved process differs from its approved request.")
    return value


def read(db, job_id):
    row = db.execute("SELECT j.program,j.argv,d.manifest FROM direct_process_jobs d JOIN jobs j ON j.id=d.id "
                     "WHERE d.id=?", (str(UUID(job_id)),)).fetchone()
    if row is None:
        raise ValueError("Unknown direct process ID on this node.")
    if row[0] != PROGRAM or row[1] != LEGACY_ARGV:
        raise ValueError("The direct process job marker is invalid.")
    return checked_manifest(row[2])


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
    spec = dict(manifest["resolved"])
    spec["env"] = dict(base_env)
    if WINDOWS:
        overrides = {key.casefold() for key in manifest["resolved"]["env"]}
        spec["env"] = {key: value for key, value in spec["env"].items() if key.casefold() not in overrides}
    spec["env"].update(manifest["resolved"]["env"])
    return spec


def record_outcome(db, job_id, state, code, reason, started):
    value = {"runtime_protocol_version": PROTOCOL_VERSION, "state": state,
             "program_exit_code": code, "reason": reason, "started": started}
    db.execute("UPDATE direct_process_jobs SET outcome=? WHERE id=?", (encode(value), job_id))


def result(db, job_id):
    manifest = read(db, job_id)
    row = db.execute("SELECT outcome FROM direct_process_jobs WHERE id=?", (job_id,)).fetchone()
    return {"manifest": manifest, "outcome": json.loads(row[0]) if row[0] is not None else None}
