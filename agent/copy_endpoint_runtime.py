# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Receiver-side binding facade; no autonomous copy process or transport.

The explicit service catalog supplies the local identity. Wire callers can
prepare an exact owner-readable intent and bind an approved intent under the
existing endpoint lock. They cannot change policy, approve, attest a worker,
assert cleanup, or acquire ownership by supplying an arbitrary token.
"""
from pathlib import Path
import stat

import copy_endpoint_bindings as bindings
import large_copy_runtime
import large_transfer_plans as plans


def _fields(value, expected):
    if set(value) != set(expected):
        raise ValueError("Use the exact endpoint binding operation fields.")


def check_path(runtime, record):
    """Filesystem checks outside SQL; SQL authorization repeats afterwards."""
    folder = Path(record["grant_snapshot"]["directory"])
    if folder.resolve(strict=True) != folder or not stat.S_ISDIR(runtime.files.regular_info(folder).st_mode):
        raise ValueError("The folder grant changed on disk.")
    # A sealed export reads its immutable spool, so its original source may
    # legitimately disappear after preparation. Keep validating the root grant.
    if record["spec"]["role"] == "import" or record["header"] is None:
        runtime.large_files._path(runtime, record)


def dispatch(runtime, operation, protocol_version=1, endpoint_id=None, **fields):
    if type(protocol_version) is not int or protocol_version != 1:
        raise ValueError("Unsupported endpoint binding protocol.")
    if operation == "info" and endpoint_id is None and not fields:
        with runtime.transaction() as db:
            return dict(bindings.info(db), enforcement_implemented=True,
                        autonomous_copy_implemented=False)
    if operation == "prepare" and endpoint_id is None:
        _fields(fields, {"manifest", "proposal", "expected_binding_revision"})
        registry = large_copy_runtime.catalog(runtime)  # Operator file I/O outside SQL.
        manifest = plans.validate_manifest(fields["manifest"])
        for side in ("source", "destination"):
            node_id = manifest["request"][side]["node_id"]
            if registry.get(node_id).node_id != node_id:
                raise ValueError("Use canonical nodes from the explicit service catalog.")
        with runtime.transaction() as db:
            return bindings.prepare(db, manifest, fields["proposal"], local_node_id=registry.local_id,
                                    expected_binding_revision=fields["expected_binding_revision"])
    key = plans.identity(endpoint_id)
    if operation == "status":
        if not set(fields) <= {"intent_sha256"}:
            raise ValueError("Read only an exact endpoint binding intent.")
        with runtime.transaction() as db:
            return bindings.status(db, key, **fields)
    if operation != "bind":
        raise ValueError("Unknown endpoint binding operation.")
    _fields(fields, {"expected_state_id", "expected_intent_sha256", "expected_binding_revision"})
    # This excludes synchronous I/O whose caller disconnected. The SQL attempt
    # ledger must independently confirm cleanup of every supervised child.
    with runtime.large_files._owned(runtime, key):
        with runtime.transaction() as db:
            record = runtime.large_files._pinned(db, key, fields["expected_state_id"])
            grant = runtime.large_files._grant(db, runtime.large_files._endpoint(record)["root"])
            if grant != record["grant_snapshot"]:
                raise ValueError("The folder grant changed before binding.")
        check_path(runtime, record)
        with runtime.transaction() as db:
            runtime.large_files._pinned(db, key, fields["expected_state_id"])
            return bindings.bind(db, key, expected_intent_sha256=fields["expected_intent_sha256"],
                                 expected_binding_revision=fields["expected_binding_revision"])
