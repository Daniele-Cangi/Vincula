# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Origin-side planning, input transfers and result collection."""

import asyncio
import json
from uuid import uuid5

from mcp.server.mcpserver.exceptions import ToolError

import jobs

capabilities = jobs.capabilities


def load(run_id, request=None):
    with jobs.transaction() as db:
        row = db.execute("SELECT request,plan FROM capability_plans WHERE id=?", (str(run_id),)).fetchone()
    if row is None:
        return None
    if request is not None and row["request"] != capabilities.encode(request):
        raise ValueError("Run ID already used for a different capability plan.")
    return json.loads(row["plan"])


def manifest(run_id, origin):
    with jobs.transaction() as db:
        row = db.execute("SELECT request,plan FROM capability_plans WHERE id=?", (str(run_id),)).fetchone()
        if row is None:
            return None
        request, plan = json.loads(row["request"]), json.loads(row["plan"])
        existing = jobs.plan_journal.get(db, "capability", str(run_id))
    if existing is not None:
        saved = jobs.plan_journal.unpack(existing)["manifest"]
        if saved["request"] != request or saved["plan"] != plan:
            raise ValueError("Local capability plan conflicts with its published manifest.")
        return saved
    return jobs.plan_journal.package({
        "version": 1, "kind": "capability", "id": str(run_id), "origin": origin,
        "request": request, "plan": plan,
    })["manifest"]


def make_plan(run_id, request, providers, origin, resolve=None):
    parts = request["parts"]
    if resolve is not None:
        parts = [
            dict(part, node_id=resolve(part["node_id"]).node_id,
                 files=[dict(item, node_id=resolve(item["node_id"]).node_id) for item in part["files"]])
            for part in parts
        ]
    nodes = {part["node_id"] for part in parts}
    if len(nodes) != len(parts):
        raise ValueError("Specify at most one capability part per node.")
    if any(providers[node] is None for node in nodes):
        raise ValueError("The requested capability is unavailable on an assigned node.")
    version, contract = providers[parts[0]["node_id"]]["version"], providers[parts[0]["node_id"]]["contract"]
    if any((providers[node]["version"], providers[node]["contract"]) != (version, contract) for node in nodes):
        raise ValueError("The assigned nodes must offer the same capability version and contract.")

    plan = {"capability_id": request["capability_id"], "parts": []}
    index = 0
    for part in parts:
        node, inputs, parameters = part["node_id"], part["files"], part["parameters"]
        provider = providers[node]
        capabilities.validate_paths(jobs.files, inputs, contract)
        capabilities.validate_parameters(parameters, contract["parameters"])
        if provider["inbox"] is None and any(item["node_id"] != node for item in inputs):
            raise ValueError("The assigned node needs an inbox for files coming from its peer.")
        plan["parts"].append({
            "node_id": node, "job_id": str(uuid5(run_id, f"capability:{origin}:{node}")),
            "revision": provider["revision"], "inbox": provider["inbox"], "parameters": parameters,
            "inputs": [{"index": index + position, "source": source} for position, source in enumerate(inputs)],
        })
        index += len(inputs)
    with jobs.transaction() as db:
        own = db.execute("SELECT 1 FROM capability_plans WHERE id=?", (str(run_id),)).fetchone()
        if own is None and jobs.plan_journal.get(db, "capability", str(run_id)) is not None:
            raise ValueError("Run ID belongs to a replicated plan. Use get_capability_run to read its original jobs.")
        db.execute("INSERT OR IGNORE INTO capability_plans VALUES(?,?,?)",
                   (str(run_id), capabilities.encode(request), capabilities.encode(plan)))
    return load(run_id, request)


async def node_call(dispatch, node, operation, **params):
    return await asyncio.to_thread(dispatch, node, {"action": "capability", "operation": operation, **params})


async def prepare_input(run_id, part, item, transfer):
    source, index = item["source"], item["index"]
    assigned = {"index": index, "root": source["root"], "path": source["path"]}
    if source["node_id"] == part["node_id"]:
        return assigned, None
    transfer_id, source, destination = input_transfer(run_id, part, item)
    receipt = await asyncio.to_thread(transfer, transfer_id, source, destination)
    end = receipt["destination"]
    if any(endpoint and endpoint["state"] == "denied" for endpoint in (receipt["source"], end)):
        raise ValueError("A required capability transfer was denied.")
    if end["state"] != "completed":
        return None, receipt
    return dict(assigned, root=destination["root"], path=destination["path"], sha256=end["result"]["sha256"]), None


def input_transfer(run_id, part, item):
    transfer_id = uuid5(run_id, f"capability-input:{part['job_id']}:{item['index']}")
    source = item["source"]
    destination = {"node_id": part["node_id"], "root": part["inbox"],
                   "path": f"capability-{transfer_id.hex}{capabilities.suffix(source['path'])}"}
    return transfer_id, source, destination


async def inspect_inputs(run_id, part, inspect_transfer):
    waiting, missing = [], False
    for item in part["inputs"]:
        if item["source"]["node_id"] == part["node_id"]:
            continue
        receipt = await asyncio.to_thread(inspect_transfer, *input_transfer(run_id, part, item))
        if receipt is None:
            missing = True
        elif receipt["destination"]["state"] != "completed":
            waiting.append(receipt)
    return waiting, missing


async def inspect_unqueued(run_id, capability_id, part, report, dispatch, inspect_transfer):
    provider = await node_call(dispatch, part["node_id"], "describe", capability_id=capability_id)
    if provider is None or provider["revision"] != part["revision"]:
        raise ValueError("Capability registration changed or was revoked before this part was queued.")
    waiting, missing = await inspect_inputs(run_id, part, inspect_transfer)
    report["waiting_transfers"] = waiting
    if any(endpoint and endpoint["state"] == "denied" for receipt in waiting
           for endpoint in (receipt["source"], receipt["destination"])):
        raise ValueError("A required capability transfer was denied.")
    if missing:
        raise ValueError("A required input transfer has not been prepared.")
    if waiting:
        raise ValueError("Required input transfers are awaiting approval or completion.")
    raise ValueError("The capability part has not been queued.")


def result_envelope(details):
    if details["state"] != "completed" or details["exit_code"] != 0:
        return None
    if details["stdout"]["truncated"]:
        raise ValueError("Adapter result output is incomplete.")
    try:
        output = json.loads(details["stdout"]["text"])
        if isinstance(output, dict) and output.keys() == {"value"}:
            if len(capabilities.encode(output)) <= capabilities.MAX_RESULT_BYTES:
                return output
    except (ValueError, RecursionError):
        pass
    raise ValueError("Adapter result contains invalid JSON data.")


async def reconcile(run_id, plan, dispatch, transfer=None, *, inspect_transfer=None, publication_errors=None):
    capability_id = plan["capability_id"]

    async def process(part):
        node, job_id = part["node_id"], part["job_id"]
        report = {"node_id": node, "job_id": job_id, "job": None, "waiting_transfers": [],
                  "output": None, "error": None}
        if publication_errors and node in publication_errors:
            report["error"] = publication_errors[node]
            return report
        try:
            details = await node_call(dispatch, node, "read", job_id=job_id, capability_id=capability_id)
            if details is not None:
                report["job"] = details
                report["output"] = result_envelope(details)
            elif transfer is not None:
                # Stage parts independently so an approval on one node cannot block the other node.
                ready = [await prepare_input(run_id, part, item, transfer) for item in part["inputs"]]
                report["waiting_transfers"] = [receipt for _, receipt in ready if receipt is not None]
                if not report["waiting_transfers"]:
                    report["job"] = await node_call(dispatch, node, "queue", job_id=job_id,
                                                    capability_id=capability_id, revision=part["revision"],
                                                    inputs=[item for item, _ in ready],
                                                    parameters=part["parameters"])
            elif inspect_transfer is not None:
                await inspect_unqueued(run_id, capability_id, part, report, dispatch, inspect_transfer)
        except (ToolError, ValueError) as error:
            report["error"] = str(error)
        return report

    parts = await asyncio.gather(*(process(part) for part in plan["parts"]))
    return {"run_id": run_id, "capability_id": capability_id, "parts": parts}
