# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Owner-local provisioning for the optional graphical desktop runtime.

SSH authenticates the owner; the installed deployment selects every executable
and path. Requests contain public group identities, never certificates or argv.
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_backend as backend
import desktop_deskflow as deskflow
import desktop_managed as managed
import desktop_security as security
import desktop_ssh
import desktop_supervisor as supervisor
import nodes


def home():
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Vincula-Desktop"
    return Path.home() / ".local/share/vincula-desktop"


def private_read(path, limit=1024 * 1024):
    path = Path(path)
    info = path.lstat()
    managed.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and not getattr(info, "st_file_attributes", 0) & 0x400, "Invalid desktop metadata file.")
    security.private_file(path)
    raw = desktop_ssh.bounded_file(path, limit)
    return raw


def deployment():
    root = home()
    security.private_directory(root)
    value = desktop_ssh.decode(private_read(root / "deployment.json"))
    managed.require(isinstance(value, dict) and set(value) == {
        "protocol", "artifact_root", "artifact_manifest_file", "artifact_manifest_sha256", "port", "enabled"}
        and type(value["protocol"]) is int and value["protocol"] == 1 and value["enabled"] is True,
        "The optional desktop runtime has not been enabled by its owner.")
    managed.require(type(value["port"]) is int and 1024 <= value["port"] <= 65535,
                    "Invalid installed desktop port.")
    for field in ("artifact_root", "artifact_manifest_file"):
        managed.require(isinstance(value[field], str) and Path(value[field]).is_absolute(),
                        "Installed artifact paths must be absolute.")
    artifact = backend.verify_artifact_manifest(private_read(value["artifact_manifest_file"]),
                                              approved_sha256=value["artifact_manifest_sha256"])
    manifest = artifact.manifest
    managed.require(manifest.platform == ("windows" if sys.platform == "win32" else "linux")
                    and deskflow.MANAGED_CAPABILITIES <= set(manifest.capabilities),
                    "Install the managed desktop build for this platform.")
    engine = Path(value["artifact_root"]) / manifest.engine_file
    managed.artifact_approval(dict(manifest.files)[manifest.engine_file], engine)
    # Dependencies are part of the approved artifact, not just its executable.
    for name, expected in manifest.files:
        path = Path(value["artifact_root"]) / name
        info = path.lstat()
        managed.require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                        and not getattr(info, "st_file_attributes", 0) & 0x400,
                        "Invalid installed artifact file.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        managed.require(digest.hexdigest() == expected, "Installed artifact integrity check failed.")
    return value, artifact, engine


def catalog(node_id):
    before = nodes.snapshot(desktop_ssh.default_registry_path())
    managed.require(before["registry"] is not None and before["registry"]["local"] == node_id,
                    "This SSH route does not identify the selected node.")
    return before


def inspect(request):
    managed.require(set(request) == {"action", "node_id"}, "Invalid inspect fields.")
    desktop_ssh.canonical_uuid(request["node_id"])
    before = catalog(request["node_id"])
    config, artifact, _ = deployment()
    endpoint = supervisor.request(desktop_ssh.default_directory(), {"action": "status"})
    managed.require(endpoint.get("ok") is True and endpoint["session"]["state"] not in supervisor.ACTIVE,
                    "Stop the existing desktop group before starting another.")
    return {"node_id": request["node_id"], "revision": before["revision"],
            "family": artifact.manifest.platform, "helper_sha256": desktop_ssh.implementation_digest(),
            "port": config["port"]}


def provision(request):
    managed.require(set(request) == {"action", "node_id", "session_id", "desktop", "expected_revision"},
                    "Invalid provisioning fields.")
    node_id, session = request["node_id"], request["session_id"]
    desktop_ssh.canonical_uuid(node_id)
    desktop_ssh.canonical_uuid(session)
    before = catalog(node_id)
    managed.require(before["revision"] == request["expected_revision"], "The destination catalog changed.")
    config, artifact, engine = deployment()
    candidate = dict(before["registry"], desktop=request["desktop"])
    registry = nodes.validate(candidate)
    managed.require(node_id in {member.node_id for member in registry.desktop.members},
                    "The destination must belong to the selected group.")
    managed.require(registry.get(registry.desktop.primary).family == "windows",
                    "This managed desktop build currently requires a Windows primary.")
    status = supervisor.request(desktop_ssh.default_directory(), {"action": "status"})
    managed.require(status.get("ok") is True and status["session"]["state"] not in supervisor.ACTIVE,
                    "A desktop session already owns this node.")
    profile = home() / "profiles" / session
    managed.require(not profile.exists() and not profile.is_symlink(), "Use a fresh desktop session identity.")
    security.private_directory(profile)
    # The engine creates its private PEM locally. Its bytes never cross Python/SSH.
    result = subprocess.run([str(engine), "--vincula-provision", str(profile),
                             "--vincula-session", session, "--vincula-node", node_id],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
                            env=managed.runtime_environment(),
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    managed.require(result.returncode == 0 and len(result.stdout) <= 4096, "Local TLS provisioning failed.")
    public = desktop_ssh.decode(result.stdout)
    managed.require(set(public) == {"protocol", "session_id", "node_id", "fingerprint_sha256", "certificate_reference"}
                    and public["protocol"] == 1 and public["session_id"] == session and public["node_id"] == node_id
                    and Path(public["certificate_reference"]) == profile / "tls/identity.pem",
                    "Invalid local TLS provisioning receipt.")
    backend.valid_digest(public["fingerprint_sha256"])
    security.private_file(profile / "tls/identity.pem")
    if before["registry"].get("desktop") != request["desktop"]:
        before = nodes.execute({"action": "desktop", "desktop": request["desktop"],
                                "expected_revision": before["revision"]}, desktop_ssh.default_registry_path())
    receipt = {"node_id": node_id, "session_id": session, "desktop": request["desktop"],
               "revision": before["revision"], "pin": public["fingerprint_sha256"]}
    supervisor.atomic_json(profile / "provision.json", receipt)
    return {"node_id": node_id, "session_id": session, "revision": before["revision"],
            "pin": receipt["pin"], "helper_sha256": desktop_ssh.implementation_digest()}


def route_address(registry, primary):
    route = registry.get(primary).route
    managed.require(route is not None and route.configured(), "The client needs a configured route to the primary.")
    # ssh -G expands operator-owned aliases without connecting. Only HostName is used.
    import node_ssh
    result = subprocess.run([str(node_ssh.executable("ssh")), "-G", *route.options, route.destination],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    managed.require(result.returncode == 0 and len(result.stdout) <= 128 * 1024, "Cannot resolve the primary SSH route.")
    values = [line.partition(" ")[2].strip() for line in result.stdout.decode("utf-8", "strict").splitlines()
              if line.startswith("hostname ")]
    managed.require(len(values) == 1, "The primary route has no unambiguous host.")
    addresses = socket.getaddrinfo(values[0], None, socket.AF_INET, socket.SOCK_STREAM)
    ips = {item[4][0] for item in addresses}
    managed.require(len(ips) == 1, "The primary route must resolve to one authorized IPv4 address.")
    return ips.pop()


def configure(request):
    managed.require(set(request) == {"action", "node_id", "group"}, "Invalid profile fields.")
    node_id, group = request["node_id"], backend.GroupManifest.from_document(request["group"])
    before = catalog(node_id)
    managed.require(dict(group.revisions).get(node_id) == before["revision"], "The destination catalog changed.")
    registry = nodes.validate(before["registry"])
    profile = home() / "profiles" / group.session_id
    receipt = desktop_ssh.decode(private_read(profile / "provision.json", 32768))
    managed.require(receipt == {"node_id": node_id, "session_id": group.session_id,
                               "desktop": before["registry"]["desktop"], "revision": before["revision"],
                               "pin": dict(group.tls_pins)[node_id]}, "The group differs from the local provision receipt.")
    managed.require(group.clipboard == registry.desktop.clipboard and group.primary == registry.desktop.primary
                    and {(s.node_id, s.x, s.y) for s in group.screens}
                    == {(s.node_id, s.x, s.y) for s in registry.desktop.members}, "The desktop layout changed.")
    config, artifact, engine = deployment()
    primary_address = None if node_id == group.primary else route_address(registry, group.primary)
    bind = "0.0.0.0"
    if primary_address is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((primary_address, config["port"]))  # Select source address; send no datagram.
            bind = route.getsockname()[0]
    paths = deskflow.LocalPaths(artifact.manifest.platform, config["artifact_root"], str(profile),
                               str(profile / "tls/identity.pem"))
    prepared = deskflow.compile_profile(group, node_id, artifact, paths,
                                        deskflow.NetworkProfile(bind, config["port"], primary_address))
    for name, text in prepared.files:
        path = profile / name
        security.private_directory(path.parent)
        with path.open("xb") as stream:
            security.protect_new_file(path)
            if sys.platform != "win32":
                os.chmod(path, 0o600)
            stream.write(text.encode("utf-8"))
    managed_config = {"group": group.to_document(), "node_id": node_id,
                      "settings_file": str(profile / deskflow.SETTINGS_FILENAME),
                      "state_file": str(profile / "state.conf"),
                      "binary_sha256": dict(artifact.manifest.files)[artifact.manifest.engine_file],
                      "owner_consent": True}
    return {"node_id": node_id, "command": {"name": "engine", "argv": list(prepared.argv),
                                             "cwd": str(profile), "managed": managed_config}}


def execute(request):
    managed.require(isinstance(request, dict), "Supply a desktop node request.")
    action = request.get("action")
    managed.require(action in {"inspect", "provision", "configure"}, "Unsupported desktop node operation.")
    return {"ok": True, "result": {"inspect": inspect, "provision": provision, "configure": configure}[action](request)}


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        managed.require(len(raw) <= 65536, "Desktop node request too large.")
        reply = execute(desktop_ssh.decode(raw))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        reply = {"ok": False, "error": "Runtime desktop non disponibile, occupato o configurazione cambiata. Verifica il nodo."}
    print(json.dumps(reply, ensure_ascii=True), flush=True)
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
