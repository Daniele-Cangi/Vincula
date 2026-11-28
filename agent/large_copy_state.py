# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Metadata-only enrollment for a future Core-owned copy controller.

Every DB API requires the caller's BEGIN IMMEDIATE; none commits or performs
filesystem, process, transport or credential I/O. Binding receipts, worker
attestation and cleanup evidence MUST be verified by a trusted LOCAL wrapper,
not exposed as assertions accepted from an MCP/wire caller. This module neither
launches a controller nor reserves/releases disk. Enrollment readiness is not
runtime readiness. Same-account full agents are outside this cooperative fence.
"""
from hashlib import sha256
import json
import math
import time
from uuid import UUID, uuid5

import large_transfer_plans as plans
import resource_policy

VERSION = 1
SIDES = ("source", "destination")
DEFAULT_POLICY = {"enabled": False, "max_pending_enrollments": 64}
SCHEMA = """
CREATE TABLE IF NOT EXISTS large_copy_policy(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS large_copy_controllers(
 id TEXT PRIMARY KEY, definition TEXT NOT NULL, definition_sha256 TEXT NOT NULL,
 epoch INTEGER NOT NULL, intent TEXT NOT NULL, intent_sha256 TEXT NOT NULL,
 approved_sha256 TEXT, automatic INTEGER NOT NULL, phase TEXT NOT NULL,
 worker_generation TEXT, stopped_from_epoch INTEGER, cleanup_epoch INTEGER,
 stop_reason TEXT, runtime_run_id TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS large_copy_outbox(
 controller_id TEXT NOT NULL, epoch INTEGER NOT NULL, side TEXT NOT NULL,
 proposal TEXT NOT NULL, proposal_sha256 TEXT NOT NULL, phase TEXT NOT NULL,
 receipt TEXT, confirmed_at REAL, PRIMARY KEY(controller_id,epoch,side)
);
CREATE TABLE IF NOT EXISTS large_copy_decisions(
 controller_id TEXT NOT NULL, epoch INTEGER NOT NULL, intent_sha256 TEXT NOT NULL,
 allowed INTEGER NOT NULL, decided REAL NOT NULL,
 PRIMARY KEY(controller_id,epoch,intent_sha256)
);
CREATE TABLE IF NOT EXISTS large_copy_worker_attestations(
 generation TEXT PRIMARY KEY, state_id TEXT NOT NULL, seen REAL NOT NULL
);
"""
FIELDS = ("id definition definition_sha256 epoch intent intent_sha256 approved_sha256 automatic phase "
          "worker_generation stopped_from_epoch cleanup_epoch stop_reason runtime_run_id created").split()


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256(canonical(value).encode("ascii")).hexdigest()


def _transaction(db):
    if not db.in_transaction:
        raise ValueError("Use the caller's BEGIN IMMEDIATE transaction.")


def _clock(now=None):
    value = time.time() if now is None else now
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError("Use a finite nonnegative time.")
    return value


def _positive(value, maximum=2**63 - 1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Use a positive bounded integer.")
    return value


def _uuid(value):
    return plans.identity(value)


def _mode(db, expected=None):
    _transaction(db)
    rows = db.execute("SELECT mode FROM policy").fetchall()
    if len(rows) != 1 or rows[0][0] not in {"ask", "full"}:
        raise ValueError("Initialize exactly one local ask/full policy.")
    value = rows[0][0]
    if expected is not None and expected != value:
        raise ValueError("The current local access mode differs.")
    return value


def policy(db):
    _transaction(db)
    row = db.execute("SELECT document FROM large_copy_policy WHERE id=1").fetchone()
    value = json.loads(row[0]) if row else DEFAULT_POLICY
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY) or type(value["enabled"]) is not bool:
        raise ValueError("Invalid metadata controller policy.")
    return {"enabled": value["enabled"], "max_pending_enrollments": _positive(value["max_pending_enrollments"], 128)}


def set_policy(db, document, expected_revision=None):
    previous = policy(db)
    if expected_revision is not None and expected_revision != digest(previous):
        raise ValueError("Controller policy changed; inspect it before replacing it.")
    if not isinstance(document, dict) or set(document) != set(DEFAULT_POLICY) or type(document["enabled"]) is not bool:
        raise ValueError("Invalid metadata controller policy.")
    checked = {"enabled": document["enabled"], "max_pending_enrollments": _positive(document["max_pending_enrollments"], 128)}
    db.execute("INSERT OR REPLACE INTO large_copy_policy VALUES(1,?)", (canonical(checked),))
    return {"policy": checked, "revision": digest(checked)}


def controller_id(transfer_id, authority_node_id):
    return str(uuid5(UUID(_uuid(transfer_id)), "large-copy:" + _uuid(authority_node_id)))


def _epochs(value):
    if not isinstance(value, dict) or set(value) != set(SIDES):
        raise ValueError("Supply both captured endpoint epochs.")
    return {side: _positive(value[side]) for side in SIDES}


def _row(db, key):
    _transaction(db)
    row = db.execute("SELECT " + ",".join(FIELDS) + " FROM large_copy_controllers WHERE id=?", (_uuid(key),)).fetchone()
    if row is None:
        raise ValueError("Unknown controller enrollment.")
    value = dict(zip(FIELDS, tuple(row)))
    value["definition"] = json.loads(value["definition"])
    value["intent"] = json.loads(value["intent"])
    if digest(value["definition"]) != value["definition_sha256"] or digest(value["intent"]) != value["intent_sha256"]:
        raise ValueError("Controller metadata digest differs.")
    return value


def _bound(db, key, expected_state, expected_epoch):
    row = _row(db, key)
    if (_uuid(expected_state) != resource_policy.state_id(db)
            or row["definition"]["authority_state_id"] != expected_state):
        raise ValueError("Controller authority history changed.")
    if _positive(expected_epoch) != row["epoch"]:
        raise ValueError("Stale controller epoch.")
    return row


def worker_seen(db, generation, *, expected_authority_state_id, now=None):
    """LOCAL worker attestation only; does not attest OS provider or launch support."""
    now = _clock(now)
    expected = _uuid(expected_authority_state_id)
    snapshot = resource_policy.status(db, now)
    worker = snapshot["worker"]
    if snapshot["state_id"] != expected or worker is None or worker["generation"] != _uuid(generation) or not worker["fresh"]:
        raise ValueError("Attest only the current fresh worker in this authority history.")
    db.execute("INSERT OR REPLACE INTO large_copy_worker_attestations VALUES(?,?,?)", (generation, expected, now))


def _worker(db, now):
    snapshot = resource_policy.status(db, now)
    worker = snapshot["worker"]
    remaining = 0.0
    if worker is not None and worker["fresh"]:
        seen = db.execute("SELECT state_id,seen FROM large_copy_worker_attestations WHERE generation=?", (worker["generation"],)).fetchone()
        if seen and seen[0] == snapshot["state_id"] and 0 <= now - seen[1] < resource_policy.FRESH_SECONDS:
            remaining = min(worker["fresh_remaining_seconds"], resource_policy.FRESH_SECONDS - (now - seen[1]))
    return {"generation": worker["generation"] if worker else None,
            "fresh": remaining > 0, "fresh_remaining_seconds": remaining}


def _approved(row, mode):
    return row["approved_sha256"] == row["intent_sha256"] and not (mode == "ask" and row["automatic"])


def worker_info(db, now=None):
    """Read metadata-adapter attestation, not provider or process readiness."""
    _transaction(db)
    return _worker(db, _clock(now))


def _intent(key, definition, epoch, endpoint_epochs):
    return {"version": VERSION, "controller_id": key, "controller_epoch": epoch,
            "manifest_sha256": definition["manifest_sha256"], "authority_state_id": definition["authority_state_id"],
            "endpoint_epochs": _epochs(endpoint_epochs), "relay_budget_bytes": definition["relay_budget_bytes"]}


def _proposal(row, side):
    if side not in SIDES:
        raise ValueError("Unknown binding side.")
    definition, intent = row["definition"], row["intent"]
    manifest = definition["manifest"]
    binding = {"controller_id": row["id"], "controller_epoch": intent["controller_epoch"],
               "authority_node_id": definition["local_node_id"], "authority_state_id": definition["authority_state_id"],
               "manifest_sha256": definition["manifest_sha256"]}
    value = {"version": VERSION, "side": side, "endpoint_id": manifest["plan"]["source_id" if side == "source" else "destination_id"],
             "endpoint_state_id": manifest["plan"][side + "_state_id"],
             "expected_endpoint_epoch": intent["endpoint_epochs"][side], "binding": binding}
    return dict(value, proposal_sha256=digest(value))


def _outbox(db, row):
    for side in SIDES:
        value = _proposal(row, side)
        db.execute("INSERT INTO large_copy_outbox VALUES(?,?,?,?,?,'pending',NULL,NULL)",
                   (row["id"], row["epoch"], side, canonical(value), value["proposal_sha256"]))


def status(db, key, now=None):
    row, now = _row(db, key), _clock(now)
    worker, settings = _worker(db, now), policy(db)
    history = row["definition"]["authority_state_id"] == resource_policy.state_id(db)
    approved = _approved(row, _mode(db))
    bindings = {}
    for side, phase, proposal_sha, receipt in db.execute("SELECT side,phase,proposal_sha256,receipt FROM large_copy_outbox WHERE controller_id=? AND epoch=?", (key, row["intent"]["controller_epoch"])):
        bindings[side] = {"phase": phase, "proposal_sha256": proposal_sha, "receipt": json.loads(receipt) if receipt else None}
    expired = now >= row["definition"]["manifest"]["plan"]["expires_at"]
    phase = row["phase"]
    if not history:
        phase = "history_mismatch"
    elif phase == "enrolling":
        if not settings["enabled"]:phase = "disabled"
        elif expired:phase = "expired"
        elif not approved:phase = "awaiting_approval"
        elif not worker["fresh"] or row["worker_generation"] not in {None, worker["generation"]}:phase = "awaiting_worker"
        elif len(bindings) != 2 or any(value["phase"] != "confirmed" for value in bindings.values()):
            phase = "binding_partial" if any(value["phase"] == "confirmed" for value in bindings.values()) else "enrolling"
        else:phase = "enrollment_ready"
    return {"version": VERSION, "controller_id": key, "controller_epoch": row["epoch"],
            "transfer_id": row["definition"]["manifest"]["id"], "authority_node_id": row["definition"]["local_node_id"],
            "authority_state_id": row["definition"]["authority_state_id"], "history_matches": history,
            "manifest_sha256": row["definition"]["manifest_sha256"], "intent": row["intent"],
            "intent_sha256": row["intent_sha256"], "approval_required": row["phase"] == "enrolling" and history and not expired and not approved,
            "phase": phase, "bindings": bindings, "worker": worker, "expired": expired,
            "enrollment_ready": phase == "enrollment_ready", "launchable": False, "operational": False,
            "runtime_implemented": False, "stop_reason": row["stop_reason"], "cleanup_epoch": row["cleanup_epoch"]}


def enroll(db, manifest, local_node_id, expected_authority_state_id, endpoint_epochs, relay_budget_bytes, mode, now=None):
    _transaction(db)
    manifest, now = plans.validate_manifest(manifest), _clock(now)
    if _uuid(local_node_id) != manifest["origin"] or _uuid(expected_authority_state_id) != resource_policy.state_id(db):
        raise ValueError("Enroll only on the manifest's actual authority and history.")
    access = _mode(db, mode)
    definition = {"manifest": manifest, "manifest_sha256": plans.package(manifest)["sha256"],
                  "local_node_id": local_node_id, "authority_state_id": expected_authority_state_id,
                  "initial_endpoint_epochs": _epochs(endpoint_epochs), "relay_budget_bytes": _positive(relay_budget_bytes, plans.MAX_BYTES)}
    key, checked = controller_id(manifest["id"], local_node_id), digest(definition)
    existing = db.execute("SELECT definition_sha256 FROM large_copy_controllers WHERE id=?", (key,)).fetchone()
    if existing:
        if existing[0] != checked:raise ValueError("Controller UUID already binds another exact enrollment.")
        return status(db, key, now)
    if now >= manifest["plan"]["expires_at"]:raise ValueError("The immutable transfer expired.")
    count = db.execute("SELECT COUNT(*) FROM large_copy_controllers WHERE phase='enrolling'").fetchone()[0]
    if count >= policy(db)["max_pending_enrollments"]:raise ValueError("Too many pending enrollments.")
    intent = _intent(key, definition, 1, endpoint_epochs)
    approved = digest(intent) if access == "full" else None
    db.execute("INSERT INTO large_copy_controllers VALUES(?,?,?,1,?,?,?,?, 'enrolling',NULL,NULL,NULL,NULL,NULL,?)",
               (key, canonical(definition), checked, canonical(intent), digest(intent), approved, int(access == "full"), now))
    _outbox(db, _row(db, key))
    return status(db, key, now)


def pending(db, now=None):
    _transaction(db)
    rows = db.execute("SELECT id FROM large_copy_controllers WHERE phase='enrolling' ORDER BY created,id").fetchall()
    return [value for row in rows if (value := status(db, row[0], now))["approval_required"]]


def decide(db, key, allowed, expected_intent_sha256, *, expected_controller_epoch, expected_authority_state_id, now=None):
    row = _bound(db, key, expected_authority_state_id, expected_controller_epoch)
    if type(allowed) is not bool or expected_intent_sha256 != row["intent_sha256"] or row["phase"] != "enrolling":
        raise ValueError("Decide only the exact current enrollment intent.")
    if _clock(now) >= row["definition"]["manifest"]["plan"]["expires_at"]:raise ValueError("Transfer expired.")
    db.execute("INSERT OR REPLACE INTO large_copy_decisions VALUES(?,?,?,?,?)", (key, row["epoch"], row["intent_sha256"], int(allowed), _clock(now)))
    if allowed:
        db.execute("UPDATE large_copy_controllers SET approved_sha256=?,automatic=0 WHERE id=?", (row["intent_sha256"], key))
    else:
        _stop(db, row, "denied")
    return status(db, key, now)


def _active(db, key, epoch, expected_state, now):
    row = _bound(db, key, expected_state, epoch)
    if row["phase"] != "enrolling" or row["runtime_run_id"] is not None:
        raise ValueError("This metadata enrollment cannot perform binding work.")
    if not policy(db)["enabled"] or not _approved(row, _mode(db)):
        raise ValueError("Enable the local policy and approve the exact intent first.")
    if now >= row["definition"]["manifest"]["plan"]["expires_at"]:raise ValueError("Transfer expired.")
    worker = _worker(db, now)
    if not worker["fresh"] or row["worker_generation"] not in {None, worker["generation"]}:
        raise ValueError("The current attested worker generation differs or is unavailable.")
    return row, worker


def binding_request(db, key, side, *, expected_controller_epoch, expected_authority_state_id, now=None):
    """Durably mark a possible metadata RPC before the LOCAL wrapper sends it."""
    now = _clock(now)
    row, worker = _active(db, key, expected_controller_epoch, expected_authority_state_id, now)
    proposal = _proposal(row, side)
    db.execute("UPDATE large_copy_controllers SET worker_generation=? WHERE id=?", (worker["generation"], key))
    db.execute("UPDATE large_copy_outbox SET phase='requested' WHERE controller_id=? AND epoch=? AND side=? AND phase='pending'", (key, row["epoch"], side))
    return proposal


def confirm_binding(db, key, side, receipt, *, worker_generation, expected_controller_epoch, expected_authority_state_id, now=None):
    """LOCAL authenticated endpoint response only; never a public 'verified' flag."""
    now = _clock(now)
    row, worker = _active(db, key, expected_controller_epoch, expected_authority_state_id, now)
    if _uuid(worker_generation) != worker["generation"] or row["worker_generation"] != worker_generation:
        raise ValueError("The binding response belongs to another worker generation.")
    proposal = _proposal(row, side)
    expected = {"version": VERSION, "side": side, "endpoint_id": proposal["endpoint_id"],
                "endpoint_state_id": proposal["endpoint_state_id"], "endpoint_epoch": proposal["expected_endpoint_epoch"],
                "binding": proposal["binding"], "proposal_sha256": proposal["proposal_sha256"], "bound": True}
    if not isinstance(receipt, dict) or canonical(receipt) != canonical(expected):
        raise ValueError("The exact binding response differs from its proposal.")
    saved = db.execute("SELECT phase,receipt FROM large_copy_outbox WHERE controller_id=? AND epoch=? AND side=?", (key, row["epoch"], side)).fetchone()
    if saved is None or saved[0] == "pending":raise ValueError("Record the outbox request before confirming it.")
    if saved[1] is not None and saved[1] != canonical(receipt):raise ValueError("A confirmed endpoint binding cannot change.")
    db.execute("UPDATE large_copy_outbox SET phase='confirmed',receipt=?,confirmed_at=COALESCE(confirmed_at,?) WHERE controller_id=? AND epoch=? AND side=?",
               (canonical(receipt), now, key, row["epoch"], side))
    return status(db, key, now)


def _stop(db, row, reason):
    if row["phase"] not in {"stopping", "stopped"}:
        db.execute("UPDATE large_copy_controllers SET stopped_from_epoch=epoch,epoch=epoch+1,phase='stopping',cleanup_epoch=NULL,stop_reason=? WHERE id=?", (reason, row["id"]))
    if reason in {"revoked", "denied"}:
        db.execute("UPDATE large_copy_controllers SET approved_sha256=NULL,automatic=0,stop_reason=? WHERE id=?", (reason, row["id"]))


def emergency_stop(db, key, now=None):
    """LOCAL owner only: fence even stale history; never authorize/clean/unbind it."""
    row = _row(db, key)
    _stop(db, row, "emergency")
    return status(db, key, now)


def confirm_cleanup(db, key, evidence, *, expected_controller_epoch, expected_authority_state_id, now=None):
    """LOCAL proof of quiescent metadata enrollment; runtime cleanup is not implemented."""
    row = _bound(db, key, expected_authority_state_id, expected_controller_epoch)
    expected = {"controller_id": key, "controller_epoch": row["epoch"],
                "previous_controller_epoch": row["stopped_from_epoch"], "worker_generation": row["worker_generation"],
                "runtime_run_id": None}
    if (row["runtime_run_id"] is not None or row["phase"] not in {"stopping", "stopped"}
            or not isinstance(evidence, dict) or canonical(evidence) != canonical(expected)):
        raise ValueError("Confirm only original-generation/epoch cleanup of a never-launched enrollment.")
    db.execute("UPDATE large_copy_controllers SET phase='stopped',cleanup_epoch=? WHERE id=?", (row["epoch"], key))
    return status(db, key, now)


def control(db, key, operation, *, expected_controller_epoch, expected_authority_state_id, endpoint_epochs=None, now=None):
    """Metadata only; resume epochs are LOCAL wrapper observations of remote barriers.

    They must not be client-selected numbers. No runtime process has been
    launched by this ledger; a non-null runtime_run_id refuses abbreviated cleanup.
    """
    row = _bound(db, key, expected_authority_state_id, expected_controller_epoch)
    if operation in {"stop", "revoke"}:
        _stop(db, row, "revoked" if operation == "revoke" else "stop")
    elif operation == "resume":
        if row["phase"] != "stopped" or row["cleanup_epoch"] != row["epoch"] or row["runtime_run_id"] is not None:
            raise ValueError("Resume only a cleanup-confirmed, never-launched enrollment.")
        if _clock(now) >= row["definition"]["manifest"]["plan"]["expires_at"]:raise ValueError("Transfer expired.")
        epoch = row["epoch"] + 1
        intent = _intent(key, row["definition"], epoch, endpoint_epochs)
        access = _mode(db)
        db.execute("UPDATE large_copy_controllers SET epoch=?,intent=?,intent_sha256=?,approved_sha256=?,automatic=?,phase='enrolling',worker_generation=NULL,stopped_from_epoch=NULL,cleanup_epoch=NULL,stop_reason=NULL WHERE id=?",
                   (epoch, canonical(intent), digest(intent), digest(intent) if access == "full" else None, int(access == "full"), key))
        _outbox(db, _row(db, key))
    else:
        raise ValueError("Unknown metadata controller operation.")
    return status(db, key, now)
