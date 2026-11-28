# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Owner-local large-file dispatcher. All filesystem work is outside SQLite.

Only the installed helper chooses filesystem paths or program argv. Wire callers
supply immutable endpoint IDs/epochs and bounded bytes, never staging paths.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import sys
from uuid import uuid4

if __name__ == "__main__":
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("file_job_runtime", Path(__file__).with_name("jobs.py"))
    runtime = module_from_spec(spec)
    spec.loader.exec_module(runtime)
    runtime.ROOT = Path(__file__).resolve().parent
    runtime.DATABASE = runtime.ROOT / "jobs.sqlite3"
    resource_policy, state, storage = runtime.resource_policy, runtime.transfer_state, runtime.transfer_storage
else:
    import resource_policy
    import transfer_state as state
    import transfer_storage as storage

PROGRAM = "@file-v2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS large_file_jobs(
 job_id TEXT PRIMARY KEY,endpoint_id TEXT NOT NULL,epoch INTEGER NOT NULL,kind TEXT NOT NULL
);
"""


def base(runtime):
    return runtime.ROOT / "large-files"


def _key(spec):
    return state.endpoint_id(spec["transfer_id"], spec["authority_node_id"], spec["role"])


def _endpoint(record):
    spec = record["spec"]
    return spec["source"] if spec["role"] == "export" else spec["destination"]


def _grant(db, alias):
    row = db.execute("SELECT directory,writable FROM file_roots WHERE alias=?", (alias,)).fetchone()
    if row is None:
        raise ValueError("The folder grant is unavailable.")
    return {"directory": row[0], "writable": bool(row[1])}


class _GrantView:
    """Use the legacy path checks against a captured grant, without holding SQL."""
    def __init__(self, grant):
        self.grant = grant

    def execute(self, query, params):
        if not query.startswith("SELECT directory,writable FROM file_roots"):
            raise ValueError("Unexpected filesystem grant lookup.")
        return self

    def fetchone(self):
        return self.grant


def _path(runtime, record):
    endpoint = _endpoint(record)
    return runtime.files.checked_path(_GrantView(record["grant_snapshot"]), endpoint["root"], endpoint["path"],
                                      write=record["spec"]["role"] == "import")


def _volume(path, role):
    return {"role": role, "id": str(path.stat().st_dev), "free_bytes": shutil.disk_usage(path).free}


def _volumes(runtime, record):
    observations = [_volume(runtime.ROOT, "spool")]
    if record["spec"]["role"] == "import":
        observations.append(_volume(_path(runtime, record).parent, "target"))
    return observations


def _authorize(runtime, key, epoch, *, claimed=True, expected_state=None):
    with runtime.transaction() as db:
        row = (_pinned(db, key, expected_state) if expected_state is not None else state.details(db, key))
        grant = _grant(db, _endpoint(row)["root"])
        row = state.guard(db, key, epoch, grant=grant, require_reserved=claimed)
        if claimed and not db.execute("SELECT 1 FROM large_file_claims WHERE endpoint_id=? AND released IS NULL", (key,)).fetchone():
            raise ValueError("Reserve disk before endpoint I/O.")
    # Filesystem checks are deliberately after COMMIT, including slow volumes.
    folder = Path(grant["directory"])
    if folder.resolve(strict=True) != folder or not stat.S_ISDIR(runtime.files.regular_info(folder).st_mode):
        raise ValueError("The folder grant changed on disk.")
    if row["spec"]["role"] == "import" or row["header"] is None:
        _path(runtime, row)
    return row


@contextmanager
def _owned(runtime, key):
    # Synchronous block handlers never fork. Their OS lock therefore excludes
    # every possible writer, including a handler whose SSH caller disconnected.
    locks = runtime.ROOT / "large-file-locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    runtime.files.regular_info(locks)
    with runtime.process_lock(locks / (key + ".lock")):
        yield


def request(runtime, spec):
    spec = state.validate_spec(spec)
    key = _key(spec)
    with runtime.transaction() as db:
        if not state.policy(db)["enabled"]:
            raise ValueError("Large-file transfers require local operator opt-in.")
        old = db.execute("SELECT 1 FROM large_file_endpoints WHERE id=?", (key,)).fetchone()
        if old:
            saved = state.details(db, key)
            if saved["spec"] != spec or spec["expected_state_id"] != resource_policy.state_id(db):
                raise ValueError("The endpoint request or durable state differs.")
            return state.status(db, key)
        local = spec["source"] if spec["role"] == "export" else spec["destination"]
        grant = _grant(db, local["root"])
    record = {"spec": spec, "grant_snapshot": grant}
    path = _path(runtime, record)
    identity = storage.source_identity(path) if spec["role"] == "export" else None
    with runtime.transaction() as db:
        if _grant(db, local["root"]) != grant:
            raise ValueError("The folder grant changed while preparing the request.")
        return state.request(db, spec, grant, identity, [], runtime.mode(db))


def reserve(runtime, key, epoch, expected_state):
    record = _authorize(runtime, key, epoch, claimed=False, expected_state=expected_state)
    observations = _volumes(runtime, record)
    with runtime.transaction() as db:
        _pinned(db, key, expected_state)
        state.guard(db, key, epoch, grant=_grant(db, _endpoint(record)["root"]), require_reserved=False)
        result = state.reserve(db, key, epoch, observations)
        return dict(state.status(db, key), reservation=result)


def queue(runtime, key, epoch, expected_state):
    record = _authorize(runtime, key, epoch, expected_state=expected_state)
    kind = "prepare" if record["spec"]["role"] == "export" else "commit"
    if kind == "prepare" and record["header"] is not None or kind == "commit" and record["receipt"] is not None:
        with runtime.transaction() as db:
            return state.status(db, key)
    with runtime.transaction() as db:
        _pinned(db, key, expected_state)
        state.guard(db, key, epoch, grant=_grant(db, _endpoint(record)["root"]))
        active = db.execute("SELECT j.id FROM large_file_jobs f JOIN jobs j ON j.id=f.job_id "
                            "WHERE f.endpoint_id=? AND j.state IN ('pending','queued','running','stopping')", (key,)).fetchone()
        if active:
            return state.status(db, key)
        job_id = str(uuid4())
        state.begin_attempt(db, key, epoch, job_id, kind)
        db.execute("INSERT INTO large_file_jobs VALUES(?,?,?,?)", (job_id, key, epoch, kind))
        db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,'queued',?)",
                   (job_id, PROGRAM, "null", runtime.time.time()))
        resource_policy.set_request(db, job_id, {"cpu_millis": 250, "memory_bytes": 16 * 1024 * 1024, "gpus": []})
        return state.status(db, key)


def arguments(runtime, db, job_id):
    row = db.execute("SELECT endpoint_id,epoch,kind FROM large_file_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None or runtime.job(db, job_id)["state"] != "running":
        raise ValueError("The owned worker must claim this endpoint job.")
    record = state.details(db, row[0])
    state.guard(db, row[0], row[1], grant=_grant(db, _endpoint(record)["root"]))
    claim = db.execute("SELECT 1 FROM resource_claims WHERE id=? AND released IS NULL", (job_id,)).fetchone()
    if claim is None:
        raise ValueError("The file job has no owned resource admission.")
    return [sys.executable, "-I", "-B", str(runtime.ROOT / "large_files.py"), job_id,
            record["spec"]["expected_state_id"]]


def finish_job(db, job_id, cleanup_confirmed, job_state, exit_code):
    if db.execute("SELECT 1 FROM large_file_jobs WHERE job_id=?", (job_id,)).fetchone() is None:
        return
    outcome = ("completed" if job_state == "completed" and exit_code == 0 else
               "stopped" if job_state == "cancelled" else "unconfirmed" if job_state == "unconfirmed" else "failed")
    state.finish_attempt(db, job_id, cleanup_confirmed, outcome)


def reconcile_job(db, job_id):
    attempt = db.execute("SELECT a.finished FROM large_file_jobs f JOIN large_file_attempts a ON a.id=f.job_id "
                         "WHERE f.job_id=?", (job_id,)).fetchone()
    if attempt is not None:
        if attempt[0] is None:
            # Core verified this original claim's cleanup receipt first. A
            # crashed owner may never have recorded its attempt outcome.
            state.finish_attempt(db, job_id, True, "unconfirmed")
        else:
            state.reconcile_attempt_cleanup(db, job_id)


def stop_job(db, job_id):
    link = db.execute("SELECT f.endpoint_id FROM large_file_jobs f JOIN jobs j ON j.id=f.job_id "
                      "WHERE f.job_id=? AND j.state IN ('pending','queued','running','stopping')", (job_id,)).fetchone()
    if link is not None:
        state.stop(db, link[0])


def worker(runtime, job_id, expected_state):
    with runtime.transaction() as db:
        link = db.execute("SELECT endpoint_id,epoch,kind FROM large_file_jobs WHERE job_id=?", (job_id,)).fetchone()
        if link is None:
            raise ValueError("Unknown supervised file operation.")
        _pinned(db, link[0], expected_state)
    key, epoch, kind = link
    authorize = lambda: _authorize(runtime, key, epoch, expected_state=expected_state)
    record = authorize()
    def get_block(index):
        with runtime.transaction() as db:
            _pinned(db, key, expected_state)
            state.guard(db, key, epoch)
            return state.get_block(db, key, index)
    def checkpoint(block):
        with runtime.transaction() as db:
            _pinned(db, key, expected_state)
            state.checkpoint(db, key, epoch, job_id if kind == "prepare" else None, **block)
    with _owned(runtime, key):
        if kind == "prepare":
            header = storage.prepare_export(_path(runtime, record), base(runtime), key, record["request_sha256"],
                record["source_identity"], get_block=get_block, checkpoint=checkpoint, authorize=authorize,
                max_bytes=record["spec"]["max_bytes"])
            with runtime.transaction() as db:
                _pinned(db, key, expected_state)
                state.source_ready(db, key, epoch, job_id, header)
        else:
            def commit_point():
                # Finish filesystem validation before this short transaction.
                # The durable permit orders publication against Stop/revoke;
                # no SQL lock is held during the hardlink or directory fsync.
                authorize()
                with runtime.transaction() as db:
                    _pinned(db, key, expected_state)
                    state.guard(db, key, epoch, grant=_grant(db, _endpoint(record)["root"]))
                    state.commit_publication(db, key, epoch, job_id)
            receipt = storage.finalize_import(base(runtime), key, record["header"], _path(runtime, record),
                get_block=get_block, authorize=authorize, commit_point=commit_point)
            with runtime.transaction() as db:
                _pinned(db, key, expected_state)
                state.published(db, key, epoch, job_id, receipt)


def _pinned(db, key, expected_state):
    record = state.details(db, key)
    if expected_state != resource_policy.state_id(db) or record["spec"]["expected_state_id"] != expected_state:
        raise ValueError("The endpoint belongs to another durable history.")
    return record


def _control(runtime, operation, key, epoch, expected_state):
    if operation == "stop":
        with runtime.transaction() as db:
            if _pinned(db, key, expected_state)["epoch"] != epoch:
                raise ValueError("Stale endpoint control epoch.")
            state.stop(db, key)
            for row in db.execute("SELECT job_id FROM large_file_jobs WHERE endpoint_id=?", (key,)).fetchall():
                runtime.request_stop(db, row[0])
            return state.status(db, key)
    with _owned(runtime, key):
        with runtime.transaction() as db:
            record = _pinned(db, key, expected_state)
            if record["epoch"] != epoch:
                raise ValueError("Stale endpoint control epoch.")
            busy = db.execute("SELECT 1 FROM large_file_attempts WHERE endpoint_id=? AND cleanup_confirmed=0", (key,)).fetchone()
            if busy:
                raise ValueError("The original file job has not confirmed cleanup.")
            if operation == "resume":
                if _grant(db, _endpoint(record)["root"]) != record["grant_snapshot"]:
                    raise ValueError("The folder grant changed.")
                return state.resume(db, key, epoch, runtime.mode(db))
        if operation != "cleanup":
            raise ValueError("Unknown endpoint control operation.")
        if record["spec"]["role"] == "import":
            storage.cleanup_pending(base(runtime), key, _path(runtime, record))
        storage.cleanup_private(base(runtime), "exports" if record["spec"]["role"] == "export" else "imports", key)
        with runtime.transaction() as db:
            _pinned(db, key, expected_state)
            return state.cleanup(db, key, epoch, cleanup_confirmed=True, physical_deleted=True)


def dispatch(runtime, operation, protocol_version=2, request_id=None, payload=b"", **fields):
    if type(protocol_version) is not int or protocol_version != 2:
        raise ValueError("Unsupported large-file protocol.")
    if operation == "info" and request_id is None and not fields and not payload:
        with runtime.transaction() as db:
            result = dict(state.info(db), mode=runtime.mode(db))
        result["copy_metadata"] = runtime.large_copy_runtime.info(runtime)
        return result, b""
    if operation == "request" and request_id is None and set(fields) == {"spec"} and not payload:
        return request(runtime, fields["spec"]), b""
    key = state._uuid(request_id)
    expected_state = fields.pop("expected_state_id", None)
    with runtime.transaction() as db:
        _pinned(db, key, expected_state)
    if operation == "status" and not fields and not payload:
        with runtime.transaction() as db:
            _pinned(db, key, expected_state)
            result = state.status(db, key)
            row = db.execute("SELECT j.id,f.kind,j.state,j.exit_code,a.cleanup_confirmed,j.created,j.finished "
                "FROM large_file_jobs f JOIN jobs j ON j.id=f.job_id JOIN large_file_attempts a ON a.id=f.job_id "
                "WHERE f.endpoint_id=? ORDER BY j.created DESC,j.id DESC LIMIT 1", (key,)).fetchone()
            result["job"] = (dict(zip(("id", "kind", "state", "exit_code", "cleanup_confirmed", "created", "finished"), row))
                             if row else None)
            if result["job"] is not None:
                result["job"]["cleanup_confirmed"] = bool(result["job"]["cleanup_confirmed"])
            return result, b""
    if operation == "blocks" and set(fields) <= {"offset", "limit"} and not payload:
        with runtime.transaction() as db:
            _pinned(db, key, expected_state)
            return state.block_page(db, key, **fields), b""
    epoch = fields.pop("epoch", None)
    if type(epoch) is not int or epoch < 1:
        raise ValueError("Use the exact positive endpoint epoch.")
    if operation in {"stop", "resume", "cleanup"} and not fields and not payload:
        return _control(runtime, operation, key, epoch, expected_state), b""
    if operation == "bind" and set(fields) == {"header"} and not payload:
        with runtime.transaction() as db:
            row = _pinned(db, key, expected_state)
            if row["epoch"] != epoch:
                raise ValueError("Stale endpoint epoch.")
            return state.bind_manifest(db, key, fields["header"], runtime.mode(db)), b""
    if operation == "reserve" and not fields and not payload:
        return reserve(runtime, key, epoch, expected_state), b""
    if operation == "queue" and not fields and not payload:
        return queue(runtime, key, epoch, expected_state), b""
    if operation not in {"block-read", "block-write", "verify-block"}:
        raise ValueError("Unknown large-file operation.")
    expected_fields = {"index"} if operation == "block-read" else {"index", "expected"}
    if set(fields) != expected_fields or operation != "block-write" and payload:
        raise ValueError("Invalid bounded block operation.")
    with _owned(runtime, key):
        record = _authorize(runtime, key, epoch, expected_state=expected_state)
        header = record["header"]
        if header is None:
            raise ValueError("No approved immutable content manifest.")
        index = fields["index"]
        if operation == "block-read":
            if record["spec"]["role"] != "export":
                raise ValueError("Only an export can send content.")
            with runtime.transaction() as db:
                _pinned(db, key, expected_state)
                block = state.get_block(db, key, index)
            data = storage.read_export_block(base(runtime), header, index, lambda _: block,
                                            authorize=lambda: _authorize(runtime, key, epoch, expected_state=expected_state))
            return {"block": block}, data
        if record["spec"]["role"] != "import":
            raise ValueError("Only an import can receive content.")
        authorize = lambda: _authorize(runtime, key, epoch, expected_state=expected_state)
        if operation == "verify-block":
            valid = storage.verify_stored_block(base(runtime), key, header, index, fields["expected"], authorize=authorize)
            if valid:
                # A prior writer can exit after publishing the durable block
                # and before its SQL checkpoint. Do not let the coordinator
                # skip those bytes while the authoritative ledger is missing.
                with runtime.transaction() as db:
                    _pinned(db, key, expected_state)
                    state.guard(db, key, epoch, grant=_grant(db, _endpoint(record)["root"]))
                    state.checkpoint(db, key, epoch, None, **fields["expected"])
            return {"valid": valid}, b""
        def checkpoint(block):
            with runtime.transaction() as db:
                _pinned(db, key, expected_state)
                state.checkpoint(db, key, epoch, None, **block)
        block = storage.store_block(base(runtime), key, header, index, payload, fields["expected"],
                                    checkpoint=checkpoint, authorize=authorize)
        return {"block": block}, b""


if __name__ == "__main__":
    # Only the installed worker produces this argv; the public RPC cannot name
    # a helper path or supply code to the supervised child.
    worker(runtime, state._uuid(sys.argv[1]), state._uuid(sys.argv[2]))
