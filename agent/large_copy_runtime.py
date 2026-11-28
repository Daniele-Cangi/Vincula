# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local metadata facade for a future autonomous copy runtime.

This facade prepares intents and reads/fences metadata. It never sends a
binding, starts a process, signs SSH requests or reserves/deletes relay data.
Attestation, binding acknowledgements and cleanup proofs are NOT wire actions.
The operator-selected catalog is read outside SQLite transactions.
"""
import json
from pathlib import Path

import large_copy_state as state
import large_transfer_plans as plans
import node_registry
import resource_policy

VERSION = 1
SETTINGS_NAME = "large-copy-controller.json"
MAX_SETTINGS_BYTES = 16 * 1024


def _fields(value, expected):
    if set(value) != set(expected):
        raise ValueError("Use the exact metadata operation fields.")


def catalog(runtime):
    """Read only the explicit service configuration; never use a user fallback."""
    with (runtime.ROOT / SETTINGS_NAME).open("rb") as stream:
        raw = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValueError("Controller settings exceed 16 KiB.")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"version", "node_id", "registry_path"}:
        raise ValueError("Invalid local controller settings.")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported local controller settings version.")
    node_id = plans.identity(value["node_id"])
    path = value["registry_path"]
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("Choose an explicit absolute service catalog path.")
    family = "windows" if runtime.WINDOWS else "linux"
    registry = node_registry.load_service(Path(path), family, "NUL" if runtime.WINDOWS else "/dev/null")
    if registry.local_id != node_id:
        raise ValueError("The service catalog belongs to another node.")
    return registry


def info(runtime):
    configured, registry = "configured", None
    try:
        registry = catalog(runtime)
    except FileNotFoundError:
        configured = "configuration_missing"
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        configured = "configuration_invalid"
    with runtime.transaction() as db:
        result = {"version": VERSION, "state_id": resource_policy.state_id(db),
                  "policy": state.policy(db), "mode": runtime.mode(db),
                  "worker": state.worker_info(db)}
        result["policy_revision"] = state.digest(result["policy"])
    return dict(result, configuration=configured,
                authority_node_id=registry.local_id if registry else None,
                registered_nodes=[node.node_id for node in registry.nodes] if registry else [],
                metadata_available=True, runtime_implemented=False, launchable=False,
                operational=False, provider_verified=False, transport_verified=False)


def _prepare(runtime, fields):
    _fields(fields, {"transfer_id", "expected_authority_state_id", "expected_manifest_sha256",
                     "proposed_endpoint_epochs", "relay_budget_bytes"})
    registry = catalog(runtime)
    transfer_id = plans.identity(fields["transfer_id"])
    with runtime.transaction() as db:
        record = runtime.plan_journal.get(db, plans.KIND, transfer_id)
        if (record is None or set(record) != {"manifest", "sha256"}
                or record["sha256"] != fields["expected_manifest_sha256"]):
            raise ValueError("Read the exact immutable transfer on its authority before preparing metadata.")
        manifest = plans.validate_manifest(record["manifest"])
        for side in state.SIDES:
            endpoint = manifest["request"][side]
            if registry.get(endpoint["node_id"]).node_id != endpoint["node_id"]:
                raise ValueError("The transfer endpoint must use a canonical registered UUID.")
        return state.enroll(db, manifest, registry.local_id, fields["expected_authority_state_id"],
                            fields["proposed_endpoint_epochs"], fields["relay_budget_bytes"], runtime.mode(db))


def _stop(runtime, controller_id, fields):
    _fields(fields, {"expected_controller_epoch", "expected_authority_state_id"})
    with runtime.transaction() as db:
        stopped = state.control(db, controller_id, "stop", **fields)
        # A never-dispatched intent has no possible SSH child or binding RPC.
        # Once ANY outbox request was recorded, only the future trusted wrapper
        # can supply a scoped cleanup proof; a wire caller cannot assert it.
        original = db.execute("SELECT worker_generation,stopped_from_epoch,runtime_run_id "
                              "FROM large_copy_controllers WHERE id=?", (controller_id,)).fetchone()
        dispatched = db.execute("SELECT 1 FROM large_copy_outbox WHERE controller_id=? AND phase!='pending'",
                                (controller_id,)).fetchone()
        pending = db.execute("SELECT side FROM large_copy_outbox WHERE controller_id=? AND epoch=? AND phase='pending'",
                             (controller_id, stopped["intent"]["controller_epoch"])).fetchall()
        if (stopped["phase"] == "stopping" and original[0] is None and original[2] is None
                and dispatched is None and {row[0] for row in pending} == set(state.SIDES)):
            evidence = {"controller_id": controller_id, "controller_epoch": stopped["controller_epoch"],
                        "previous_controller_epoch": original[1], "worker_generation": original[0],
                        "runtime_run_id": None}
            stopped = state.confirm_cleanup(db, controller_id, evidence,
                expected_controller_epoch=stopped["controller_epoch"],
                expected_authority_state_id=fields["expected_authority_state_id"])
        return stopped


def dispatch(runtime, operation, protocol_version=VERSION, controller_id=None, **fields):
    if type(protocol_version) is not int or protocol_version != VERSION:
        raise ValueError("Unsupported copy metadata protocol.")
    if operation == "info" and controller_id is None and not fields:
        return info(runtime)
    if operation == "prepare" and controller_id is None:
        # These epochs are a proposed intent, not authenticated endpoint
        # observations or cleanup barriers. No binding operation is exposed.
        return _prepare(runtime, fields)
    key = plans.identity(controller_id)
    if operation == "read" and not fields:
        with runtime.transaction() as db:
            return state.status(db, key)
    if operation == "stop":
        return _stop(runtime, key, fields)
    raise ValueError("Unknown copy metadata operation; autonomous execution is not implemented.")
