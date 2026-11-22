# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local session lifecycle skeleton. This module does not provision or start Deskflow."""

import argparse
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Event, RLock, Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import desktop_security


ACTIVE = {"prepared", "starting", "running", "stopping"}
MAX_MESSAGE = 65536


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use canonical UUIDs for session and node identities.")
    return value


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"session_id", "revision", "local_id", "primary", "members", "commands"}:
        raise ValueError("A prepared session needs session_id, revision, local_id, primary, members and commands.")
    for key in ("session_id", "local_id", "primary"):
        canonical_uuid(plan[key])
    members = plan["members"]
    if (not isinstance(members, list) or not 1 <= len(members) <= 64
            or any(canonical_uuid(member) != member for member in members) or len(set(members)) != len(members)
            or plan["local_id"] not in members or plan["primary"] not in members):
        raise ValueError("Prepare a unique member list including the local and primary nodes.")
    if not isinstance(plan["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["revision"]):
        raise ValueError("The prepared plan needs the exact registry revision.")
    commands = plan["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= 8:
        raise ValueError("Prepare between one and eight foreground components.")
    names = set()
    for command in commands:
        if not isinstance(command, dict) or not {"name", "argv"} <= command.keys() <= {"name", "argv", "cwd"}:
            raise ValueError("Each component needs name and argv, with optional cwd.")
        name, argv = command["name"], command["argv"]
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or name in names):
            raise ValueError("Use distinct, simple foreground component names.")
        names.add(name)
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
                or not all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 8192 for arg in argv)
                or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file()):
            raise ValueError("Each component needs an argv list with an existing absolute executable; no shell is implied.")
        if "cwd" in command and (not isinstance(command["cwd"], str) or not Path(command["cwd"]).is_absolute()
                                 or not Path(command["cwd"]).is_dir()):
            raise ValueError("Component cwd must be an existing absolute directory.")
    return json.loads(json.dumps(plan))


def atomic_json(path, value):
    stage = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".desktop-", delete=False) as stream:
            stage = Path(stream.name)
            desktop_security.protect_new_file(stage)
            stream.write((json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        stage.replace(path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


@contextmanager
def owner_lock(directory):
    directory = desktop_security.private_directory(directory)
    lock_path = directory / "owner.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        desktop_security.private_file(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR)
    else:
        try:
            desktop_security.protect_new_file(lock_path)
        except OSError:
            os.close(descriptor)
            raise
    with os.fdopen(descriptor, "r+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Supervisor:
    def __init__(self, directory, lease_seconds=5, guardian_lease=2):
        if not 0.5 <= lease_seconds <= 60 or not 0.5 <= guardian_lease <= 10:
            raise ValueError("Lease durations are outside the supported range.")
        self.directory = desktop_security.private_directory(directory)
        self.lease_seconds, self.guardian_lease = lease_seconds, guardian_lease
        self.lock = RLock()
        self.stop_event, self.closed = Event(), Event()
        self.plan, self.jobs, self.worker = None, [], None
        self.deadline = None
        self.state, self.reason = "idle", None
        self.instance = str(uuid4())
        self.heartbeat_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.heartbeat_socket.setblocking(False)
        self.previous_session = None
        try:
            previous = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("session_id") is not None:
                self.previous_session = canonical_uuid(previous["session_id"])
        except (FileNotFoundError, ValueError):
            pass
        # Previous PIDs are diagnostic only. A restarted supervisor never adopts them.
        self.persist()
        self.monitor = Thread(target=self.watch, name="desktop-lease", daemon=True)
        self.monitor.start()

    def status(self):
        with self.lock:
            return {"instance": self.instance, "state": self.state, "reason": self.reason,
                    "session_id": self.plan["session_id"] if self.plan else None,
                    "revision": self.plan["revision"] if self.plan else None,
                    "local_id": self.plan["local_id"] if self.plan else None,
                    "primary": self.plan["primary"] if self.plan else None,
                    "members": list(self.plan["members"]) if self.plan else [],
                    "role": None if not self.plan else "primary" if self.plan["local_id"] == self.plan["primary"] else "client",
                    "previous_session": self.previous_session, "desktop_ready": False,
                    "lease_remaining": max(0, self.deadline - monotonic()) if self.deadline is not None else None,
                    "components": [{"name": job["name"], "guardian_pid": job["process"].pid,
                                    "pid": job.get("pid"), "exit_code": job.get("exit_code"),
                                    "log": str(job["log"])} for job in self.jobs]}

    def persist(self):
        status = self.status()
        if self.plan is not None:
            atomic_json(self.directory / self.plan["session_id"] / "state.json", status)
        atomic_json(self.directory / "state.json", status)

    def prepare(self, plan):
        candidate = validate_plan(plan)
        with self.lock:
            self.require_private_control()
            if self.state in ACTIVE or self.worker is not None and self.worker.is_alive():
                raise ValueError("Stop the current session before preparing another one.")
            session_dir = self.directory / candidate["session_id"]
            if session_dir.exists():
                raise FileExistsError("The session UUID already has a history directory.")
            desktop_security.private_directory(session_dir)
            atomic_json(self.directory / candidate["session_id"] / "plan.json", candidate)
            self.plan, self.jobs = candidate, []
            self.stop_event.clear()
            self.deadline = monotonic() + self.lease_seconds
            self.state, self.reason = "prepared", None
            self.persist()
            return self.status()

    def require_session(self, session_id):
        if self.plan is None or session_id != self.plan["session_id"]:
            raise ValueError("The session ID does not match this supervisor’s prepared session.")

    def require_private_control(self):
        try:
            desktop_security.private_directory(self.directory)
            endpoint = self.directory / "endpoint.json"
            if endpoint.exists():
                desktop_security.private_file(endpoint)
        except OSError:
            self.stop(reason="failed: unsafe local control permissions")
            raise

    def require_live_lease(self):
        now = monotonic()
        if self.deadline is None or now >= self.deadline:
            self.stop(reason="expired")
            raise ValueError("The session lease has expired; prepare a new session.")
        return now

    def commit(self, session_id):
        with self.lock:
            self.require_session(session_id)
            if self.state != "prepared":
                raise ValueError("Only a prepared session can be committed.")
            self.require_private_control()
            self.require_live_lease()
            self.state = "starting"
            self.persist()
            self.require_live_lease()
            self.worker = Thread(target=self.launch, name="desktop-start", daemon=True)
            self.worker.start()
            return self.status()

    def heartbeat(self, session_id):
        with self.lock:
            self.require_session(session_id)
            if self.state not in {"prepared", "starting", "running"} or self.stop_event.is_set():
                raise ValueError("This session cannot renew its lease.")
            self.deadline = self.require_live_lease() + self.lease_seconds
            return self.status()

    def stop(self, session_id=None, reason="stopped"):
        with self.lock:
            if session_id is not None:
                self.require_session(session_id)
            if self.state in ACTIVE and not self.stop_event.is_set():
                self.reason, self.state = reason, "stopping"
                self.deadline = None
                self.stop_event.set()
                self.persist()
            return self.status()

    def read_events(self, job):
        try:
            for line in job["process"].stdout:
                event = json.loads(line)
                with self.lock:
                    if event.get("event") == "started":
                        job["pid"] = event["pid"]
                        job["heartbeat_port"] = event["heartbeat_port"]
                        job["ready"].set()
                    elif event.get("event") == "finished":
                        job["exit_code"] = event.get("exit_code")
                        job["finished"] = True
                        self.persist()
                    elif event.get("event") == "error":
                        job["error"] = event.get("message", "Guardian failed.")
                        job["ready"].set()
        except (OSError, ValueError, KeyError) as error:
            with self.lock:
                job["error"] = str(error)
                job["ready"].set()
        finally:
            job["process"].stdout.close()
            job["reader_done"].set()

    def launch(self):
        try:
            session_dir = self.directory / self.plan["session_id"]
            session_dir.mkdir(mode=0o700, exist_ok=True)
            for command in self.plan["commands"]:
                if self.stop_event.is_set():
                    return
                log_path = session_dir / (command["name"] + ".log")
                token = secrets.token_hex(32)
                specification = session_dir / (command["name"] + ".json")
                atomic_json(specification, {"argv": command["argv"], "cwd": command.get("cwd"),
                                            "log": str(log_path), "lease_seconds": self.guardian_lease,
                                            "heartbeat_token": token})
                with self.lock:
                    if self.stop_event.is_set():
                        return
                    self.require_live_lease()
                with open(session_dir / (command["name"] + "-guardian.log"), "ab", buffering=0) as diagnostics:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("desktop_process.py")), str(specification)],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics,
                                               text=True, encoding="utf-8", close_fds=True,
                                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                                               start_new_session=sys.platform != "win32")
                job = {"name": command["name"], "process": process, "ready": Event(), "log": log_path,
                       "reader_done": Event(), "input_closed": False, "finished": False,
                       "heartbeat_token": token.encode("ascii")}
                with self.lock:
                    self.jobs.append(job)
                    if self.stop_event.is_set():
                        process.stdin.close()
                        job["input_closed"] = True
                Thread(target=self.read_events, args=(job,), daemon=True).start()
                expires = monotonic() + 4
                while not job["ready"].wait(0.03):
                    if self.stop_event.is_set():
                        return
                    if process.poll() is not None or monotonic() >= expires:
                        raise OSError("The component guardian did not acknowledge startup.")
                if job.get("error"):
                    raise OSError(job["error"])
            with self.lock:
                if not self.stop_event.is_set():
                    self.state = "running"
                    self.persist()
        except (OSError, ValueError) as error:
            self.stop(reason="failed: " + str(error))

    def watch(self):
        while not self.closed.wait(0.05):
            with self.lock:
                if self.deadline is not None and monotonic() >= self.deadline:
                    self.stop(reason="expired")
                if self.state in {"starting", "running"}:
                    for job in self.jobs:
                        if job["process"].poll() is not None or job["finished"]:
                            self.stop(reason="failed: component " + job["name"] + " exited")
                            break
                if self.state not in ACTIVE:
                    continue
                for job in self.jobs:
                    stream = job["process"].stdin
                    if not job["input_closed"]:
                        try:
                            if self.stop_event.is_set():
                                stream.close()
                                job["input_closed"] = True
                            elif "heartbeat_port" in job:
                                self.heartbeat_socket.sendto(job["heartbeat_token"], ("127.0.0.1", job["heartbeat_port"]))
                        except (BrokenPipeError, OSError):
                            stream.close()
                            job["input_closed"] = True
                            if not self.stop_event.is_set():
                                self.stop(reason="failed: guardian control closed")
                starting = self.worker is not None and self.worker.is_alive()
                if self.state == "stopping" and not starting and all(job["process"].poll() is not None and job["reader_done"].is_set() for job in self.jobs):
                    self.state = "expired" if self.reason == "expired" else "failed" if self.reason.startswith("failed:") else "stopped"
                    self.persist()

    def close(self):
        self.stop()
        expires = monotonic() + self.guardian_lease + 5
        while self.status()["state"] in ACTIVE and monotonic() < expires:
            sleep(0.02)
        self.closed.set()
        self.monitor.join(timeout=1)
        self.heartbeat_socket.close()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        shutdown = False
        try:
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise ValueError("Control request is incomplete or too large.")
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("token"), str) or not hmac.compare_digest(request["token"], self.server.token):
                raise ValueError("Unauthorized local desktop control request.")
            action = request.get("action")
            allowed = {"prepare": {"plan"}, "status": set(), "commit": {"session_id"},
                       "heartbeat": {"session_id"}, "stop": set(), "shutdown": set()}
            if action not in allowed or set(request) != {"token", "action"} | allowed[action]:
                raise ValueError("Invalid local desktop control action or fields.")
            supervisor = self.server.supervisor
            if action == "prepare":
                result = supervisor.prepare(request["plan"])
            elif action == "commit":
                result = supervisor.commit(request["session_id"])
            elif action == "heartbeat":
                result = supervisor.heartbeat(request["session_id"])
            elif action in {"stop", "shutdown"}:
                shutdown = action == "shutdown"
                result = supervisor.stop()
            else:
                result = supervisor.status()
            self.wfile.write((json.dumps({"ok": True, "session": result}) + "\n").encode("utf-8"))
        except (OSError, ValueError, TypeError, KeyError) as error:
            try:
                self.wfile.write((json.dumps({"ok": False, "error": str(error)}) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            if shutdown:
                Thread(target=self.server.shutdown, daemon=True).start()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False


def serve(directory, lease_seconds=5, guardian_lease=2):
    directory = Path(directory).resolve()
    with owner_lock(directory):
        endpoint = directory / "endpoint.json"
        if endpoint.exists():
            desktop_security.private_file(endpoint)
        supervisor = Supervisor(directory, lease_seconds, guardian_lease)
        try:
            with Server(("127.0.0.1", 0), Handler) as server:
                server.token, server.supervisor = secrets.token_hex(32), supervisor
                atomic_json(endpoint, {"port": server.server_address[1], "token": server.token,
                                       "instance": supervisor.instance})
                print(json.dumps({"ok": True, "endpoint": str(endpoint)}), flush=True)
                server.serve_forever(poll_interval=0.05)
        finally:
            supervisor.close()
            endpoint.unlink(missing_ok=True)


def request(directory, payload, timeout=2):
    if not isinstance(payload, dict) or "token" in payload:
        raise ValueError("Supply a control object without a token; the local endpoint authenticates it.")
    emergency = payload.get("action") in {"stop", "shutdown"} and set(payload) == {"action"}
    # Permission drift must not remove the owner's emergency stop capability.
    # The server still authenticates the token and never permits unsafe prepare/commit.
    directory = Path(directory).resolve() if emergency else desktop_security.private_directory(directory)
    endpoint_path = directory / "endpoint.json"
    if not emergency:
        desktop_security.private_file(endpoint_path)
    endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
    raw = (json.dumps({**payload, "token": endpoint["token"]}) + "\n").encode("utf-8")
    if len(raw) > MAX_MESSAGE:
        raise ValueError("Control request is too large.")
    with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=timeout) as stream:
        stream.sendall(raw)
        with stream.makefile("rb") as reader:
            result = reader.readline(MAX_MESSAGE + 1)
    if len(result) > MAX_MESSAGE:
        raise ValueError("Control reply is too large.")
    return json.loads(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "request"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=float, default=5)
    parser.add_argument("--guardian-lease", type=float, default=2)
    args = parser.parse_args()
    try:
        if args.mode == "serve":
            serve(args.directory, args.lease_seconds, args.guardian_lease)
            return 0
        result = request(args.directory, json.load(sys.stdin))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
