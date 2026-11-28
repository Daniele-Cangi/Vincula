# Native process execution through MCP

Vincula can execute a new command and new arguments on an enrolled node without registering a command variant. The destination owner must first enable native process execution. This feature adds an execution mechanism, while retaining the existing `ask` and `full` access levels.

Existing installations default to native execution **disabled**, including installations already using `full` for their registered programs and file roots. Installing or updating the helper does not expand that authorization.

## Enable access locally

Run the installed destination helper as its owner. For Linux:

```sh
python3 ~/.local/share/vincula-jobs/jobs.py process-access enable
```

For Windows, in PowerShell:

```powershell
py -3 "$env:LOCALAPPDATA\Vincula-Jobs\jobs.py" process-access enable
```

Use `process-access disable` to revoke future native execution. Disabling cancels pending and queued native jobs, leaves registered-program jobs unchanged, and prevents a claimed native job from launching when disable wins the destination transaction. A process already launched continues until completion, timeout or an explicit `stop_job` request. Changing `full` to `ask` returns queued jobs to pending and requires approval of their process manifest.

The owner chooses the mode with `mode ask` or `mode full`, as before. Native execution is not an additional access level. An enrollment interface can offer the independent native-execution opt-in; this API does not enable it on existing nodes.

## Tools

- `get_process_execution_info(node_id)` reports the helper protocol version, owner opt-in, current mode and input limits. It describes the helper implementation and policy. It does not certify the version or PID of a worker currently running.
- `execute_process(node_id, job_id, argv, cwd?, env?, stdin_text?, timeout_seconds=300)` persists and requests one noninteractive execution. Supply a fresh UUID and reuse the identical request after an uncertain reply.
- `read_process(node_id, job_id)` returns the stored original and resolved requests, their SHA-256, lifecycle, stdout/stderr tails and protocol-aware outcome.
- `stop_job(node_id, job_id)` cancels a queued request or stops the running process tree using the existing destination containment mechanism.

The optional `resources` argument to `execute_process` selects resource-aware
protocol 2. `expected_state_id`, obtained from `get_node_resources`, pins the
destination's durable history for that invocation and can also be supplied to
`read_process`. Calls omitting these fields retain protocol 1 behavior. The
[resource scheduling guide](resource-scheduling.md) describes cooperative
admission, native execution plans, local concurrency policy and their limits.

Example MCP arguments:

```json
{
  "node_id": "registered-node-id",
  "job_id": "c4c546fe-9d0c-466b-959b-84b72b5fd8d7",
  "argv": ["python3", "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
  "cwd": "/absolute/destination/directory",
  "env": {"EXAMPLE_MODE": "offline"},
  "stdin_text": "example input",
  "timeout_seconds": 30
}
```

`argv` is a list passed with `shell=False`. Shell expansion is absent. To use a shell or interpreter, name it explicitly in argv. On Windows, direct `.bat` and `.cmd` paths are rejected; invoke `cmd.exe` explicitly for those scripts. Explicit shell commands retain that shell's own quoting rules.

An executable may be an absolute installed path or a bare name. Bare names resolve once against the destination worker's saved PATH, including its PATHEXT on Windows. Relative executable paths and relative PATH entries are rejected or skipped. Start or update the destination worker if its PATH metadata is absent. The SSH request environment is not a fallback. Explicit `env.PATH` changes the program's environment, while executable resolution still uses the saved worker PATH.

`cwd` must be an existing absolute destination directory. Its default is the destination account home. The resolved executable and cwd are fixed at submission. Explicit environment overrides are immutable and are applied over the actual worker environment at launch; inherited base variables are not snapshotted in the request.

## Approval, retry and results

In `ask`, the destination displays the complete original/resolved manifest in a scrollable preview, including argv, cwd, explicit environment, input, timeout and digest. Approval is bound to that exact SHA-256. For owner CLI approval, inspect `pending`, then run:

```sh
python3 ~/.local/share/vincula-jobs/jobs.py approve JOB_UUID --sha256 MANIFEST_SHA256
```

Use the equivalent installed Windows helper path for Windows. An older approval command that only changes the queue state cannot authorize a native process. In `full`, an enabled owner policy binds the request automatically and queues it immediately.

The same UUID and the same original request retrieve the existing job without resolving PATH again. A different argv, cwd, environment, input or timeout conflicts. Repeating a cancelled, completed or unconfirmed request never runs it again. Use a new UUID for a deliberate new execution. These guarantees identify the requested invocation; they do not pin executable file contents or prevent an owner from replacing programs on disk.

A normal program exit has `outcome.reason="exit"` and preserves its native exit code, including nonzero codes. `state="completed"` means execution completed; it does not mean exit code zero. Confirmed launch errors have `state="failed"`, `reason="launch_error"` and a null program exit code. A native exit already confirmed when the coordinator reads late retains its exit code and `reason="exit"`. A timeout has `state="failed"` and `reason="timeout"`, after process-tree cleanup. An explicit stop has `state="cancelled"` and `reason="stop"`. When a forced Windows JobObject stop cannot retrieve the child's native exit code, that code remains null.

`outcome` identifies results recorded by a protocol-aware worker. Its `started` value is true after confirmed launch, false after confirmed failure to launch, and null when launch acknowledgment was lost and execution may have begun. An incompatible legacy worker cannot launch the native request marker. If such a worker reports its own helper exit, `read_process` returns `unconfirmed` and a null program exit code instead of presenting it as the requested program's result. A worker crash or lost launch/exit acknowledgment similarly preserves uncertainty. Reconnect and read the same UUID rather than submitting a duplicate.

On Windows, the parent creates a private acknowledgment directory under the protected job root and uses it as the helper's working directory. Acknowledgments use only fixed `ready.json` and `outcome.json` names with exclusive staging files; no acknowledgment path comes from the request. The actual program retains its immutable approved `cwd`. Directory cleanup runs after process-tree cleanup, and succeeds in the normal/stop tests. An external open handle or an abrupt worker termination can leave inert acknowledgment files, so directory removal is not a process-containment guarantee.

Timeout is measured after launch acknowledgment, excluding approval and queue wait. The default is 300 seconds, with integer values from 1 to 3600. Input is UTF-8, at most 64 KiB, supplied once followed by EOF. The normalized request is at most 256 KiB of UTF-8 JSON. Native OS limits on argv and environment still apply; large program data can use stdin or files. Requests always travel as a bounded frame on stdin, including info and read operations. The frame is eight lowercase hexadecimal ASCII digits for the UTF-8 JSON byte length, LF, and exactly that many JSON bytes. The receiver does not wait for SSH EOF. The total wire bound is 512 KiB. This avoids command-line expansion and Windows command-line size limits. Responses use UTF-8 and a 1 MiB transport bound. Plan journals use the same framing through the new `journal-framed-stdin` helper command for Windows and large Linux requests; the existing raw JSON `journal-stdin` and base64 `rpc` entrypoints remain available unchanged. Log tails retain the existing 4096-byte limit per stream.

## Account and interaction scope

An approved or preauthorized process runs with the worker account's operating-system permissions. It receives no additional elevation. This API is not an OS sandbox. File-root grants still scope the structured MCP file methods; arbitrary approved code has the normal filesystem access of its account.

This initial API has no PTY/ConPTY, incremental stdin or streaming logs. `read_process` polls bounded log tails. Launching an application does not provide GUI automation or desktop-session consent. Those capabilities require their own desktop backend and authorization.

## Verification

Run the direct manifest, native lifecycle, transport and real MCP tests with the project Python environment:

```sh
python -W error::ResourceWarning -m unittest discover -s agent -p 'test_direct_execution.py'
python -W error::ResourceWarning -m unittest discover -s agent -p 'test_process*.py'
```

They use temporary job databases, account-state directories and workers. Coverage includes default-disabled migration, digest approval, old approval rejection, ask/full, immutable retry, changed PATH, Unicode/input EOF, cwd/environment, native exit codes, launch failure, timeout and deliberately delayed exit observation, process-tree stop, legacy outcome reconciliation and actual MCP stdio calls. Linux also exercises worker SIGKILL, restart and no reexecution. Native Windows tests exercise JobObject containment, an open ready file during child exit, and the native Python UTF-8 helper command with accented text, emoji and responses above 256 KiB.

## Windows SSH transport verified on real hardware

The Windows host tested on 2026-11-22 had no explicit OpenSSH `DefaultShell` registry value, and its `ComSpec` pointed to `cmd.exe`. The fixed command invokes `py.exe -3 -IB -c` with a literal bootstrap that locates the helper through Python `os.environ['LOCALAPPDATA']` and `runpy`. Request JSON and argument values remain exclusively on framed stdin, and Python writes UTF-8 JSON bytes directly to stdout. The command does not expand a user-supplied shell string or change elevation.

In the tested `powershell.exe -EncodedCommand` bridge, a diagnostic reader stopped after 32768 stdin bytes, and large physical SSH calls timed out. A separate explicit PowerShell/.NET wrapper that inherited stdin for its native child also timed out with the large payload. These are observations of those tested paths. Non-default SSH shells are not certified by this verification, and the host's SSHD configuration was not changed.

The native command passed three physical SSH repeats with 180240-byte Unicode process requests and 360783-byte responses, plus three 189440-byte journal requests. Each native call took approximately 0.42–0.53 seconds within the unchanged 12-second request budget. The journal envelope contained 61339 bytes of semantic JSON plus 128101 bytes of explicitly declared synthetic JSON whitespace to test the larger transport bound. Its stored digest and retrieved manifest matched. Truncated, malformed, oversized and wrong-action frames were rejected on both entrypoints without changing private job or journal counts. The legacy helper rejected both new entrypoints before dispatch.

All physical verification used a temporary helper installation and database. Production worker versions, access modes, native opt-in, keys and SSHD settings were unchanged.
