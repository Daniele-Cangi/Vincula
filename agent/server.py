# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Status and destination-authorized jobs for registered Vincula nodes."""

import asyncio
import base64
from collections import defaultdict
from contextlib import suppress
from functools import cache
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import UUID, uuid5

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, JsonValue

import batches
import jobs
import linux_status
import node_registry
import node_ssh
import capability_workflow

dock_dir = Path(__file__).resolve().parents[1] / "outputs" / "Windows Dock"
sys.path.insert(0, str(dock_dir))
import windows_ssh

NodeId = Annotated[str, Field(pattern=node_registry.SELECTOR_PATTERN)]
Availability = Literal["available", "unavailable", "not_configured"]

mcp = MCPServer("vincula", instructions="Inspect Vincula nodes and manage their locally authorized programs.")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

WINDOWS_RESOURCES = r"""
$gpus = $null
try {
    $gpus = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name } | ForEach-Object {
        @{ name = $_.Name; vram_total_bytes = $null; vram_available_bytes = $null }
    })
} catch {}
$nvidia = Get-Command nvidia-smi.exe -CommandType Application -ErrorAction SilentlyContinue
if ($nvidia) {
    try {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $nvidia.Source
        $info.Arguments = '--query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits'
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($info)
        try {
            if (!$process.WaitForExit(2000)) { $process.Kill() }
            elseif ($process.ExitCode -eq 0) {
                $gpuRows = @($process.StandardOutput.ReadToEnd() -split '\r?\n' |
                    Where-Object { $_ } | ConvertFrom-Csv -Header name,total,free)
                if ($gpuRows.Count) {
                    $nvidiaGpus = @($gpuRows | ForEach-Object {
                        @{ name = $_.name.Trim(); vram_total_bytes = [int64]$_.total * 1MB
                           vram_available_bytes = [int64]$_.free * 1MB }
                    })
                    $gpus = @($gpus | Where-Object { $_.name -and $_.name -notmatch 'NVIDIA' }) + $nvidiaGpus
                }
            }
        } finally { $process.Dispose() }
    } catch {}
}
$status | Add-Member -NotePropertyMembers @{
    cpu_logical = [Environment]::ProcessorCount; architecture = $env:PROCESSOR_ARCHITECTURE
    gpus = $gpus
}
$status | ConvertTo-Json -Compress -Depth 5
"""
WINDOWS_STATUS = "$status = & {\n" + windows_ssh.STATUS_COMMAND + "\n} | ConvertFrom-Json\n" + WINDOWS_RESOURCES


class GPU(BaseModel):
    name: str
    vram_total_bytes: int | None = Field(default=None, ge=0)
    vram_available_bytes: int | None = Field(default=None, ge=0)


class Volume(BaseModel):
    path: str
    total_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)


class Metrics(BaseModel):
    os: str
    os_version: str | None
    uptime_seconds: int = Field(ge=0)
    cpu_percent: float | None = Field(ge=0, le=100)
    memory_total_bytes: int = Field(gt=0)
    memory_available_bytes: int = Field(ge=0)
    cpu_logical: int | None = Field(default=None, ge=1)
    architecture: str | None = None
    gpus: list[GPU] | None = None
    volumes: list[Volume] | None = None


class Node(BaseModel):
    node_id: NodeId
    label: str
    os_family: Literal["windows", "linux"]
    aliases: list[str]
    network: str | None
    local: bool
    availability: Availability


class Inventory(BaseModel):
    nodes: list[Node]


class NodeStatus(Node):
    metrics: Metrics | None


def legacy_registry():
    if sys.platform not in {"win32", "linux"}:
        raise RuntimeError("Vincula MCP runs on Windows or Linux.")
    if sys.platform == "win32":
        local = "windows"
        app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        config = app_data / "Linux Dock" / "ssh_config"
        linux_route = node_registry.Route("linux-dock", ("-F", str(config)), (config,), "Ethernet")
        windows_route = None
    else:
        local = "ubuntu"
        linux_route = None
        windows_route = node_registry.Route(
            windows_ssh.DESTINATION, tuple(windows_ssh._ssh_options()),
            (windows_ssh.HOST_KEY, windows_ssh.IDENTITY), "Ethernet",
        )
    return node_registry.Registry(local, (
        node_registry.Node("ubuntu", "Ubuntu", "linux", route=linux_route),
        node_registry.Node("windows", "Windows", "windows", route=windows_route),
    ))


@cache
def registry():
    path = os.environ.get("VINCULA_NODE_REGISTRY")
    if not path:
        if sys.platform == "win32":
            app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            candidate = app_data / "Vincula" / "nodes.json"
        else:
            candidate = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vincula" / "nodes.json"
        path = candidate if candidate.is_file() else None
    return node_registry.load(Path(path) if path else None, legacy_registry(), node_ssh.EMPTY_CONFIG)


def local_node() -> NodeId:
    return registry().local_id


def registered_node(selector):
    try:
        return registry().get(selector)
    except ValueError as error:
        raise ToolError(str(error)) from None


def windows_metrics(node, is_local):
    if is_local:
        status = windows_ssh.local_status(timeout=node_ssh.PROBE_TIMEOUT, command=WINDOWS_STATUS)
    else:
        status = node_ssh.json_command(node.route, windows_ssh._powershell_command(WINDOWS_STATUS))
    return Metrics(
        os=status["os"], os_version=status["build"], uptime_seconds=status["uptime"],
        cpu_percent=status["cpu"], memory_total_bytes=status["ram_total"],
        memory_available_bytes=status["ram_free"],
        cpu_logical=status.get("cpu_logical"), architecture=status.get("architecture"), gpus=status.get("gpus"),
        volumes=[Volume(path=disk["drive"], total_bytes=disk["total"], available_bytes=disk["free"])
                 for disk in status.get("disks", []) if disk["total"] is not None and disk["free"] is not None],
    )


def read_metrics(node, is_local):
    if node.family == "windows":
        return windows_metrics(node, is_local)
    if is_local:
        return Metrics.model_validate(linux_status.read_status())
    probe = base64.b64encode(Path(linux_status.__file__).read_bytes()).decode("ascii")
    return Metrics.model_validate(node_ssh.json_command(node.route, f"printf %s {probe} | base64 -d | python3 -IB -"))


def node_status(node):
    is_local = node.node_id == local_node()
    metadata = {"node_id": node.node_id, "label": node.label, "os_family": node.family,
                "aliases": list(node.aliases), "network": node.route.network if node.route else None, "local": is_local}
    if not is_local and (node.route is None or not node.route.configured()):
        return NodeStatus(**metadata, availability="not_configured", metrics=None)
    try:
        metrics = read_metrics(node, is_local)
    except (windows_ssh.LinkError, OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        # Keep private paths, account names and SSH diagnostics off the MCP connection.
        return NodeStatus(**metadata, availability="unavailable", metrics=None)
    return NodeStatus(**metadata, availability="available", metrics=metrics)


@mcp.tool(annotations=READ_ONLY)
async def list_nodes() -> Inventory:
    """List registered nodes, their canonical IDs, aliases and reachability from this origin."""
    results = await asyncio.gather(*(asyncio.to_thread(node_status, node) for node in registry().nodes))
    return Inventory(nodes=[Node.model_validate(result.model_dump(exclude={"metrics"})) for result in results])


@mcp.tool(annotations=READ_ONLY)
async def get_node_status(node_id: NodeId) -> NodeStatus:
    """Read OS, uptime and available resource metrics on a registered node."""
    return await asyncio.to_thread(node_status, registered_node(node_id))


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
    node = registered_node(node_id)
    if node.node_id == local_node():
        try:
            return jobs.dispatch(**request)
        except ValueError as error:
            raise ToolError(str(error)) from None
    payload = base64.b64encode(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()).decode("ascii")
    try:
        if node.family == "linux":
            command = f'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" rpc {payload}'
        else:
            script = (
                "$helper = Join-Path $env:LOCALAPPDATA 'Vincula-Jobs/jobs.py'\n"
                f"& py.exe -3 -IB $helper rpc '{payload}'\nexit $LASTEXITCODE"
            )
            command = windows_ssh._powershell_command(script)
        reply = node_ssh.json_command(node.route, command)
        if not isinstance(reply, dict) or reply.keys() not in ({"result"}, {"error"}):
            raise ValueError("Invalid remote job reply.")
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
                catalogs = dict(await asyncio.gather(*(registered_programs(node.node_id) for node in registry().nodes)))
            assignments = [dict(task, node_id=registered_node(task["node_id"]).node_id)
                           if task["node_id"] else task for task in request]
            plan = batches.make_plan(batch_id, local_node(), assignments, catalogs)
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


class InstalledTools(BaseModel):
    source: Literal["worker", "request"]
    mode: Literal["ask", "full"]
    commands: list[str]
    next_offset: int | None


class ShellEntry(BaseModel):
    sequence: int
    job: Job


class ShellSession(BaseModel):
    id: UUID
    created: float
    closed: float | None
    mode: Literal["ask", "full"]
    commands: list[ShellEntry]
    output: JobDetails | None
    next_after: int | None


@mcp.tool(annotations=READ_ONLY)
async def discover_tools(
    node_id: NodeId,
    prefix: Annotated[str, Field(max_length=80)] = "",
    offset: Annotated[int, Field(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Field(ge=1, le=jobs.PAGE_LIMIT)] = jobs.PAGE_LIMIT,
) -> InstalledTools:
    """List installed executable names on the node's PATH without granting execution rights."""
    return await asyncio.to_thread(destination_request, node_id, {
        "action": "discover", "prefix": prefix, "offset": offset, "limit": limit,
    })


def shell_request(node_id, operation, session_id, **payload):
    return destination_request(node_id, {
        "action": "shell", "operation": operation, "session_id": str(session_id), **payload,
    })


@mcp.tool()
async def open_shell(node_id: NodeId, session_id: UUID) -> ShellSession:
    """Open or recover a durable command session for authorized program names on this node."""
    return await asyncio.to_thread(shell_request, node_id, "open", session_id)


@mcp.tool()
async def send_shell_command(
    node_id: NodeId, session_id: UUID, job_id: UUID,
    program: Annotated[str, Field(pattern=f"^{jobs.NAME.pattern}$")],
) -> Job:
    """Send one registered program name. Each command follows the destination approval policy."""
    return await asyncio.to_thread(shell_request, node_id, "send", session_id, job_id=str(job_id), program=program)


@mcp.tool(annotations=READ_ONLY)
async def read_shell(
    node_id: NodeId, session_id: UUID, job_id: UUID | None = None,
    after: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0,
) -> ShellSession:
    """Read a page of command states and optionally the output of a command in this session."""
    return await asyncio.to_thread(shell_request, node_id, "read", session_id,
                                   job_id=str(job_id) if job_id else None, after=after)


@mcp.tool()
async def close_shell(node_id: NodeId, session_id: UUID) -> ShellSession:
    """Close the session and stop its active command. The history stays available."""
    return await asyncio.to_thread(shell_request, node_id, "close", session_id)


FolderAlias = Annotated[str, Field(pattern=f"^{jobs.NAME.pattern}$")]
RelativeFile = Annotated[str, Field(max_length=240)]


class FileRoot(BaseModel):
    alias: str
    writable: bool


class FileRoots(BaseModel):
    roots: list[FileRoot]


class FileRequest(BaseModel):
    id: UUID
    state: Literal["pending", "approved", "denied", "completed"]
    operation: Literal["browse", "read", "export", "import"]
    root: str
    path: str
    result: dict | None


class FileEndpoint(BaseModel):
    node_id: NodeId
    root: FolderAlias
    path: RelativeFile


class TransferReceipt(BaseModel):
    transfer_id: UUID
    source: FileRequest | None
    destination: FileRequest


def file_request(node_id, operation, request_id=None, **fields):
    return destination_request(node_id, {
        "action": "files", "operation": operation, "request_id": str(request_id) if request_id else None,
        **fields,
    })


@mcp.tool(annotations=READ_ONLY)
async def list_file_roots(node_id: NodeId) -> FileRoots:
    """List the folder aliases and write permissions configured on this node."""
    return FileRoots(roots=await asyncio.to_thread(file_request, node_id, "roots"))


@mcp.tool()
async def request_file(
    node_id: NodeId, request_id: UUID, operation: Literal["browse", "read"],
    root: FolderAlias, path: RelativeFile = "",
    offset: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0,
) -> FileRequest:
    """Browse an authorized directory or read a UTF-8 file page under the local approval policy."""
    return await asyncio.to_thread(file_request, node_id, "request", request_id, root=root,
                                   relative=path, offset=offset, operation_kind=operation)


@mcp.tool()
async def get_file_request(node_id: NodeId, request_id: UUID) -> FileRequest:
    """Retrieve a file request and materialize its authorized result."""
    return await asyncio.to_thread(file_request, node_id, "status", request_id)


def sftp_copy(node, remote, local, sending):
    if node.family == "windows":
        remote_name = windows_ssh._sftp_name(windows_ssh._file_path(remote))
    else:
        if not remote.startswith("/") or any(c in ('\\', '"') or ord(c) < 32 for c in remote):
            raise ValueError("SSH staging path is not supported.")
        remote_name = f'"{remote}"'
    command = f"put payload {remote_name}" if sending else f"get {remote_name} payload"
    node_ssh.copy(node.route, command, local.parent)


def copy_staged(source, destination, outbound, inbound, expected):
    if source.node_id == local_node():
        sftp_copy(destination, inbound, Path(outbound), sending=True)
    elif destination.node_id == local_node():
        sftp_copy(source, outbound, Path(inbound), sending=False)
    else:
        with TemporaryDirectory(prefix="relay-", dir=jobs.ROOT) as directory:
            payload = Path(directory) / "payload"
            sftp_copy(source, outbound, payload, sending=False)
            if jobs.files.copy_limited(payload, None) != expected:
                raise ValueError("The relayed file does not match the approved export.")
            sftp_copy(destination, inbound, payload, sending=True)


def cleanup_transfer(source_node, source_id, dest_node, dest_id):
    for node, request_id in ((source_node, source_id), (dest_node, dest_id)):
        with suppress(ToolError):
            file_request(node, "cleanup", request_id)


def execute_transfer(transfer_id, source, destination):
    source_node = registered_node(source.node_id)
    destination_node = registered_node(destination.node_id)
    if source_node.node_id == destination_node.node_id:
        raise ValueError("Choose endpoints on different registered nodes.")
    with jobs.transaction() as db:
        jobs.files.save_plan(db, str(transfer_id), {
            "source": source.model_dump(), "destination": destination.model_dump(),
        })
    source_id, dest_id = uuid5(transfer_id, "export"), uuid5(transfer_id, "import")
    dest = file_request(destination.node_id, "request", dest_id, root=destination.root,
                        relative=destination.path, operation_kind="import")
    if dest["state"] == "completed":
        cleanup_transfer(source.node_id, source_id, destination.node_id, dest_id)
        return TransferReceipt(transfer_id=transfer_id, source=None, destination=dest)
    src = file_request(source.node_id, "request", source_id, root=source.root,
                       relative=source.path, operation_kind="export")
    if src["state"] == dest["state"] == "approved":
        outbound = file_request(source.node_id, "stage", source_id)
        inbound = file_request(destination.node_id, "stage", dest_id)
        expected = {"size": outbound["size"], "sha256": outbound["sha256"]}
        copy_staged(source_node, destination_node, outbound["path"], inbound["path"], expected)
        file_request(destination.node_id, "commit", dest_id, expected=expected)
        dest = file_request(destination.node_id, "status", dest_id)
        cleanup_transfer(source.node_id, source_id, destination.node_id, dest_id)
    return TransferReceipt(transfer_id=transfer_id, source=src, destination=dest)


def transfer_with_lock(transfer_id, source, destination):
    try:
        if not jobs.DATABASE.is_file():
            raise ValueError("Initialize the job runner on the origin first.")
        locks = jobs.ROOT / "file-transfer-locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        with jobs.process_lock(locks / f"{transfer_id}.lock"):
            return execute_transfer(transfer_id, source, destination)
    except ValueError as error:
        raise ToolError(str(error)) from None
    except (OSError, subprocess.SubprocessError, windows_ssh.LinkError):
        raise ToolError("Could not transfer the file. Retry with the same transfer ID.") from None


@mcp.tool()
async def transfer_file(transfer_id: UUID, source: FileEndpoint, destination: FileEndpoint) -> TransferReceipt:
    """Transfer a file after both nodes grant access. Reuse the UUID while waiting or retrying."""
    return await asyncio.to_thread(transfer_with_lock, transfer_id, source, destination)


class CapabilityParameter(BaseModel):
    type: Literal["string", "integer", "boolean", "number"]
    required: bool = False
    enum: list[JsonValue] | None = None
    description: str = ""


class CapabilityContract(BaseModel):
    max_files: int
    max_input_bytes: int
    extensions: list[str]
    parameters: dict[str, CapabilityParameter]


class CapabilityProvider(BaseModel):
    id: str
    version: str
    revision: str
    description: str
    inbox: str | None
    contract: CapabilityContract


class Capabilities(BaseModel):
    capabilities: list[CapabilityProvider]


class CapabilityPart(BaseModel):
    node_id: NodeId
    files: Annotated[list[FileEndpoint], Field(max_length=jobs.capabilities.MAX_FILES)] = Field(default_factory=list)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class CapabilityOutput(BaseModel):
    value: JsonValue


class CapabilityPartReport(BaseModel):
    node_id: NodeId
    job_id: UUID
    job: Job | None
    waiting_transfers: list[TransferReceipt]
    output: CapabilityOutput | None
    error: str | None


class CapabilityReport(BaseModel):
    run_id: UUID
    capability_id: str
    parts: list[CapabilityPartReport]


@mcp.tool(annotations=READ_ONLY)
async def discover_capabilities(node_id: NodeId) -> Capabilities:
    """List operator-registered capabilities, versions, parameters and file requirements on a node."""
    providers = await capability_workflow.node_call(destination_request, node_id, "describe")
    return Capabilities(capabilities=providers)


def transfer_capability_input(transfer_id, source, destination):
    return transfer_with_lock(transfer_id, FileEndpoint.model_validate(source),
                              FileEndpoint.model_validate(destination)).model_dump(mode="json")


@mcp.tool()
async def submit_capability(
    run_id: UUID, capability_id: str, parts: Annotated[list[CapabilityPart], Field(min_length=1, max_length=jobs.capabilities.MAX_PARTS)],
) -> CapabilityReport:
    """Run a discovered capability on the selected nodes. Repeat the same run after transfer approvals."""
    request = {"capability_id": capability_id, "parts": [part.model_dump(mode="json") for part in parts]}
    try:
        jobs.capabilities.valid_id(capability_id)
        plan = await asyncio.to_thread(capability_workflow.load, run_id, request)
        if plan is None:
            nodes = list(dict.fromkeys(registered_node(part.node_id).node_id for part in parts))
            providers = await asyncio.gather(*(
                capability_workflow.node_call(destination_request, node, "describe", capability_id=capability_id)
                for node in nodes
            ))
            plan = await asyncio.to_thread(capability_workflow.make_plan, run_id, request,
                                           dict(zip(nodes, providers)), local_node(), registry().get)
    except ValueError as error:
        raise ToolError(str(error)) from None
    return await capability_workflow.reconcile(run_id, plan, destination_request, transfer_capability_input)


@mcp.tool(annotations=READ_ONLY)
async def get_capability_run(run_id: UUID) -> CapabilityReport:
    """Read parts, job output and status for a capability run submitted from this node."""
    try:
        plan = await asyncio.to_thread(capability_workflow.load, run_id)
    except ValueError as error:
        raise ToolError(str(error)) from None
    if plan is None:
        raise ToolError("Unknown capability run on this origin.")
    return await capability_workflow.reconcile(run_id, plan, destination_request)


if __name__ == "__main__":
    local_node()
    mcp.run(transport="stdio")
