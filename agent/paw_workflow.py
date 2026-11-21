# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Origin-side planning and reconciliation for the offline PAW capability."""

import asyncio
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import uuid5

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, FiniteFloat, StrictInt, TypeAdapter

import jobs


class PawResult(BaseModel):
    index: StrictInt
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    score: FiniteFloat
    decision: str
    seal_verified: Literal[True]


RESULT_ROWS = TypeAdapter(list[PawResult])


def load(analysis_id, request=None):
    with jobs.transaction() as db:
        row = db.execute("SELECT request,plan FROM paw_plans WHERE id=?", (str(analysis_id),)).fetchone()
    if row is None:
        return None
    if request is not None and json.loads(row["request"]) != request:
        raise ValueError("Analysis ID already used for a different plan.")
    return json.loads(row["plan"])


def validate_request(request):
    nodes, seen = set(), set()
    for part in request:
        if part["node_id"] in nodes:
            raise ValueError("Specify at most one part per node.")
        nodes.add(part["node_id"])
        for item in part["files"]:
            key = (item["node_id"], item["root"], item["path"])
            if key in seen or PurePosixPath(item["path"]).suffix.lower() != ".eml":
                raise ValueError("Choose distinct EML files for the analysis.")
            if len(item["path"].encode("utf-8")) > 240:
                raise ValueError("A PAW input path exceeds 240 UTF-8 bytes.")
            jobs.files.components(item["path"])
            seen.add(key)


def make_plan(analysis_id, request, providers, origin):
    if any(not providers[part["node_id"]] for part in request):
        raise ValueError("An assigned node has no available PAW capability.")
    releases = {providers[part["node_id"]]["paw_version"] for part in request}
    if len(releases) != 1:
        raise ValueError("Use the same registered PAW version on the assigned nodes.")
    plan, index = [], 0
    for piece in request:
        node = piece["node_id"]
        provider = providers[node]
        plan.append({"node_id": node, "job_id": str(uuid5(analysis_id, f"paw:{origin}:{node}")),
                     "revision": provider["revision"], "inbox": provider["inbox"],
                     "inputs": [{"index": index + pos, "source": file} for pos, file in enumerate(piece["files"])]})
        index += len(piece["files"])
    with jobs.transaction() as db:
        db.execute("INSERT OR IGNORE INTO paw_plans VALUES(?,?,?)",
                   (str(analysis_id), json.dumps(request), json.dumps(plan)))
    return load(analysis_id, request)


async def node_call(dispatch, node, operation, **params):
    return await asyncio.to_thread(dispatch, node, {"action": "paw", "operation": operation, **params})


async def prepare_input(analysis_id, part, item, transfer):
    source, index = item["source"], item["index"]
    assigned = {"index": index, "root": source["root"], "path": source["path"]}
    if source["node_id"] == part["node_id"]:
        return assigned, None
    transfer_id = uuid5(analysis_id, f"paw-input:{part['job_id']}:{index}")
    destination = {"node_id": part["node_id"], "root": part["inbox"], "path": f"paw-{transfer_id.hex}.eml"}
    receipt = await asyncio.to_thread(transfer, transfer_id, source, destination)
    end = receipt["destination"]
    if any(endpoint and endpoint["state"] == "denied" for endpoint in (receipt["source"], end)):
        raise ValueError("A required PAW transfer was denied.")
    if end["state"] != "completed":
        return None, receipt
    return dict(assigned, root=destination["root"], path=destination["path"],
                sha256=end["result"]["sha256"], source=source), None


def result_rows(details, part):
    if details["state"] != "completed" or details["exit_code"] != 0:
        return None
    if details["stdout"]["truncated"]:
        raise ValueError("PAW report output is incomplete.")
    try:
        rows = RESULT_ROWS.validate_json(details["stdout"]["text"])
    except ValueError:
        raise ValueError("PAW report contains invalid result data.") from None
    if [row.index for row in rows] != [item["index"] for item in part["inputs"]]:
        raise ValueError("PAW report does not match the assigned files.")
    return rows


async def reconcile(analysis_id, plan, dispatch, transfer=None):
    async def process(part):
        node, job_id = part["node_id"], part["job_id"]
        report = {"node_id": node, "job_id": job_id, "job": None, "waiting_transfers": [],
                  "results": None, "error": None}
        try:
            details = await node_call(dispatch, node, "read", job_id=job_id)
            if details is not None:
                report["job"] = details
                report["results"] = result_rows(details, part)
            elif transfer:
                # Finish staging compatible parts independently, without opening either node's shell.
                ready = [await prepare_input(analysis_id, part, item, transfer) for item in part["inputs"]]
                waiting = [receipt for _, receipt in ready if receipt is not None]
                report["waiting_transfers"] = waiting
                if not waiting:
                    report["job"] = await node_call(dispatch, node, "queue", job_id=job_id,
                                                    revision=part["revision"], inputs=[item for item, _ in ready])
        except (ToolError, ValueError) as error:
            report["error"] = str(error)
        return report

    parts = await asyncio.gather(*(process(part) for part in plan))
    results = None
    if all(part["results"] is not None for part in parts):
        results = sorted((row for part in parts for row in part["results"]), key=lambda row: row.index)
    return {"analysis_id": analysis_id, "capability": jobs.paw.CAPABILITY, "parts": parts, "results": results}
