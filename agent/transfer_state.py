# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol-2 endpoint metadata and cooperative disk reservations, with no file I/O.

Callers own BEGIN IMMEDIATE and perform filesystem/process work outside it.
Grant/identity/volume observations and cleanup booleans are trusted LOCAL wrapper
inputs, never assertions accepted directly from the wire. Metadata cannot prove
physical deletion or descendant death. The wrapper must quiesce endpoint I/O
before cleanup, validate grants before every I/O, and bind each owned job UUID.
"""
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
import time
from uuid import UUID, uuid5

import resource_policy

VERSION = 2
BLOCK_SIZE = 4 * 1024 * 1024
MAX_FILE_BYTES = 1024**4
PAGE_LIMIT = 128
DIGEST = re.compile(r"[0-9a-f]{64}")
ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,47}")
DEFAULT_POLICY = {"enabled": False, "max_file_bytes": MAX_FILE_BYTES,
                  "max_reserved_bytes": 2 * MAX_FILE_BYTES, "max_active_endpoints": 2,
                  "max_pending_endpoints": 64, "margin_bytes": 64 * 1024 * 1024}
TERMINAL = {"denied", "revoked", "cleaned", "published"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS large_file_policy(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS large_file_workers(generation TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS large_file_endpoints(
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, grant_snapshot TEXT NOT NULL, source_identity TEXT,
 request_sha256 TEXT NOT NULL, intent_sha256 TEXT, intent_auto INTEGER NOT NULL,
 header TEXT, content_sha256 TEXT, content_approved TEXT, content_auto INTEGER NOT NULL,
 phase TEXT NOT NULL, epoch INTEGER NOT NULL, created REAL NOT NULL, receipt TEXT,
 verified_blocks INTEGER NOT NULL, verified_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS large_file_claims(
 endpoint_id TEXT PRIMARY KEY, bytes INTEGER NOT NULL, volumes TEXT NOT NULL,
 created REAL NOT NULL, released REAL
);
CREATE TABLE IF NOT EXISTS large_file_blocks(
 endpoint_id TEXT NOT NULL, block_index INTEGER NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(endpoint_id,block_index)
);
CREATE TABLE IF NOT EXISTS large_file_attempts(
 id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL, epoch INTEGER NOT NULL, kind TEXT NOT NULL,
 created REAL NOT NULL, finished REAL, cleanup_confirmed INTEGER NOT NULL, outcome TEXT
);
CREATE TABLE IF NOT EXISTS large_file_publications(
 endpoint_id TEXT NOT NULL, epoch INTEGER NOT NULL, attempt_id TEXT UNIQUE NOT NULL,
 header_sha256 TEXT NOT NULL, committed_at REAL NOT NULL,
 PRIMARY KEY(endpoint_id,epoch)
);
"""
FIELDS = ("id spec grant_snapshot source_identity request_sha256 intent_sha256 intent_auto header "
          "content_sha256 content_approved content_auto phase epoch created receipt verified_blocks verified_bytes").split()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("ascii")).hexdigest()


def _tx(db):
    if not db.in_transaction:
        raise ValueError("Use the caller's BEGIN IMMEDIATE transaction.")


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use canonical transfer, node, endpoint and job UUIDs.")
    return value


def _digest(value):
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ValueError("Use a lowercase SHA-256 digest.")
    return value


def _integer(value, maximum=2**63 - 1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid transfer quantity.")
    return value


def _now(value=None):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid transfer timestamp.")
    return value


def endpoint_id(transfer_id, authority_node_id, role):
    if role not in {"export", "import"}:
        raise ValueError("Unknown transfer role.")
    return str(uuid5(UUID(_uuid(transfer_id)), "large-file:" + _uuid(authority_node_id) + ":" + role))


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY) or type(value["enabled"]) is not bool:
        raise ValueError("Invalid large-file operator policy.")
    return {"enabled": value["enabled"],
            "max_file_bytes": _integer(value["max_file_bytes"], MAX_FILE_BYTES, 1),
            "max_reserved_bytes": _integer(value["max_reserved_bytes"], minimum=1),
            "max_active_endpoints": _integer(value["max_active_endpoints"], 16, 1),
            "max_pending_endpoints": _integer(value["max_pending_endpoints"], 128, 1),
            "margin_bytes": _integer(value["margin_bytes"])}


def policy(db):
    _tx(db)
    row = db.execute("SELECT document FROM large_file_policy WHERE id=1").fetchone()
    return validate_policy(json.loads(row[0]) if row else DEFAULT_POLICY)


def set_policy(db, document, expected_revision=None):
    previous = policy(db)
    if expected_revision is not None and expected_revision != digest(previous):
        raise ValueError("Large-file policy changed; inspect it before replacement.")
    checked = validate_policy(document)
    db.execute("INSERT OR REPLACE INTO large_file_policy VALUES(1,?)", (canonical(checked),))
    return {"policy": checked, "revision": digest(checked)}


def _access(db, proposed=None):
    _tx(db)
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='policy'").fetchone()
    access = db.execute("SELECT mode FROM policy").fetchone()[0] if exists else proposed or "ask"
    if access not in {"ask", "full"} or proposed is not None and proposed != access:
        raise ValueError("Use the destination's current ask/full policy.")
    return access


def worker_seen(db, generation, now=None):
    now = _now(now)
    worker = resource_policy.status(db, now)["worker"]
    if worker is None or not worker["fresh"] or worker["generation"] != _uuid(generation):
        raise ValueError("Only the current live file-aware worker can attest this feature.")
    db.execute("INSERT OR REPLACE INTO large_file_workers VALUES(?,?)", (generation, now))


def info(db, now=None):
    now = _now(now)
    snapshot = resource_policy.status(db, now)
    worker, remaining = snapshot["worker"], 0.0
    if worker is not None and worker["fresh"]:
        seen = db.execute("SELECT seen FROM large_file_workers WHERE generation=?", (worker["generation"],)).fetchone()
        if seen is not None and 0 <= now - seen[0] < resource_policy.FRESH_SECONDS:
            remaining = min(worker["fresh_remaining_seconds"], resource_policy.FRESH_SECONDS - (now - seen[0]))
    value = policy(db)
    return {"version": VERSION, "state_id": snapshot["state_id"], "policy": value,
            "policy_revision": digest(value), "block_size": BLOCK_SIZE,
            "file_worker_fresh": remaining > 0, "file_worker_remaining_seconds": remaining}


def _endpoint(value):
    if not isinstance(value, dict) or set(value) != {"node_id", "root", "path"}:
        raise ValueError("Invalid transfer endpoint.")
    _uuid(value["node_id"])
    if not isinstance(value["root"], str) or ALIAS.fullmatch(value["root"]) is None:
        raise ValueError("Invalid folder alias.")
    path = value["path"]
    if not isinstance(path, str) or not 1 <= len(path.encode("utf-8")) <= 240:
        raise ValueError("Supply a bounded relative transfer path.")
    for part in path.split("/"):
        if part in {"", ".", ".."} or part.endswith((" ", ".")) or any(c in '\\:<>|?*"' or ord(c) < 32 for c in part):
            raise ValueError("Invalid transfer path component.")
    return dict(value)


def validate_spec(value):
    fields = {"transfer_id", "authority_node_id", "source", "destination", "role", "expected_state_id",
              "max_bytes", "expires_at", "source_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid large-file request fields.")
    result = dict(value, source=_endpoint(value["source"]), destination=_endpoint(value["destination"]))
    endpoint_id(value["transfer_id"], value["authority_node_id"], value["role"])
    _uuid(value["expected_state_id"]);_integer(value["max_bytes"], MAX_FILE_BYTES, 1);_now(value["expires_at"])
    if result["source"]["node_id"] == result["destination"]["node_id"]:
        raise ValueError("Choose different source and destination nodes.")
    if value["role"] == "export":
        if value["source_sha256"] is not None:
            raise ValueError("An export does not bind another source request.")
    else:
        _digest(value["source_sha256"])
    return result


def _grant(value):
    if (not isinstance(value, dict) or set(value) != {"directory", "writable"}
            or type(value["writable"]) is not bool or not isinstance(value["directory"], str)
            or not 1 <= len(value["directory"]) <= 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value["directory"])
            or not (PurePosixPath(value["directory"]).is_absolute() or PureWindowsPath(value["directory"]).is_absolute())):
        raise ValueError("Invalid private folder grant snapshot.")
    return dict(value)


def _identity(value):
    if not isinstance(value, dict) or set(value) != {"dev", "ino", "size", "mtime_ns", "ctime_ns"}:
        raise ValueError("Invalid private source identity snapshot.")
    for name in ("dev", "ino"):_integer(value[name], 2**128 - 1)
    _integer(value["size"], MAX_FILE_BYTES)
    for name in ("mtime_ns", "ctime_ns"):_integer(value[name], minimum=-(2**63))
    return dict(value)


def _row(db, key):
    _tx(db)
    row = db.execute("SELECT " + ",".join(FIELDS) + " FROM large_file_endpoints WHERE id=?", (_uuid(key),)).fetchone()
    if row is None:raise ValueError("Unknown large-file endpoint.")
    result = dict(zip(FIELDS, tuple(row)))
    for name in ("spec", "grant_snapshot", "source_identity", "header", "receipt"):
        result[name] = json.loads(result[name]) if result[name] is not None else None
    return result


def details(db, key):
    """Owner-internal snapshot; absolute grant and file identities are private."""
    return _row(db, key)


def _approval(row, access):
    if row["intent_sha256"] != row["request_sha256"] or access == "ask" and row["intent_auto"]:
        return "intent", row["request_sha256"]
    if row["spec"]["role"] == "import" and row["header"] is not None and (
            row["content_approved"] != row["content_sha256"] or access == "ask" and row["content_auto"]):
        return "content", row["content_sha256"]
    return None, None


def status(db, key, now=None):
    row, now = _row(db, key), _now(now)
    stage, approval = _approval(row, _access(db))
    phase = row["phase"]
    if phase in TERMINAL:
        stage, approval = None, None
    if phase not in TERMINAL | {"stopping", "stopped"} and stage is not None:
        phase = "pending" if stage == "intent" else "content_pending"
    claim = db.execute("SELECT bytes FROM large_file_claims WHERE endpoint_id=? AND released IS NULL", (key,)).fetchone()
    publication = db.execute("SELECT MAX(epoch) FROM large_file_publications WHERE endpoint_id=?", (key,)).fetchone()[0]
    receipt = None if row["receipt"] is None else {k: v for k, v in row["receipt"].items() if k != "target_identity"}
    return {"id": key, "version": VERSION, "spec": row["spec"], "state": phase, "epoch": row["epoch"],
            "request_sha256": row["request_sha256"], "approval_stage": stage, "approval_sha256": approval,
            "expired": now >= row["spec"]["expires_at"], "header": row["header"], "receipt": receipt,
            "publication_committed": publication is not None, "publication_epoch": publication,
            "reserved_bytes": claim[0] if claim else 0, "verified_blocks": row["verified_blocks"],
            "verified_bytes": row["verified_bytes"]}


def pending(db):
    _tx(db)
    keys = db.execute("SELECT id FROM large_file_endpoints WHERE phase NOT IN ('denied','revoked','cleaned','published') ORDER BY created,id").fetchall()
    return [value for row in keys if (value := status(db, row[0]))["approval_stage"] is not None]


def request(db, spec, grant, source_identity, volumes, mode, now=None):
    _tx(db)
    spec, grant, now, access = validate_spec(spec), _grant(grant), _now(now), _access(db, mode)
    key = endpoint_id(spec["transfer_id"], spec["authority_node_id"], spec["role"])
    identity = _identity(source_identity) if spec["role"] == "export" else None
    if spec["role"] == "import" and (source_identity is not None or not grant["writable"]):
        raise ValueError("An import needs a writable folder and no source identity.")
    if identity is not None and identity["size"] > spec["max_bytes"]:
        raise ValueError("The source exceeds the requested maximum.")
    approved = digest({"spec": spec, "grant": grant, "source_identity": identity})
    existing = db.execute("SELECT request_sha256 FROM large_file_endpoints WHERE id=?", (key,)).fetchone()
    if existing:
        if existing[0] != approved:raise ValueError("Endpoint UUID already binds a different request, grant or source identity.")
        return status(db, key, now)
    settings = policy(db)
    if not settings["enabled"] or spec["max_bytes"] > settings["max_file_bytes"]:
        raise ValueError("Large-file transfers require an explicit compatible local policy.")
    if spec["expected_state_id"] != resource_policy.state_id(db) or now >= spec["expires_at"]:
        raise ValueError("Transfer state identity changed or the request expired.")
    count = db.execute("SELECT COUNT(*) FROM large_file_endpoints e WHERE phase NOT IN ('denied','revoked','cleaned','published') "
                       "AND NOT EXISTS(SELECT 1 FROM large_file_claims c WHERE c.endpoint_id=e.id AND c.released IS NULL)").fetchone()[0]
    if count >= settings["max_pending_endpoints"]:raise ValueError("Too many unreserved endpoint requests.")
    db.execute("INSERT INTO large_file_endpoints VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,0,?,1,?,NULL,0,0)",
               (key, canonical(spec), canonical(grant), None if identity is None else canonical(identity), approved,
                approved if access == "full" else None, int(access == "full"), "approved", now))
    return status(db, key, now)


def guard(db, key, epoch, grant=None, source_identity=None, now=None, *, require_reserved=True):
    row, now = _row(db, key), _now(now)
    if type(epoch) is not int or epoch != row["epoch"]:raise ValueError("Stale endpoint epoch.")
    if row["spec"]["expected_state_id"] != resource_policy.state_id(db):raise ValueError("Destination state history changed.")
    if row["phase"] in TERMINAL | {"stopping", "stopped"}:raise ValueError("Endpoint I/O is stopped or terminal.")
    if not policy(db)["enabled"] or now >= row["spec"]["expires_at"]:raise ValueError("Transfer disabled or expired; reservations remain held.")
    if _approval(row, _access(db))[0] is not None:raise ValueError("The endpoint requires exact local approval.")
    if require_reserved and not db.execute("SELECT 1 FROM large_file_claims WHERE endpoint_id=? AND released IS NULL", (key,)).fetchone():
        raise ValueError("Reserve endpoint disk before I/O.")
    if grant is not None and _grant(grant) != row["grant_snapshot"]:raise ValueError("The folder grant changed.")
    if source_identity is not None and _identity(source_identity) != row["source_identity"]:raise ValueError("The source identity changed.")
    return row


def decide(db, key, allowed, expected_sha256=None, now=None):
    row = _row(db, key)
    if type(allowed) is not bool:raise ValueError("Approval decision must be a boolean.")
    if row["phase"] in TERMINAL | {"stopping", "stopped"}:raise ValueError("Endpoint is not awaiting approval.")
    stage, expected = _approval(row, _access(db))
    if stage is None:raise ValueError("Endpoint is not awaiting approval.")
    if not allowed:
        db.execute("UPDATE large_file_endpoints SET phase='denied',epoch=epoch+1 WHERE id=?", (key,))
    else:
        if expected_sha256 != expected:raise ValueError("Approve the exact current request/content digest.")
        if _now(now) >= row["spec"]["expires_at"] or not policy(db)["enabled"]:raise ValueError("Transfer disabled or expired.")
        if stage == "intent":db.execute("UPDATE large_file_endpoints SET intent_sha256=?,intent_auto=0 WHERE id=?", (expected, key))
        else:db.execute("UPDATE large_file_endpoints SET content_approved=?,content_auto=0 WHERE id=?", (expected, key))
    return status(db, key, now)


def _volumes(volumes, role, budget):
    roles = {"spool"} if role == "export" else {"spool", "target"}
    if not isinstance(volumes, list) or len(volumes) != len(roles):raise ValueError("Supply fresh spool/target volume observations.")
    result, seen = [], set()
    for item in volumes:
        if not isinstance(item, dict) or set(item) != {"role", "id", "free_bytes"} or item["role"] not in roles or item["role"] in seen:
            raise ValueError("Invalid volume observation roles.")
        identity = item["id"]
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256 or not identity.isprintable():raise ValueError("Use a stable volume identity.")
        _integer(item["free_bytes"]);seen.add(item["role"])
        result.append({"role": item["role"], "id": identity, "bytes": budget, "free_bytes": item["free_bytes"]})
    return sorted(result, key=lambda item: item["role"])


def reserve(db, key, epoch, volumes, now=None):
    row = guard(db, key, epoch, now=now, require_reserved=False)
    rows = _volumes(volumes, row["spec"]["role"], row["spec"]["max_bytes"])
    stored = [{k: v for k, v in item.items() if k != "free_bytes"} for item in rows]
    existing = db.execute("SELECT volumes,released FROM large_file_claims WHERE endpoint_id=?", (key,)).fetchone()
    if existing:
        if json.loads(existing[0]) != stored or existing[1] is not None:raise ValueError("Reservation identity changed or was already cleaned.")
        return {"reserved": True, "reason": None}
    if not info(db, now)["file_worker_fresh"]:return {"reserved": False, "reason": "file_worker_unavailable"}
    settings = policy(db)
    held = db.execute("SELECT bytes,volumes FROM large_file_claims WHERE released IS NULL").fetchall()
    total = sum(item["bytes"] for item in stored)
    if len(held) >= settings["max_active_endpoints"]:return {"reserved": False, "reason": "endpoint_limit"}
    if total + sum(item[0] for item in held) > settings["max_reserved_bytes"]:return {"reserved": False, "reason": "disk_budget"}
    used, fresh = {}, {}
    for item in rows:fresh[item["id"]] = min(fresh.get(item["id"], item["free_bytes"]), item["free_bytes"])
    for _, document in held:
        for item in json.loads(document):used[item["id"]] = used.get(item["id"], 0) + item["bytes"]
    for item in stored:used[item["id"]] = used.get(item["id"], 0) + item["bytes"]
    if any(used[identity] + settings["margin_bytes"] > free for identity, free in fresh.items()):
        return {"reserved": False, "reason": "volume_space"}
    db.execute("INSERT INTO large_file_claims VALUES(?,?,?,?,NULL)", (key, total, canonical(stored), _now(now)))
    return {"reserved": True, "reason": None}


def begin_attempt(db, key, epoch, job_id, kind, now=None):
    row = guard(db, key, epoch, now=now);_uuid(job_id)
    required = "prepare" if row["spec"]["role"] == "export" else "commit"
    if kind != required:raise ValueError("Wrong supervised endpoint job kind.")
    old = db.execute("SELECT endpoint_id,epoch,kind FROM large_file_attempts WHERE id=?", (job_id,)).fetchone()
    if old:
        if tuple(old) != (key, epoch, kind):raise ValueError("Attempt UUID already belongs to another endpoint operation.")
        return job_id
    if not info(db, now)["file_worker_fresh"]:raise ValueError("No current file-aware worker is attested.")
    if not db.execute("SELECT 1 FROM large_file_claims WHERE endpoint_id=? AND released IS NULL", (key,)).fetchone():raise ValueError("Reserve disk before starting I/O.")
    if db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)).fetchone():raise ValueError("An earlier endpoint job has not confirmed cleanup.")
    if kind == "commit" and row["header"] is None:raise ValueError("Bind and approve content before publication.")
    db.execute("INSERT INTO large_file_attempts VALUES(?,?,?,?,?,NULL,0,NULL)", (job_id, key, epoch, kind, _now(now)))
    db.execute("UPDATE large_file_endpoints SET phase=? WHERE id=?", ("preparing" if kind == "prepare" else "publishing", key))
    return job_id


def _attempt(db, key, epoch, attempt_id, kind, *, allow_finished=False):
    row = db.execute("SELECT endpoint_id,epoch,kind,finished FROM large_file_attempts WHERE id=?", (_uuid(attempt_id),)).fetchone()
    if row is None or tuple(row[:3]) != (key, epoch, kind) or row[3] is not None and not allow_finished:raise ValueError("Stale or completed endpoint attempt.")


def _block_size(row, index):
    size = row["source_identity"]["size"] if row["spec"]["role"] == "export" else (row["header"] or {}).get("size")
    _integer(index)
    if size is None or index >= (size + BLOCK_SIZE - 1) // BLOCK_SIZE:raise ValueError("Invalid block index.")
    return min(BLOCK_SIZE, size - index * BLOCK_SIZE)


def get_block(db, key, index):
    _row(db, key);_integer(index)
    row = db.execute("SELECT block_index,size,sha256 FROM large_file_blocks WHERE endpoint_id=? AND block_index=?", (key, index)).fetchone()
    return dict(zip(("index", "size", "sha256"), tuple(row))) if row else None


def block_page(db, key, offset=0, limit=PAGE_LIMIT):
    _row(db, key);_integer(offset);_integer(limit, PAGE_LIMIT, 1)
    rows = db.execute("SELECT block_index,size,sha256 FROM large_file_blocks WHERE endpoint_id=? AND block_index>=? ORDER BY block_index LIMIT ?", (key, offset, limit + 1)).fetchall()
    return {"blocks": [dict(zip(("index", "size", "sha256"), tuple(row))) for row in rows[:limit]],
            "next_offset": rows[limit][0] if len(rows) > limit else None}


def checkpoint(db, key, epoch, attempt_id, index, size, sha256):
    row = guard(db, key, epoch)
    if row["spec"]["role"] == "export":_attempt(db, key, epoch, attempt_id, "prepare")
    elif attempt_id is not None:_attempt(db, key, epoch, attempt_id, "commit")
    if size != _block_size(row, index) or type(size) is not int:raise ValueError("Block size differs from the immutable file size.")
    _digest(sha256)
    old = get_block(db, key, index)
    if old is not None:
        if old != {"index": index, "size": size, "sha256": sha256}:raise ValueError("Verified block metadata changed.")
        return old
    db.execute("INSERT INTO large_file_blocks VALUES(?,?,?,?)", (key, index, size, sha256))
    db.execute("UPDATE large_file_endpoints SET verified_blocks=verified_blocks+1,verified_bytes=verified_bytes+? WHERE id=?", (size, key))
    return {"index": index, "size": size, "sha256": sha256}


def _header(value):
    fields = {"version", "export_id", "request_sha256", "size", "sha256", "block_size", "block_count", "blocks_sha256"}
    if not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Invalid bounded content header.")
    _uuid(value["export_id"]);_digest(value["request_sha256"]);_digest(value["sha256"]);_digest(value["blocks_sha256"])
    _integer(value["size"], MAX_FILE_BYTES);_integer(value["block_count"], MAX_FILE_BYTES // BLOCK_SIZE)
    if type(value["block_size"]) is not int or value["block_size"] != BLOCK_SIZE or value["block_count"] != (value["size"] + BLOCK_SIZE - 1) // BLOCK_SIZE:
        raise ValueError("Content block geometry is not protocol 2.")
    if not value["size"] and (value["sha256"] != sha256(b"").hexdigest() or value["blocks_sha256"] != sha256(b"").hexdigest()):
        raise ValueError("Empty content needs the empty file/block digest.")
    return dict(value)


def _complete(db, key, header):
    row = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0),MIN(block_index),MAX(block_index) FROM large_file_blocks WHERE endpoint_id=?", (key,)).fetchone()
    if row[0] != header["block_count"] or row[1] != header["size"] or row[0] and (row[2] != 0 or row[3] != row[0] - 1):
        raise ValueError("The verified block ledger is incomplete.")


def source_ready(db, key, epoch, attempt_id, header):
    row = guard(db, key, epoch);header = _header(header)
    if row["spec"]["role"] != "export":raise ValueError("Only an export can publish a source header.")
    if (header["export_id"] != key or header["request_sha256"] != row["request_sha256"]
            or header["size"] != row["source_identity"]["size"]):raise ValueError("Content header differs from its approved export request.")
    if row["header"] is not None:
        if row["header"] != header:raise ValueError("The immutable source header changed.")
        return header
    _attempt(db, key, epoch, attempt_id, "prepare")
    _complete(db, key, header)
    db.execute("UPDATE large_file_endpoints SET header=?,phase='ready' WHERE id=?", (canonical(header), key))
    return header


def bind_manifest(db, key, header, mode, now=None):
    row = _row(db, key);header = _header(header);access = _access(db, mode)
    if row["spec"]["role"] != "import" or header["export_id"] != endpoint_id(row["spec"]["transfer_id"], row["spec"]["authority_node_id"], "export") or header["request_sha256"] != row["spec"]["source_sha256"] or header["size"] > row["spec"]["max_bytes"]:
        raise ValueError("Content header does not bind the import's source request.")
    if row["header"] is not None:
        if row["header"] != header:raise ValueError("Import UUID already binds different content.")
        return status(db, key, now)
    guard(db, key, row["epoch"], now=now)
    content = digest({"request_sha256": row["request_sha256"], "header": header})
    db.execute("UPDATE large_file_endpoints SET header=?,content_sha256=?,content_approved=?,content_auto=?,phase='receiving' WHERE id=?",
               (canonical(header), content, content if access == "full" else None, int(access == "full"), key))
    return status(db, key, now)


def _publication(db, key, epoch, attempt_id, row):
    if type(epoch) is not int or epoch < 1:
        raise ValueError("Invalid publication epoch.")
    _attempt(db, key, epoch, attempt_id, "commit", allow_finished=True)
    permit = db.execute("SELECT attempt_id,header_sha256 FROM large_file_publications WHERE endpoint_id=? AND epoch=?", (key, epoch)).fetchone()
    if permit is None or permit[0] != attempt_id or row["header"] is None or permit[1] != digest(row["header"]):
        raise ValueError("Publication requires its exact durable attempt/header permit.")


def commit_publication(db, key, epoch, attempt_id):
    """Linearize authorization before the non-SQL hard link; retain every permit.

    Stop/revoke after the barrier fences further I/O but cannot promise the final
    link is absent. The wrapper compares the current grant in this same short
    transaction. A permit never releases disk or authorizes a different job.
    """
    row = _row(db, key)
    existing = db.execute("SELECT 1 FROM large_file_publications WHERE endpoint_id=? AND epoch=?", (key, epoch)).fetchone()
    if existing:
        _publication(db, key, epoch, attempt_id, row)
        return status(db, key)
    row = guard(db, key, epoch)
    _attempt(db, key, epoch, attempt_id, "commit")
    if row["spec"]["role"] != "import" or row["header"] is None:
        raise ValueError("Only approved import content can cross the publication barrier.")
    _complete(db, key, row["header"])
    db.execute("INSERT INTO large_file_publications VALUES(?,?,?,?,?)",
               (key, epoch, attempt_id, digest(row["header"]), _now()))
    return status(db, key)


def published(db, key, epoch, attempt_id, receipt):
    row = _row(db, key)
    if row["spec"]["expected_state_id"] != resource_policy.state_id(db):
        raise ValueError("Publication state history changed.")
    _publication(db, key, epoch, attempt_id, row)
    if row["receipt"] is not None:
        if row["receipt"] != receipt:raise ValueError("The immutable publication receipt changed.")
        return status(db, key)
    if row["phase"] == "cleaned":
        raise ValueError("Reconcile publication before declaring endpoint cleanup complete.")
    if not isinstance(receipt, dict) or row["header"] is None or set(receipt) != set(row["header"]) | {"import_id", "published", "target_identity"}:
        raise ValueError("Invalid private publication receipt.")
    if receipt["import_id"] != key or receipt["published"] is not True or {k: receipt[k] for k in row["header"]} != row["header"]:
        raise ValueError("Publication does not match approved content.")
    identity = receipt["target_identity"]
    if not isinstance(identity, dict) or set(identity) != {"dev", "ino"}:raise ValueError("Missing stable target identity.")
    for value in identity.values():_integer(value, 2**128 - 1)
    _complete(db, key, row["header"])
    db.execute("UPDATE large_file_endpoints SET phase='published',receipt=? WHERE id=?", (canonical(receipt), key))
    return status(db, key)


def finish_attempt(db, attempt_id, cleanup_confirmed, outcome):
    _tx(db);_uuid(attempt_id)
    if type(cleanup_confirmed) is not bool or outcome not in {"completed", "failed", "stopped", "unconfirmed"}:raise ValueError("Invalid trusted endpoint job outcome.")
    row = db.execute("SELECT endpoint_id,kind,finished,cleanup_confirmed,outcome FROM large_file_attempts WHERE id=?", (attempt_id,)).fetchone()
    if row is None:raise ValueError("Unknown supervised endpoint job.")
    if row[2] is not None:
        if row[4] != outcome or bool(row[3]) != cleanup_confirmed:raise ValueError("An endpoint attempt outcome cannot change; reconcile verified cleanup separately.")
        return status(db, row[0])
    endpoint = _row(db, row[0])
    if outcome == "completed" and ((row[1] == "prepare" and endpoint["header"] is None) or (row[1] == "commit" and endpoint["receipt"] is None)):
        raise ValueError("A completed file job needs its durable content/publication receipt.")
    db.execute("UPDATE large_file_attempts SET finished=?,cleanup_confirmed=?,outcome=? WHERE id=?", (_now(), int(cleanup_confirmed), outcome, attempt_id))
    if endpoint["phase"] not in TERMINAL | {"ready", "receiving"}:
        db.execute("UPDATE large_file_endpoints SET phase=? WHERE id=?", ("stopped" if cleanup_confirmed else "stopping", row[0]))
    return status(db, row[0])


def reconcile_attempt_cleanup(db, attempt_id):
    """Trusted owner only: call after verifying the scoped resource cleanup receipt.

    This records eventual process cleanup without changing an uncertain outcome,
    endpoint epoch, publication receipt, or disk reservation. The SQL ledger cannot
    itself verify guardian nonces/handles; the local Core wrapper must do that.
    """
    _tx(db)
    row = db.execute("SELECT endpoint_id,finished FROM large_file_attempts WHERE id=?", (_uuid(attempt_id),)).fetchone()
    if row is None or row[1] is None:
        raise ValueError("Reconcile only a recorded endpoint attempt outcome.")
    db.execute("UPDATE large_file_attempts SET cleanup_confirmed=1 WHERE id=?", (attempt_id,))
    if not db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (row[0],)).fetchone():
        db.execute("UPDATE large_file_endpoints SET phase='stopped' WHERE id=? AND phase='stopping'", (row[0],))
    return status(db, row[0])


def stop(db, key, now=None):
    row = _row(db, key)
    if row["phase"] not in TERMINAL | {"stopping", "stopped"}:
        active = db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)).fetchone()
        db.execute("UPDATE large_file_endpoints SET epoch=epoch+1,phase=? WHERE id=?", ("stopping" if active else "stopped", key))
    return status(db, key, now)


def resume(db, key, expected_epoch, mode, now=None):
    row = _row(db, key);_access(db, mode)
    if type(expected_epoch) is not int or expected_epoch != row["epoch"] or row["phase"] != "stopped":raise ValueError("Resume requires the exact stopped endpoint epoch.")
    if db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)).fetchone():raise ValueError("Previous endpoint process cleanup remains unconfirmed.")
    if not policy(db)["enabled"] or _now(now) >= row["spec"]["expires_at"]:raise ValueError("Transfer disabled or expired.")
    if row["spec"]["expected_state_id"] != resource_policy.state_id(db):raise ValueError("Transfer state history changed.")
    phase = "ready" if row["spec"]["role"] == "export" and row["header"] is not None else "receiving" if row["header"] is not None else "approved"
    db.execute("UPDATE large_file_endpoints SET epoch=epoch+1,phase=? WHERE id=?", (phase, key))
    return status(db, key, now)


def revoke_root(db, alias, now=None):
    _tx(db)
    keys = db.execute("SELECT id FROM large_file_endpoints WHERE phase NOT IN ('published','cleaned','denied','revoked')").fetchall()
    jobs = []
    for key, in keys:
        row = _row(db, key)
        local = row["spec"]["source"] if row["spec"]["role"] == "export" else row["spec"]["destination"]
        if local["root"] != alias:continue
        db.execute("UPDATE large_file_endpoints SET phase='revoked',epoch=epoch+1,intent_sha256=NULL,content_approved=NULL WHERE id=?", (key,))
        jobs.extend(r[0] for r in db.execute("SELECT id FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)))
    return jobs


def cleanup(db, key, epoch, cleanup_confirmed, physical_deleted, now=None):
    row = _row(db, key)
    if type(epoch) is not int or epoch != row["epoch"]:raise ValueError("Stale cleanup epoch.")
    if type(cleanup_confirmed) is not bool or type(physical_deleted) is not bool:raise ValueError("Cleanup requires explicit trusted booleans.")
    if not cleanup_confirmed or not physical_deleted:raise ValueError("Disk reservations remain held until processes and physical deletion are confirmed.")
    if db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)).fetchone():raise ValueError("An endpoint job still owns possible I/O.")
    db.execute("UPDATE large_file_claims SET released=? WHERE endpoint_id=? AND released IS NULL", (_now(now), key))
    db.execute("UPDATE large_file_endpoints SET phase='cleaned',epoch=epoch+1 WHERE id=? AND phase!='cleaned'", (key,))
    return status(db, key, now)
