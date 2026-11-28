# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure, immutable resource-aware plans for native process requests.

Observations and costs are advisory. Only a destination can authorize, resolve
and claim a process; this module never reserves resources or transfers files.
The saved origin is the plan's stable authority, not the client replaying it.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from uuid import UUID, uuid5

import direct_execution as direct
import resource_policy as resources

MAX_TASKS = 12
MAX_CANDIDATES = 64
MAX_REQUEST_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
SELECTOR = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
DIGEST = re.compile(r"[0-9a-f]{64}")
PROCESS_FIELDS = {"argv", "cwd", "env", "stdin_text", "timeout_seconds", "resources"}
PART_FIELDS = {"index", "candidate_index", "node_id", "job_id", "process", "expected_state_id", "decision"}
DECISION_FIELDS = {"advisory", "policy_revision", "worker_generation", "worker_platform", "cpu_percent",
                   "snapshot_admissible_now", "projected_admissible_now", "reason", "held_jobs", "queued_jobs",
                   "planned_jobs", "parallel_slots", "compute_seconds", "setup_seconds", "queue_seconds",
                   "planned_seconds", "busy_seconds", "rpc_seconds", "control_seconds", "score_seconds"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _bounded(value, limit):
    try:
        raw = canonical(value)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("Execution plans must contain finite JSON values.") from None
    _require(len(raw.encode("utf-8")) <= limit, "Execution plan JSON exceeds its size limit.")
    return json.loads(raw)


def _identity(value):
    if isinstance(value, UUID):
        return str(value)
    try:
        _require(isinstance(value, str) and str(UUID(value)) == value, "Use a canonical UUID.")
    except (TypeError, AttributeError):
        raise ValueError("Use a canonical UUID.") from None
    return value


def _node(value):
    _require(isinstance(value, str) and SELECTOR.fullmatch(value) is not None, "Invalid execution node ID.")
    return value


def _number(value, *, positive=False):
    _require(type(value) in (int, float), "Execution cost estimates must be finite numbers.")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError("Execution cost estimate is too large.") from None
    _require(math.isfinite(value) and (value > 0 if positive else value >= 0),
             "Execution cost estimates must be finite and nonnegative; compute time must be positive.")
    return value


def _count(value):
    _require(type(value) is int and 0 <= value <= resources.MAX_INTEGER, "Invalid execution queue count.")
    return value


def _process(value, platform="portable"):
    _require(isinstance(value, dict) and {"argv", "resources"} <= value.keys() <= PROCESS_FIELDS,
             "Declare a native process and its resource requirements.")
    try:
        return direct.request(**value, protocol_version=2, _platform=platform)
    except TypeError:
        raise ValueError("Invalid native execution process request.") from None


def _cost_sum(values):
    try:
        return _number(math.fsum(values))
    except OverflowError:
        raise ValueError("Execution cost is too large to represent.") from None


def normalize_request(value):
    _require(isinstance(value, dict) and set(value) == {"tasks"}, "Declare independent execution tasks.")
    tasks = value["tasks"]
    _require(isinstance(tasks, list) and 1 <= len(tasks) <= MAX_TASKS, "Declare between 1 and 12 execution tasks.")
    result = []
    for task in tasks:
        _require(isinstance(task, dict) and set(task) == {"candidates"}, "Invalid execution task fields.")
        candidates = task["candidates"]
        _require(isinstance(candidates, list) and 1 <= len(candidates) <= MAX_CANDIDATES,
                 "Declare between 1 and 64 candidates for each task.")
        normalized = []
        for candidate in candidates:
            _require(isinstance(candidate, dict) and {"node_id", "process"} <= candidate.keys()
                     <= {"node_id", "process", "estimated_seconds", "setup_seconds"}, "Invalid execution candidate fields.")
            normalized.append({"node_id": _node(candidate["node_id"]), "process": _process(candidate["process"]),
                               "estimated_seconds": _number(candidate.get("estimated_seconds", 1), positive=True),
                               "setup_seconds": _number(candidate.get("setup_seconds", 0))})
        result.append({"candidates": normalized})
    return _bounded({"tasks": result}, MAX_REQUEST_BYTES)


def _snapshot(value):
    _require(isinstance(value, dict), "Missing resource observation.")
    result = deepcopy(value)
    result["request_round_trip_seconds"] = _number(result.get("request_round_trip_seconds", 0))
    _identity(result["state_id"])
    _require(isinstance(result["policy_revision"], str) and DIGEST.fullmatch(result["policy_revision"]),
             "Invalid resource policy revision.")
    result["policy"] = resources.validate_policy(result["policy"])
    worker = result["worker"]
    if worker is not None:
        _identity(worker["generation"])
        _require(worker["platform"] in ("linux", "windows"), "Unknown destination platform.")
        if worker.get("observation") is not None:
            worker["observation"] = resources.validate_observation(worker["observation"])
    used = result["declared_consumption"]
    for name in ("jobs", "cpu_millis", "memory_bytes"):
        _count(used[name])
    _require(isinstance(result["claims"], list) and isinstance(used["gpus"], dict), "Invalid held resource claims.")
    _require(isinstance(result["job_counts"], dict), "Invalid resource queue.")
    for count in result["job_counts"].values():
        _count(count)
    return result


def _parallel_slots(snapshot, demand):
    policy, worker = snapshot["policy"], snapshot["worker"]
    observed = worker["observation"] if worker["observation_fresh"] else None
    slots = policy["max_jobs"]
    for key, field in (("cpu_millis", "cpu_millis"), ("memory_bytes", "memory_total_bytes")):
        if demand[key]:
            capacity = observed[field]
            if policy[key] is not None:
                capacity = min(capacity, policy[key])
            slots = min(slots, capacity // demand[key])
    devices = {gpu["id"]: gpu for gpu in (observed or {}).get("gpus") or []}
    budgets = None if policy["gpus"] is None else {gpu["id"]: gpu["vram_bytes"] for gpu in policy["gpus"]}
    for gpu in demand["gpus"]:
        if gpu["exclusive"]:
            slots = min(slots, 1)
        if gpu["vram_bytes"]:
            capacity = devices[gpu["id"]]["vram_total_bytes"]
            if budgets is not None:
                capacity = min(capacity, budgets[gpu["id"]])
            slots = min(slots, capacity // gpu["vram_bytes"])
    return max(1, slots)


def _decision(candidate, snapshot, projected, planned_jobs, planned_work):
    demand, worker = candidate["process"]["resources"], snapshot["worker"]
    assessment = resources.evaluate(snapshot, demand)
    future = resources.evaluate(projected, demand)
    slots = _parallel_slots(snapshot, demand)
    observation = worker["observation"] if worker["observation_fresh"] else None
    load = (observation or {}).get("cpu_percent")
    compute = candidate["estimated_seconds"] * (1 + (load or 0) / 100)
    # Queue durations are unknown: charge candidate-sized work units. Planned
    # work is known only through caller estimates, never through reservations.
    held, queued = snapshot["declared_consumption"]["jobs"], snapshot["job_counts"].get("queued", 0)
    queue_cost, plan_cost = compute * (held + queued) / slots, planned_work / slots
    busy = 0.0 if future["admissible_now"] else compute
    rpc = snapshot["request_round_trip_seconds"]
    # Four control RPCs before queueing and two result reads: an advisory cost
    # heuristic, not a guaranteed call count or completion-time prediction.
    control = rpc * 6
    score = _cost_sum((candidate["setup_seconds"], compute, queue_cost, plan_cost, busy, control))
    return {"advisory": True, "policy_revision": snapshot["policy_revision"],
            "worker_generation": worker["generation"], "worker_platform": worker["platform"],
            "cpu_percent": load, "snapshot_admissible_now": assessment["admissible_now"],
            "projected_admissible_now": future["admissible_now"], "reason": future["reason"],
            "held_jobs": held, "queued_jobs": queued, "planned_jobs": planned_jobs, "parallel_slots": slots,
            "compute_seconds": compute, "setup_seconds": candidate["setup_seconds"], "queue_seconds": queue_cost,
            "planned_seconds": plan_cost, "busy_seconds": busy, "rpc_seconds": rpc,
            "control_seconds": control, "score_seconds": score}


def _project(snapshot, part):
    demand, used = part["process"]["resources"], snapshot["declared_consumption"]
    used["jobs"] += 1
    used["cpu_millis"] += demand["cpu_millis"]
    used["memory_bytes"] += demand["memory_bytes"]
    for gpu in demand["gpus"]:
        held = used["gpus"].setdefault(gpu["id"], {"vram_bytes": 0, "exclusive": False})
        held["vram_bytes"] += gpu["vram_bytes"]
        held["exclusive"] |= gpu["exclusive"]
    snapshot["claims"].append({"job_id": part["job_id"], "generation": snapshot["worker"]["generation"],
                               "requirements": deepcopy(demand), "legacy": False, "exclusive": False, "created": 0})


def make_manifest(plan_id, origin, request, snapshots):
    """Choose once; callers must publish the immutable plan before any effects."""
    plan_id, origin, request = _identity(plan_id), _node(origin), normalize_request(request)
    _require(isinstance(snapshots, dict), "Supply destination resource snapshots.")
    views, projected, counts, work = {}, {}, defaultdict(int), defaultdict(float)
    plan = []
    for index, task in enumerate(request["tasks"]):
        choices = []
        for candidate_index, candidate in enumerate(task["candidates"]):
            node = candidate["node_id"]
            try:
                if node not in views:
                    views[node] = _snapshot(snapshots.get(node))
                    projected[node] = deepcopy(views[node])
                snapshot = views[node]
                if not resources.evaluate(snapshot, candidate["process"]["resources"])["eligible"]:
                    continue
                _process(candidate["process"], snapshot["worker"]["platform"])
                decision = _decision(candidate, snapshot, projected[node], counts[node], work[node])
                choices.append((decision["score_seconds"], node != origin, node, candidate_index, decision))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        _require(bool(choices), f"Task {index} has no eligible observed execution candidate.")
        _, _, node, candidate_index, decision = min(choices, key=lambda choice: choice[:4])
        candidate = task["candidates"][candidate_index]
        part = {"index": index, "candidate_index": candidate_index, "node_id": node,
                "job_id": str(uuid5(UUID(plan_id), f"execution:{origin}:{index}")),
                "process": deepcopy(candidate["process"]), "expected_state_id": views[node]["state_id"], "decision": decision}
        plan.append(part)
        counts[node] += 1
        work[node] += decision["compute_seconds"] + decision["setup_seconds"]
        _project(projected[node], part)
    return validate_manifest({"version": 1, "kind": "execution", "id": plan_id, "origin": origin,
                              "request": request, "plan": plan})


def _same_cost(actual, expected):
    return math.isclose(_number(actual), expected, rel_tol=1e-12, abs_tol=1e-9)


def _validate_decision(value, candidate, planned_jobs, planned_work):
    _require(isinstance(value, dict) and set(value) == DECISION_FIELDS and value["advisory"] is True,
             "Invalid advisory execution decision.")
    _identity(value["worker_generation"])
    _require(isinstance(value["policy_revision"], str) and DIGEST.fullmatch(value["policy_revision"]),
             "Invalid advisory policy revision.")
    _require(value["worker_platform"] in ("linux", "windows"), "Unknown advisory destination platform.")
    for name in ("snapshot_admissible_now", "projected_admissible_now"):
        _require(type(value[name]) is bool, "Invalid advisory admission flag.")
    reason = value["reason"]
    _require(reason is None or isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,96}", reason),
             "Invalid advisory admission reason.")
    _require(value["projected_admissible_now"] == (reason is None), "Inconsistent advisory admission reason.")
    load = value["cpu_percent"]
    _require(load is None or 0 <= _number(load) <= 100, "Invalid advisory CPU load.")
    held, queued, slots = _count(value["held_jobs"]), _count(value["queued_jobs"]), _count(value["parallel_slots"])
    _require(1 <= slots <= resources.MAX_JOBS and type(value["planned_jobs"]) is int
             and value["planned_jobs"] == planned_jobs, "Invalid projected execution queue.")
    compute = candidate["estimated_seconds"] * (1 + (load or 0) / 100)
    expected = {"compute_seconds": compute, "setup_seconds": candidate["setup_seconds"],
                "queue_seconds": compute * (held + queued) / slots, "planned_seconds": planned_work / slots,
                "busy_seconds": 0.0 if value["projected_admissible_now"] else compute,
                "control_seconds": _number(value["rpc_seconds"]) * 6}
    expected["score_seconds"] = _cost_sum(expected.values())
    _require(all(_same_cost(value[name], cost) for name, cost in expected.items()), "Inconsistent advisory execution costs.")
    _process(candidate["process"], value["worker_platform"])


def validate_manifest(manifest):
    _require(isinstance(manifest, dict) and set(manifest) == {"version", "kind", "id", "origin", "request", "plan"},
             "Invalid execution manifest fields.")
    _require(type(manifest["version"]) is int and manifest["version"] == 1 and manifest["kind"] == "execution",
             "Unsupported execution manifest kind or version.")
    plan_id, origin = _identity(manifest["id"]), _node(manifest["origin"])
    request = normalize_request(manifest["request"])
    _require(canonical(request) == canonical(manifest["request"]), "Execution manifest request is not canonical.")
    plan = manifest["plan"]
    _require(isinstance(plan, list) and len(plan) == len(request["tasks"]), "Invalid execution manifest part count.")
    counts, work = defaultdict(int), defaultdict(float)
    for index, (part, task) in enumerate(zip(plan, request["tasks"])):
        _require(isinstance(part, dict) and set(part) == PART_FIELDS and type(part["index"]) is int
                 and part["index"] == index, "Invalid execution manifest part.")
        choice = part["candidate_index"]
        _require(type(choice) is int and 0 <= choice < len(task["candidates"]), "Invalid execution candidate index.")
        candidate = task["candidates"][choice]
        node = candidate["node_id"]
        process = _process(part["process"])
        _require(part["node_id"] == node and canonical(process) == canonical(candidate["process"])
                 and canonical(process) == canonical(part["process"]),
                 "Execution assignment differs from its immutable candidate.")
        _require(part["job_id"] == str(uuid5(UUID(plan_id), f"execution:{origin}:{index}")),
                 "Execution job UUID does not belong to its plan and origin.")
        _identity(part["expected_state_id"])
        _validate_decision(part["decision"], candidate, counts[node], work[node])
        counts[node] += 1
        work[node] += part["decision"]["compute_seconds"] + part["decision"]["setup_seconds"]
    return _bounded(manifest, MAX_MANIFEST_BYTES)


def select_replica(plan_id, records):
    plan_id, selected, digest = _identity(plan_id), None, None
    _require(isinstance(records, (list, tuple)) and len(records) <= MAX_CANDIDATES,
             "Supply at most 64 execution replica records.")
    for record in records:
        if record is None:
            continue
        _require(isinstance(record, dict) and set(record) == {"manifest", "sha256"}, "Invalid execution replica fields.")
        checked = validate_manifest(record["manifest"])
        actual = sha256(canonical(checked).encode("utf-8")).hexdigest()
        _require(record["sha256"] == actual and checked["id"] == plan_id, "Execution replica digest or UUID mismatch.")
        _require(digest is None or digest == actual, "Conflicting execution manifests on reachable nodes.")
        selected, digest = checked, actual
    return selected
