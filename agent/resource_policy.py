# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cooperative destination-local resource admission, not OS resource isolation.

Run SCHEMA during explicit runtime initialization. Every function taking db
requires the caller's BEGIN IMMEDIATE transaction and never commits it. The
worker owns its exclusive process lock before publishing a generation. Only
that trusted owner may confirm that a job's process tree has been cleaned up.
"""
from hashlib import sha256
import json
import math
import time
from uuid import UUID, uuid4

PROTOCOL_VERSION = 2
MAX_JOBS = 64
FRESH_SECONDS = 5
MAX_INTEGER = 2**63 - 1
HARD_LIMITS = {"cpu": False, "memory": False, "gpu": False}
DEFAULT_POLICY = {"max_jobs": 1, "cpu_millis": None, "memory_bytes": None,
                  "gpus": None, "legacy_defaults": None}
SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_identity(id INTEGER PRIMARY KEY CHECK(id=1), state_id TEXT NOT NULL);
INSERT OR IGNORE INTO resource_identity
SELECT 1,substr(v,1,8)||'-'||substr(v,9,4)||'-'||substr(v,13,4)||'-'||substr(v,17,4)||'-'||substr(v,21,12)
FROM (SELECT lower(hex(randomblob(16))) AS v);
CREATE TABLE IF NOT EXISTS resource_policy(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_requests(id TEXT PRIMARY KEY, requirements TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_worker(
    id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL, protocol INTEGER NOT NULL,
    platform TEXT, heartbeat REAL NOT NULL, observation TEXT
);
CREATE TABLE IF NOT EXISTS resource_worker_stops(generation TEXT PRIMARY KEY, stopped REAL NOT NULL);
CREATE TABLE IF NOT EXISTS resource_claims(
    id TEXT PRIMARY KEY, generation TEXT NOT NULL, requirements TEXT NOT NULL,
    legacy INTEGER NOT NULL, exclusive INTEGER NOT NULL, created REAL NOT NULL,
    released REAL, cleanup_nonce TEXT NOT NULL
);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _transaction(db):
    if not db.in_transaction:
        raise ValueError("Resource operations require the caller's BEGIN IMMEDIATE transaction.")


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use a canonical job or worker generation UUID.")
    return value


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError("Resource quantities must be nonnegative bounded integers.")
    return value


def _clock(now):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError("Use a finite nonnegative observation time.")
    return now


def _gpu_id(value):
    # Device indices change with enumeration. Require a provider's stable UUID.
    if not isinstance(value, str) or not value.startswith(("GPU-", "MIG-")):
        raise ValueError("A stable NVIDIA GPU/MIG UUID is required; device indices are not identities.")
    _uuid(value[4:])
    return value


def _gpu_rows(rows, fields):
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("Supply a bounded list of GPU resource records.")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", *fields}:
            raise ValueError("Invalid GPU resource fields.")
        result.append(dict(row, id=_gpu_id(row["id"])))
    if len({row["id"] for row in result}) != len(result):
        raise ValueError("GPU identities must be unique.")
    return sorted(result, key=lambda row: row["id"])


def validate_requirements(value):
    if not isinstance(value, dict) or set(value) != {"cpu_millis", "memory_bytes", "gpus"}:
        raise ValueError("Declare exactly CPU, memory and GPU requirements.")
    gpus = _gpu_rows(value["gpus"], {"vram_bytes", "exclusive"})
    for row in gpus:
        _integer(row["vram_bytes"])
        if type(row["exclusive"]) is not bool:
            raise ValueError("GPU exclusivity must be an explicit boolean.")
    return {"cpu_millis": _integer(value["cpu_millis"]),
            "memory_bytes": _integer(value["memory_bytes"]), "gpus": gpus}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise ValueError("Invalid local resource policy fields.")
    max_jobs = _integer(value["max_jobs"], minimum=1)
    if max_jobs > MAX_JOBS:
        raise ValueError("Local resource concurrency exceeds the supported worker pool.")
    gpus = None if value["gpus"] is None else _gpu_rows(value["gpus"], {"vram_bytes"})
    for row in gpus or []:
        _integer(row["vram_bytes"])
    return {"max_jobs": max_jobs,
            "cpu_millis": None if value["cpu_millis"] is None else _integer(value["cpu_millis"]),
            "memory_bytes": None if value["memory_bytes"] is None else _integer(value["memory_bytes"]),
            "gpus": gpus, "legacy_defaults": None if value["legacy_defaults"] is None
            else validate_requirements(value["legacy_defaults"])}


def validate_observation(value):
    fields = {"observed_at", "cpu_millis", "memory_total_bytes", "memory_available_bytes", "gpus"}
    if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {"cpu_percent"}:
        raise ValueError("Invalid worker resource observation fields.")
    result = {"observed_at": _clock(value["observed_at"])}
    load = value.get("cpu_percent")
    if load is not None and (type(load) not in (float, int) or not math.isfinite(load) or not 0 <= load <= 100):
        raise ValueError("Invalid observed CPU load.")
    result["cpu_percent"] = load
    for field in fields - {"observed_at", "gpus"}:
        result[field] = None if value[field] is None else _integer(value[field])
    total, free = result["memory_total_bytes"], result["memory_available_bytes"]
    if free is not None and (total is None or free > total):
        raise ValueError("Invalid observed available memory.")
    result["gpus"] = None if value["gpus"] is None else _gpu_rows(
        value["gpus"], {"vram_total_bytes", "vram_available_bytes"})
    for row in result["gpus"] or []:
        for field in ("vram_total_bytes", "vram_available_bytes"):
            if row[field] is not None:
                _integer(row[field])
        if row["vram_available_bytes"] is not None and (
                row["vram_total_bytes"] is None or row["vram_available_bytes"] > row["vram_total_bytes"]):
            raise ValueError("Invalid observed GPU memory.")
    return result


def policy(db):
    _transaction(db)
    row = db.execute("SELECT document FROM resource_policy WHERE id=1").fetchone()
    return validate_policy(json.loads(row[0]) if row else DEFAULT_POLICY)


def state_id(db):
    _transaction(db)
    row = db.execute("SELECT state_id FROM resource_identity WHERE id=1").fetchone()
    if row is None:
        raise ValueError("Resource state has not been initialized.")
    return _uuid(row[0])


def _revision(value):
    return sha256(canonical(value).encode()).hexdigest()


def set_policy(db, document, expected_revision=None):
    current = policy(db)
    if expected_revision is not None and expected_revision != _revision(current):
        raise ValueError("The local resource policy changed; inspect it before replacing it.")
    checked = validate_policy(document)
    db.execute("INSERT OR REPLACE INTO resource_policy VALUES(1,?)", (canonical(checked),))
    # Running claims remain held even when an operator lowers a budget.
    return {"policy": checked, "revision": _revision(checked)}


def set_request(db, job_id, requirements):
    _transaction(db)
    job_id, checked = _uuid(job_id), validate_requirements(requirements)
    row = db.execute("SELECT requirements FROM resource_requests WHERE id=?", (job_id,)).fetchone()
    if row is not None and row[0] != canonical(checked):
        raise ValueError("This job UUID already declares different resources.")
    if row is None and db.execute("SELECT 1 FROM resource_claims WHERE id=?", (job_id,)).fetchone():
        raise ValueError("This job UUID already belongs to an existing resource claim.")
    db.execute("INSERT OR IGNORE INTO resource_requests VALUES(?,?)", (job_id, canonical(checked)))
    return checked


def request_for_job(db, job_id):
    _transaction(db)
    row = db.execute("SELECT requirements FROM resource_requests WHERE id=?", (_uuid(job_id),)).fetchone()
    return validate_requirements(json.loads(row[0])) if row else None


def worker_start(db, generation, platform=None, cleanup_confirmed_previous=False, now=None):
    _transaction(db)
    if platform is not None and platform not in {"linux", "windows"}:
        raise ValueError("Unsupported worker resource platform.")
    # A new generation never implicitly releases old claims, even after lock acquisition.
    if type(cleanup_confirmed_previous) is not bool:
        raise ValueError("Cleanup confirmation must be explicit.")
    generation, now = _uuid(generation), _clock(now)
    previous = db.execute("SELECT generation FROM resource_worker WHERE id=1").fetchone()
    if ((previous is not None and previous[0] == generation)
            or db.execute("SELECT 1 FROM resource_worker_stops WHERE generation=?", (generation,)).fetchone()):
        raise ValueError("Use a new UUID for each worker lifetime.")
    db.execute("INSERT OR REPLACE INTO resource_worker VALUES(1,?,?,?, ?,NULL)",
               (generation, PROTOCOL_VERSION, platform, now))
    return generation


def heartbeat(db, generation, observation=None, now=None):
    _transaction(db)
    generation, now = _uuid(generation), _clock(now)
    if db.execute("SELECT 1 FROM resource_worker_stops WHERE generation=?", (generation,)).fetchone():
        raise ValueError("A stopped worker generation cannot publish another heartbeat.")
    sample = None if observation is None else canonical(validate_observation(observation))
    changed = db.execute("UPDATE resource_worker SET heartbeat=?,observation=? WHERE id=1 AND generation=? AND protocol=?",
                         (now, sample, generation, PROTOCOL_VERSION))
    if not changed.rowcount:
        raise ValueError("This worker generation no longer owns resource admission.")


def worker_stopped(db, generation, now=None):
    """Retire the current owner immediately; reservations are not cleanup proof."""
    _transaction(db)
    generation, now = _uuid(generation), _clock(now)
    current = db.execute("SELECT generation FROM resource_worker WHERE id=1").fetchone()
    if current is None or current[0] != generation:
        raise ValueError("Only the current worker generation may retire itself.")
    db.execute("INSERT OR IGNORE INTO resource_worker_stops VALUES(?,?)", (generation, now))
    return True


def _claims(db):
    rows = db.execute("SELECT id,generation,requirements,legacy,exclusive,created,released "
                      "FROM resource_claims WHERE released IS NULL ORDER BY created,id").fetchall()
    return [{"job_id": row[0], "generation": row[1],
             "requirements": validate_requirements(json.loads(row[2])), "legacy": bool(row[3]),
             "exclusive": bool(row[4]), "created": row[5]} for row in rows]


def _fresh(timestamp, now):
    return 0 <= now - timestamp <= FRESH_SECONDS


def _remaining(timestamp, now):
    return float(FRESH_SECONDS - (now - timestamp)) if _fresh(timestamp, now) else 0.0


def status(db, now=None):
    value, now = policy(db), _clock(now)
    row = db.execute("SELECT generation,protocol,platform,heartbeat,observation FROM resource_worker WHERE id=1").fetchone()
    worker = None
    if row:
        stopped = db.execute("SELECT 1 FROM resource_worker_stops WHERE generation=?", (row[0],)).fetchone() is not None
        observation = None if row[4] is None else validate_observation(json.loads(row[4]))
        worker = {"generation": row[0], "protocol_version": row[1], "platform": row[2],
                  "heartbeat_at": row[3], "stopped": stopped,
                  "fresh": not stopped and row[1] == PROTOCOL_VERSION and _fresh(row[3], now),
                  "fresh_remaining_seconds": _remaining(row[3], now)
                  if not stopped and row[1] == PROTOCOL_VERSION else 0.0,
                  "observation": observation,
                  "observation_fresh": not stopped and observation is not None and _fresh(observation["observed_at"], now),
                  "observation_remaining_seconds": _remaining(observation["observed_at"], now)
                  if not stopped and observation is not None else 0.0}
    claims = _claims(db)
    used = {"jobs": len(claims), "cpu_millis": sum(c["requirements"]["cpu_millis"] for c in claims),
            "memory_bytes": sum(c["requirements"]["memory_bytes"] for c in claims), "gpus": {}}
    for claim in claims:
        for gpu in claim["requirements"]["gpus"]:
            total = used["gpus"].setdefault(gpu["id"], {"vram_bytes": 0, "exclusive": False})
            total["vram_bytes"] += gpu["vram_bytes"]
            total["exclusive"] |= gpu["exclusive"]
    counts = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
        counts = dict(db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state").fetchall())
    return {"protocol_version": PROTOCOL_VERSION, "state_id": state_id(db), "policy": value, "policy_revision": _revision(value),
            "worker": worker, "claims": claims, "declared_consumption": used,
            "job_counts": counts, "admission_only": True, "hard_limits_enforced": dict(HARD_LIMITS)}


def _eligibility_reason(snapshot, requirements):
    if snapshot.get("protocol_version") != PROTOCOL_VERSION:
        return "unsupported_resource_protocol"
    worker, limits = snapshot["worker"], snapshot["policy"]
    if worker is None or worker["fresh"] is not True or worker["protocol_version"] != PROTOCOL_VERSION:
        return "worker_unavailable"
    needs_observation = requirements["cpu_millis"] or requirements["memory_bytes"] or requirements["gpus"]
    if not needs_observation:
        return None
    if worker["observation_fresh"] is not True:
        return "observation_unavailable"
    observed = validate_observation(worker["observation"])
    for name, observed_name in (("cpu_millis", "cpu_millis"), ("memory_bytes", "memory_total_bytes")):
        if requirements[name]:
            if observed[observed_name] is None:
                return name + "_unavailable"
            budget = observed[observed_name] if limits[name] is None else limits[name]
            if requirements[name] > min(budget, observed[observed_name]):
                return name + "_request_exceeds_total"
    devices = {gpu["id"]: gpu for gpu in observed["gpus"] or []}
    budgets = _gpu_budgets(limits, devices)
    for gpu in requirements["gpus"]:
        device = devices.get(gpu["id"])
        if device is None or gpu["id"] not in budgets or device["vram_total_bytes"] is None:
            return "gpu_unavailable"
        if gpu["vram_bytes"] > min(budgets[gpu["id"]], device["vram_total_bytes"]):
            return "gpu_request_exceeds_total"
    return None


def _gpu_budgets(limits, devices):
    if limits["gpus"] is None:
        return {identity: gpu["vram_total_bytes"] for identity, gpu in devices.items()
                if gpu["vram_total_bytes"] is not None}
    return {gpu["id"]: gpu["vram_bytes"] for gpu in limits["gpus"]}


def evaluate(snapshot, requirements):
    """Pure advisory placement check. It neither reserves nor authorizes a job."""
    checked = validate_requirements(requirements)
    reason = _eligibility_reason(snapshot, checked)
    if reason is not None:
        return {"eligible": False, "admissible_now": False, "reason": reason}
    reason = _capacity_reason(snapshot, checked, False, snapshot["worker"]["generation"])
    return {"eligible": True, "admissible_now": reason is None, "reason": reason}


def _capacity_reason(snapshot, requirements, exclusive, generation):
    worker, limits, used = snapshot["worker"], snapshot["policy"], snapshot["declared_consumption"]
    if worker is None or not worker["fresh"] or worker["generation"] != generation:
        return "worker_unavailable"
    reason = _eligibility_reason(snapshot, requirements)
    if reason is not None:
        return reason
    claims = snapshot["claims"]
    if any(claim["generation"] != generation for claim in claims):
        return "previous_cleanup_unconfirmed"
    if used["jobs"] >= limits["max_jobs"]:
        return "concurrency_limit"
    if claims and (exclusive or any(claim["exclusive"] for claim in claims)):
        return "legacy_exclusive"
    needs_observation = requirements["cpu_millis"] or requirements["memory_bytes"] or requirements["gpus"]
    if not needs_observation:
        return None
    if not worker["observation_fresh"]:
        return "observation_unavailable"
    observed = worker["observation"]
    for name, observed_name in (("cpu_millis", "cpu_millis"), ("memory_bytes", "memory_available_bytes")):
        if not requirements[name]:
            continue
        if observed[observed_name] is None:
            return name + "_unavailable"
        # Conservatively charge every held promise against observed available RAM;
        # observations do not identify which allocations already belong to our jobs.
        budget = observed[observed_name] if limits[name] is None else limits[name]
        if used[name] + requirements[name] > min(budget, observed[observed_name]):
            return name + "_limit"
    devices = {gpu["id"]: gpu for gpu in observed["gpus"] or []}
    budgets = _gpu_budgets(limits, devices)
    for gpu in requirements["gpus"]:
        identity = gpu["id"]
        device = devices.get(identity)
        if identity not in budgets or device is None or device["vram_available_bytes"] is None:
            return "gpu_unavailable"
        previous = used["gpus"].get(identity)
        if previous and (gpu["exclusive"] or previous["exclusive"]):
            return "gpu_exclusive"
        reserved = previous["vram_bytes"] if previous else 0
        if reserved + gpu["vram_bytes"] > min(budgets[identity], device["vram_available_bytes"]):
            return "gpu_vram_limit"
    return None


def admission(db, job_id, generation, requirements=None, now=None):
    job_id, generation = _uuid(job_id), _uuid(generation)
    snapshot = status(db, now)
    saved = request_for_job(db, job_id)
    if requirements is not None:
        requirements = validate_requirements(requirements)
        if saved is not None and saved != requirements:
            raise ValueError("The job's declared resources changed.")
    else:
        requirements = saved
    legacy = requirements is None
    exclusive = legacy and snapshot["policy"]["legacy_defaults"] is None
    if legacy:
        requirements = snapshot["policy"]["legacy_defaults"] or {"cpu_millis": 0, "memory_bytes": 0, "gpus": []}
    existing = db.execute("SELECT generation,requirements,legacy,exclusive,released FROM resource_claims WHERE id=?", (job_id,)).fetchone()
    if existing:
        if legacy and bool(existing[2]):
            # An owner changing legacy defaults cannot rewrite a held job's promise.
            requirements, exclusive = json.loads(existing[1]), bool(existing[3])
        if (existing[0] != generation or json.loads(existing[1]) != requirements
                or bool(existing[2]) != legacy or bool(existing[3]) != exclusive):
            raise ValueError("The job UUID already belongs to another resource claim.")
        worker = snapshot["worker"]
        reason = "already_released" if existing[4] is not None else "worker_unavailable" if (
            worker is None or not worker["fresh"] or worker["generation"] != generation) else None
        return {"admitted": reason is None, "reason": reason,
                "requirements": requirements, "legacy": legacy, "exclusive": exclusive}
    reason = _capacity_reason(snapshot, requirements, exclusive, generation)
    return {"admitted": reason is None, "reason": reason, "requirements": requirements,
            "legacy": legacy, "exclusive": exclusive}


def try_claim(db, job_id, generation, requirements=None, now=None):
    now = _clock(now)
    result = admission(db, job_id, generation, requirements, now)
    if not result["admitted"]:
        return False
    if requirements is not None:
        set_request(db, job_id, requirements)
    db.execute("INSERT OR IGNORE INTO resource_claims VALUES(?,?,?,?,?,?,NULL,?)",
               (job_id, generation, canonical(result["requirements"]), int(result["legacy"]),
                int(result["exclusive"]), now, str(uuid4())))
    return True


def finish(db, job_id, generation, cleanup_confirmed, now=None):
    _transaction(db)
    job_id, generation = _uuid(job_id), _uuid(generation)
    if type(cleanup_confirmed) is not bool:
        raise ValueError("Cleanup confirmation must be an explicit boolean.")
    row = db.execute("SELECT generation,released FROM resource_claims WHERE id=?", (job_id,)).fetchone()
    if row is None or row[0] != generation:
        raise ValueError("Unknown job claim or worker generation.")
    if not cleanup_confirmed:
        return False
    if row[1] is None:
        current = db.execute("SELECT generation FROM resource_worker WHERE id=1").fetchone()
        if current is None or current[0] != generation:
            raise ValueError("A previous worker claim requires its scoped guardian cleanup receipt.")
        db.execute("UPDATE resource_claims SET released=? WHERE id=? AND generation=? AND released IS NULL",
                   (_clock(now), job_id, generation))
    return True


def claim_nonce(db, job_id, generation):
    """Owner-internal guardian binding; never include it in MCP status."""
    _transaction(db)
    row = db.execute("SELECT cleanup_nonce FROM resource_claims WHERE id=? AND generation=?",
                     (_uuid(job_id), _uuid(generation))).fetchone()
    if row is None:
        raise ValueError("Unknown guardian resource claim.")
    return row[0]


def reconcile(db, receipt, now=None):
    """Verify scoped metadata AFTER the owner verifies its private receipt file.

    This does not authenticate a file or prove process death by itself. The
    guardian must emit its receipt only after confirming descendant cleanup.
    """
    fields = {"protocol_version", "job_id", "generation", "cleanup_nonce", "cleanup_confirmed"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt["protocol_version"]) is not int or receipt["protocol_version"] != PROTOCOL_VERSION
            or receipt["cleanup_confirmed"] is not True):
        raise ValueError("Invalid confirmed guardian cleanup receipt.")
    expected = claim_nonce(db, receipt["job_id"], receipt["generation"])
    if receipt["cleanup_nonce"] != expected:
        raise ValueError("The cleanup receipt does not bind to the original guardian.")
    db.execute("UPDATE resource_claims SET released=? WHERE id=? AND generation=? AND released IS NULL",
               (_clock(now), receipt["job_id"], receipt["generation"]))
    return True
