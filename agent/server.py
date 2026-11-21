# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Status and destination-authorized jobs for the two configured Vincula nodes."""

import asyncio
import base64
from collections import defaultdict
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Literal
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import batches
import jobs
import linux_status

dock_dir = Path(__file__).resolve().parents[1] / "outputs" / "Windows Dock"
sys.path.insert(0, str(dock_dir))
import windows_ssh

NodeId = Literal["ubuntu", "windows"]
Availability = Literal["available", "unavailable", "not_configured"]
NODE_IDS: tuple[NodeId, ...] = ("ubuntu", "windows")
PROBE_TIMEOUT = 12

mcp = MCPServer("vincula", instructions="Inspect Vincula nodes and manage their locally authorized programs.")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class Metrics(BaseModel):
    os: str
    os_version: str | None
    uptime_seconds: int = Field(ge=0)
    cpu_percent: float | None = Field(ge=0, le=100)
    memory_total_bytes: int = Field(gt=0)
    memory_available_bytes: int = Field(ge=0)


class Node(BaseModel):
    node_id: NodeId
    local: bool
    availability: Availability


class Inventory(BaseModel):
    nodes: list[Node]


class NodeStatus(Node):
    metrics: Metrics | None


def local_node() -> NodeId:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        return "ubuntu"
    raise RuntimeError("Vincula MCP runs on Windows or Linux.")


def linux_ssh_config() -> Path:
    app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return app_data / "Linux Dock" / "ssh_config"


def peer_configured(node_id: NodeId) -> bool:
    if node_id == "windows":
        return windows_ssh.HOST_KEY.is_file() and windows_ssh.IDENTITY.is_file()
    return linux_ssh_config().is_file()


def windows_ssh_env() -> dict[str, str]:
    env = os.environ.copy()
    if env.get("PROGRAMDATA"):
        return env
    # Some MCP clients filter the environment. OpenSSH still needs ProgramData.
    from ctypes import byref, c_ubyte, c_wchar_p, windll

    program_data_id = UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97")
    folder_id = (c_ubyte * 16).from_buffer_copy(program_data_id.bytes_le)
    path = c_wchar_p()
    try:
        if windll.shell32.SHGetKnownFolderPath(byref(folder_id), 0, None, byref(path)) != 0:
            raise OSError("Could not locate Windows ProgramData.")
        env["PROGRAMDATA"] = path.value
    finally:
        windll.ole32.CoTaskMemFree(path)
    return env


def remote_linux_status() -> dict:
    probe = base64.b64encode(Path(linux_status.__file__).read_bytes()).decode("ascii")
    return linux_ssh_json(f"printf %s {probe} | base64 -d | python3 -IB -")


def linux_ssh_json(command: str) -> dict:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    ssh = system_root / "System32" / "OpenSSH" / "ssh.exe"
    # Windows OpenSSH can leave a captured pipe open after delivering its output.
    with TemporaryFile() as output, TemporaryFile() as errors:
        subprocess.run(
            [ssh, "-F", linux_ssh_config(), "-nT", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
             "-o", "ConnectTimeout=6", "-o", "ClearAllForwardings=yes",
             "linux-dock", command],
            stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW, env=windows_ssh_env(),
            timeout=PROBE_TIMEOUT, check=True,
        )
        output.seek(0)
        return json.loads(output.read(128 * 1024))


def windows_metrics(is_local: bool) -> Metrics:
    read = windows_ssh.local_status if is_local else windows_ssh.status
    status = read(timeout=PROBE_TIMEOUT)
    return Metrics(
        os=status["os"], os_version=status["build"], uptime_seconds=status["uptime"],
        cpu_percent=status["cpu"], memory_total_bytes=status["ram_total"],
        memory_available_bytes=status["ram_free"],
    )


def node_status(node_id: NodeId, origin: NodeId) -> NodeStatus:
    is_local = node_id == origin
    try:
        if not is_local and not peer_configured(node_id):
            return NodeStatus(node_id=node_id, local=False, availability="not_configured", metrics=None)
        if node_id == "windows":
            metrics = windows_metrics(is_local)
        else:
            read = linux_status.read_status if is_local else remote_linux_status
            metrics = Metrics.model_validate(read())
    except (windows_ssh.LinkError, OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        # Keep private paths, account names and SSH diagnostics off the MCP connection.
        return NodeStatus(node_id=node_id, local=is_local, availability="unavailable", metrics=None)
    return NodeStatus(node_id=node_id, local=is_local, availability="available", metrics=metrics)


@mcp.tool(annotations=READ_ONLY)
async def list_nodes() -> Inventory:
    """List the two known nodes, marking which one is local and whether each status probe succeeds."""
    origin = local_node()
    results = await asyncio.gather(*(asyncio.to_thread(node_status, node_id, origin) for node_id in NODE_IDS))
    return Inventory(nodes=[
        Node(node_id=result.node_id, local=result.local, availability=result.availability)
        for result in results
    ])


@mcp.tool(annotations=READ_ONLY)
async def get_node_status(node_id: NodeId) -> NodeStatus:
    """Read OS, uptime, CPU and physical memory on the requested node."""
    return await asyncio.to_thread(node_status, node_id, local_node())


class Job(BaseModel):
    id: UUID
    program: str
    state: Literal["pending", "queued", "running", "stopping", "completed", "cancelled", "failed", "unconfirmed"]
    exit_code: int | None
    created: float
    finished: float | None


class LogTail(BaseModel):
    text: str
    truncated: bool


class JobDetails(Job):
    stdout: LogTail
    stderr: LogTail


class JobCatalog(BaseModel):
    mode: Literal["ask", "full"]
    programs: list[str]
    jobs: list[Job]


def destination_job(node_id, action, job_id=None, program=None):
    return destination_request(
        node_id, {"action": action, "job_id": str(job_id) if job_id else None, "program": program},
    )


def destination_request(node_id, request):
    if node_id == local_node():
        try:
            return jobs.dispatch(**request)
        except ValueError as error:
            raise ToolError(str(error)) from None
    payload = base64.b64encode(json.dumps(request).encode()).decode("ascii")
    try:
        if node_id == "ubuntu":
            reply = linux_ssh_json(f'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" rpc {payload}')
        else:
            script = (
                "$helper = Join-Path $env:LOCALAPPDATA 'Vincula-Jobs/jobs.py'\n"
                f"& py.exe -3 -IB $helper rpc '{payload}'\nexit $LASTEXITCODE"
            )
            response = windows_ssh._powershell(script, timeout=PROBE_TIMEOUT)
            windows_ssh._require_success(response)
            reply = json.loads(response.stdout)
    except (windows_ssh.LinkError, OSError, subprocess.SubprocessError, ValueError):
        raise ToolError("Job service unavailable. Check the connection and destination installation. "
                         "Retry a start with the same job ID.") from None
    if "error" in reply:
        raise ToolError(reply["error"])
    return reply["result"]


@mcp.tool(annotations=READ_ONLY)
async def list_jobs(node_id: NodeId) -> JobCatalog:
    """List permission mode, registered program names and the 50 newest jobs on this node."""
    return await asyncio.to_thread(destination_job, node_id, "list")


@mcp.tool()
async def start_job(node_id: NodeId, program: str, job_id: UUID) -> Job:
    """Request a registered program. Supply a fresh UUID and reuse it for retries."""
    if not jobs.NAME.fullmatch(program):
        raise ToolError("Invalid program name.")
    return await asyncio.to_thread(destination_job, node_id, "start", job_id, program)


@mcp.tool(annotations=READ_ONLY)
async def read_job(node_id: NodeId, job_id: UUID) -> JobDetails:
    """Read durable job state, exit code and the last 4096 bytes of stdout and stderr."""
    return await asyncio.to_thread(destination_job, node_id, "read", job_id)


@mcp.tool()
async def stop_job(node_id: NodeId, job_id: UUID) -> Job:
    """Cancel a waiting job or request termination of its running process tree."""
    return await asyncio.to_thread(destination_job, node_id, "stop", job_id)


class BatchTaskRequest(BaseModel):
    program: str = Field(pattern=f"^{jobs.NAME.pattern}$")
    node_id: NodeId | None = None


class BatchTaskResult(BaseModel):
    index: int
    node_id: NodeId
    program: str
    job_id: UUID
    job: Job | None = None
    error: str | None = None


class BatchReport(BaseModel):
    batch_id: UUID
    origin: NodeId
    tasks: list[BatchTaskResult]


async def registered_programs(node_id):
    try:
        catalog = await asyncio.to_thread(destination_job, node_id, "list")
        return node_id, catalog["programs"]
    except ToolError:
        return node_id, []


async def batch_report(batch_id, plan, operation):
    by_node = defaultdict(list)
    for task in plan:
        by_node[task["node_id"]].append(task)

    async def contact(node_id, tasks):
        items = [{"job_id": task["job_id"], "program": task["program"]} for task in tasks]
        request = {"action": "batch", "operation": operation, "items": items}
        try:
            replies = await asyncio.to_thread(destination_request, node_id, request)
        except ToolError as error:
            return [BatchTaskResult(**task, error=str(error)) for task in tasks]
        if not isinstance(replies, list) or len(replies) != len(tasks):
            return [BatchTaskResult(**task, error="Incomplete batch response from node.") for task in tasks]
        results = []
        for task, reply in zip(tasks, replies):
            try:
                result = BatchTaskResult(**task, **reply)
                if (result.job is None) == (result.error is None):
                    raise ValueError("Expected one job result or one error")
                if result.job and (result.job.id != result.job_id or result.job.program != result.program):
                    raise ValueError("Mismatched job")
            except (TypeError, ValueError):
                result = BatchTaskResult(**task, error="Invalid job response from node.")
            results.append(result)
        return results

    parts = await asyncio.gather(*(contact(node, tasks) for node, tasks in by_node.items()))
    results = sorted((task for part in parts for task in part), key=lambda task: task.index)
    return BatchReport(batch_id=batch_id, origin=local_node(), tasks=results)


@mcp.tool()
async def submit_batch(
    batch_id: UUID, tasks: list[BatchTaskRequest] = Field(min_length=1, max_length=jobs.BATCH_LIMIT),
) -> BatchReport:
    """Dispatch registered programs in parallel. Retry with the same UUID and ordered tasks."""
    request = [task.model_dump() for task in tasks]
    try:
        plan = await asyncio.to_thread(batches.load, batch_id, request)
        if plan is None:
            catalogs = {}
            if any(task.node_id is None for task in tasks):
                catalogs = dict(await asyncio.gather(*(registered_programs(node) for node in NODE_IDS)))
            plan = batches.make_plan(batch_id, local_node(), request, catalogs)
            plan = await asyncio.to_thread(batches.save, batch_id, request, plan)
    except ValueError as error:
        raise ToolError(str(error)) from None
    return await batch_report(batch_id, plan, "start")


@mcp.tool(annotations=READ_ONLY)
async def get_batch(batch_id: UUID) -> BatchReport:
    """Read each task's state and exit code on the PC where the batch was submitted."""
    try:
        plan = await asyncio.to_thread(batches.load, batch_id)
    except ValueError as error:
        raise ToolError(str(error)) from None
    if plan is None:
        raise ToolError("Unknown batch ID on this origin.")
    return await batch_report(batch_id, plan, "status")


if __name__ == "__main__":
    local_node()
    mcp.run(transport="stdio")
