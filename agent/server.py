# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Status and destination-authorized jobs for registered Vincula nodes."""

import asyncio
import base64
from collections import OrderedDict, defaultdict
from contextlib import suppress
import json
import os
import subprocess
import sys
from time import monotonic
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import UUID, uuid5

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue

import batches
import jobs
import linux_status
import node_registry
import node_ssh
import capability_workflow
import execution_plans
import large_transfer_plans

dock_dir = Path(__file__).resolve().parents[1] / "outputs" / "Windows Dock"
sys.path.insert(0, str(dock_dir))
import windows_ssh

NodeId = Annotated[str, Field(pattern=node_registry.SELECTOR_PATTERN)]
Availability = Literal["available", "unavailable", "not_configured"]

mcp = MCPServer("vincula", instructions="Inspect Vincula nodes and execute work under each node owner's access policy.")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

WINDOWS_NVIDIA_LOCATOR = r"""
$nvidia = $null
foreach ($entry in ($env:PATH -split ';')) {
    $directory = ([string]$entry).Trim().Trim('"')
    if ($directory -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+(?:[\\/]|$))') { continue }
    try {
        $candidate = [IO.Path]::GetFullPath([IO.Path]::Combine($directory, 'nvidia-smi.exe'))
        if ([IO.File]::Exists($candidate)) { $nvidia = $candidate; break }
    } catch [ArgumentException] { continue }
      catch [NotSupportedException] { continue }
      catch [IO.IOException] { continue }
}
"""
WINDOWS_RESOURCES = r"""
$gpus = $null
try {
    $gpus = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name } | ForEach-Object {
        @{ name = $_.Name; vram_total_bytes = $null; vram_available_bytes = $null }
    })
} catch {}
""" + WINDOWS_NVIDIA_LOCATOR + r"""
if ($nvidia) {
    try {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $nvidia
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
WINDOWS_STATUS_SOURCE = "$status = & {\n" + windows_ssh.STATUS_COMMAND + "\n} | ConvertFrom-Json\n" + WINDOWS_RESOURCES
# These fixed scripts have no here-strings. Preserve newlines while keeping the
# encoded SSH command below cmd.exe's 8191-character limit on default Windows SSH.
WINDOWS_STATUS = "\n".join(
    line.lstrip() for line in WINDOWS_STATUS_SOURCE.splitlines()
)


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


WINDOWS_JOURNAL_STDIN = r"""
$helper = Join-Path $env:LOCALAPPDATA 'Vincula-Jobs/jobs.py'
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = (Get-Command py.exe -ErrorAction Stop).Source
$info.Arguments = '-3 -IB "' + $helper + '" journal-stdin'
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardInput = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($info)
try {
    $output = $process.StandardOutput.ReadToEndAsync()
    $errors = $process.StandardError.ReadToEndAsync()
    $source = [Console]::OpenStandardInput()
    $buffer = New-Object byte[] 8192
    $total = 0
    while (($count = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $total += $count
        if ($total -gt __JOURNAL_LIMIT__) { throw 'Plan journal request exceeds its size limit.' }
        $process.StandardInput.BaseStream.Write($buffer, 0, $count)
    }
    $process.StandardInput.Close()
    if (!$process.WaitForExit(10000)) { throw 'Plan journal receiver timed out.' }
    [Console]::Out.Write($output.GetAwaiter().GetResult())
    [Console]::Error.Write($errors.GetAwaiter().GetResult())
    exit $process.ExitCode
} finally {
    if (!$process.HasExited) { $process.Kill() }
    $process.Dispose()
}
"""

def windows_framed_bootstrap(command):
    if command not in {"process-stdin", "journal-framed-stdin"}:
        raise ValueError("Unknown framed destination entrypoint.")
    # Python inherits the SSH byte stream directly. ConsoleHost's separate stdin
    # reader is deliberately absent, and no request content enters the command.
    return ("import os,runpy,sys;"
            f"sys.argv=['jobs.py','{command}'];"
            "runpy.run_path(os.path.join(os.environ['LOCALAPPDATA'],'Vincula-Jobs','jobs.py'),"
            "run_name='__main__')")


def windows_framed_command(command):
    return f'py.exe -3 -IB -c "{windows_framed_bootstrap(command)}"'


def remote_request(node, request):
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if request.get("action") == "process":
        if len(raw) > jobs.direct_execution.MAX_TRANSPORT_BYTES:
            raise ValueError("The process request exceeds its transport size limit.")
        if node.family == "linux":
            command = 'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" process-stdin'
        else:
            command = windows_framed_command("process-stdin")
        data = jobs.wire_framing.pack(raw, jobs.direct_execution.MAX_TRANSPORT_BYTES)
        return node_ssh.json_command(node.route, command, input_data=data,
                                     output_limit=jobs.direct_execution.MAX_RESPONSE_BYTES)
    journal = request.get("action") == "plan"
    if journal and (len(raw) > 4096 or node.family == "windows"):
        if len(raw) > jobs.plan_journal.MAX_REQUEST_BYTES:
            raise ValueError("Plan journal request exceeds its size limit.")
        if node.family == "linux":
            command = 'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" journal-framed-stdin'
        else:
            command = windows_framed_command("journal-framed-stdin")
        return node_ssh.json_command(node.route, command,
                                     input_data=jobs.wire_framing.pack(raw, jobs.plan_journal.MAX_REQUEST_BYTES),
                                     output_limit=jobs.direct_execution.MAX_RESPONSE_BYTES)
    payload = base64.b64encode(raw).decode("ascii")
    if node.family == "linux":
        command = f'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" rpc {payload}'
    else:
        script = (
            "$helper = Join-Path $env:LOCALAPPDATA 'Vincula-Jobs/jobs.py'\n"
            f"& py.exe -3 -IB $helper rpc '{payload}'\nexit $LASTEXITCODE"
        )
        command = windows_ssh._powershell_command(script)
    if journal:
        return node_ssh.json_command(node.route, command, output_limit=jobs.plan_journal.MAX_RESPONSE_BYTES)
    return node_ssh.json_command(node.route, command)


def destination_request(node_id, request):
    node = registered_node(node_id)
    if node.node_id == local_node():
        try:
            return jobs.dispatch(**request)
        except ValueError as error:
            raise ToolError(str(error)) from None
    try:
        reply = remote_request(node, request)
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


class ProcessExecutionInfo(BaseModel):
    node_id: NodeId
    protocol_version: int
    enabled: bool
    mode: Literal["ask", "full"]
    interactive: Literal[False]
    max_request_bytes: int
    max_stdin_bytes: int
    max_response_bytes: int
    default_timeout_seconds: int
    max_timeout_seconds: int


class ProcessOutcome(BaseModel):
    runtime_protocol_version: int
    state: str
    program_exit_code: int | None
    reason: Literal["exit", "stop", "timeout", "launch_error", "unconfirmed"]
    started: bool | None


class ProcessDetails(JobDetails):
    manifest: dict[str, JsonValue]
    outcome: ProcessOutcome | None


def destination_process(node_id, operation, **fields):
    return destination_request(node_id, {"action": "process", "operation": operation,
                                        "protocol_version": jobs.direct_execution.PROTOCOL_VERSION, **fields})


ResourceQuantity = Annotated[int, Field(ge=0, le=2**63 - 1, strict=True)]


class ResourceGPURequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=160)
    vram_bytes: ResourceQuantity
    exclusive: Annotated[bool, Field(strict=True)] = False


class ProcessResources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_millis: ResourceQuantity
    memory_bytes: ResourceQuantity
    gpus: list[ResourceGPURequest] = Field(default_factory=list, max_length=64)


class ResourceObservationGPU(BaseModel):
    id: str
    vram_total_bytes: ResourceQuantity | None
    vram_available_bytes: ResourceQuantity | None


class ResourceObservation(BaseModel):
    observed_at: float
    cpu_millis: ResourceQuantity | None
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_total_bytes: ResourceQuantity | None
    memory_available_bytes: ResourceQuantity | None
    gpus: list[ResourceObservationGPU] | None


class ResourceWorker(BaseModel):
    generation: UUID
    protocol_version: Literal[2]
    platform: Literal["linux", "windows"] | None
    heartbeat_at: float
    fresh: bool
    observation: ResourceObservation | None
    observation_fresh: bool
    stopped: bool = False
    fresh_remaining_seconds: float = Field(default=0, ge=0, le=5, allow_inf_nan=False)
    observation_remaining_seconds: float = Field(default=0, ge=0, le=5, allow_inf_nan=False)


class ResourceBudgetGPU(BaseModel):
    id: str
    vram_bytes: ResourceQuantity


class ResourcePolicy(BaseModel):
    max_jobs: Annotated[int, Field(ge=1, le=64, strict=True)]
    cpu_millis: ResourceQuantity | None
    memory_bytes: ResourceQuantity | None
    gpus: list[ResourceBudgetGPU] | None
    legacy_defaults: ProcessResources | None


class ResourceClaim(BaseModel):
    job_id: UUID
    generation: UUID
    requirements: ProcessResources
    legacy: bool
    exclusive: bool
    created: float


class NodeResources(BaseModel):
    node_id: NodeId
    os_family: Literal["linux", "windows"]
    mode: Literal["ask", "full"]
    native_execution_enabled: bool
    protocol_version: Literal[2]
    state_id: UUID
    policy: ResourcePolicy
    policy_revision: str
    worker: ResourceWorker | None
    claims: list[ResourceClaim]
    declared_consumption: dict[str, JsonValue]
    job_counts: dict[str, int]
    admission_only: Literal[True]
    hard_limits_enforced: dict[str, Literal[False]]
    request_round_trip_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


async def node_resources(node_id):
    node = registered_node(node_id)
    before = monotonic()
    reply = await asyncio.to_thread(destination_request, node.node_id, {"action": "resources"})
    state = NodeResources(node_id=node.node_id, os_family=node.family, **reply)
    state.request_round_trip_seconds = max(0, monotonic() - before)
    if state.worker is not None:
        # A full request round trip is an upper bound on time spent in transit.
        # Never compare clocks from two different computers to renew freshness.
        elapsed = state.request_round_trip_seconds
        worker = state.worker
        worker.fresh_remaining_seconds = max(0, worker.fresh_remaining_seconds - elapsed)
        worker.observation_remaining_seconds = max(0, worker.observation_remaining_seconds - elapsed)
        worker.fresh = worker.fresh and worker.fresh_remaining_seconds > 0 and not worker.stopped
        worker.observation_fresh = (worker.observation_fresh and worker.fresh
                                    and worker.observation_remaining_seconds > 0)
    return state


@mcp.tool(annotations=READ_ONLY)
async def get_node_resources(node_id: NodeId) -> NodeResources:
    """Read owner budgets, live worker generation and fresh CPU/RAM/GPU observations.
    Claims remain held until owned process cleanup is confirmed. Budgets govern
    declared admission only: CPU, memory and GPU OS hard limits are not installed.
    state_id identifies destination durable history; pin it when replaying a plan.
    """
    return await node_resources(node_id)


@mcp.tool(annotations=READ_ONLY)
async def get_process_execution_info(node_id: NodeId) -> ProcessExecutionInfo:
    """Read native helper protocol and owner opt-in. This does not certify a running worker's version."""
    node = registered_node(node_id)
    result = await asyncio.to_thread(destination_process, node.node_id, "info")
    return ProcessExecutionInfo(node_id=node.node_id, **result)


@mcp.tool()
async def execute_process(node_id: NodeId, job_id: UUID, argv: list[str], cwd: str | None = None,
                          env: dict[str, str] | None = None, stdin_text: str | None = None,
                          timeout_seconds: Annotated[int, Field(ge=1, le=3600, strict=True)] = 300,
                          resources: ProcessResources | None = None, expected_state_id: UUID | None = None) -> ProcessDetails:
    """Execute native argv without preregistration when the owner enabled it. Ask requires exact approval;
    full queues immediately with the worker account's OS permissions. No shell expansion or elevation.
    cwd is absolute or defaults to home. UTF-8 input (64 KiB) ends at EOF. No PTY or streaming stdin.
    Supply a UUID and reuse the identical request on retry. Timeout includes computation, not queue time.
    Windows batch scripts need an explicit cmd.exe interpreter. Use read_process and stop_job afterwards.
    Optional resources use protocol2 and require a live resource-aware worker.
    Pin expected_state_id from get_node_resources to reject a replaced destination
    database. Calls without resources preserve the existing process protocol.
    """
    extra = {}
    if resources is not None:
        extra = {"protocol_version": jobs.direct_execution.RESOURCE_PROTOCOL_VERSION,
                 "resources": resources.model_dump(),
                 "expected_state_id": str(expected_state_id) if expected_state_id is not None else None}
    elif expected_state_id is not None:
        raise ToolError("A destination state pin requires a resource-aware process request.")
    return await asyncio.to_thread(destination_process, node_id, "execute", job_id=str(job_id), argv=argv,
                                   cwd=cwd, env=env, stdin_text=stdin_text, timeout_seconds=timeout_seconds, **extra)


@mcp.tool(annotations=READ_ONLY)
async def read_process(node_id: NodeId, job_id: UUID, expected_state_id: UUID | None = None) -> ProcessDetails:
    """Read immutable original/resolved request, approval digest, bounded log tails and confirmed outcome.
    An incompatible legacy worker cannot execute this request; its helper exit is reported unconfirmed.
    """
    fields = ({"protocol_version": 2, "expected_state_id": str(expected_state_id)}
              if expected_state_id is not None else {})
    return await asyncio.to_thread(destination_process, node_id, "read", job_id=str(job_id), **fields)


class ExecutionInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] | None = None
    stdin_text: str | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=3600, strict=True)] = 300
    resources: ProcessResources


class ExecutionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: NodeId
    process: ExecutionInvocation
    estimated_seconds: float = Field(default=1, gt=0, strict=True, allow_inf_nan=False)
    setup_seconds: float = Field(default=0, ge=0, strict=True, allow_inf_nan=False)


class ExecutionTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[ExecutionCandidate] = Field(min_length=1, max_length=64)


class ExecutionPartResult(BaseModel):
    index: int
    node_id: NodeId
    job_id: UUID
    job: ProcessDetails | None = None
    error: str | None = None


class ExecutionPlanReport(BaseModel):
    plan_id: UUID
    authority_node_id: NodeId
    coordinator_node_id: NodeId
    manifest_sha256: str
    manifest: dict[str, JsonValue]
    parts: list[ExecutionPartResult]


async def execution_manifest(plan_id, authority):
    """Find the original plan without turning absence on a peer into creation."""
    authority_reachable = False
    try:
        record = await asyncio.to_thread(destination_request, authority,
            {"action": "plan", "operation": "get", "kind": "execution", "plan_id": str(plan_id)})
        authority_reachable = True
    except ToolError:
        record = None
    local = await asyncio.to_thread(jobs.dispatch, "plan", operation="get", kind="execution", plan_id=str(plan_id))
    replicas = await find_plan_manifest("execution", plan_id)
    rows = [record, local]
    if replicas is not None:
        rows.append(jobs.plan_journal.package(replicas))
    saved = execution_plans.select_replica(str(plan_id), rows)
    if saved is not None and saved["origin"] != authority:
        raise ValueError("This plan UUID belongs to a different authority; preserve the original plan handle.")
    return saved, authority_reachable


async def store_execution_authority(manifest):
    """One stable destination CAS prevents competing coordinators from creating two plans."""
    expected = jobs.plan_journal.package(manifest)
    receipt = await asyncio.to_thread(destination_request, manifest["origin"],
        {"action": "plan", "operation": "store", "manifest": manifest})
    if isinstance(receipt, dict) and receipt.get("state") == "conflict":
        record = await asyncio.to_thread(destination_request, manifest["origin"],
            {"action": "plan", "operation": "get", "kind": "execution", "plan_id": manifest["id"]})
        original = execution_plans.select_replica(manifest["id"], [record])
        if original is None or original["origin"] != manifest["origin"] or original["request"] != manifest["request"]:
            raise ValueError("The plan authority already holds a different immutable request.")
        return original
    jobs.plan_journal.check_replication(receipt, expected["sha256"])
    return manifest


async def execution_report(manifest, operation, publication_errors=None):
    async def contact(part):
        identity = {name: part[name] for name in ("index", "node_id", "job_id")}
        try:
            if publication_errors and part["node_id"] in publication_errors:
                raise ToolError(publication_errors[part["node_id"]])
            current = await node_resources(part["node_id"])
            if str(current.state_id) != part["expected_state_id"]:
                raise ValueError("Destination durable state changed; the original job cannot be adopted or replayed.")
            if operation == "execute":
                result = await asyncio.to_thread(destination_process, part["node_id"], "execute",
                    protocol_version=2, job_id=part["job_id"], expected_state_id=part["expected_state_id"], **part["process"])
            else:
                result = await asyncio.to_thread(destination_process, part["node_id"], "read",
                    protocol_version=2, job_id=part["job_id"], expected_state_id=part["expected_state_id"])
            job = ProcessDetails(**result)
            checked = jobs.direct_execution.checked_manifest(json.dumps(job.manifest), _platform=current.os_family)
            if (str(job.id) != part["job_id"] or job.program != jobs.direct_execution.PROGRAM_V2
                    or checked["protocol_version"] != 2 or checked["original"] != part["process"]
                    or checked["destination_state_id"] != part["expected_state_id"]):
                raise ValueError("The destination returned a different immutable execution request.")
            if operation == "stop":
                # The receiver checks durable identity and digest atomically
                # with Stop, rather than trusting this prior observational read.
                result = await asyncio.to_thread(destination_process, part["node_id"], "stop",
                    protocol_version=2, job_id=part["job_id"], expected_state_id=part["expected_state_id"],
                    expected_sha256=checked["sha256"])
                job = ProcessDetails(**result)
            return ExecutionPartResult(**identity, job=job)
        except (ToolError, OSError, ValueError, TypeError, KeyError) as error:
            return ExecutionPartResult(**identity, error=str(error))
    parts = await asyncio.gather(*(contact(part) for part in manifest["plan"]))
    record = jobs.plan_journal.package(manifest)
    return ExecutionPlanReport(plan_id=manifest["id"], authority_node_id=manifest["origin"],
        coordinator_node_id=local_node(), manifest_sha256=record["sha256"], manifest=manifest, parts=parts)


@mcp.tool()
async def submit_execution_plan(plan_id: UUID, authority_node_id: NodeId,
        tasks: list[ExecutionTask] = Field(min_length=1, max_length=12)) -> ExecutionPlanReport:
    """Assign and execute arbitrary prepared argv using fresh resources and local admission.
    Keep BOTH plan_id and authority_node_id unchanged on retry or another origin.
    The authority atomically fixes the plan before any execution; existing replicas
    let another coordinator resume the exact jobs even while the authority is down.
    A new unknown plan needs its authority online. Candidate costs are advisory;
    prepare compatible inputs/programs before submitting, without registrations.
    Ask requires exact destination approvals; full proceeds within owner budgets.
    """
    try:
        authority = registered_node(authority_node_id).node_id
        manifest, reachable = await execution_manifest(plan_id, authority)
        saved_nodes = {candidate["node_id"] for task in manifest["request"]["tasks"]
                       for candidate in task["candidates"]} if manifest is not None else set()
        request = {"tasks": [task.model_dump() for task in tasks]}
        for task in request["tasks"]:
            for candidate in task["candidates"]:
                selector = candidate["node_id"]
                try:
                    candidate["node_id"] = registered_node(selector).node_id
                except ToolError:
                    # A replica already fixes the original choices. An unused
                    # UUID need not be installed on this new coordinator; do not
                    # resolve aliases or invent identities for an unknown plan.
                    if selector not in saved_nodes or str(UUID(selector)) != selector:
                        raise
        request = execution_plans.normalize_request(request)
        if manifest is not None:
            if manifest["request"] != request:
                raise ValueError("Plan ID already used for a different immutable process request.")
        else:
            if not reachable:
                raise ValueError("An unknown plan cannot be created while its fixed authority is unavailable.")
            candidates = dict.fromkeys(candidate["node_id"] for task in request["tasks"] for candidate in task["candidates"])
            async def observe(node):
                try:
                    state = (await node_resources(node)).model_dump(mode="json")
                    return node, state if state["native_execution_enabled"] else None
                except (ToolError, OSError, ValueError, TypeError, KeyError):
                    return node, None
            snapshots = dict(await asyncio.gather(*(observe(node) for node in candidates)))
            proposed = execution_plans.make_manifest(str(plan_id), authority, request, snapshots)
            manifest = await store_execution_authority(proposed)
        publication_errors = await publish_plan(manifest)
    except (ValueError, TypeError, KeyError) as error:
        raise ToolError(str(error)) from None
    return await execution_report(manifest, "execute", publication_errors)


async def known_execution_report(plan_id, authority_node_id, operation):
    try:
        authority = registered_node(authority_node_id).node_id
        manifest, _ = await execution_manifest(plan_id, authority)
        if manifest is None:
            raise ValueError("Unknown execution plan on its authority and reachable replicas.")
    except (ValueError, TypeError, KeyError) as error:
        raise ToolError(str(error)) from None
    return await execution_report(manifest, operation)


@mcp.tool(annotations=READ_ONLY)
async def get_execution_plan(plan_id: UUID, authority_node_id: NodeId) -> ExecutionPlanReport:
    """Read original process UUIDs, logs and outcomes from reachable replicas and workers.
    This operation neither assigns replacements nor restarts missing jobs.
    """
    return await known_execution_report(plan_id, authority_node_id, "read")


@mcp.tool()
async def stop_execution_plan(plan_id: UUID, authority_node_id: NodeId) -> ExecutionPlanReport:
    """Request Stop for only the verified original process jobs, independently of other plans."""
    return await known_execution_report(plan_id, authority_node_id, "stop")


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


async def batch_report(batch_id, plan, operation, *, origin=None, publication_errors=None):
    by_node = defaultdict(list)
    for task in plan:
        by_node[task["node_id"]].append(task)

    async def contact(node_id, tasks):
        if publication_errors and node_id in publication_errors:
            return [BatchTaskResult(**task, error=publication_errors[node_id]) for task in tasks]
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
    return BatchReport(batch_id=batch_id, origin=origin or local_node(), tasks=results)


async def publish_plan(manifest):
    record = jobs.plan_journal.package(manifest)
    receipt = await asyncio.to_thread(jobs.dispatch, "plan", operation="store", manifest=manifest)
    jobs.plan_journal.check_replication(receipt, record["sha256"])

    async def publish(node_id):
        try:
            receipt = await asyncio.to_thread(destination_request, node_id, {
                "action": "plan", "operation": "store", "manifest": manifest,
            })
        except ToolError as error:
            return node_id, str(error)
        jobs.plan_journal.check_replication(receipt, record["sha256"])
        return node_id, None

    if manifest["kind"] == "large-file":
        nodes = dict.fromkeys(manifest["request"][side]["node_id"] for side in ("source", "destination"))
    else:
        parts = manifest["plan"] if manifest["kind"] in {"batch", "execution"} else manifest["plan"]["parts"]
        nodes = dict.fromkeys(part["node_id"] for part in parts)
    replies = await asyncio.gather(*(publish(node) for node in nodes))
    return {node: error for node, error in replies if error is not None}


async def find_plan_manifest(kind, plan_id):
    async def read(node_id):
        try:
            return await asyncio.to_thread(destination_request, node_id, {
                "action": "plan", "operation": "get", "kind": kind, "plan_id": str(plan_id),
            })
        except ToolError:
            # An unavailable peer cannot prevent reads of manifests and jobs on the other peers.
            return None

    records = await asyncio.gather(*(read(node.node_id) for node in registry().nodes))
    if kind == "batch":
        return batches.select_replica(plan_id, records)
    return jobs.plan_journal.select_replica(kind, plan_id, records)


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
        manifest = await asyncio.to_thread(batches.manifest, batch_id, local_node())
        publication_errors = await publish_plan(manifest)
    except ValueError as error:
        raise ToolError(str(error)) from None
    return await batch_report(batch_id, plan, "start", publication_errors=publication_errors)


@mcp.tool(annotations=READ_ONLY)
async def get_batch(batch_id: UUID) -> BatchReport:
    """Read original job UUIDs and results, recovering a published plan from reachable workers."""
    try:
        manifest = await asyncio.to_thread(batches.manifest, batch_id, local_node())
        if manifest is None:
            manifest = await find_plan_manifest("batch", batch_id)
    except ValueError as error:
        raise ToolError(str(error)) from None
    if manifest is None:
        raise ToolError("Unknown batch ID on this origin and its reachable replicas.")
    return await batch_report(batch_id, manifest["plan"], "status", origin=manifest["origin"])


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


def optional_transfer_status(endpoint, request_id, operation):
    try:
        reply = file_request(endpoint["node_id"], "inspect-transfer", request_id)
    except ToolError as error:
        if str(error) == "Unknown file request.":
            return None
        raise
    try:
        result = FileRequest.model_validate(reply)
    except ValueError:
        raise ValueError("A worker returned an invalid transfer status.") from None
    if (result.id != request_id or result.operation != operation
            or result.root != endpoint["root"] or result.path != endpoint["path"]):
        raise ValueError("The transfer request does not match the recovered capability input.")
    return result.model_dump(mode="json")


def inspect_capability_transfer(transfer_id, source, destination):
    dest = optional_transfer_status(destination, uuid5(transfer_id, "import"), "import")
    if dest is None:
        return None
    src = None
    if dest["state"] != "completed":
        src = optional_transfer_status(source, uuid5(transfer_id, "export"), "export")
    return {"transfer_id": str(transfer_id), "source": src, "destination": dest}


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
        manifest = await asyncio.to_thread(capability_workflow.manifest, run_id, local_node())
        publication_errors = await publish_plan(manifest)
    except ValueError as error:
        raise ToolError(str(error)) from None
    return await capability_workflow.reconcile(run_id, plan, destination_request, transfer_capability_input,
                                               publication_errors=publication_errors)


@mcp.tool(annotations=READ_ONLY)
async def get_capability_run(run_id: UUID) -> CapabilityReport:
    """Read original capability parts, recovering a published manifest from reachable workers."""
    try:
        manifest = await asyncio.to_thread(capability_workflow.manifest, run_id, local_node())
        if manifest is None:
            manifest = await find_plan_manifest("capability", run_id)
    except ValueError as error:
        raise ToolError(str(error)) from None
    if manifest is None:
        raise ToolError("Unknown capability run on this origin and its reachable replicas.")
    return await capability_workflow.reconcile(run_id, manifest["plan"], destination_request,
                                               inspect_transfer=inspect_capability_transfer)


class LargeFileEndpoint(FileEndpoint):
    model_config = ConfigDict(extra="forbid")


class LargeTransferReport(BaseModel):
    transfer_id: UUID
    authority_node_id: NodeId
    manifest_sha256: str
    manifest: dict[str, JsonValue]
    state: str
    source: dict[str, JsonValue] | None = None
    destination: dict[str, JsonValue] | None = None
    errors: dict[str, str] = Field(default_factory=dict)
    bytes_copied_this_call: int = 0
    blocks_checked_this_call: int = 0
    coordinator_next_block: int | None = None
    publication_may_exist: bool = False
    stop_semantics: str = ("Endpoint epoch fencing; in-flight network delivery is not certified stopped. "
                           "Publication authorized before Stop may complete; an absent receipt does not prove an absent final file.")


# A cursor is only a performance hint for this MCP process, never evidence of
# integrity. Another process/origin or endpoint epoch starts verification at 0;
# final assembly always verifies every block and the whole-file hash again.
_large_cursors = OrderedDict()


def _large_cursor_key(manifest, destination):
    return (jobs.plan_journal.package(manifest)["sha256"], destination["epoch"],
            jobs.transfer_state.digest(destination["header"]))


def _large_cursor_save(key, index):
    _large_cursors[key] = max(index, _large_cursors.get(key, 0))
    _large_cursors.move_to_end(key)
    while len(_large_cursors) > 128:
        _large_cursors.popitem(last=False)


def large_exchange(node_id, operation, *, payload=b"", request_id=None, **fields):
    """The operator registry chooses every connection; no caller-supplied routes."""
    node = registered_node(node_id)
    request = {"action": "large-files", "protocol_version": 2, "operation": operation, **fields}
    if request_id is not None:
        request["request_id"] = str(request_id)
    try:
        if node.node_id == local_node():
            request.pop("action")
            result, data = jobs.large_files.dispatch(jobs.LOCAL_RUNTIME, payload=payload, **request)
        else:
            reply, data = jobs.transfer_wire.remote_exchange(node.route, node.family, request, payload,
                                                             spool_dir=jobs.ROOT)
            if not isinstance(reply, dict) or set(reply) not in ({"result"}, {"error"}):
                raise ValueError("Invalid large-file response envelope.")
            if "error" in reply:
                raise ToolError(str(reply["error"]))
            result = reply["result"]
        if operation != "block-read" and data:
            raise ValueError("Unexpected binary data in a metadata response.")
        return result, data
    except ToolError:
        raise
    except ValueError as error:
        raise ToolError(str(error)) from None
    except (OSError, subprocess.SubprocessError, windows_ssh.LinkError):
        raise ToolError("Large-file service unavailable. Preserve the transfer handle and retry after checking its status.") from None


async def large_info(node_id):
    before = monotonic()
    reply, _ = await asyncio.to_thread(large_exchange, node_id, "info")
    if not isinstance(reply, dict) or reply.get("version") != 2:
        raise ToolError("The helper does not support large-file protocol 2.")
    reply = dict(reply)
    remaining = max(0, reply["file_worker_remaining_seconds"] - (monotonic() - before))
    reply.update(file_worker_remaining_seconds=remaining,
                 file_worker_fresh=reply["file_worker_fresh"] and remaining > 0)
    return reply


@mcp.tool(annotations=READ_ONLY)
async def get_large_file_info(node_id: NodeId) -> dict[str, JsonValue]:
    """Read the owner's explicit large-file opt-in, disk budget and file-aware worker attestation.
    Existing file tools retain their 64 MiB limit; this protocol is separately disabled by default.
    """
    return await large_info(node_id)


async def large_manifest(transfer_id, authority):
    reachable = False
    try:
        first = await asyncio.to_thread(destination_request, authority,
            {"action": "plan", "operation": "get", "kind": "large-file", "plan_id": str(transfer_id)})
        reachable = True
    except ToolError:
        first = None
    local = await asyncio.to_thread(jobs.dispatch, "plan", operation="get", kind="large-file", plan_id=str(transfer_id))
    remote = await find_plan_manifest("large-file", transfer_id)
    manifest = large_transfer_plans.select_replica(str(transfer_id),
        [first, local, jobs.plan_journal.package(remote) if remote is not None else None])
    if manifest is not None and manifest["origin"] != authority:
        raise ToolError("Preserve the original large-file authority as well as its transfer UUID.")
    return manifest, reachable


async def large_authority(manifest):
    expected = jobs.plan_journal.package(manifest)
    receipt = await asyncio.to_thread(destination_request, manifest["origin"],
        {"action": "plan", "operation": "store", "manifest": manifest})
    if isinstance(receipt, dict) and receipt.get("state") == "conflict":
        record = await asyncio.to_thread(destination_request, manifest["origin"],
            {"action": "plan", "operation": "get", "kind": "large-file", "plan_id": manifest["id"]})
        original = large_transfer_plans.select_replica(manifest["id"], [record])
        if original is None or original["origin"] != manifest["origin"] or original["request"] != manifest["request"]:
            raise ToolError("The authority holds a different immutable large-file request.")
        return original
    jobs.plan_journal.check_replication(receipt, expected["sha256"])
    return manifest


async def large_endpoint(manifest, side, operation, *, current=None, payload=b"", **fields):
    endpoint = manifest["request"][side]
    plan = manifest["plan"]
    request_id = plan["source_id" if side == "source" else "destination_id"]
    arguments = {"request_id": request_id, "expected_state_id": plan[side + "_state_id"], **fields}
    if current is not None:
        arguments["epoch"] = current["epoch"]
    return await asyncio.to_thread(large_exchange, endpoint["node_id"], operation, payload=payload, **arguments)


def _large_status(manifest, side, value):
    plan, request = manifest["plan"], manifest["request"]
    role = "export" if side == "source" else "import"
    key = plan["source_id" if side == "source" else "destination_id"]
    if not isinstance(value, dict) or value.get("id") != key or value.get("version") != 2:
        raise ValueError("The large-file endpoint identity differs.")
    expected = jobs.transfer_state.validate_spec(large_transfer_plans.endpoint_spec(manifest, role,
        value.get("spec", {}).get("source_sha256") if role == "import" else None))
    if value.get("spec") != expected:
        raise ValueError("The large-file endpoint request differs from the saved plan.")
    jobs.transfer_state._digest(value.get("request_sha256"))
    if value.get("header") is not None:
        header = jobs.transfer_storage.validate_header(value["header"])
        source_digest = value["request_sha256"] if role == "export" else expected["source_sha256"]
        if header["export_id"] != plan["source_id"] or header["request_sha256"] != source_digest:
            raise ValueError("The content header belongs to a different export request.")
    return value


async def large_report(manifest, copied=0):
    endpoints, errors = {}, {}
    async def read(side):
        try:
            value, _ = await large_endpoint(manifest, side, "status")
            endpoints[side] = _large_status(manifest, side, value)
        except (ToolError, ValueError, TypeError, KeyError) as error:
            endpoints[side] = None
            errors[side] = str(error)
    await asyncio.gather(read("source"), read("destination"))
    source, destination = endpoints["source"], endpoints["destination"]
    if source and destination:
        if (destination["spec"]["source_sha256"] != source["request_sha256"]
                or destination["header"] is not None and destination["header"] != source["header"]):
            raise ToolError("The endpoint content bindings disagree; preserve both endpoints for inspection.")
    receipt = destination.get("receipt") if destination else None
    if receipt is not None:
        header = destination.get("header")
        if (not isinstance(receipt, dict) or header is None or set(receipt) != set(header) | {"import_id", "published"}
                or receipt["published"] is not True or receipt["import_id"] != destination["id"]
                or {key: receipt[key] for key in header} != header):
            raise ToolError("The destination publication receipt differs from its immutable content manifest.")
        phase = "completed"
    elif destination and destination["state"] == "published":
        raise ToolError("The destination claims publication without a durable receipt.")
    elif errors:
        phase = "unavailable"
    elif any(endpoint["state"] == "stopping" for endpoint in (source, destination)):
        phase = "stopping"
    elif any(endpoint["state"] in {"stopped", "revoked", "denied", "cleaned"}
             for endpoint in (source, destination)):
        phase = "stopped"
    elif any(endpoint["approval_stage"] is not None for endpoint in (source, destination)):
        phase = "awaiting_approval"
    else:
        phase = destination["state"] if source["header"] is not None else source["state"]
    return LargeTransferReport(transfer_id=manifest["id"], authority_node_id=manifest["origin"],
        manifest_sha256=jobs.plan_journal.package(manifest)["sha256"], manifest=manifest,
        state=phase, source=source, destination=destination, errors=errors, bytes_copied_this_call=copied,
        publication_may_exist=bool(destination and (destination.get("publication_committed") or receipt is not None)))


async def known_large(transfer_id, authority_node_id):
    authority = registered_node(authority_node_id).node_id
    manifest, _ = await large_manifest(transfer_id, authority)
    if manifest is None:
        raise ToolError("Unknown large-file transfer on its authority and reachable replicas.")
    return manifest


@mcp.tool()
async def start_large_transfer(transfer_id: UUID, authority_node_id: NodeId, source: LargeFileEndpoint,
        destination: LargeFileEndpoint,
        max_bytes: Annotated[int, Field(ge=1, le=1024**4, strict=True)],
        ttl_seconds: Annotated[int, Field(ge=60, le=30*86400, strict=True)] = 86400) -> LargeTransferReport:
    """Create an immutable transfer handle; reserve both endpoints before preparing data.
    Supply an explicit byte budget. Ask approves export/import intent, then the exact
    content manifest on the destination; full proceeds within prior grants and budgets.
    Use advance_large_transfer to move bounded blocks, and get_large_transfer for progress.
    Preserve both UUID and authority on retry or another coordinator. No file is overwritten.
    """
    authority = registered_node(authority_node_id).node_id
    request = {"source": source.model_dump(), "destination": destination.model_dump(),
               "max_bytes": max_bytes, "ttl_seconds": ttl_seconds}
    for side in ("source", "destination"):
        request[side]["node_id"] = registered_node(request[side]["node_id"]).node_id
    try:
        request = large_transfer_plans.normalize_request(request)
        manifest, reachable = await large_manifest(transfer_id, authority)
        if manifest is None:
            if not reachable:
                raise ToolError("An unknown transfer needs its fixed authority online.")
            observed = await asyncio.gather(*(large_info(request[side]["node_id"]) for side in ("source", "destination")))
            proposed = large_transfer_plans.make_manifest(str(transfer_id), authority, request,
                observed[0]["state_id"], observed[1]["state_id"], jobs.time.time())
            manifest = await large_authority(proposed)
        elif manifest["request"] != request:
            raise ToolError("Transfer UUID already binds different endpoints or budgets.")
        errors = await publish_plan(manifest)
        if errors:
            raise ToolError("The immutable transfer could not be replicated before effects.")
        source_result, _ = await asyncio.to_thread(large_exchange, request["source"]["node_id"], "request",
            spec=large_transfer_plans.endpoint_spec(manifest, "export"))
        source_result = _large_status(manifest, "source", source_result)
        await asyncio.to_thread(large_exchange, request["destination"]["node_id"], "request",
            spec=large_transfer_plans.endpoint_spec(manifest, "import", source_result["request_sha256"]))
        return await _advance_large(manifest, 0)
    except (ValueError, TypeError, KeyError) as error:
        raise ToolError(str(error)) from None


async def _advance_large(manifest, maximum):
    progress = {"copied": 0, "checked": 0, "cursor_key": None}
    try:
        return await _advance_large_impl(manifest, maximum, progress)
    except ToolError as error:
        # An owned publication can finish between our snapshot and a guarded
        # operation. Reconcile only this exact terminal refusal and only from
        # an authenticated, cross-bound publication receipt; never retry effects.
        if str(error) != "Endpoint I/O is stopped or terminal.":
            raise
        report = await large_report(manifest, progress["copied"])
        if report.state != "completed":
            raise
        report.blocks_checked_this_call = progress["checked"]
        if progress["cursor_key"] is not None:
            report.coordinator_next_block = _large_cursors.get(progress["cursor_key"], 0)
        return report


async def _advance_large_impl(manifest, maximum, progress_state):
    report = await large_report(manifest)
    if report.state in {"completed", "publishing", "unavailable", "stopping", "stopped", "awaiting_approval"}:
        return report
    source, destination = report.source, report.destination
    for side, current in (("source", source), ("destination", destination)):
        result, _ = await large_endpoint(manifest, side, "reserve", current=current)
        if not result["reservation"]["reserved"]:
            return await large_report(manifest)
    if source["header"] is None:
        await large_endpoint(manifest, "source", "queue", current=source)
        return await large_report(manifest)
    destination, _ = await large_endpoint(manifest, "destination", "bind", current=destination, header=source["header"])
    if (destination["approval_stage"] is not None or maximum == 0
            or destination["state"] in {"publishing", "published"}):
        return await large_report(manifest)
    copied = 0
    # A bounded call is resumable from any origin. The source snapshot and
    # destination blocks remain durable; this first increment has no daemon
    # that continues copying after the coordinating MCP process is gone.
    cursor_key = _large_cursor_key(manifest, destination)
    progress_state["cursor_key"] = cursor_key
    page_offset, examined = _large_cursors.get(cursor_key, 0), 0
    async def progress():
        value = await large_report(manifest, copied)
        value.blocks_checked_this_call = examined
        value.coordinator_next_block = _large_cursors.get(cursor_key, 0)
        return value
    while examined < maximum:
        page, _ = await large_endpoint(manifest, "source", "blocks", offset=page_offset, limit=128)
        for block in page["blocks"]:
            examined += 1
            progress_state["checked"] = examined
            valid, _ = await large_endpoint(manifest, "destination", "verify-block", current=destination,
                                           index=block["index"], expected=block)
            if valid["valid"]:
                _large_cursor_save(cursor_key, block["index"] + 1)
                if examined >= maximum:
                    if block["index"] + 1 == source["header"]["block_count"]:
                        await large_endpoint(manifest, "destination", "queue", current=destination)
                    return await progress()
                continue
            received, data = await large_endpoint(manifest, "source", "block-read", current=source, index=block["index"])
            if received.get("block") != block:
                raise ToolError("The source returned different immutable block metadata.")
            written, _ = await large_endpoint(manifest, "destination", "block-write", current=destination,
                                             index=block["index"], expected=block, payload=data)
            if written.get("block") != block:
                raise ToolError("The destination did not confirm the expected block.")
            copied += len(data)
            progress_state["copied"] = copied
            _large_cursor_save(cursor_key, block["index"] + 1)
            if examined >= maximum:
                if block["index"] + 1 == source["header"]["block_count"]:
                    await large_endpoint(manifest, "destination", "queue", current=destination)
                return await progress()
        if page["next_offset"] is None:
            await large_endpoint(manifest, "destination", "queue", current=destination)
            return await progress()
        page_offset = page["next_offset"]
    return await progress()


@mcp.tool()
async def advance_large_transfer(transfer_id: UUID, authority_node_id: NodeId,
        max_blocks: Annotated[int, Field(ge=1, le=8, strict=True)] = 1) -> LargeTransferReport:
    """Check at most max_blocks of 4 MiB and copy missing blocks, then report progress.
    Repeat to completion. Copy/hash work occurs outside write transactions. An
    origin loss retains endpoint checkpoints; another origin uses the same handle.
    A new coordinator verifies previously committed blocks before skipping them. This is
    not an always-on background transfer or a throughput benchmark.
    """
    try:
        return await _advance_large(await known_large(transfer_id, authority_node_id), max_blocks)
    except (ValueError, TypeError, KeyError) as error:
        raise ToolError(str(error)) from None


@mcp.tool(annotations=READ_ONLY)
async def get_large_transfer(transfer_id: UUID, authority_node_id: NodeId) -> LargeTransferReport:
    """Read durable progress and receipts without copying, hashing or resuming."""
    return await large_report(await known_large(transfer_id, authority_node_id))


async def large_control(transfer_id, authority_node_id, operation):
    manifest = await known_large(transfer_id, authority_node_id)
    report = await large_report(manifest)
    if operation == "cleanup" and report.state != "completed":
        raise ToolError("Cleanup requires the authenticated publication receipt; stopped partials are retained.")
    errors = {}
    async def contact(side):
        current = report.source if side == "source" else report.destination
        if current is None:
            errors[side] = report.errors.get(side, "The endpoint is unavailable; its stop is unconfirmed.")
            return
        if operation == "resume" and current["state"] not in {"stopped"}:
            return
        try:
            await large_endpoint(manifest, side, operation, current=current)
        except (ToolError, ValueError, TypeError, KeyError) as error:
            errors[side] = str(error)
    await asyncio.gather(contact("source"), contact("destination"))
    result = await large_report(manifest)
    result.errors.update(errors)
    if result.errors and result.state != "completed":
        result.state = "unavailable"
    return result


@mcp.tool()
async def stop_large_transfer(transfer_id: UUID, authority_node_id: NodeId) -> LargeTransferReport:
    """Fence old endpoint epochs and stop supervised writers, without taking their I/O locks.
    Current network delivery may still be in flight; the report does not certify a
    remote connection has stopped. Retain partials and disk claims for explicit resume.
    """
    return await large_control(transfer_id, authority_node_id, "stop")


@mcp.tool()
async def resume_large_transfer(transfer_id: UUID, authority_node_id: NodeId) -> LargeTransferReport:
    """Explicitly resume stopped endpoints after their owned writers confirm cleanup."""
    return await large_control(transfer_id, authority_node_id, "resume")


@mcp.tool()
async def cleanup_large_transfer(transfer_id: UUID, authority_node_id: NodeId) -> LargeTransferReport:
    """Remove only private staging after authenticated destination publication; preserve the final file."""
    return await large_control(transfer_id, authority_node_id, "cleanup")


if __name__ == "__main__":
    local_node()
    mcp.run(transport="stdio")
