# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local Linux controller identity. Private key bytes stay in OS tools/files, never Python."""
import argparse
import base64
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

if sys.platform.startswith("linux"):
    import fcntl
    import pwd
else:
    fcntl = pwd = None

SCHEMA = 1
# Public systemd credential identifier, not credential contents.
CREDENTIAL_NAME = "vincula-controller-key"
ENCRYPTION_PROVIDERS = ("host", "host+tpm2")
SYSTEMCTL = "/usr/bin/systemctl"
TOOLS = ("systemctl", "systemd-creds", "systemd-analyze", "ssh-keygen", "ssh-agent", "ssh-add", "python3")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unit_word(value, executable=False):
    require(value and all(32 <= ord(c) != 127 for c in value), "Invalid unit argument")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (value.replace("$", "$$") if executable else value) + '"'


@dataclass(frozen=True)
class Paths:
    configuration: Path = Path("/etc/vincula/controller")
    system: Path = Path("/etc/systemd/system")
    helper: Path = Path("/usr/local/libexec/vincula/controller-identity.py")
    runtime: Path = Path("/run")
    anchor: Path = Path("/")
    admin_uid: int = 0

    def __post_init__(self):
        require(all(p.is_absolute() and ".." not in p.parts and p.is_relative_to(self.anchor)
                    for p in (self.configuration, self.system, self.helper, self.runtime)), "Invalid OS paths")


def command(argv, *, env=None, output=False, timeout=30):
    # Commands which process private material always have both outputs discarded.
    result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout, env=env)
    return result.returncode, result.stdout.decode("utf-8", "strict").strip() if output else ""


class Controller:
    def __init__(self, owner, encryption=None, *, paths=None, runner=command, account=None):
        require(sys.platform.startswith("linux"), "Controller identity requires Linux")
        require(isinstance(owner, str) and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", owner), "Explicit Linux owner required")
        require(encryption is None or encryption in ENCRYPTION_PROVIDERS, "Select host or host+tpm2 encryption")
        self.account = account or pwd.getpwnam(owner)
        require(self.account.pw_name == owner, "Owner account mismatch")
        self.paths, self.runner, self.encryption = paths or Paths(), runner, encryption
        self.name = "vincula-controller-" + str(self.account.pw_uid) + ".service"
        self.directory = self.paths.configuration / str(self.account.pw_uid)
        self.public = self.directory / "identity.pub"
        self.cipher = self.directory / "identity.cred"
        self.receipt = self.directory / "provider.json"
        self.unit = self.paths.system / self.name
        self.socket = self.paths.runtime / ("vincula-identity-" + str(self.account.pw_uid)) / "agent.sock"
        self.created = {}
        self.pins = ExitStack()
        self.activation_attempted = False

    def environment(self, *, agent=False):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C",
               "HOME": self.account.pw_dir, "SSH_ASKPASS_REQUIRE": "never"}
        if agent:
            env["SSH_AUTH_SOCK"] = str(self.socket)
        return env

    def run(self, argv, *, output=False, timeout=30):
        return self.runner(argv, env=self.environment(), output=output, timeout=timeout)

    def checked(self, argv, **kwargs):
        code, output = self.run(argv, **kwargs)
        require(code == 0, "Local controller operation failed; command output withheld")
        return output

    def show(self):
        code, raw = self.run([SYSTEMCTL, "show", self.name, "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,FragmentPath"], output=True)
        values = {key: value for key, separator, value in (line.partition("=") for line in raw.splitlines()) if separator}
        require(code == 0 and "LoadState" in values, "System manager state unavailable")
        return values

    def unit_bytes(self):
        runtime = self.socket.parent.name
        load = ["/usr/bin/python3", "-I", "-B", str(self.paths.helper), "load", "--owner", self.account.pw_name]
        return ("\n".join([
            "[Unit]", "Description=Vincula controller OpenSSH identity", "After=local-fs.target",
            "StartLimitIntervalSec=120", "StartLimitBurst=3", "", "[Service]", "Type=exec",
            "User=" + self.account.pw_name, "RuntimeDirectory=" + runtime, "RuntimeDirectoryMode=0700",
            "UMask=0077", "LoadCredentialEncrypted=" + unit_word(CREDENTIAL_NAME + ":" + str(self.cipher)),
            "UnsetEnvironment=DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS SSH_AUTH_SOCK",
            "Environment=SSH_ASKPASS_REQUIRE=never",
            "ExecStart=" + " ".join(unit_word(x, True) for x in ("/usr/bin/ssh-agent", "-D", "-a", str(self.socket))),
            "ExecStartPost=" + " ".join(unit_word(x, True) for x in load),
            "Restart=on-failure", "RestartSec=5s", "TimeoutStartSec=20s", "TimeoutStopSec=10s",
            "KillMode=control-group", "StandardInput=null", "StandardOutput=null", "StandardError=null",
            "", "[Install]", "WantedBy=multi-user.target", ""]) ).encode()

    def _create_directory(self, path):
        path.mkdir(mode=0o755)
        created = path.lstat()
        require(stat.S_ISDIR(created.st_mode) and created.st_uid == self.paths.admin_uid
                and not created.st_mode & 0o022, "New controller directory is not protected")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            pinned = os.fstat(fd)
            identity = (created.st_dev, created.st_ino)
            require(stat.S_ISDIR(pinned.st_mode) and pinned.st_uid == self.paths.admin_uid
                    and (pinned.st_dev, pinned.st_ino) == identity, "New controller directory identity changed")
            os.fchmod(fd, 0o755)
            current, pinned = path.lstat(), os.fstat(fd)
            require((current.st_dev, current.st_ino) == identity == (pinned.st_dev, pinned.st_ino)
                    and pinned.st_uid == self.paths.admin_uid and stat.S_IMODE(pinned.st_mode) == 0o755,
                    "New controller directory identity or mode changed")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _searchable(self, info):
        require(self.account.pw_uid in (0, info.st_uid) or info.st_mode & 0o011,
                "Controller directory is not searchable by selected owner; inspect OS permissions")

    def _parents(self, directory):
        for path in reversed((directory, *directory.parents)):
            if not path.is_relative_to(self.paths.anchor):
                continue
            if not path.exists():
                self._create_directory(path)
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.paths.admin_uid and not info.st_mode & 0o022,
                    "Controller OS directory must be protected")
            self._searchable(info)

    def _check_parents(self, directory):
        for path in (directory, *directory.parents):
            if path.is_relative_to(self.paths.anchor):
                info = path.lstat()
                require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.paths.admin_uid and not info.st_mode & 0o022,
                        "Unprotected controller directory")
                self._searchable(info)

    def _public_file(self, path, *, mode=None):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == self.paths.admin_uid
                    and not info.st_mode & 0o022 and (mode is None or stat.S_IMODE(info.st_mode) == mode),
                    "Controller OS file is not protected")
            with os.fdopen(os.dup(fd), "rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024, "Controller OS file too large")
            return raw
        finally:
            os.close(fd)

    def _publish(self, path, raw, *, mode=0o644):
        self._parents(path.parent)
        # Refuse replacement, retain original FD until completion to prevent inode-reuse ABA.
        fd, temporary = tempfile.mkstemp(prefix=".vincula-", dir=path.parent)
        temporary = Path(temporary)
        self.pins.callback(os.close, fd)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(os.dup(fd), "wb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            info = os.fstat(fd)
            os.link(temporary, path)
            self.created[path] = (fd, info.st_dev, info.st_ino, digest(raw))
        finally:
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _owned(self, path):
        if path not in self.created:
            return False
        fd, dev, ino, wanted = self.created[path]
        try:
            live = path.lstat()
            pinned = os.fstat(fd)
            return (live.st_dev, live.st_ino) == (dev, ino) == (pinned.st_dev, pinned.st_ino) and digest(self._public_file(path)) == wanted
        except (OSError, ValueError):
            return False

    def support(self):
        for tool in TOOLS:
            require(Path("/usr/bin", tool).is_file(), "Required OS tool unavailable")
        for tool in ("systemctl", "systemd-creds"):
            version = self.checked(["/usr/bin/" + tool, "--version"], output=True)
            match = re.match(r"systemd\s+(\d+)", version)
            require(match is not None and int(match[1]) >= 250, "systemd 250 or later required")

    def plan(self):
        require(self.encryption is not None, "Explicit encryption provider required")
        self.support()
        state = self.show()
        expected = [self.public, self.cipher, self.receipt, self.unit]
        any_existing = any(os.path.lexists(path) for path in expected)
        if os.path.lexists(self.paths.helper):
            self._check_parents(self.paths.helper.parent)
            require(self._public_file(self.paths.helper, mode=0o644) == Path(__file__).read_bytes(),
                    "Existing controller helper differs")
        if any_existing:
            self.verify_installed(cipher_digest=os.geteuid() == 0)
        else:
            require(state["LoadState"] == "not-found", "Controller unit already exists outside this provider")
        require(not os.path.lexists(self.unit.with_name(self.name + ".d")), "Controller unit drop-ins need separate review")
        return {"owner": self.account.pw_name, "uid": self.account.pw_uid, "unit": self.name,
                "identity_file": str(self.public), "identity_agent": str(self.socket), "encryption": self.encryption,
                "already_installed": any_existing, "system_unit": state,
                "enrollment_required": True, "routes_modified": False,
                "private_material_exported": False, "cipher_digest_verified": any_existing and os.geteuid() == 0}

    def verify_installed(self, *, cipher_digest=True):
        for directory in (self.directory, self.unit.parent, self.paths.helper.parent):
            # Observation only: unlike _parents, never create anything.
            self._check_parents(directory)
        receipt = json.loads(self._public_file(self.receipt))
        require(receipt.get("schema") == SCHEMA and receipt.get("owner") == self.account.pw_name
                and receipt.get("uid") == self.account.pw_uid and receipt.get("encryption") in ENCRYPTION_PROVIDERS
                and (self.encryption is None or receipt["encryption"] == self.encryption), "Controller receipt mismatch")
        require(self._public_file(self.unit) == self.unit_bytes(), "Controller unit differs from selected provider")
        for path in (self.public, self.cipher, self.unit, self.paths.helper):
            mode = 0o600 if path == self.cipher else 0o644
            if path == self.cipher and not cipher_digest:
                info = path.lstat()
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == self.paths.admin_uid
                        and stat.S_IMODE(info.st_mode) == mode, "Unprotected encrypted credential")
                continue
            require(digest(self._public_file(path, mode=mode)) == receipt["sha256"].get(str(path)), "Controller payload mismatch")
        require(self._public_file(self.paths.helper) == Path(__file__).read_bytes(), "Controller helper upgrade requires separate review")
        return receipt

    def socket_owned(self):
        try:
            parent, info = self.socket.parent.lstat(), self.socket.lstat()
            return (stat.S_ISDIR(parent.st_mode) and parent.st_uid == self.account.pw_uid and not parent.st_mode & 0o077
                    and stat.S_ISSOCK(info.st_mode) and info.st_uid == self.account.pw_uid)
        except OSError:
            return False

    def signature(self):
        if not self.socket_owned():
            return False
        code, _ = self.runner(["/usr/bin/ssh-add", "-T", str(self.public)], env=self.environment(agent=True), timeout=5)
        return code == 0

    def status(self):
        state = self.show()
        try:
            parts = self._public_file(self.public).decode("ascii").split()
            require(len(parts) >= 2 and parts[0] == "ssh-ed25519", "Invalid public identity")
            fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(parts[1], validate=True)).digest()).decode().rstrip("=")
        except (OSError, ValueError):
            fingerprint = None
        return {"owner": self.account.pw_name, "unit": self.name, "system_unit": state,
                "identity_file": str(self.public), "identity_agent": str(self.socket),
                "fingerprint": fingerprint,
                "socket_owned": self.socket_owned(), "signature_valid": self.signature(),
                "agent_process_exact": self.process_ready(state),
                "observed_unix": int(time.time()), "network_verified": False}

    def process_ready(self, state):
        try:
            pid = int(state.get("MainPID", "0"))
            if pid < 2 or state.get("FragmentPath") != str(self.unit):
                return False
            process = Path("/proc") / str(pid)
            argv = (process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            return process.stat().st_uid == self.account.pw_uid and argv == [
                b"/usr/bin/ssh-agent", b"-D", b"-a", os.fsencode(self.socket)]
        except (OSError, ValueError):
            return False

    def start_ready(self):
        self.activation_attempted = True
        self.checked([SYSTEMCTL, "enable", "--now", self.name])
        observed = self.status()
        require(observed["system_unit"].get("ActiveState") == "active"
                and observed["system_unit"].get("UnitFileState") == "enabled"
                and observed["agent_process_exact"] and observed["signature_valid"],
                "Controller service readiness not confirmed")
        return observed

    def load(self):
        require(os.geteuid() == self.account.pw_uid, "Credential loader must run as selected owner")
        directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        require(directory and Path(directory).is_absolute(), "System credential directory unavailable")
        private = Path(directory) / CREDENTIAL_NAME
        info = private.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in (0, self.account.pw_uid) and not info.st_mode & 0o077,
                "System credential is not private")
        deadline = time.monotonic() + 5
        while not self.socket_owned() and time.monotonic() < deadline:
            time.sleep(0.05)
        require(self.socket_owned(), "Controller socket unavailable")
        code, _ = self.runner(["/usr/bin/ssh-add", str(private)], env=self.environment(agent=True), timeout=5)
        require(code == 0 and self.signature(), "Controller identity load/signature failed")
        return {"loaded": True}

    def _identity(self, stage):
        private, cipher = stage / "identity", stage / "identity.cred"
        self.checked(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vincula-controller-" + str(self.account.pw_uid), "-f", str(private)])
        self.checked(["/usr/bin/systemd-creds", "--name=" + CREDENTIAL_NAME, "--with-key=" + self.encryption,
                      "encrypt", str(private), str(cipher)])
        # Read only the PUBLIC key and OS-encrypted envelope, never the plaintext private file.
        public = private.with_suffix(".pub").read_bytes()
        parts = public.decode("ascii").split()
        require(len(parts) >= 2 and parts[0] == "ssh-ed25519", "OS public key output invalid")
        blob = base64.b64decode(parts[1], validate=True)
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        return public, cipher.read_bytes(), fingerprint

    def _rollback(self):
        # Keep the complete identity/configuration when lifecycle ownership is uncertain.
        if not all(self._owned(path) for path in self.created):
            return False
        if self.activation_attempted:
            if not self._owned(self.unit):
                return False
            code, _ = self.run([SYSTEMCTL, "disable", "--now", self.name])
            if code != 0:
                return False
            state = self.show()
            if state.get("ActiveState") not in ("inactive", "failed") or state.get("MainPID") != "0":
                return False
        for path in reversed(list(self.created)):
            if self._owned(path):
                path.unlink()
        self.run([SYSTEMCTL, "daemon-reload"])
        return True

    def install(self):
        require(os.geteuid() == 0, "Installation requires local root authorization")
        require(self.encryption is not None, "Explicit encryption provider required")
        self._parents(self.paths.runtime)
        lock = self.paths.runtime / "vincula-controller-install.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == self.paths.admin_uid
                    and not info.st_mode & 0o077, "Unsafe controller installation lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.plan()
            if before["already_installed"]:
                return {"already_installed": True, **self.start_ready()}
            with self.pins:
                try:
                    with tempfile.TemporaryDirectory(prefix="vincula-controller-setup-", dir=self.paths.runtime) as temp:
                        public, cipher, fingerprint = self._identity(Path(temp))
                    payloads = {self.public: public, self.cipher: cipher, self.unit: self.unit_bytes(),
                                self.paths.helper: Path(__file__).read_bytes()}
                    # A helper shared with another owner's provider must match exactly.
                    for path, raw in payloads.items():
                        if path == self.paths.helper and os.path.lexists(path):
                            self._check_parents(path.parent)
                            require(self._public_file(path, mode=0o644) == raw, "Existing controller helper differs")
                        else:
                            self._publish(path, raw, mode=0o600 if path == self.cipher else 0o644)
                    receipt = {"schema": SCHEMA, "owner": self.account.pw_name, "uid": self.account.pw_uid,
                               "encryption": self.encryption, "fingerprint": fingerprint,
                               "sha256": {str(path): digest(raw) for path, raw in payloads.items()}}
                    self._publish(self.receipt, (json.dumps(receipt, indent=2) + "\n").encode())
                    self.checked(["/usr/bin/systemd-analyze", "verify", str(self.unit)])
                    self.checked([SYSTEMCTL, "daemon-reload"])
                    observed = self.start_ready()
                    return {"installed": True, "fingerprint": fingerprint, **observed}
                except (ValueError, OSError, subprocess.SubprocessError):
                    self._rollback()
                    raise ValueError("Controller installation failed; owned cleanup attempted, inspect local OS state") from None
        finally:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "status", "install", "load"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--encryption", choices=ENCRYPTION_PROVIDERS)
    args = parser.parse_args()
    try:
        provider = Controller(args.owner, args.encryption)
        result = getattr(provider, args.action)()
        if args.action != "load":
            print(json.dumps(result))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        print(json.dumps({"error": "Controller operation failed; inspect local OS state"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
