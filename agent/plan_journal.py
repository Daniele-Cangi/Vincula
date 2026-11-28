# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Immutable execution manifests held by workers for coordinator-independent reads."""

from hashlib import sha256
import json
import re
import time
from uuid import UUID

MAX_BYTES = 256 * 1024
MAX_REQUEST_BYTES = MAX_BYTES + 4096
MAX_RESPONSE_BYTES = 2 * MAX_BYTES + 4096
MAX_TASKS = 12
MAX_PARTS = 16
MAX_FILES = 4
SELECTOR = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PROGRAM = re.compile(r"[a-z][a-z0-9_-]{0,47}")
CAPABILITY = re.compile(r"[a-z][a-z0-9_-]{0,23}\.[a-z][a-z0-9_-]{0,23}")
DIGEST = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{32}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_journal(
    kind TEXT NOT NULL, id TEXT NOT NULL, manifest TEXT NOT NULL,
    sha256 TEXT NOT NULL, created REAL NOT NULL,
    PRIMARY KEY(kind,id)
);
"""


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use a canonical plan or job UUID.")
    return value


def selector(value):
    return isinstance(value, str) and SELECTOR.fullmatch(value) is not None


def validate_task(task, index, requested):
    if not isinstance(task, dict) or task.keys() != {"index", "node_id", "program", "job_id"}:
        raise ValueError("Invalid batch manifest task.")
    if type(task["index"]) is not int or task["index"] != index or not selector(task["node_id"]):
        raise ValueError("Invalid batch manifest assignment.")
    canonical_uuid(task["job_id"])
    if not isinstance(requested, dict) or requested.keys() != {"node_id", "program"}:
        raise ValueError("Invalid batch manifest request.")
    program = requested["program"]
    if not isinstance(program, str) or not PROGRAM.fullmatch(program) or task["program"] != program:
        raise ValueError("Invalid batch manifest program.")
    if requested["node_id"] is not None and not selector(requested["node_id"]):
        raise ValueError("Invalid batch manifest node selector.")


def validate_batch(manifest):
    request, plan = manifest["request"], manifest["plan"]
    if (not isinstance(request, list) or not isinstance(plan, list)
            or not 1 <= len(request) <= MAX_TASKS or len(request) != len(plan)):
        raise ValueError("Invalid batch manifest size.")
    for index, (requested, task) in enumerate(zip(request, plan)):
        validate_task(task, index, requested)
    if len({task["job_id"] for task in plan}) != len(plan):
        raise ValueError("Duplicate job UUID in batch manifest.")


def parameters(value):
    if not isinstance(value, dict) or len(value) > 12:
        raise ValueError("Invalid capability manifest parameters.")
    for name, item in value.items():
        if not isinstance(name, str) or not PROGRAM.fullmatch(name) or type(item) not in (str, int, float, bool):
            raise ValueError("Invalid capability manifest parameter.")
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))) > 512:
        raise ValueError("Capability manifest parameters exceed 512 bytes.")


def endpoint(value):
    if not isinstance(value, dict) or value.keys() != {"node_id", "root", "path"}:
        raise ValueError("Invalid capability manifest file.")
    if not selector(value["node_id"]) or not isinstance(value["root"], str) or not PROGRAM.fullmatch(value["root"]):
        raise ValueError("Invalid capability manifest file origin.")
    path = value["path"]
    if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 240:
        raise ValueError("Invalid capability manifest file path.")


def validate_capability_input(part, source, item, expected_index):
    endpoint(source)
    if not isinstance(item, dict) or item.keys() != {"index", "source"}:
        raise ValueError("Invalid capability manifest input.")
    if type(item["index"]) is not int or item["index"] != expected_index:
        raise ValueError("Invalid capability manifest input index.")
    endpoint(item["source"])
    if source["root"] != item["source"]["root"] or source["path"] != item["source"]["path"]:
        raise ValueError("Capability manifest input differs from the request.")
    if item["source"]["node_id"] != part["node_id"] and part["inbox"] is None:
        raise ValueError("A remote capability input needs an inbox.")


def validate_capability_part(part, requested, next_index):
    if (not isinstance(part, dict)
            or part.keys() != {"node_id", "job_id", "revision", "inbox", "parameters", "inputs"}):
        raise ValueError("Invalid capability manifest part.")
    if not selector(part["node_id"]) or not isinstance(part["revision"], str) or not REVISION.fullmatch(part["revision"]):
        raise ValueError("Invalid capability manifest assignment or revision.")
    canonical_uuid(part["job_id"])
    if part["inbox"] is not None and (not isinstance(part["inbox"], str) or not PROGRAM.fullmatch(part["inbox"])):
        raise ValueError("Invalid capability manifest inbox.")
    if not isinstance(requested, dict) or requested.keys() != {"node_id", "files", "parameters"}:
        raise ValueError("Invalid capability manifest request.")
    if not selector(requested["node_id"]) or requested["parameters"] != part["parameters"]:
        raise ValueError("Capability manifest parameters differ from the request.")
    parameters(part["parameters"])
    files, inputs = requested["files"], part["inputs"]
    if (not isinstance(files, list) or not isinstance(inputs, list)
            or len(files) != len(inputs) or len(inputs) > MAX_FILES):
        raise ValueError("Invalid capability manifest input count.")
    for offset, (source, item) in enumerate(zip(files, inputs)):
        validate_capability_input(part, source, item, next_index + offset)
    return next_index + len(inputs)


def validate_capability(manifest):
    request, plan = manifest["request"], manifest["plan"]
    if (not isinstance(request, dict) or request.keys() != {"capability_id", "parts"}
            or not isinstance(plan, dict) or plan.keys() != {"capability_id", "parts"}):
        raise ValueError("Invalid capability manifest fields.")
    capability_id = request["capability_id"]
    if not isinstance(capability_id, str) or not CAPABILITY.fullmatch(capability_id) or plan["capability_id"] != capability_id:
        raise ValueError("Invalid capability manifest ID.")
    parts, requested = plan["parts"], request["parts"]
    if (not isinstance(parts, list) or not isinstance(requested, list)
            or not 1 <= len(parts) <= MAX_PARTS or len(parts) != len(requested)):
        raise ValueError("Invalid capability manifest part count.")
    next_index = 0
    for part, source in zip(parts, requested):
        next_index = validate_capability_part(part, source, next_index)
    if len({part["node_id"] for part in parts}) != len(parts) or len({part["job_id"] for part in parts}) != len(parts):
        raise ValueError("Duplicate node or job UUID in capability manifest.")


def package(manifest):
    if not isinstance(manifest, dict) or manifest.keys() != {"version", "kind", "id", "origin", "request", "plan"}:
        raise ValueError("Invalid plan manifest fields.")
    if type(manifest["version"]) is not int or manifest["version"] != 1 or manifest["kind"] not in ("batch", "capability", "execution", "large-file"):
        raise ValueError("Unsupported plan manifest version or kind.")
    canonical_uuid(manifest["id"])
    if not selector(manifest["origin"]):
        raise ValueError("Invalid plan manifest origin.")
    if manifest["kind"] == "batch":
        validate_batch(manifest)
    elif manifest["kind"] == "capability":
        validate_capability(manifest)
    elif manifest["kind"] == "execution":
        # Loaded from the pinned adapter directory by jobs.py under -I. Keeping
        # this import lazy preserves independent use of the legacy journal.
        import execution_plans
        execution_plans.validate_manifest(manifest)
    else:
        import large_transfer_plans
        large_transfer_plans.validate_manifest(manifest)
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Plan manifest exceeds 256 KiB.")
    return {"manifest": json.loads(raw), "sha256": sha256(raw.encode("utf-8")).hexdigest()}


def unpack(record):
    if not isinstance(record, dict) or record.keys() != {"manifest", "sha256"}:
        raise ValueError("Invalid plan journal reply.")
    digest = record["sha256"]
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("Invalid plan journal digest.")
    checked = package(record["manifest"])
    if checked["sha256"] != digest:
        raise ValueError("Plan journal digest does not match its manifest.")
    return checked


def get(db, kind, plan_id):
    if kind not in ("batch", "capability", "execution", "large-file"):
        raise ValueError("Unsupported plan journal kind.")
    canonical_uuid(plan_id)
    row = db.execute("SELECT manifest,sha256 FROM plan_journal WHERE kind=? AND id=?", (kind, plan_id)).fetchone()
    if row is None:
        return None
    try:
        record = unpack({"manifest": json.loads(row[0]), "sha256": row[1]})
        if record["manifest"]["kind"] != kind or record["manifest"]["id"] != plan_id:
            return {"state": "invalid"}
        return record
    except (TypeError, ValueError, RecursionError):
        return {"state": "invalid"}


def store(db, manifest):
    record = package(manifest)
    body, digest = record["manifest"], record["sha256"]
    db.execute("INSERT OR IGNORE INTO plan_journal VALUES(?,?,?,?,?)", (
        body["kind"], body["id"], json.dumps(body, ensure_ascii=True), digest, time.time(),
    ))
    saved = get(db, body["kind"], body["id"])
    if saved.keys() == {"state"}:
        return saved
    if saved["sha256"] != digest:
        return {"state": "conflict", "sha256": saved["sha256"]}
    return {"state": "stored", "sha256": digest}


def dispatch(db, operation, manifest=None, kind=None, plan_id=None):
    if operation == "store":
        return store(db, manifest)
    if operation == "get":
        return get(db, kind, plan_id)
    raise ValueError("Unknown plan journal operation.")


def select_replica(kind, plan_id, records):
    selected = None
    for record in records:
        if record is None:
            continue
        checked = unpack(record)
        body = checked["manifest"]
        if body["kind"] != kind or body["id"] != str(plan_id):
            raise ValueError("A replica returned another plan kind or UUID.")
        if selected is not None and checked["sha256"] != selected["sha256"]:
            raise ValueError("Conflicting execution manifests were found on reachable nodes.")
        selected = checked
    return selected["manifest"] if selected else None


def check_replication(receipt, expected_digest):
    if not isinstance(receipt, dict):
        raise ValueError("Invalid plan journal receipt.")
    if receipt.get("state") == "conflict":
        raise ValueError("A destination holds a conflicting execution manifest.")
    if receipt != {"state": "stored", "sha256": expected_digest}:
        raise ValueError("A destination did not confirm the execution manifest digest.")
