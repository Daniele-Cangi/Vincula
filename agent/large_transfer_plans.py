# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Immutable topology for large files; contains no host-local paths or credentials."""
from hashlib import sha256
import json
import math
import re
from uuid import UUID, uuid5

BLOCK_SIZE = 4 * 1024 * 1024
MAX_BYTES = 1024**4
MAX_TTL = 30 * 86400
MAX_MANIFEST_BYTES = 16 * 1024
KIND = "large-file"
SELECTOR = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
ROOT = re.compile(r"[a-z][a-z0-9_-]{0,47}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use a canonical transfer or destination-state UUID.")
    return value


def selector(value):
    if not isinstance(value, str) or SELECTOR.fullmatch(value) is None:
        raise ValueError("Use a registered node ID.")
    return value


def relative(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240:
        raise ValueError("Use a bounded relative file path.")
    for part in value.split("/"):
        if (part in {"", ".", ".."} or part.endswith((" ", "."))
                or part.casefold().startswith(".vincula-import-")
                or any(char in '\\:<>|?*"' or ord(char) < 32 for char in part)):
            raise ValueError("Use a relative path inside an authorized folder.")
    # Host-specific reserved names are checked on the endpoint, not on an
    # unrelated coordinator's OS when reading an existing Linux-file manifest.
    return value


def endpoint(value):
    if not isinstance(value, dict) or set(value) != {"node_id", "root", "path"}:
        raise ValueError("Invalid large-file endpoint fields.")
    if not isinstance(value["root"], str) or ROOT.fullmatch(value["root"]) is None:
        raise ValueError("Use an existing authorized folder alias.")
    return {"node_id": selector(value["node_id"]), "root": value["root"], "path": relative(value["path"])}


def normalize_request(value):
    if not isinstance(value, dict) or set(value) != {"source", "destination", "max_bytes", "ttl_seconds"}:
        raise ValueError("Invalid large-file transfer request.")
    maximum, ttl = value["max_bytes"], value["ttl_seconds"]
    if type(maximum) is not int or not 1 <= maximum <= MAX_BYTES:
        raise ValueError("Declare a positive file budget up to one TiB.")
    if type(ttl) is not int or not 60 <= ttl <= MAX_TTL:
        raise ValueError("Declare a retention deadline between one minute and thirty days.")
    source, destination = endpoint(value["source"]), endpoint(value["destination"])
    identity(source["node_id"]);identity(destination["node_id"])
    if source["node_id"] == destination["node_id"]:
        raise ValueError("Choose endpoints on different registered nodes.")
    return {"source": source, "destination": destination, "max_bytes": maximum, "ttl_seconds": ttl}


def endpoint_id(transfer_id, authority, role):
    if role not in {"export", "import"}:
        raise ValueError("Unknown large-file endpoint role.")
    return str(uuid5(UUID(identity(transfer_id)), f"large-file:{identity(authority)}:{role}"))


def make_manifest(transfer_id, authority, request, source_state, destination_state, now):
    if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
        raise ValueError("Use a finite creation time.")
    value = {"version": 1, "kind": KIND, "id": identity(transfer_id), "origin": selector(authority),
             "request": normalize_request(request), "plan": {
                 "source_id": endpoint_id(transfer_id, authority, "export"),
                 "destination_id": endpoint_id(transfer_id, authority, "import"),
                 "source_state_id": identity(source_state), "destination_state_id": identity(destination_state),
                 "created_at": float(now), "expires_at": float(now) + request["ttl_seconds"], "block_size": BLOCK_SIZE}}
    return validate_manifest(value)


def validate_manifest(value):
    if not isinstance(value, dict) or set(value) != {"version", "kind", "id", "origin", "request", "plan"}:
        raise ValueError("Invalid large-file plan manifest.")
    if type(value["version"]) is not int or value["version"] != 1 or value["kind"] != KIND:
        raise ValueError("Unsupported large-file plan version.")
    key, origin, request = identity(value["id"]), selector(value["origin"]), normalize_request(value["request"])
    plan = value["plan"]
    fields = {"source_id", "destination_id", "source_state_id", "destination_state_id", "created_at", "expires_at", "block_size"}
    if not isinstance(plan, dict) or set(plan) != fields:
        raise ValueError("Invalid large-file plan assignment.")
    for name in ("source_state_id", "destination_state_id"):
        identity(plan[name])
    if plan["source_id"] != endpoint_id(key, origin, "export") or plan["destination_id"] != endpoint_id(key, origin, "import"):
        raise ValueError("Large-file endpoint IDs differ from the original handle.")
    if type(plan["block_size"]) is not int or plan["block_size"] != BLOCK_SIZE:
        raise ValueError("Unsupported large-file block size.")
    for name in ("created_at", "expires_at"):
        if type(plan[name]) not in (float, int) or not math.isfinite(plan[name]) or plan[name] < 0:
            raise ValueError("Invalid large-file retention timestamp.")
    if plan["expires_at"] != plan["created_at"] + request["ttl_seconds"]:
        raise ValueError("Large-file retention differs from its immutable request.")
    raw = canonical(value).encode()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Large-file plan metadata exceeds 16 KiB.")
    return json.loads(raw)


def endpoint_spec(manifest, role, source_sha256=None):
    manifest = validate_manifest(manifest)
    if role not in {"export", "import"}:
        raise ValueError("Unknown large-file endpoint role.")
    plan, request = manifest["plan"], manifest["request"]
    return {"transfer_id": manifest["id"], "authority_node_id": manifest["origin"],
            "source": request["source"], "destination": request["destination"], "role": role,
            "expected_state_id": plan["source_state_id" if role == "export" else "destination_state_id"],
            "max_bytes": request["max_bytes"], "expires_at": plan["expires_at"], "source_sha256": source_sha256}


def package(manifest):
    manifest = validate_manifest(manifest)
    return {"manifest": manifest, "sha256": sha256(canonical(manifest).encode()).hexdigest()}


def select_replica(transfer_id, records):
    selected = None
    for record in records:
        if record is None:
            continue
        if not isinstance(record, dict) or set(record) != {"manifest", "sha256"}:
            raise ValueError("Invalid large-file replica record.")
        checked = package(record["manifest"])
        if checked["sha256"] != record["sha256"] or checked["manifest"]["id"] != identity(transfer_id):
            raise ValueError("Large-file replica digest or identity differs.")
        if selected is not None and selected["sha256"] != checked["sha256"]:
            raise ValueError("Conflicting large-file plans on reachable replicas.")
        selected = checked
    return selected["manifest"] if selected else None
