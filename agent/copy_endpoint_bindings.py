# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cooperative endpoint ownership, not credentials or same-account isolation.

All APIs use the caller's BEGIN IMMEDIATE. This module performs no filesystem,
process, credential or network I/O. The LOCAL wrapper must hold the endpoint's
existing _owned lock for bind/resume and every I/O, check filesystem grants
outside SQL, and repeat authorization inside the short metadata transaction.
Queued jobs must carry their original token/revision and recheck before launch,
I/O, checkpoints and publication. A lock or elapsed time is not cleanup proof:
bind/control also inspect the durable endpoint attempt ledger. Worker attestation
is an internal LOCAL call, never a wire assertion. No API unbinds or frees quota.
"""
from hashlib import sha256
import json
import math
import time
from uuid import UUID, uuid5

import large_transfer_plans as plans
import resource_policy
import transfer_state as files

VERSION = 1
DEFAULT_POLICY = {"enabled": False, "max_pending_bindings": 64}
SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_binding_policy(
 id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copy_binding_intents(
 endpoint_id TEXT NOT NULL, intent_sha256 TEXT NOT NULL, document TEXT NOT NULL,
 decision INTEGER, automatic INTEGER NOT NULL, bound_revision INTEGER, created REAL NOT NULL,
 PRIMARY KEY(endpoint_id,intent_sha256)
);
CREATE TABLE IF NOT EXISTS copy_endpoint_bindings(
 endpoint_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, intent_sha256 TEXT NOT NULL,
 state_id TEXT NOT NULL, endpoint_epoch INTEGER NOT NULL, binding TEXT NOT NULL, phase TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copy_binding_events(
 endpoint_id TEXT NOT NULL, revision INTEGER NOT NULL, event TEXT NOT NULL,
 intent_sha256 TEXT NOT NULL, endpoint_epoch INTEGER NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(endpoint_id,revision)
);
CREATE TABLE IF NOT EXISTS copy_binding_workers(
 generation TEXT PRIMARY KEY, state_id TEXT NOT NULL, seen REAL NOT NULL
);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("ascii")).hexdigest()


def _tx(db):
    if not db.in_transaction:
        raise ValueError("Use the caller's BEGIN IMMEDIATE transaction.")


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Use a bounded integer, not a boolean.")
    return value


def _clock(now):
    now = time.time() if now is None else now
    if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
        raise ValueError("Use a finite nonnegative timestamp.")
    return now


def _mode(db):
    _tx(db)
    rows = db.execute("SELECT mode FROM policy").fetchall()
    if len(rows) != 1 or rows[0][0] not in {"ask", "full"}:
        raise ValueError("Use the current local ask/full policy.")
    return rows[0][0]


def _settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY) or type(value["enabled"]) is not bool:
        raise ValueError("Invalid receiving endpoint binding policy.")
    return {"enabled": value["enabled"], "max_pending_bindings": _integer(value["max_pending_bindings"], 1, 128)}


def policy(db):
    _tx(db)
    row = db.execute("SELECT document FROM copy_binding_policy WHERE id=1").fetchone()
    return _settings(json.loads(row[0]) if row else DEFAULT_POLICY)


def set_policy(db, document, expected_revision=None):
    old = policy(db)
    if expected_revision is not None and expected_revision != digest(old):
        raise ValueError("Endpoint binding policy changed.")
    checked = _settings(document)
    db.execute("INSERT OR REPLACE INTO copy_binding_policy VALUES(1,?)", (canonical(checked),))
    return {"policy": checked, "revision": digest(checked)}


def worker_seen(db, generation, *, expected_state_id, now=None):
    """LOCAL worker feature attestation; existing helper files cannot attest it."""
    _tx(db)
    now = _clock(now)
    snapshot = resource_policy.status(db, now)
    worker = snapshot["worker"]
    if (plans.identity(expected_state_id) != snapshot["state_id"] or worker is None
            or plans.identity(generation) != worker["generation"] or not worker["fresh"]):
        raise ValueError("Attest only the current fresh worker in this endpoint history.")
    db.execute("INSERT OR REPLACE INTO copy_binding_workers VALUES(?,?,?)", (generation, expected_state_id, now))


def info(db, now=None):
    _tx(db)
    now = _clock(now)
    snapshot = resource_policy.status(db, now)
    worker, remaining = snapshot["worker"], 0.0
    if worker is not None and worker["fresh"]:
        seen = db.execute("SELECT state_id,seen FROM copy_binding_workers WHERE generation=?", (worker["generation"],)).fetchone()
        if seen and seen[0] == snapshot["state_id"] and 0 <= now - seen[1] < resource_policy.FRESH_SECONDS:
            remaining = min(worker["fresh_remaining_seconds"], resource_policy.FRESH_SECONDS - (now - seen[1]))
    settings = policy(db)
    return {"version": VERSION, "state_id": snapshot["state_id"], "policy": settings,
            "policy_revision": digest(settings), "worker": {"generation": worker["generation"] if worker else None,
            "fresh": remaining > 0, "fresh_remaining_seconds": remaining}, "runtime_implemented": False}


def _record(db, key):
    _tx(db)
    row = db.execute("SELECT revision,intent_sha256,state_id,endpoint_epoch,binding,phase "
                     "FROM copy_endpoint_bindings WHERE endpoint_id=?", (plans.identity(key),)).fetchone()
    if row is None:
        return None
    return dict(zip(("revision", "intent_sha256", "state_id", "endpoint_epoch", "binding", "phase"),
                    (*row[:4], json.loads(row[4]), row[5])))


def _revision(record):
    return record["revision"] if record else 0


def _cas(record, expected):
    if _integer(expected) != _revision(record):
        raise ValueError("Endpoint binding revision changed.")


def _intent_document(raw, expected):
    document = json.loads(raw)
    if not isinstance(document, dict) or digest(document) != expected:
        raise ValueError("Endpoint binding intent digest differs.")
    return document


def _intent(db, key, expected):
    row = db.execute("SELECT document,decision,automatic,bound_revision FROM copy_binding_intents "
                     "WHERE endpoint_id=? AND intent_sha256=?", (key, expected)).fetchone()
    if row is None:
        raise ValueError("Unknown exact endpoint binding intent.")
    document = _intent_document(row[0], expected)
    return {"document": document, "decision": row[1], "automatic": row[2], "bound_revision": row[3]}


def _has_pending_capacity(db, now, maximum):
    """Only live metadata proposals consume this cap; retain every history row.

    Stream one pinned, bounded manifest at a time, without SQLite JSON extensions.
    Expiry never releases a binding, a process claim or a disk reservation.
    """
    count = 0
    for raw, checked in db.execute("SELECT document,intent_sha256 FROM copy_binding_intents "
                                   "WHERE bound_revision IS NULL AND decision IS NOT 0"):
        document = _intent_document(raw, checked)
        manifest = plans.validate_manifest(document["manifest"])
        if now < manifest["plan"]["expires_at"]:
            count += 1
            if count >= maximum:
                return False
    return True


def _consent(intent, access):
    return intent["decision"] == 1 and not (access == "ask" and intent["automatic"])


def _context(db, manifest, proposal, local_node_id, *, exact_epoch=True):
    manifest = plans.validate_manifest(manifest)
    fields = {"version", "side", "endpoint_id", "endpoint_state_id", "expected_endpoint_epoch", "binding", "proposal_sha256"}
    if not isinstance(proposal, dict) or set(proposal) != fields:
        raise ValueError("Use the exact controller endpoint proposal.")
    if type(proposal["version"]) is not int or proposal["version"] != VERSION or proposal["side"] not in {"source", "destination"}:
        raise ValueError("Invalid endpoint proposal version or side.")
    side = proposal["side"]
    role = "export" if side == "source" else "import"
    key = manifest["plan"][side + "_id"]
    state_id = manifest["plan"][side + "_state_id"]
    binding = proposal["binding"]
    if not isinstance(binding, dict) or set(binding) != {"controller_id", "controller_epoch", "authority_node_id", "authority_state_id", "manifest_sha256"}:
        raise ValueError("Invalid controller binding fields.")
    controller = str(uuid5(UUID(manifest["id"]), "large-copy:" + manifest["origin"]))
    if (binding["controller_id"] != controller or binding["authority_node_id"] != manifest["origin"]
            or binding["manifest_sha256"] != plans.package(manifest)["sha256"]):
        raise ValueError("Controller identity or immutable manifest differs.")
    plans.identity(binding["authority_state_id"])
    _integer(binding["controller_epoch"], 1)
    _integer(proposal["expected_endpoint_epoch"], 1)
    if (proposal["endpoint_id"] != key or proposal["endpoint_state_id"] != state_id
            or state_id != resource_policy.state_id(db)
            or plans.identity(local_node_id) != manifest["request"][side]["node_id"]
            or proposal["proposal_sha256"] != digest({k: v for k, v in proposal.items() if k != "proposal_sha256"})):
        raise ValueError("Endpoint proposal, local identity or state history differs.")
    endpoint = files.details(db, key)
    spec = files.validate_spec(endpoint["spec"])
    if (canonical(spec) != canonical(plans.endpoint_spec(manifest, role, spec["source_sha256"]))
            or exact_epoch and endpoint["epoch"] != proposal["expected_endpoint_epoch"]):
        raise ValueError("The endpoint request or epoch differs from the controller proposal.")
    request_hash = digest({"spec": spec, "grant": endpoint["grant_snapshot"], "source_identity": endpoint["source_identity"]})
    if request_hash != endpoint["request_sha256"]:
        raise ValueError("The endpoint request/grant/source identity digest differs.")
    return manifest, endpoint


def _pins(record, proposal):
    if record is None:
        return
    for name in ("controller_id", "authority_node_id", "authority_state_id", "manifest_sha256"):
        if record["binding"][name] != proposal["binding"][name]:
            raise ValueError("An endpoint cannot replace its pinned controller, authority history or manifest.")


def _grant(db, endpoint):
    side = "source" if endpoint["spec"]["role"] == "export" else "destination"
    row = db.execute("SELECT directory,writable FROM file_roots WHERE alias=?", (endpoint["spec"][side]["root"],)).fetchone()
    if row is None or canonical({"directory": row[0], "writable": bool(row[1])}) != canonical(endpoint["grant_snapshot"]):
        raise ValueError("The current local folder grant differs.")


def _cleanup(db, key):
    if db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)).fetchone():
        raise ValueError("An earlier endpoint job still owns possible I/O.")


def _authorized(db, key, intent, now, *, control=False):
    document = intent["document"]
    _, endpoint = _context(db, document["manifest"], document["proposal"], document["local_node_id"], exact_epoch=False)
    if endpoint["request_sha256"] != document["request_sha256"]:
        raise ValueError("Endpoint binding intent no longer matches its exact request.")
    _grant(db, endpoint)
    if not policy(db)["enabled"] or not _consent(intent, _mode(db)) or not info(db, now)["worker"]["fresh"]:
        raise ValueError("Binding policy, exact local consent or current feature-aware worker is unavailable.")
    if control:
        if (endpoint["phase"] in files.TERMINAL or not files.policy(db)["enabled"]
                or now >= endpoint["spec"]["expires_at"] or files.status(db, key, now)["approval_stage"] is not None):
            raise ValueError("The endpoint requires current file policy, history and exact approval.")
    else:
        files.guard(db, key, endpoint["epoch"], grant=endpoint["grant_snapshot"], now=now, require_reserved=False)
    return endpoint


def prepare(db, manifest, proposal, *, local_node_id, expected_binding_revision, now=None):
    """Store an immutable human preview; never changes an existing active intent."""
    _tx(db)
    now = _clock(now)
    manifest, endpoint = _context(db, manifest, proposal, local_node_id)
    key, record = proposal["endpoint_id"], _record(db, proposal["endpoint_id"])
    _pins(record, proposal)
    document = {"version": VERSION, "manifest": manifest, "proposal": proposal, "local_node_id": local_node_id,
                "request_sha256": endpoint["request_sha256"], "expected_binding_revision": _integer(expected_binding_revision)}
    checked = digest(document)
    if db.execute("SELECT 1 FROM copy_binding_intents WHERE endpoint_id=? AND intent_sha256=?", (key, checked)).fetchone():
        return status(db, key, checked, now)
    _cas(record, expected_binding_revision)
    if now >= endpoint["spec"]["expires_at"]:
        raise ValueError("Transfer expired.")
    if not _has_pending_capacity(db, now, policy(db)["max_pending_bindings"]):
        raise ValueError("Too many pending endpoint binding intents.")
    automatic = _mode(db) == "full"
    db.execute("INSERT INTO copy_binding_intents VALUES(?,?,?,?,?,NULL,?)",
               (key, checked, canonical(document), 1 if automatic else None, int(automatic), now))
    return status(db, key, checked, now)


def status(db, key, intent_sha256=None, now=None):
    _tx(db)
    now, record = _clock(now), _record(db, key)
    endpoint = files.details(db, key)
    checked = intent_sha256
    if checked is None and record:
        checked = record["intent_sha256"]
    intent = _intent(db, key, checked) if checked is not None else None
    history = endpoint["spec"]["expected_state_id"] == resource_policy.state_id(db) and (record is None or record["state_id"] == resource_policy.state_id(db))
    return {"version": VERSION, "endpoint_id": key, "managed": record is not None,
            "binding_revision": _revision(record), "endpoint_epoch": endpoint["epoch"], "history_matches": history,
            "phase": record["phase"] if record else "unmanaged", "token": record["binding"]["proposal_sha256"] if record else None,
            "active_intent_sha256": record["intent_sha256"] if record else None,
            "intent_sha256": checked, "preview": intent["document"] if intent else None,
            "approval_required": intent is not None and intent["decision"] != 0 and not _consent(intent, _mode(db))
            and history and now < endpoint["spec"]["expires_at"],
            "worker": info(db, now)["worker"], "runtime_implemented": False}


def pending(db, now=None):
    _tx(db)
    return [value for row in db.execute("SELECT endpoint_id,intent_sha256 FROM copy_binding_intents ORDER BY created,intent_sha256").fetchall()
            if (value := status(db, row[0], row[1], now))["approval_required"]]


def decide(db, key, allowed, *, expected_intent_sha256, expected_binding_revision, now=None):
    _tx(db)
    now, record = _clock(now), _record(db, key)
    _cas(record, expected_binding_revision)
    intent = _intent(db, key, expected_intent_sha256)
    if type(allowed) is not bool:
        raise ValueError("An approval must be an explicit boolean.")
    document = intent["document"]
    _context(db, document["manifest"], document["proposal"], document["local_node_id"], exact_epoch=False)
    if now >= document["manifest"]["plan"]["expires_at"]:
        raise ValueError("Transfer expired.")
    db.execute("UPDATE copy_binding_intents SET decision=?,automatic=0 WHERE endpoint_id=? AND intent_sha256=?",
               (int(allowed), key, expected_intent_sha256))
    if not allowed and record is not None and record["intent_sha256"] == expected_intent_sha256:
        return _stop(db, key, "revoked", now)
    return status(db, key, expected_intent_sha256, now)


def _ack(proposal):
    return {"version": VERSION, "side": proposal["side"], "endpoint_id": proposal["endpoint_id"],
            "endpoint_state_id": proposal["endpoint_state_id"], "endpoint_epoch": proposal["expected_endpoint_epoch"],
            "binding": proposal["binding"], "proposal_sha256": proposal["proposal_sha256"], "bound": True}


def _event(db, key, revision, event, checked, epoch, now):
    db.execute("INSERT INTO copy_binding_events VALUES(?,?,?,?,?,?)", (key, revision, event, checked, epoch, now))


def bind(db, key, *, expected_intent_sha256, expected_binding_revision, now=None):
    """LOCAL wrapper holds _owned; SQL attempts must already prove cleanup."""
    _tx(db)
    now, record = _clock(now), _record(db, key)
    intent = _intent(db, key, expected_intent_sha256)
    document, proposal = intent["document"], intent["document"]["proposal"]
    _integer(expected_binding_revision)
    if document["expected_binding_revision"] != expected_binding_revision:
        raise ValueError("Bind only the exact prepared revision.")
    _pins(record, proposal)
    endpoint = _authorized(db, key, intent, now)
    if record is not None and record["intent_sha256"] == expected_intent_sha256 and record["phase"] == "active":
        if (record["revision"] != expected_binding_revision + 1 or record["endpoint_epoch"] != endpoint["epoch"]
                or endpoint["epoch"] != proposal["expected_endpoint_epoch"]):
            raise ValueError("The original binding has since been fenced.")
        return _ack(proposal)
    _cas(record, expected_binding_revision)
    if endpoint["epoch"] != proposal["expected_endpoint_epoch"]:
        raise ValueError("Endpoint epoch changed before binding.")
    if record is not None and (record["phase"] == "active" or endpoint["epoch"] <= record["endpoint_epoch"]
                              or proposal["binding"]["controller_epoch"] <= record["binding"]["controller_epoch"]):
        raise ValueError("Fence/resume the previous endpoint and increment the controller epoch before replacing a binding.")
    _cleanup(db, key)
    revision = _revision(record) + 1
    token_binding = dict(proposal["binding"], proposal_sha256=proposal["proposal_sha256"])
    db.execute("INSERT OR REPLACE INTO copy_endpoint_bindings VALUES(?,?,?,?,?,?, 'active')",
               (key, revision, expected_intent_sha256, proposal["endpoint_state_id"], endpoint["epoch"], canonical(token_binding)))
    db.execute("UPDATE copy_binding_intents SET bound_revision=? WHERE endpoint_id=? AND intent_sha256=?",
               (revision, key, expected_intent_sha256))
    _event(db, key, revision, "bound", expected_intent_sha256, endpoint["epoch"], now)
    return _ack(proposal)


def _fence_record(db, key, endpoint_epoch, token, revision):
    _tx(db)
    record = _record(db, key)
    if record is None:
        if token is not None or revision is not None:
            raise ValueError("An unmanaged endpoint has no binding token.")
        return None
    _cas(record, revision)
    if token != record["binding"]["proposal_sha256"]:
        raise ValueError("A managed endpoint requires its current exact token.")
    endpoint = files.details(db, key)
    actual = resource_policy.state_id(db)
    if record["state_id"] != actual or endpoint["spec"]["expected_state_id"] != actual:
        raise ValueError("The endpoint binding belongs to a different state history.")
    if _integer(endpoint_epoch, 1) != endpoint["epoch"]:
        raise ValueError("Stale current endpoint epoch.")
    return record


def guard_fence(db, key, current_file_epoch, token=None, *, expected_binding_revision=None):
    """Authenticate Stop/cleanup without requiring permission for further I/O.

    Never proves cleanup, changes a binding, or enables data. The LOCAL wrapper
    still holds _owned and verifies process/attempt/physical cleanup evidence.
    """
    record = _fence_record(db, key, current_file_epoch, token, expected_binding_revision)
    return None if record is None else {"managed": True, "binding_revision": record["revision"],
                                        "endpoint_epoch": current_file_epoch}


def _guard(db, key, endpoint_epoch, token, revision, now, *, control):
    record = _fence_record(db, key, endpoint_epoch, token, revision)
    if record is None:
        return None
    intent = _intent(db, key, record["intent_sha256"])
    endpoint = _authorized(db, key, intent, now, control=control)
    if control:
        _cleanup(db, key)
    elif record["phase"] != "active" or record["endpoint_epoch"] != endpoint["epoch"]:
        raise ValueError("The binding is fenced; data I/O cannot resume implicitly.")
    return status(db, key, now=now)


def guard_data(db, key, endpoint_epoch, token=None, *, expected_binding_revision=None, now=None):
    """Call for every data/metadata mutation, including queued job callbacks."""
    return _guard(db, key, endpoint_epoch, token, expected_binding_revision, _clock(now), control=False)


def guard_control(db, key, endpoint_epoch, token=None, *, expected_binding_revision=None, now=None):
    """Authorize controlled resume only; does not reactivate a stopped binding.

    The LOCAL wrapper holds _owned and checks filesystem grants outside SQL.
    Emergency Stop and trusted cleanup do not use this policy-gated operation.
    """
    return _guard(db, key, endpoint_epoch, token, expected_binding_revision, _clock(now), control=True)


def _stop(db, key, reason, now):
    record = _record(db, key)
    endpoint = files.stop(db, key, now)
    if record is not None:
        if reason == "revoked":
            db.execute("UPDATE copy_binding_intents SET decision=0,automatic=0 WHERE endpoint_id=? AND intent_sha256=?",
                       (key, record["intent_sha256"]))
        phase = "revoked" if reason == "revoked" or record["phase"] == "revoked" else "stopped"
        if record["phase"] == "active" or phase != record["phase"]:
            revision = record["revision"] + 1
            db.execute("UPDATE copy_endpoint_bindings SET revision=?,phase=? WHERE endpoint_id=?", (revision, phase, key))
            _event(db, key, revision, reason, record["intent_sha256"], endpoint["epoch"], now)
    value = status(db, key, now=now)
    value["stop_job_ids"] = [row[0] for row in db.execute("SELECT id FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,))]
    return value


def emergency_stop(db, key, now=None):
    """LOCAL owner fence, even with missing policy, routes or stale history."""
    _tx(db)
    return _stop(db, key, "stopped", _clock(now))


def revoke(db, key, *, expected_binding_revision, now=None):
    _tx(db)
    _cas(_record(db, key), expected_binding_revision)
    return _stop(db, key, "revoked", _clock(now))
