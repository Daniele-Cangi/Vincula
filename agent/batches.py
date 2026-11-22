# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Immutable assignment plans stored on the node that submits a batch."""

from collections import Counter
import json
from uuid import uuid5

import jobs


def make_plan(batch_id, origin, request, catalogs):
    """Assign unpinned tasks to the eligible node with the fewest planned tasks."""
    assigned = Counter(task["node_id"] for task in request if task["node_id"] is not None)
    plan = []
    for index, task in enumerate(request):
        node, program = task["node_id"], task["program"]
        if node is None:
            eligible = [node for node, programs in catalogs.items() if program in programs]
            if not eligible:
                raise ValueError(f"No reachable node registers program '{program}' for task {index}.")
            node = min(eligible, key=lambda candidate: (assigned[candidate], candidate))
            assigned[node] += 1
        plan.append({"index": index, "node_id": node, "program": program,
                     "job_id": str(uuid5(batch_id, f"{origin}:{index}"))})
    return plan


def load(batch_id, request=None):
    with jobs.transaction() as db:
        row = db.execute("SELECT request, plan FROM batches WHERE id=?", (str(batch_id),)).fetchone()
    if row is None:
        return None
    if request is not None and json.loads(row["request"]) != request:
        raise ValueError("Batch ID already used for a different task list.")
    return json.loads(row["plan"])


def save(batch_id, request, plan):
    with jobs.transaction() as db:
        own = db.execute("SELECT 1 FROM batches WHERE id=?", (str(batch_id),)).fetchone()
        if own is None and jobs.plan_journal.get(db, "batch", str(batch_id)) is not None:
            raise ValueError("Batch ID belongs to a replicated plan. Use get_batch to read its original jobs.")
        db.execute("INSERT OR IGNORE INTO batches VALUES(?,?,?)",
                   (str(batch_id), json.dumps(request), json.dumps(plan)))
    # A concurrent submission may have stored its plan first.
    return load(batch_id, request)


def manifest(batch_id, origin):
    """Wrap a legacy local plan while retaining any already-published origin."""
    with jobs.transaction() as db:
        row = db.execute("SELECT request,plan FROM batches WHERE id=?", (str(batch_id),)).fetchone()
        if row is None:
            return None
        request, plan = json.loads(row["request"]), json.loads(row["plan"])
        existing = jobs.plan_journal.get(db, "batch", str(batch_id))
    if existing is not None:
        saved = jobs.plan_journal.unpack(existing)["manifest"]
        if saved["request"] != request or saved["plan"] != plan:
            raise ValueError("Local batch plan conflicts with its published manifest.")
        return saved
    return jobs.plan_journal.package({
        "version": 1, "kind": "batch", "id": str(batch_id), "origin": origin,
        "request": request, "plan": plan,
    })["manifest"]


def select_replica(batch_id, records):
    """Choose only a consistent set of validated replicas for this batch UUID."""
    selected = None
    for record in records:
        if record is None:
            continue
        checked = jobs.plan_journal.unpack(record)
        if checked["manifest"]["kind"] != "batch":
            raise ValueError("A replica returned another plan kind instead of a batch.")
        if checked["manifest"]["id"] != str(batch_id):
            raise ValueError("A replica returned another batch UUID.")
        if selected is not None and checked["sha256"] != selected["sha256"]:
            raise ValueError("Conflicting batch manifests were found on reachable nodes.")
        selected = checked
    return selected["manifest"] if selected else None
