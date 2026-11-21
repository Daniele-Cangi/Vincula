# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Destination-side job queue, operator CLI and worker. Only rpc() is exposed through MCP."""

import argparse
import base64
from contextlib import ExitStack, closing, contextmanager, suppress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import UUID, uuid4

WINDOWS = sys.platform == "win32"
ROOT = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Vincula-Jobs" if WINDOWS
        else Path.home() / ".local/share/vincula-jobs")
DATABASE = ROOT / "jobs.sqlite3"
TAIL_BYTES = 4096
NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}")
BATCH_LIMIT = 12
SUMMARY = "id, program, state, exit_code, created, finished"


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
        """)
    installed = ROOT / "jobs.py"
    if Path(__file__).resolve() != installed:
        stage = ROOT / (uuid4().hex + ".tmp")
        try:
            shutil.copyfile(__file__, stage)
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


def rpc(action, job_id=None, program=None):
    """Fixed dispatcher for the local and SSH interfaces."""
    with transaction() as db:
        if action == "list":
            return {"mode": mode(db),
                    "programs": [r[0] for r in db.execute("SELECT name FROM programs ORDER BY name")],
                    "jobs": [dict(r) for r in db.execute(f"SELECT {SUMMARY} FROM jobs ORDER BY created DESC LIMIT 50")]}
        job_id = str(UUID(job_id))
        if action == "start":
            existing = db.execute("SELECT program FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing:
                if existing[0] != program:
                    raise ValueError("Job ID already used for another program.")
            else:
                registered = db.execute("SELECT argv FROM programs WHERE name=?", (program,)).fetchone()
                if not registered:
                    raise ValueError("Program is not authorized on this node.")
                state = "queued" if mode(db) == "full" else "pending"
                db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,?,?)",
                           (job_id, program, registered[0], state, time.time()))
        elif action == "stop":
            db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE id=? AND state IN ('pending','queued')",
                       (time.time(), job_id))
            db.execute("UPDATE jobs SET state='stopping' WHERE id=? AND state='running'", (job_id,))
        elif action not in {"read", "status"}:
            raise ValueError("Unknown job operation.")
        result = job(db, job_id)
        if action == "read":
            result.update(stdout=tail(job_id, "out"), stderr=tail(job_id, "err"))
        return result


def dispatch(action, **payload):
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


def set_mode(db, new_mode):
    if mode(db) == new_mode:
        return
    db.execute("UPDATE policy SET mode=?", (new_mode,))
    if new_mode == "ask":
        db.execute("UPDATE jobs SET state='pending' WHERE state='queued'")


def decide(db, job_id, allowed):
    changed = db.execute("UPDATE jobs SET state=?,finished=? WHERE id=? AND state='pending'",
                         ("queued" if allowed else "cancelled", None if allowed else time.time(), str(UUID(job_id))))
    if not changed.rowcount:
        raise ValueError("Job is not awaiting approval.")


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
            for row in db.execute("SELECT id,program,argv FROM jobs WHERE state='pending' ORDER BY created"):
                print(json.dumps(dict(row)))
        elif args.command in {"approve", "deny"}:
            decide(db, args.id, args.command == "approve")

@contextmanager
def exclusive_worker():
    with (ROOT / "worker.lock").open("a+b") as lock:
        lock.seek(0)
        if WINDOWS:
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def program_process(argv, stdout, stderr):
    streams = dict(stdout=stdout, stderr=stderr, cwd=ROOT)
    if WINDOWS:
        import win32job
        # The helper waits for argv until its entire descendant tree is contained.
        group = win32job.CreateJobObject(None, "Vincula-" + uuid4().hex)
        try:
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
            try:
                proc.stdin.write(json.dumps(argv).encode())
                proc.stdin.close()
                yield proc
            finally:
                with suppress(BrokenPipeError):
                    proc.stdin.close()
                win32job.TerminateJobObject(group, 1)
                proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while win32job.QueryInformationJobObject(group, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Windows could not confirm that the job processes stopped.")
                    time.sleep(0.025)
        finally:
            group.Close()
    else:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True, **streams)
        try:
            yield proc
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def exited(proc):
    if WINDOWS:
        return proc.poll() is not None
    # Observe the exit without reaping until cleanup has signaled the process group.
    return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def run_one(row):
    job_id = row["id"]
    code = None
    state = "unconfirmed"
    proc = None
    try:
        with ExitStack() as stack:
            with transaction() as db:
                stopped_before_launch = job(db, job_id)["state"] == "stopping"
                if not stopped_before_launch:
                    out = stack.enter_context((ROOT / f"{job_id}.out").open("xb"))
                    err = stack.enter_context((ROOT / f"{job_id}.err").open("xb"))
                    proc = stack.enter_context(program_process(json.loads(row["argv"]), out, err))
            if stopped_before_launch:
                state = "cancelled"
                return
            while not exited(proc):
                with transaction() as db:
                    stopping = job(db, job_id)["state"] == "stopping"
                if stopping:
                    break
                time.sleep(0.2)
            else:
                stopping = False
        state = "cancelled" if stopping else "completed"
        code = proc.returncode
    except Exception as error:
        if proc is None:
            state = "failed"
        with suppress(OSError), (ROOT / f"{job_id}.err").open("ab") as err:
            err.write(str(error).encode("utf-8", "replace"))
    finally:
        with transaction() as db:
            db.execute("UPDATE jobs SET state=?,exit_code=?,finished=? WHERE id=?", (state, code, time.time(), job_id))


def work():
    with exclusive_worker():
        shutdown = ROOT / "worker.stop"
        shutdown.unlink(missing_ok=True)
        with transaction() as db:
            db.execute("UPDATE jobs SET state='unconfirmed',finished=? WHERE state IN ('running','stopping')",
                       (time.time(),))
        while not shutdown.exists():
            with transaction() as db:
                row = db.execute("SELECT id,argv FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            if row:
                run_one(row)
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
        commands.add_parser(name).add_argument("id")
    commands.add_parser("rpc").add_argument("payload")
    args = parser.parse_args()
    if args.command == "init":
        initialize()
    elif args.command == "worker":
        work()
    elif args.command == "shutdown":
        (ROOT / "worker.stop").touch()
    elif args.command == "rpc":
        try:
            request = json.loads(base64.b64decode(args.payload, validate=True))
            result = {"result": dispatch(**request)}
        except (ValueError, KeyError, TypeError, sqlite3.Error, OSError) as error:
            result = {"error": str(error)}
        print(json.dumps(result))
    else:
        operator(args)


if __name__ == "__main__":
    if sys.argv[1:] == ["_launch"] and WINDOWS:
        import ctypes
        code = subprocess.call(json.load(sys.stdin), stdin=subprocess.DEVNULL)
        ctypes.windll.kernel32.ExitProcess(ctypes.c_uint32(code))
    else:
        main()
