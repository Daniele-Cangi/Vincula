# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Destination-side job queue, operator CLI and worker. Only rpc() is exposed through MCP."""

import argparse
import base64
from contextlib import ExitStack, closing, contextmanager, suppress
import json
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from tempfile import TemporaryDirectory, TemporaryFile
from uuid import UUID, uuid4

WINDOWS = sys.platform == "win32"
ROOT = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Vincula-Jobs" if WINDOWS
        else Path.home() / ".local/share/vincula-jobs")
DATABASE = ROOT / "jobs.sqlite3"
TAIL_BYTES = 4096
NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}")
BATCH_LIMIT = 12
PAGE_LIMIT = 50
SUMMARY = "id, program, state, exit_code, created, finished"
ACTIVE = ("pending", "queued", "running", "stopping")

def load_adapter(name):
    # The SSH entry point runs with Python -I, so load the installed adapter by absolute path.
    spec = spec_from_file_location(name, Path(__file__).resolve().with_name(f"{name}.py"))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


files = load_adapter("vincula_files")
capabilities = load_adapter("vincula_capabilities")
plan_journal = load_adapter("plan_journal")
wire_framing = load_adapter("wire_framing")
direct_execution = load_adapter("direct_execution")


def initialize():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if WINDOWS:
        # An elevated SSH session may create a directory owned by Administrators.
        # Also grant the actual user SID access for the normal desktop worker.
        import ntsecuritycon
        import win32api
        import win32security as security
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), ntsecuritycon.TOKEN_QUERY)
        try:
            account = security.GetTokenInformation(token, security.TokenUser)[0]
        finally:
            token.Close()
        acl = security.ACL()
        for sid in (account, security.ConvertStringSidToSid("S-1-5-18"),
                    security.ConvertStringSidToSid("S-1-5-32-544")):
            acl.AddAccessAllowedAceEx(security.ACL_REVISION,
                                      ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE,
                                      ntsecuritycon.FILE_ALL_ACCESS, sid)
        security.SetNamedSecurityInfo(str(ROOT), security.SE_FILE_OBJECT,
                                      security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
                                      None, None, acl, None)
    with closing(sqlite3.connect(DATABASE)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS policy(mode TEXT NOT NULL);
            INSERT INTO policy SELECT 'ask' WHERE NOT EXISTS (SELECT 1 FROM policy);
            CREATE TABLE IF NOT EXISTS programs(name TEXT PRIMARY KEY, argv TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, program TEXT NOT NULL, argv TEXT NOT NULL,
                state TEXT NOT NULL, exit_code INTEGER, created REAL NOT NULL, finished REAL
            );
            CREATE TABLE IF NOT EXISTS batches(
                id TEXT PRIMARY KEY, request TEXT NOT NULL, plan TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_environment(
                id INTEGER PRIMARY KEY CHECK (id=1), path TEXT NOT NULL, pathext TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shell_sessions(
                id TEXT PRIMARY KEY, created REAL NOT NULL, closed REAL
            );
            CREATE TABLE IF NOT EXISTS shell_entries(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS shell_history ON shell_entries(session_id,sequence);
        """)
        db.executescript(files.SCHEMA)
        db.executescript(capabilities.SCHEMA)
        db.executescript(plan_journal.SCHEMA)
        db.executescript(direct_execution.SCHEMA)
        # Older specialized jobs have no fixed argv or manifest the current worker can launch.
        with db:
            db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE state IN ('pending','queued') "
                       "AND argv='[]' AND id NOT IN (SELECT id FROM capability_jobs)", (time.time(),))
    for name in ("vincula_files.py", "vincula_capabilities.py", "plan_journal.py", "wire_framing.py",
                 "direct_execution.py", "jobs.py"):
        source = Path(__file__).resolve().with_name(name)
        installed = ROOT / name
        if source == installed:
            continue
        stage = ROOT / (uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source, stage)
            stage.replace(installed)
        finally:
            stage.unlink(missing_ok=True)


@contextmanager
def transaction():
    if not DATABASE.is_file():
        raise ValueError("Initialize the job runner on this node first.")
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        yield db


def mode(db):
    return db.execute("SELECT mode FROM policy").fetchone()[0]


def job(db, job_id):
    row = db.execute(f"SELECT {SUMMARY} FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown job ID on this node.")
    return dict(row)


def tail(job_id, stream):
    path = ROOT / f"{job_id}.{stream}"
    try:
        with path.open("rb") as file:
            size = file.seek(0, os.SEEK_END)
            file.seek(max(0, size - TAIL_BYTES))
            return {"text": file.read(TAIL_BYTES).decode("utf-8", "replace"), "truncated": size > TAIL_BYTES}
    except FileNotFoundError:
        return {"text": "", "truncated": False}


def read_output(db, job_id):
    return dict(job(db, job_id), stdout=tail(job_id, "out"), stderr=tail(job_id, "err"))


def enqueue(db, job_id, program):
    existing = db.execute("SELECT program FROM jobs WHERE id=?", (job_id,)).fetchone()
    if existing:
        if existing[0] != program:
            raise ValueError("Job ID already used for another program.")
        return
    registered = db.execute("SELECT argv FROM programs WHERE name=?", (program,)).fetchone()
    if not registered:
        raise ValueError("Program is not authorized on this node.")
    state = "queued" if mode(db) == "full" else "pending"
    db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,?,?)",
               (job_id, program, registered[0], state, time.time()))


def request_stop(db, job_id):
    db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE id=? AND state IN ('pending','queued')",
               (time.time(), job_id))
    db.execute("UPDATE jobs SET state='stopping' WHERE id=? AND state='running'", (job_id,))


def rpc(action, job_id=None, program=None):
    """Fixed dispatcher for the local and SSH interfaces."""
    with transaction() as db:
        if action == "list":
            return {"mode": mode(db),
                    "programs": [r[0] for r in db.execute("SELECT name FROM programs ORDER BY name")],
                    "jobs": [dict(r) for r in db.execute(f"SELECT {SUMMARY} FROM jobs ORDER BY created DESC LIMIT 50")]}
        job_id = str(UUID(job_id))
        if action == "start":
            enqueue(db, job_id, program)
        elif action == "stop":
            request_stop(db, job_id)
        elif action == "read":
            return read_output(db, job_id)
        elif action != "status":
            raise ValueError("Unknown job operation.")
        return job(db, job_id)


def executable_names(path, pathext):
    names = set()
    extensions = {value.strip().casefold() for value in pathext.split(";") if value.strip()}
    for directory in dict.fromkeys(path.split(os.pathsep)):
        if not os.path.isabs(directory):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    with suppress(OSError):
                        if not entry.name.isprintable() or not entry.is_file():
                            continue
                        if WINDOWS and os.path.splitext(entry.name)[1].casefold() in extensions:
                            names.add(entry.name.casefold())
                        elif not WINDOWS and os.access(entry.path, os.X_OK):
                            names.add(entry.name)
        except OSError:
            continue
    return names


def discover_tools(prefix="", offset=0, limit=PAGE_LIMIT):
    if not (0 <= offset and 1 <= limit <= PAGE_LIMIT):
        raise ValueError("Invalid inventory page.")
    with transaction() as db:
        saved = db.execute("SELECT path,pathext FROM worker_environment WHERE id=1").fetchone()
        access = mode(db)
    path, pathext = saved or (os.environ.get("PATH", ""), os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"))
    matching = sorted(name for name in executable_names(path, pathext)
                      if name.casefold().startswith(prefix.casefold()))
    end = offset + limit
    return {"source": "worker" if saved else "request", "mode": access,
            "commands": matching[offset:end], "next_offset": end if end < len(matching) else None}


def active_shell_entry(db, session_id):
    entry = db.execute(
        "SELECT e.job_id, j.state FROM shell_entries e JOIN jobs j ON j.id=e.job_id "
        "WHERE e.session_id=? ORDER BY e.sequence DESC LIMIT 1", (session_id,),
    ).fetchone()
    return entry["job_id"] if entry and entry["state"] in ACTIVE else None


def shell_send(db, session, job_id, program):
    session_id = session["id"]
    if not NAME.fullmatch(program):
        raise ValueError("Enter a single registered program name.")
    linked = db.execute("SELECT session_id FROM shell_entries WHERE job_id=?", (job_id,)).fetchone()
    if linked:
        if linked["session_id"] != session_id:
            raise ValueError("Job ID belongs to another session.")
        # A retried request remains safe even after the session was closed.
        enqueue(db, job_id, program)
    else:
        if session["closed"] is not None:
            raise ValueError("This session is closed.")
        if active_shell_entry(db, session_id):
            raise ValueError("Wait for the session's current command or stop it before sending another.")
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise ValueError("Job ID already used outside this session.")
        enqueue(db, job_id, program)
        db.execute("INSERT INTO shell_entries(session_id,job_id) VALUES(?,?)", (session_id, job_id))
    return job(db, job_id)


def shell_rpc(operation, session_id, job_id=None, program=None, after=0):
    session_id = str(UUID(session_id))
    with transaction() as db:
        if operation == "open":
            db.execute("INSERT OR IGNORE INTO shell_sessions(id,created) VALUES(?,?)", (session_id, time.time()))
        session = db.execute("SELECT id,created,closed FROM shell_sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("Unknown shell session on this node.")
        if operation == "send":
            return shell_send(db, session, str(UUID(job_id)), program)
        if operation == "close" and session["closed"] is None:
            current = active_shell_entry(db, session_id)
            if current:
                request_stop(db, current)
            db.execute("UPDATE shell_sessions SET closed=? WHERE id=?", (time.time(), session_id))
            session = db.execute("SELECT id,created,closed FROM shell_sessions WHERE id=?", (session_id,)).fetchone()
        if operation not in {"open", "read", "close"}:
            raise ValueError("Unknown shell operation.")
        output = None
        if job_id is not None:
            job_id = str(UUID(job_id))
            linked = db.execute(
                "SELECT 1 FROM shell_entries WHERE session_id=? AND job_id=?", (session_id, job_id),
            ).fetchone()
            if not linked:
                raise ValueError("Unknown command in this shell session.")
            output = read_output(db, job_id)
        entries = db.execute(
            "SELECT sequence,job_id FROM shell_entries WHERE session_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?", (session_id, after, PAGE_LIMIT + 1),
        ).fetchall()
        history = [{"sequence": entry["sequence"], "job": job(db, entry["job_id"])} for entry in entries[:PAGE_LIMIT]]
        next_after = history[-1]["sequence"] if len(entries) > PAGE_LIMIT else None
        return dict(session, mode=mode(db), commands=history, output=output, next_after=next_after)


def dispatch_capability(operation, capability_id=None, job_id=None, revision=None, inputs=None, parameters=None):
    with transaction() as db:
        if operation == "describe":
            return capabilities.describe(db, files, capability_id)
        key = str(UUID(job_id))
        if operation == "queue":
            capabilities.queue(db, files, mode(db), key, capability_id, revision, inputs, parameters)
            return job(db, key)
        if operation == "read":
            exists = db.execute("SELECT 1 FROM capability_jobs c JOIN jobs j ON j.id=c.id "
                                "WHERE c.id=? AND j.program=?", (key, capabilities.valid_id(capability_id))).fetchone()
            return read_output(db, key) if exists else None
        raise ValueError("Unknown capability operation.")


def dispatch(action, **payload):
    if action == "process":
        return dispatch_process(**payload)
    if action == "plan":
        with transaction() as db:
            return plan_journal.dispatch(db, **payload)
    if action == "capability":
        return dispatch_capability(**payload)
    if action == "files":
        try:
            with transaction() as db:
                return files.dispatch(db, ROOT, mode(db), **payload)
        except OSError:
            raise ValueError("The file or folder is unavailable, or its filesystem refused the operation.") from None
    if action == "discover":
        return discover_tools(**payload)
    if action == "shell":
        return shell_rpc(**payload)
    if action != "batch":
        return rpc(action, **payload)

    operation, items = payload["operation"], payload["items"]
    if operation not in {"start", "status"} or not 1 <= len(items) <= BATCH_LIMIT:
        raise ValueError("Invalid batch request.")
    results = []
    for item in items:
        try:
            results.append({"job": rpc(operation, item["job_id"], item["program"])})
        except ValueError as error:
            results.append({"error": str(error)})
    return results


def dispatch_process(operation, protocol_version, job_id=None, **payload):
    if type(protocol_version) is not int or protocol_version != direct_execution.PROTOCOL_VERSION:
        raise ValueError("Unsupported direct process protocol version.")
    with transaction() as db:
        if operation == "info" and not payload and job_id is None:
            return dict(direct_execution.support(db), mode=mode(db))
        key = str(UUID(job_id))
        if operation == "execute":
            direct_execution.queue(db, mode(db), key, **payload)
        elif operation != "read" or payload:
            raise ValueError("Unknown direct process operation.")
        details = read_output(db, key)
        value = direct_execution.result(db, key)
        # Legacy workers cannot launch a null-argv marker, but may report their own
        # helper's exit as completed. Only the protocol-aware outcome is a process result.
        if value["outcome"] is None and details["state"] in {"completed", "failed", "unconfirmed"}:
            details.update(state="unconfirmed", exit_code=None)
        elif value["outcome"] is not None:
            details["exit_code"] = value["outcome"]["program_exit_code"]
        return dict(details, **value)


def set_mode(db, new_mode):
    if mode(db) == new_mode:
        return
    db.execute("UPDATE policy SET mode=?", (new_mode,))
    if new_mode == "ask":
        db.execute("UPDATE jobs SET state='pending' WHERE state='queued'")
        db.execute("UPDATE direct_process_jobs SET approved_sha256=NULL,approval_mode=NULL "
                   "WHERE id IN (SELECT id FROM jobs WHERE state='pending')")
        db.execute("UPDATE file_requests SET state='pending' WHERE state='approved' AND auto=1")


def decide(db, job_id, allowed, expected_sha256=None):
    job_id = str(UUID(job_id))
    if allowed and db.execute("SELECT 1 FROM direct_process_jobs WHERE id=?", (job_id,)).fetchone():
        direct_execution.approve(db, job_id, expected_sha256)
        return
    changed = db.execute("UPDATE jobs SET state=?,finished=? WHERE id=? AND state='pending'",
                         ("queued" if allowed else "cancelled", None if allowed else time.time(), str(UUID(job_id))))
    if not changed.rowcount:
        raise ValueError("Job is not awaiting approval.")


def pending_jobs(db):
    return db.execute(
        "SELECT j.id,j.program,COALESCE(d.manifest,p.manifest,j.argv) AS argv FROM jobs j "
        "LEFT JOIN capability_jobs p ON p.id=j.id LEFT JOIN direct_process_jobs d ON d.id=j.id "
        "WHERE j.state='pending' ORDER BY j.created",
    ).fetchall()


def operator(args):
    with transaction() as db:
        if args.command == "mode":
            set_mode(db, args.mode)
        elif args.command in {"register", "remove"}:
            if not NAME.fullmatch(args.name):
                raise ValueError("Invalid program name.")
            if args.command == "register":
                if not args.argv or not Path(args.argv[0]).is_absolute() or not Path(args.argv[0]).is_file():
                    raise ValueError("Specify the absolute path of an installed executable and its fixed arguments.")
                db.execute("INSERT OR REPLACE INTO programs VALUES(?,?)", (args.name, json.dumps(args.argv)))
            else:
                db.execute("DELETE FROM programs WHERE name=?", (args.name,))
            db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE program=? AND state IN ('pending','queued')",
                       (time.time(), args.name))
        elif args.command == "pending":
            for row in pending_jobs(db):
                print(json.dumps(dict(row)))
        elif args.command in {"approve", "deny"}:
            decide(db, args.id, args.command == "approve", getattr(args, "sha256", None))
        elif args.command == "process-access":
            direct_execution.set_enabled(db, args.access == "enable")
        elif args.command in {"files-grant", "files-revoke"}:
            if not NAME.fullmatch(args.alias):
                raise ValueError("Invalid folder alias.")
            files.grant(db, args.alias, getattr(args, "directory", None), getattr(args, "write", False))
        elif args.command == "files-pending":
            for row in files.permission_items(db):
                print(json.dumps(dict(row)))
        elif args.command in {"files-approve", "files-deny"}:
            files.decide(db, args.id, args.command == "files-approve")
        elif args.command == "capability-revoke":
            capabilities.revoke(db, args.id)

@contextmanager
def process_lock(path):
    with path.open("a+b") as lock:
        lock.seek(0)
        if WINDOWS:
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock


def exclusive_worker():
    return process_lock(ROOT / "worker.lock")


def linux_children():
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    return [int(pid) for pid in children.read_text(encoding="ascii").split()]


def reap_linux_children(program_pid):
    for pid in linux_children():
        if pid != program_pid:
            with suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)


def stop_linux_program(proc):
    # Keep the leader's PID reserved until its group has received the signal.
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    # A subreaper adopts orphaned descendants, including programs that call setsid.
    while True:
        for pid in linux_children():
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.025)


@contextmanager
def launch_context(payload):
    """Keep legacy argv unchanged; new launches carry native options as JSON data."""
    if not isinstance(payload, dict):
        yield payload, {"stdin": subprocess.DEVNULL}
        return
    if (payload.keys() != {"protocol_version", "request"}
            or type(payload["protocol_version"]) is not int
            or payload["protocol_version"] != direct_execution.PROTOCOL_VERSION):
        raise ValueError("Unsupported native launcher protocol.")
    # The public request bound has already been checked before queueing. The
    # internal specification additionally inherits the worker environment.
    spec = direct_execution.request(**payload["request"], _check_size=False)
    if not Path(spec["argv"][0]).is_absolute() or spec["cwd"] is None or spec["env"] is None:
        raise ValueError("The native launcher requires a resolved process specification.")
    with TemporaryFile() as source:
        source.write((spec["stdin_text"] or "").encode("utf-8"))
        source.seek(0)
        yield spec["argv"], {"stdin": source, "cwd": spec["cwd"], "env": spec["env"],
                            "executable": spec["argv"][0]}


def supervise_linux(parent_pid, ready_fd):
    import ctypes

    interrupted = False

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, interrupt)
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG closes the gap left by a worker killed before its finally.
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not supervise the Linux worker")
    if os.getppid() != parent_pid:
        raise RuntimeError("The worker exited before supervision was established.")
    # PR_SET_CHILD_SUBREAPER keeps escaped descendants available for cleanup/reaping.
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not supervise Linux descendants")
    payload = json.load(sys.stdin)
    if interrupted or os.getppid() != parent_pid:
        raise RuntimeError("The worker exited before its program started.")
    with launch_context(payload) as (argv, options):
        try:
            proc = subprocess.Popen(argv, start_new_session=True, **options)
        except Exception as error:
            os.write(ready_fd, json.dumps({"error": str(error)}).encode() + b"\n")
            raise
        try:
            with suppress(BrokenPipeError):
                os.write(ready_fd, b'{"started":true}\n')
            while not interrupted and not exited(proc):
                reap_linux_children(proc.pid)
                time.sleep(0.025)
        finally:
            try:
                native_exited = exited(proc)
            finally:
                stop_linux_program(proc)
    with suppress(BrokenPipeError):
        result = {"exit_code": proc.returncode}
        if isinstance(payload, dict):
            result["native_exited"] = native_exited
        os.write(ready_fd, json.dumps(result).encode() + b"\n")
    os.close(ready_fd)
    return 0


class UnconfirmedLaunchError(RuntimeError):
    """The launcher may have run the program before losing its confirmation."""


def confirm_linux_launch(proc, ready, payload):
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
        line = ready.readline(8192)
        if not line:
            raise UnconfirmedLaunchError("Linux supervisor exited before confirming program launch.")
        result = json.loads(line)
    except (OSError, ValueError) as error:
        raise UnconfirmedLaunchError("Linux supervisor could not confirm program launch.") from error
    if isinstance(result, dict):
        if set(result) == {"started"} and result["started"] is True:
            return
        if set(result) == {"error"} and isinstance(result["error"], str):
            raise RuntimeError(result["error"] or "Linux program could not start.")
    raise UnconfirmedLaunchError("Linux supervisor returned an invalid launch confirmation.")


@contextmanager
def linux_program_process(argv, stdout, stderr, worker_lock):
    payload = json.dumps(argv).encode()
    reader, writer = os.pipe()
    inherited = (writer,) if worker_lock is None else (writer, worker_lock)
    with os.fdopen(reader, "rb") as ready:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", __file__, "_supervise", str(os.getpid()), str(writer)],
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, cwd=ROOT,
                start_new_session=True, pass_fds=inherited,
            )
        finally:
            os.close(writer)
        launched = False
        try:
            confirm_linux_launch(proc, ready, payload)
            launched = True
            yield proc
        finally:
            with suppress(BrokenPipeError):
                proc.stdin.close()
            if not exited(proc):
                proc.terminate()
            # The supervisor retains the worker lock until its process group is stopped.
            proc.wait()
            if launched:
                line = ready.readline(8192)
                result = json.loads(line) if line else {}
                expected = {"exit_code", "native_exited"} if isinstance(argv, dict) else {"exit_code"}
                if (proc.returncode != 0 or set(result) != expected
                        or ("native_exited" in result and type(result["native_exited"]) is not bool)
                        or type(result["exit_code"]) is not int):
                    raise RuntimeError("Linux supervisor could not confirm the program exit.")
                proc.returncode = result["exit_code"]
                proc.native_exited = result.get("native_exited", False)


def write_launch_status(value, *, completed=False):
    # The launcher cwd is a fresh private directory created by its parent.
    # No acknowledgment filename or directory comes from the JSON request.
    target = Path("outcome.json" if completed else "ready.json")
    stage = Path("outcome.tmp" if completed else "ready.tmp")
    with stage.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
    stage.replace(target)


def windows_launch(payload):
    if not isinstance(payload, dict):
        return subprocess.call(payload, stdin=subprocess.DEVNULL)
    # The enclosing JobObject owns cleanup even if an acknowledgment is lost.
    with launch_context(payload) as (argv, options):
        try:
            proc = subprocess.Popen(argv, **options)
        except Exception as error:
            write_launch_status({"error": str(error)[:4096]})
            raise
        write_launch_status({"started": True})
        code = proc.wait()
        # Never replace the ready file while another Windows process has it
        # open without FILE_SHARE_DELETE. Each acknowledgment is immutable.
        write_launch_status({"started": True, "exit_code": code}, completed=True)
    return 0


def read_launch_status(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("Oversized native launch acknowledgment.")
        value = json.loads(raw)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise UnconfirmedLaunchError("Invalid native launch acknowledgment.")
    return value


def confirm_windows_launch(proc, status):
    deadline = time.monotonic() + 10
    while True:
        try:
            value = read_launch_status(status)
        except (ValueError, OSError) as error:
            raise UnconfirmedLaunchError("Windows could not confirm program launch.") from error
        if value is not None:
            if set(value) == {"error"} and isinstance(value["error"], str):
                raise RuntimeError(value["error"])
            if (value.get("started") is True and value.keys() <= {"started", "exit_code"}
                    and ("exit_code" not in value or type(value["exit_code"]) is int)):
                return
            raise UnconfirmedLaunchError("Windows returned an invalid launch acknowledgment.")
        if proc.poll() is not None or time.monotonic() >= deadline:
            raise UnconfirmedLaunchError("Windows helper exited before confirming program launch.")
        time.sleep(0.025)


@contextmanager
def program_process(argv, stdout, stderr, worker_lock=None):
    streams = dict(stdout=stdout, stderr=stderr, cwd=ROOT)
    if WINDOWS:
        import win32job
        # The helper waits for argv until its entire descendant tree is contained.
        group = win32job.CreateJobObject(None, "Vincula-" + uuid4().hex)
        launch_directory = None
        status = None
        try:
            if isinstance(argv, dict):
                launch_directory = TemporaryDirectory(prefix="native-launch-", dir=ROOT, ignore_cleanup_errors=True)
                streams["cwd"] = launch_directory.name
                status = Path(launch_directory.name) / "ready.json"
            limits = win32job.QueryInformationJobObject(group, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(group, win32job.JobObjectExtendedLimitInformation, limits)
            proc = subprocess.Popen([sys.executable, "-I", __file__, "_launch"], stdin=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW, **streams)
            try:
                win32job.AssignProcessToJobObject(group, int(proc._handle))
            except Exception:
                proc.kill()
                proc.wait()
                proc.stdin.close()
                raise
            launched = False
            try:
                proc.stdin.write(json.dumps(argv).encode())
                proc.stdin.close()
                if status is not None:
                    confirm_windows_launch(proc, status)
                launched = True
                yield proc
            finally:
                with suppress(BrokenPipeError):
                    proc.stdin.close()
                normal_exit = proc.poll() is not None
                win32job.TerminateJobObject(group, 1)
                proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while win32job.QueryInformationJobObject(group, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Windows could not confirm that the job processes stopped.")
                    time.sleep(0.025)
                if status is not None and launched:
                    result = read_launch_status(status.with_name("outcome.json"))
                    if (result and result.get("started") is True and set(result) == {"started", "exit_code"}
                            and type(result["exit_code"]) is int):
                        proc.returncode = result["exit_code"]
                        proc.native_exited = True
                    elif normal_exit:
                        raise UnconfirmedLaunchError("Windows could not confirm the program exit.")
                    else:
                        # A forced stop confirms containment, but not the child's native exit code.
                        proc.returncode = None
                        proc.native_exited = False
        finally:
            group.Close()
            if launch_directory is not None:
                launch_directory.cleanup()
    else:
        with linux_program_process(argv, stdout, stderr, worker_lock) as proc:
            yield proc


def exited(proc):
    if WINDOWS:
        return proc.poll() is not None
    # Observe the exit without reaping until cleanup has signaled the process group.
    return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def run_one(row, worker_lock=None):
    job_id = row["id"]
    code = None
    state = "unconfirmed"
    proc = None
    direct = "program" in row.keys() and row["program"] == direct_execution.PROGRAM
    reason = "unconfirmed"
    try:
        with ExitStack() as stack:
            with transaction() as db:
                stopped_before_launch = job(db, job_id)["state"] == "stopping"
                if not stopped_before_launch:
                    out = stack.enter_context((ROOT / f"{job_id}.out").open("xb"))
                    err = stack.enter_context((ROOT / f"{job_id}.err").open("xb"))
                    direct = direct or db.execute("SELECT 1 FROM direct_process_jobs WHERE id=?", (job_id,)).fetchone() is not None
                    if direct:
                        spec = direct_execution.launch_spec(db, job_id, os.environ)
                        argv = {"protocol_version": direct_execution.PROTOCOL_VERSION, "request": spec}
                    else:
                        argv = (capabilities.arguments(db, job_id, ROOT) if capabilities.ID.fullmatch(row["program"])
                                else json.loads(row["argv"]))
                    proc = stack.enter_context(program_process(argv, out, err, worker_lock))
            if stopped_before_launch:
                state = "cancelled"
                reason = "stop"
                return
            deadline = time.monotonic() + spec["timeout_seconds"] if direct else None
            timed_out = False
            while not exited(proc):
                with transaction() as db:
                    stopping = job(db, job_id)["state"] == "stopping"
                if stopping:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.2)
            else:
                stopping = False
        if direct and timed_out and getattr(proc, "native_exited", False):
            # A late coordinator read must not turn an already confirmed native
            # exit into a timeout simply because it noticed the helper later.
            timed_out = False
        state = "cancelled" if stopping else "failed" if timed_out else "completed"
        reason = "stop" if stopping else "timeout" if timed_out else "exit"
        code = proc.returncode
    except Exception as error:
        if proc is None and not isinstance(error, UnconfirmedLaunchError):
            state = "failed"
        reason = "launch_error" if state == "failed" else "unconfirmed"
        with suppress(OSError), (ROOT / f"{job_id}.err").open("ab") as err:
            err.write(str(error).encode("utf-8", "replace"))
    finally:
        with transaction() as db:
            db.execute("UPDATE jobs SET state=?,exit_code=?,finished=? WHERE id=?", (state, code, time.time(), job_id))
            if direct:
                started = None if state == "unconfirmed" and proc is None else proc is not None
                direct_execution.record_outcome(db, job_id, state, code, reason, started)


def work():
    with exclusive_worker() as ownership:
        shutdown = ROOT / "worker.stop"
        shutdown.unlink(missing_ok=True)
        with transaction() as db:
            db.execute("INSERT OR REPLACE INTO worker_environment VALUES(1,?,?)",
                       (os.environ.get("PATH", ""), os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")))
            db.execute("UPDATE jobs SET state='unconfirmed',finished=? WHERE state IN ('running','stopping')",
                       (time.time(),))
        while not shutdown.exists():
            with transaction() as db:
                row = db.execute("SELECT id,program,argv FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            if row:
                run_one(row, ownership.fileno())
            else:
                time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "worker", "shutdown", "pending"):
        commands.add_parser(name)
    commands.add_parser("mode").add_argument("mode", choices=("ask", "full"))
    register = commands.add_parser("register")
    register.add_argument("name")
    register.add_argument("argv", nargs=argparse.REMAINDER)
    commands.add_parser("remove").add_argument("name")
    for name in ("approve", "deny"):
        approval = commands.add_parser(name)
        approval.add_argument("id")
        approval.add_argument("--sha256", help="Digest of the reviewed native process manifest.")
    commands.add_parser("process-access").add_argument("access", choices=("enable", "disable"))
    grant = commands.add_parser("files-grant")
    grant.add_argument("alias")
    grant.add_argument("directory")
    grant.add_argument("--write", action="store_true")
    commands.add_parser("files-revoke").add_argument("alias")
    commands.add_parser("capability-register", help="Register a capability from a JSON manifest on stdin.")
    commands.add_parser("capability-revoke").add_argument("id")
    commands.add_parser("files-pending")
    for name in ("files-approve", "files-deny"):
        commands.add_parser(name).add_argument("id")
    commands.add_parser("rpc").add_argument("payload")
    commands.add_parser("journal-stdin")
    commands.add_parser("journal-framed-stdin")
    commands.add_parser("process-stdin")
    args = parser.parse_args()
    if args.command == "init":
        initialize()
    elif args.command == "worker":
        work()
    elif args.command == "shutdown":
        (ROOT / "worker.stop").touch()
    elif args.command == "capability-register":
        raw = sys.stdin.buffer.read(capabilities.MAX_MANIFEST_BYTES + 1)
        if len(raw) > capabilities.MAX_MANIFEST_BYTES:
            parser.error("The capability manifest exceeds 16 KiB.")
        try:
            manifest = json.loads(raw)
            with transaction() as db:
                capabilities.register(db, files, manifest)
        except (OSError, ValueError, TypeError) as error:
            parser.error(str(error))
    elif args.command in {"rpc", "journal-stdin", "journal-framed-stdin", "process-stdin"}:
        try:
            if args.command in {"journal-stdin", "journal-framed-stdin", "process-stdin"}:
                maximum = (plan_journal.MAX_REQUEST_BYTES if args.command != "process-stdin"
                           else direct_execution.MAX_TRANSPORT_BYTES)
                raw = (sys.stdin.buffer.read(maximum + 1) if args.command == "journal-stdin"
                       else wire_framing.read(sys.stdin.buffer, maximum))
                if len(raw) > maximum:
                    raise ValueError("Plan journal request exceeds its size limit." if args.command == "journal-stdin"
                                     else "The process stdin request exceeds its size limit.")
                request = json.loads(raw)
                action = "process" if args.command == "process-stdin" else "plan"
                if not isinstance(request, dict) or request.get("action") != action:
                    raise ValueError("The journal stdin protocol accepts only plan operations." if action == "plan"
                                     else "The process stdin protocol accepts only process operations.")
            else:
                request = json.loads(base64.b64decode(args.payload, validate=True))
            result = {"result": dispatch(**request)}
        except (ValueError, KeyError, TypeError, sqlite3.Error, OSError) as error:
            result = {"error": str(error)}
        if args.command in {"process-stdin", "journal-framed-stdin"}:
            sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
        else:
            print(json.dumps(result))
    else:
        operator(args)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_supervise" and not WINDOWS:
        sys.exit(supervise_linux(int(sys.argv[2]), int(sys.argv[3])))
    elif sys.argv[1:] == ["_launch"] and WINDOWS:
        import ctypes
        code = windows_launch(json.load(sys.stdin))
        ctypes.windll.kernel32.ExitProcess(ctypes.c_uint32(code))
    else:
        main()
