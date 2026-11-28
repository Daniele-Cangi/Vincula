# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local Linux receiver setup. No MCP endpoint, GUI, key management or runtime migration."""
import argparse
from contextlib import ExitStack, closing
from dataclasses import dataclass
import hashlib
import inspect
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
from uuid import UUID, uuid4

if sys.platform.startswith("linux"):
    import fcntl
    import pwd
else:
    fcntl = pwd = None

HELPERS = ("jobs.py", "direct_execution.py", "plan_journal.py", "vincula_capabilities.py", "vincula_files.py", "wire_framing.py",
           "resource_policy.py", "worker_resources.py", "execution_plans.py")
UNIT = "vincula-core-worker.service"
OLD_UNIT = "vincula-agent-worker.service"
ACTIVE = ("pending", "queued", "running", "stopping")
NETWORK_HELPER = b'''# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""One operator-selected OS connection; no secrets or application authentication."""
import argparse,ipaddress,json,subprocess
from pathlib import Path
from uuid import UUID
def present(address):
 executable=next(str(p) for p in (Path("/usr/sbin/ip"),Path("/usr/bin/ip")) if p.is_file())
 p=subprocess.run([executable,"-j","-4","address","show"],stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=5)
 return any(a.get("local")==address for d in json.loads(p.stdout) for a in d.get("addr_info",[]))
def main():
 p=argparse.ArgumentParser();p.add_argument("--address",type=ipaddress.IPv4Address,required=True);p.add_argument("--connection",type=UUID,required=True);a=p.parse_args()
 if present(str(a.address)):return 0
 p=subprocess.run(["/usr/bin/nmcli","--wait","30","connection","up","uuid",str(a.connection)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=35)
 return 0 if p.returncode==0 and present(str(a.address)) else 1
if __name__=="__main__":raise SystemExit(main())
'''

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def table_digests(db):
    """Canonical values for both tuple connections and the runtime's SQLite.Row."""
    import hashlib
    tables = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if name not in {"policy", "direct_execution_policy", "worker_environment"}:
            rows = [tuple(row) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]
            tables[name] = hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()
    return tables

def require(condition, message):
    if not condition:
        raise ValueError(message)

def linux_only():
    require(sys.platform.startswith("linux"), "Core receiver setup requires Linux")

def root_owned(path, interpreter=False):
    """Read-only check before executing code as an explicitly selected root owner."""
    require(path.is_absolute() and ".." not in path.parts, "Root owner requires absolute trusted paths")
    for item in reversed((path, *path.parents)):
        info = item.lstat()
        link = stat.S_ISLNK(info.st_mode)
        require(info.st_uid == 0 and ((link and interpreter) or (not link and not info.st_mode & 0o022)), "Root owner path is not OS-protected")
    if interpreter:
        root_owned(path.resolve(strict=True))

def root_caches(directory):
    cache = directory / "__pycache__"
    if os.path.lexists(cache):
        root_owned(cache)
        for path in cache.rglob("*"):
            root_owned(path)

def valid_worker_path(value):
    require(isinstance(value, str) and value and all(ord(c) >= 32 and ord(c) != 127 for c in value)
            and all(Path(part).is_absolute() for part in value.split(":")), "Worker PATH entries must be absolute and free of controls")
    return value

def unique_object(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "Duplicate manifest key")
        result[name] = value
    return result

def word(value, executable=False):
    require(isinstance(value, str) and value and all(ord(c) >= 32 and ord(c) != 127 for c in value), "Invalid unit value")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (value.replace("$", "$$") if executable else value) + '"'

def scalar_path(value):
    """WorkingDirectory consumes a literal path, not the unit's argv/list syntax."""
    require(Path(value).is_absolute() and all(ord(c) >= 32 and ord(c) != 127 for c in value), "Invalid scalar unit path")
    return value.replace("%", "%%")

def read_file(path, owners):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in owners, "Unsafe file identity")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read(), (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)

@dataclass(frozen=True)
class Settings:
    owner: str
    release: Path
    manifest_sha256: str
    access: str
    native_execution: str
    python: Path
    network_profile: str | None = None
    expected_ipv4: str | None = None
    ssh_unit: str = "ssh.service"
    worker_path: str | None = None

    def __post_init__(self):
        require(bool(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", self.owner)), "Invalid Linux owner")
        require(self.release.is_absolute() and self.python.is_absolute(), "Absolute local paths required")
        require(bool(re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256)), "External canonical manifest SHA256 required")
        require(self.access in {"ask", "full"} and self.native_execution in {"enable", "disable"}, "Explicit owner choices required")
        require((self.network_profile is None) == (self.expected_ipv4 is None), "Network UUID/address must be paired")
        if self.network_profile is not None:
            require(str(UUID(self.network_profile)) == self.network_profile, "Canonical network UUID required")
            address = ipaddress.IPv4Address(self.expected_ipv4)
            require(not address.is_unspecified and not address.is_multicast, "Concrete expected IPv4 required")
        require(bool(re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", self.ssh_unit)), "Invalid SSH unit")
        if self.worker_path is not None:
            valid_worker_path(self.worker_path)

@dataclass(frozen=True)
class Paths:
    system: Path = Path("/etc/systemd/system")
    maintenance: Path = Path("/var/lib/vincula-maintenance")
    libexec: Path = Path("/usr/local/libexec/vincula")
    proc: Path = Path("/proc")
    anchor: Path = Path("/")
    admin_uid: int = 0

def run_command(argv, *, account, user=False, bus=False, check=True):
    linux_only()
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": account.pw_dir}
    if bus:
        env.update(XDG_RUNTIME_DIR=f"/run/user/{account.pw_uid}", DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{account.pw_uid}/bus")
    if user and os.geteuid() != account.pw_uid:
        argv = ["/usr/sbin/runuser", "-u", account.pw_name, "--", *argv]
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40, env=env)
    require(not check or result.returncode == 0, "Local setup command failed; diagnostics not exported")
    return result.stdout.strip()

class Installer:
    def __init__(self, settings, *, paths=None, runner=run_command, account=None):
        linux_only()
        self.settings, self.paths, self.runner = settings, paths or Paths(), runner
        self.account = account or pwd.getpwnam(settings.owner)
        require(self.account.pw_name == settings.owner, "Select an explicit OS worker owner")
        self.home = Path(self.account.pw_dir)
        require(self.home.is_absolute(), "Absolute account home required")
        self.state = self.home / ".local/share/vincula-jobs"
        self.db = self.state / "jobs.sqlite3"
        self.job = settings.release / "agent/jobs.py"
        self.old_file = self.home / ".config/systemd/user" / OLD_UNIT
        self.unit_file = self.paths.system / UNIT
        self.created = {}
        self.created_fds = {}
        self._payload_pins = ExitStack()
        self.user_disabled = self.user_stopped = self.system_started = False

    def command(self, argv, **kwargs):
        return self.runner(argv, account=self.account, **kwargs)

    def ctl(self, *args, user=False, check=True):
        return self.command(["/usr/bin/systemctl", *(["--user"] if user else []), *args], user=user, bus=user, check=check)

    def show(self, name, user=False):
        raw = self.ctl("show", name, "-p", "LoadState,UnitFileState,ActiveState,MainPID,ControlGroup,FragmentPath", user=user, check=False)
        return {key: value for key, separator, value in (line.partition("=") for line in raw.splitlines()) if separator}

    def state_snapshot(self):
        _, identity = read_file(self.db, {self.account.pw_uid})
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.execute("BEGIN")
            pair = [db.execute("SELECT mode FROM policy").fetchone()[0],
                    int(db.execute("SELECT enabled FROM direct_execution_policy WHERE id=1").fetchone()[0])]
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN (?,?,?,?)", ACTIVE).fetchone()[0]
            row = db.execute("SELECT path FROM worker_environment WHERE id=1").fetchone()
            tables = table_digests(db)
        require(read_file(self.db, {self.account.pw_uid})[1] == identity, "Database inode changed")
        require(pair[0] in {"ask", "full"} and pair[1] in (0, 1), "Invalid initialized owner policy")
        return {"policy": pair, "active": active, "worker_path": row[0] if row else None, "tables": tables}

    def lock_owners(self):
        path = self.state / "worker.lock"
        if not path.exists():
            return []
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == self.account.pw_uid, "Unsafe worker lock")
        target = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
        result = []
        for line in (self.paths.proc / "locks").read_text().splitlines():
            fields = line.split()
            if len(fields) > 5 and fields[1] == "FLOCK":
                major, minor, inode = fields[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) == target:
                    result.append(int(fields[4]))
        return result

    def ready(self):
        state = self.show(UNIT)
        pid = int(state.get("MainPID", "0"))
        if state.get("ActiveState") != "active" or pid < 2:
            return False
        root = self.paths.proc / str(pid)
        try:
            command = (root / "cmdline").read_bytes().removesuffix(b"\0").split(b"\0")
            return root.stat().st_uid == self.account.pw_uid and command == [os.fsencode(s) for s in self.worker_argv()] and self.lock_owners() == [pid]
        except FileNotFoundError:
            return False

    def worker_argv(self):
        return (str(self.settings.python), "-I", "-B", str(self.job), "worker")

    def payloads(self, worker_path):
        cfg = self.settings
        lines = ["[Unit]", "Description=Vincula Core receiver worker", "After=local-fs.target",
                 "RequiresMountsFor=" + word(str(self.state)) + " " + word(str(cfg.release)),
                 "", "[Service]", "Type=exec", "User=" + cfg.owner, "SetLoginEnvironment=yes",
                 "WorkingDirectory=" + scalar_path(str(self.state)), "Environment=" + word("PATH=" + worker_path),
                 "UnsetEnvironment=DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS SSH_AUTH_SOCK",
                 "ExecStart=" + " ".join(word(s, True) for s in self.worker_argv()),
                 "Restart=on-failure", "RestartSec=5s", "KillMode=control-group", "TimeoutStopSec=30s",
                 "UMask=0077", "StandardInput=null", "StandardOutput=journal", "StandardError=journal",
                 "", "[Install]", "WantedBy=multi-user.target", ""]
        payloads = {self.unit_file: "\n".join(lines).encode()}
        if cfg.network_profile:
            helper = self.paths.libexec / "core-network.py"
            dropin = self.paths.system / (cfg.ssh_unit + ".d") / "95-vincula-core-network.conf"
            command = ("/usr/bin/python3", "-I", "-B", str(helper), "--address", cfg.expected_ipv4, "--connection", cfg.network_profile)
            payloads[helper] = NETWORK_HELPER
            payloads[dropin] = ("[Service]\nExecStartPre=" + " ".join(word(s, True) for s in command) + "\n").encode()
        return payloads

    def _verify_release(self):
        cfg = self.settings
        if self.account.pw_uid == 0:
            for path in (self.home, self.state, self.db, cfg.release, cfg.release / "_release-manifest.json",
                         *(cfg.release / "agent" / n for n in HELPERS), *(self.state / n for n in HELPERS)):
                root_owned(path)
            root_owned(cfg.python, interpreter=True)
            for directory in (cfg.release / "agent", self.state):
                root_caches(directory)
        raw, _ = read_file(cfg.release / "_release-manifest.json", {0, self.account.pw_uid})
        manifest = json.loads(raw, object_pairs_hook=unique_object)
        require(sha(json.dumps(manifest, indent=2).encode()) == cfg.manifest_sha256, "Release manifest differs from operator pin")
        require(re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"]) is not None, "Full release revision required")
        for name in HELPERS:
            wanted = manifest["files"]["agent/" + name]["sha256"]
            for path in (cfg.release / "agent" / name, self.state / name):
                require(sha(read_file(path, {0, self.account.pw_uid})[0]) == wanted, "Release/installed helper mismatch")
        return manifest

    def _check_payloads(self, payloads):
        for path, data in payloads.items():
            if os.path.lexists(path):
                require(read_file(path, {self.paths.admin_uid})[0] == data, "Existing OS setup differs; review upgrade separately")
                require(not path.lstat().st_mode & 0o022, "Writable OS payload is not trusted")
        require(not self.unit_file.with_name(UNIT + ".d").exists(), "Worker drop-ins need separate review")

    def _legacy_worker(self):
        old = self.show(OLD_UNIT, user=True)
        if self.old_file.exists():
            old_bytes, _ = read_file(self.old_file, {self.account.pw_uid})
            if not old.get("UnitFileState"):
                old["UnitFileState"] = self.ctl("is-enabled", OLD_UNIT, user=True, check=False)
            require(old.get("UnitFileState") in {"enabled", "enabled-runtime", "disabled", "static", "indirect", "linked", "linked-runtime", "masked", "masked-runtime"}, "Legacy worker enablement is unknown; offline inspection required")
        else:
            old_bytes = None
        require(old.get("UnitFileState") not in {"enabled", "enabled-runtime"} or old_bytes is not None, "Enabled legacy worker file is not owned by this setup")
        if old.get("ActiveState") == "active":
            require(old_bytes is not None and old.get("FragmentPath") == str(self.old_file), "Unknown active user worker unit")
            require(self.lock_owners() == [int(old.get("MainPID", "0"))], "Active user worker lock not confirmed")
        else:
            require(not self.lock_owners() or self.ready(), "Unowned worker lock prevents setup")
        return old, old_bytes

    def plan(self):
        cfg = self.settings
        manifest = self._verify_release()
        state = self.state_snapshot()
        require(state["active"] == 0, "Existing active work prevents setup")
        worker_path = valid_worker_path(cfg.worker_path if cfg.worker_path is not None else state["worker_path"])
        payloads = self.payloads(worker_path)
        self._check_payloads(payloads)
        old, old_bytes = self._legacy_worker()
        return {"source_commit": manifest["source_commit"], "owner": cfg.owner, "home": str(self.home),
                "before": state, "target_policy": [cfg.access, int(cfg.native_execution == "enable")],
                "payloads": payloads, "old_unit": old, "old_bytes": old_bytes, "system_unit": self.show(UNIT)}

    def _root_parents(self, directory):
        require(directory.is_relative_to(self.paths.anchor), "OS target outside local setup anchor")
        for path in reversed((directory, *directory.parents)):
            if not path.is_relative_to(self.paths.anchor):
                continue
            if not path.exists():
                path.mkdir(mode=0o755)
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.paths.admin_uid and not info.st_mode & 0o022, "Unsafe OS setup directory")

    def _put(self, path, raw, mode=0o644):
        self._root_parents(path.parent)
        if os.path.lexists(path):
            require(read_file(path, {self.paths.admin_uid})[0] == raw, "OS payload changed")
            require(not path.lstat().st_mode & 0o022, "Writable OS payload is not trusted")
            return
        temporary = path.with_name("." + path.name + "." + uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._payload_pins.callback(os.close, fd)
        try:
            with os.fdopen(os.dup(fd), "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            info = os.fstat(fd)
            os.link(temporary, path)
            self.created[path] = ((info.st_dev, info.st_ino), sha(raw))
            self.created_fds[path] = fd
        finally:
            temporary.unlink(missing_ok=True)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def _native_verify(self):
        code = "import runpy;from pathlib import Path;m=runpy.run_path(" + repr(str(self.job)) + ",run_name='core_setup_probe');assert m['ROOT']==Path(" + repr(str(self.state)) + ");assert m['DATABASE'].is_file()"
        self.command([str(self.settings.python), "-I", "-B", "-c", code], user=True)
        targets = [str(self.unit_file), *([self.settings.ssh_unit] if self.settings.network_profile else [])]
        self.command(["/usr/bin/systemd-analyze", "verify", *targets])
        if self.settings.network_profile:
            value = self.command(["/usr/bin/nmcli", "-g", "connection.uuid,connection.permissions", "connection", "show", "uuid", self.settings.network_profile])
            require(value.splitlines() == [self.settings.network_profile] or value.splitlines() == [self.settings.network_profile, ""], "Select a system-owned authorized network profile")

    def _policy(self, before):
        code = (inspect.getsource(table_digests) + "\nimport runpy;m=runpy.run_path(" + repr(str(self.job)) + ",run_name='core_owner_setup')\n"
                "with m['transaction']() as db:\n"
                " assert [m['mode'](db),int(m['direct_execution'].support(db)['enabled'])]==" + repr(before["policy"]) + "\n"
                " m['set_mode'](db," + repr(self.settings.access) + ")\n"
                " m['direct_execution'].set_enabled(db," + repr(self.settings.native_execution == "enable") + ")\n"
                " tables=table_digests(db)\n"
                " assert tables==" + repr(before["tables"]) + "\n")
        self.command([str(self.settings.python), "-I", "-B", "-c", code], user=True)

    def _wait_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.ready():
                return
            time.sleep(0.2)
        raise RuntimeError("System worker readiness not confirmed")

    def _drain(self, unit, user=False):
        self.command([str(self.settings.python), "-I", "-B", str(self.job), "shutdown"], user=True)
        deadline = time.monotonic() + 8
        while self.show(unit, user=user).get("ActiveState") == "active" and time.monotonic() < deadline:
            time.sleep(0.2)
        require(self.show(unit, user=user).get("ActiveState") != "active" and self.state_snapshot()["active"] == 0, "Graceful worker drain not confirmed; no forced stop")

    def _rollback(self, plan):
        require(self.state_snapshot()["active"] == 0, "Rollback deferred: new work exists")
        for path in self.created:
            self._owned_payload(path)
        if self.system_started:
            require(self.unit_file in self.created and plan["system_unit"].get("ActiveState") != "active", "Rollback does not own this worker lifecycle")
            self._drain(UNIT)
            self._owned_payload(self.unit_file)
            self.ctl("disable", "--now", UNIT)
        for path in reversed(self.created):
            self._owned_payload(path)
            path.unlink()
        self.ctl("daemon-reload")
        if self.user_disabled:
            require(read_file(self.old_file, {self.account.pw_uid})[0] == plan["old_bytes"], "Original user unit changed")
            self.ctl("--no-reload", *(["--runtime"] if plan["old_unit"].get("UnitFileState") == "enabled-runtime" else []), "enable", OLD_UNIT, user=True)
        if self.user_stopped:
            require(read_file(self.old_file, {self.account.pw_uid})[0] == plan["old_bytes"], "Original user unit changed")
            self.ctl("start", OLD_UNIT, user=True)

    def _owned_payload(self, path):
        identity, digest = self.created[path]
        original = os.fstat(self.created_fds[path])
        require((original.st_dev, original.st_ino) == identity, "Payload descriptor identity changed")
        raw, current = read_file(path, {self.paths.admin_uid})
        require(current == identity and sha(raw) == digest, "Rollback refuses changed OS file")

    def install(self):
        require(os.geteuid() == 0, "Install is a local root-only operation")
        self._root_parents(self.paths.maintenance)
        lock = self.paths.maintenance / "setup.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == self.paths.admin_uid and not info.st_mode & 0o077, "Unsafe installer lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._install()
        finally:
            try:
                self._payload_pins.close()
            finally:
                self.created_fds.clear()
                os.close(descriptor)

    def _cutover(self, plan):
        for path, raw in plan["payloads"].items():
            self._put(path, raw)
        self._native_verify()
        self.ctl("daemon-reload")
        require(self.state_snapshot() == plan["before"], "State changed before worker cutover")
        if plan["old_unit"].get("UnitFileState") in {"enabled", "enabled-runtime"}:
            self.user_disabled = True
            self.ctl("--no-reload", *(["--runtime"] if plan["old_unit"].get("UnitFileState") == "enabled-runtime" else []), "disable", OLD_UNIT, user=True)
        if plan["old_unit"].get("ActiveState") == "active":
            self.user_stopped = True
            self._drain(OLD_UNIT, user=True)
            self.ctl("stop", OLD_UNIT, user=True)
        require(not self.lock_owners() and self.state_snapshot()["active"] == 0, "Previous worker/work still present")
        self.system_started = True
        self.ctl("enable", "--now", UNIT)
        self._wait_ready()
        require(self.state_snapshot()["tables"] == plan["before"]["tables"], "Existing tables changed")

    def _install_failure(self, error, plan, policy_attempted, committed):
        if policy_attempted and not committed:
            try:
                committed = self.state_snapshot()["policy"] != plan["before"]["policy"]
            except Exception:
                committed = True  # Unknown commit result: preserve state for local review.
        result = {"error_class": type(error).__name__, "policy_committed": committed}
        if not committed:
            try:
                self._rollback(plan); result["rollback"] = True
            except Exception:
                result.update(rollback=False, manual_review_required=True)
        else:
            result["manual_review_required"] = True
        return result

    def _install(self):
        plan = self.plan()
        if all(path.exists() for path in plan["payloads"]) and plan["system_unit"].get("UnitFileState") == "enabled" and self.ready() and plan["before"]["policy"] == plan["target_policy"]:
            return {"applied": False, "already_configured": True, "worker_running": True, "desktop_ready": False}
        require(not os.path.lexists(self.unit_file), "Existing system worker needs separate repair/upgrade review")
        require(plan["system_unit"].get("ActiveState") != "active", "Existing active system worker is not owned by setup")
        backup = self.paths.maintenance / ("receiver-" + uuid4().hex)
        backup.mkdir(mode=0o700)
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup / "jobs.sqlite3.backup")) as target:
            source.backup(target)
        os.chmod(backup / "jobs.sqlite3.backup", 0o600)
        (backup / "intent.json").write_text(json.dumps({k: v for k, v in plan.items() if k not in {"payloads", "old_bytes"}}, indent=2))
        (backup / "intent.json").chmod(0o600)
        if plan["old_bytes"] is not None:
            (backup / "user-unit.before").write_bytes(plan["old_bytes"])
            (backup / "user-unit.before").chmod(0o600)
        committed = policy_attempted = False
        result = {"applied": False, "backup": str(backup), "desktop_ready": False}
        try:
            self._cutover(plan)
            policy_attempted = True
            self._policy(plan["before"])
            committed = True
            after = self.state_snapshot()
            require(after["policy"] == plan["target_policy"] and after["tables"] == plan["before"]["tables"] and self.ready(), "Post-install state not confirmed")
            result.update(applied=True, worker_running=True, policy=after["policy"], reboot_verified=False)
        except Exception as error:
            result.update(self._install_failure(error, plan, policy_attempted, committed))
        (backup / "receipt.json").write_text(json.dumps(result, indent=2))
        for path in backup.iterdir():
            path.chmod(0o600)
        return result

def main():
    linux_only()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "status", "install"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--access", choices=("ask", "full"))
    parser.add_argument("--native-execution", choices=("enable", "disable"))
    parser.add_argument("--worker-path")
    parser.add_argument("--network-profile")
    parser.add_argument("--expected-ipv4")
    parser.add_argument("--ssh-unit", default="ssh.service")
    args = parser.parse_args()
    if args.action == "status":
        account = pwd.getpwnam(args.owner)
        state = Path(account.pw_dir) / ".local/share/vincula-jobs"
        result = subprocess.run(["/usr/bin/systemctl", "show", UNIT, "-p", "ActiveState,MainPID,ControlGroup"], capture_output=True, text=True, timeout=10)
        print(json.dumps({"owner": args.owner, "state_initialized": (state / "jobs.sqlite3").is_file(), "system_worker": result.stdout.strip(), "reboot_verified": False, "desktop_ready": False}))
        return 0
    require(all((args.release, args.manifest_sha256, args.python, args.access, args.native_execution)), "Plan/install require release, manifest pin, Python and both explicit owner choices")
    installer = Installer(Settings(args.owner, args.release, args.manifest_sha256, args.access, args.native_execution,
                                   args.python, args.network_profile, args.expected_ipv4, args.ssh_unit, args.worker_path))
    if args.action == "plan":
        plan = installer.plan()
        public = {k: v for k, v in plan.items() if k not in {"payloads", "old_bytes"}}
        public["os_files"] = {str(path): sha(raw) for path, raw in plan["payloads"].items()}
        print(json.dumps(public, ensure_ascii=True))
    else:
        result = installer.install(); print(json.dumps(result)); return 0 if result.get("applied") or result.get("already_configured") else 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
