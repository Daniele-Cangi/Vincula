# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install the optional owner-local graphical supervisor, separately from Core.

No engine or input provider starts at installation or login: the supervisor is
idle until a session-scoped, authenticated prepare/commit arrives.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_backend
import desktop_node
import desktop_security
import desktop_ssh
import desktop_supervisor
import nodes

SUPPORT_FILES = ("desktop_session.py", "desktop_ssh.py", "desktop_supervisor.py", "desktop_security.py",
                 "desktop_process.py", "desktop_managed.py", "desktop_backend.py", "desktop_deskflow.py",
                 "desktop_node.py", "desktop_app.py", "desktop_coordinator.py", "node_ssh.py", "node_registry.py",
                 "nodes.py", "desktop_layout.py", "wire_framing.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bundle(source):
    """Read a fixed code allowlist; caller approves its canonical digest out of band."""
    source = Path(source)
    result, raw = {}, {}
    for name in SUPPORT_FILES:
        path = source / name
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and not getattr(info, "st_file_attributes", 0) & 0x400, "Invalid support source file.")
        data = desktop_ssh.bounded_file(path, 1024 * 1024)
        result[name], raw[name] = hashlib.sha256(data).hexdigest(), data
    manifest = {"protocol": 1, "files": result}
    digest = hashlib.sha256(desktop_backend.canonical_json(manifest)).hexdigest()
    return digest, manifest, raw


def verify_installed():
    root = desktop_node.home()
    manifest = desktop_ssh.decode(desktop_node.private_read(root / "support.json"))
    require(isinstance(manifest, dict) and set(manifest) == {"protocol", "files", "bundle_sha256"}
            and type(manifest["protocol"]) is int and manifest["protocol"] == 1
            and isinstance(manifest["files"], dict) and set(manifest["files"]) == set(SUPPORT_FILES),
            "Invalid installed support receipt.")
    expected = {"protocol": 1, "files": manifest["files"]}
    require(hashlib.sha256(desktop_backend.canonical_json(expected)).hexdigest() == manifest["bundle_sha256"],
            "Installed support receipt is inconsistent.")
    for name, expected in manifest["files"].items():
        require(hashlib.sha256(desktop_node.private_read(root / name)).hexdigest() == expected,
                "Installed desktop support changed.")
    config = desktop_ssh.decode(desktop_node.private_read(root / "session.json", 16384))
    require(isinstance(config, dict) and set(config) == {"protocol", "python"}
            and type(config["protocol"]) is int and config["protocol"] == 1
            and isinstance(config["python"], str) and Path(config["python"]).is_absolute()
            and Path(config["python"]).is_file(), "Invalid desktop interpreter selection.")
    return config


def exclusive_file(path, raw):
    path = Path(path)
    if path.exists() or path.is_symlink():
        require(desktop_node.private_read(path) == raw, "Existing desktop setup differs; no overwrite was performed.")
        return False
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        desktop_security.protect_new_file(path)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def desktop_quote(value):
    require(isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "Invalid desktop launch argument.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def autostart_content(python, script):
    arguments = [str(python), "-IB", str(script), "serve"]
    execution = " ".join(desktop_quote(arg) for arg in arguments).replace("\\", "\\\\")
    return ("[Desktop Entry]\nType=Application\nName=Vincula Desktop Runtime\n"
            "Comment=Optional graphical session supervisor; no Core credentials\nExec=" + execution
            + "\nTerminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n")


def install(source, approved_sha256, registry, python):
    desktop_backend.valid_digest(approved_sha256)
    digest, manifest, files = bundle(source)
    require(digest == approved_sha256, "Desktop support differs from the externally approved digest.")
    python, registry = Path(python).resolve(), Path(registry).absolute()
    require(python.is_file() and registry.is_absolute(), "Select an installed interpreter and absolute node catalog.")
    snapshot = nodes.snapshot(registry)
    require(snapshot["registry"] is not None, "The selected node catalog is absent.")
    # Artifact installation/owner opt-in precedes support publication.
    desktop_node.deployment()
    root = desktop_security.private_directory(desktop_node.home())
    if (desktop_ssh.default_directory() / "endpoint.json").exists():
        status = desktop_supervisor.request(desktop_ssh.default_directory(), {"action": "status"})
        require(status.get("ok") is True and status["session"]["state"] not in desktop_supervisor.ACTIVE,
                "Stop the desktop group before changing its installed support.")
    publication = {root / name: raw for name, raw in files.items()}
    publication[root / "catalog.json"] = desktop_backend.canonical_json({"registry": str(registry)})
    publication[root / "session.json"] = desktop_backend.canonical_json({"protocol": 1, "python": str(python)})
    publication[root / "support.json"] = desktop_backend.canonical_json(dict(manifest, bundle_sha256=digest))
    # Reject every conflicting existing entry before creating any support file.
    for path, raw in publication.items():
        if path.exists() or path.is_symlink():
            require(desktop_node.private_read(path) == raw, "Existing desktop support conflicts with this release.")
    if sys.platform != "win32":
        autostart = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
        entry = autostart / "vincula-desktop-runtime.desktop"
        content = autostart_content(python, root / "desktop_session.py").encode("utf-8")
        if entry.exists() or entry.is_symlink():
            require(desktop_node.private_read(entry) == content, "Existing autostart entry differs.")
    for path, raw in publication.items():
        exclusive_file(path, raw)
    verify_installed()
    if sys.platform == "win32":
        task("install")
    else:
        autostart.mkdir(parents=True, exist_ok=True)
        exclusive_file(entry, content)
    return {"installed": True, "bundle_sha256": digest, "supervisor_started": False, "engine_started": False}


# Native Task Scheduler API, never desktop UI automation. Everything substituted
# comes from the private, verified local setup; stdin is a small JSON document.
TASK_ASSERT = r"""
function Assert-VinculaDesktopTask([xml]$x,$c) {
  $ns=New-Object Xml.XmlNamespaceManager($x.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
  $exec=$x.SelectNodes('//t:Actions/t:Exec',$ns)
  $actions=$x.SelectNodes('//t:Actions/*',$ns)
  $p=$x.SelectSingleNode('//t:Principals/t:Principal',$ns)
  $triggers=$x.SelectNodes('//t:Triggers/t:LogonTrigger',$ns)
  $allTriggers=$x.SelectNodes('//t:Triggers/*',$ns)
  $settings=$x.SelectSingleNode('//t:Settings',$ns)
  if ($actions.Count -ne 1 -or $exec.Count -ne 1 -or $exec[0].Command -ne $c.python -or $exec[0].Arguments -ne $c.arguments -or
      $exec[0].WorkingDirectory -ne $c.root -or $p.UserId -ne $c.sid -or $p.LogonType -ne 'InteractiveToken' -or $p.RunLevel -ne 'LeastPrivilege' -or
      $allTriggers.Count -ne 1 -or $triggers.Count -ne 1 -or $triggers[0].UserId -ne $c.sid -or $settings.ExecutionTimeLimit -ne 'PT0S' -or
      $settings.MultipleInstancesPolicy -ne 'IgnoreNew' -or $settings.DisallowStartIfOnBatteries -ne 'false' -or
      $settings.StopIfGoingOnBatteries -ne 'false' -or ($settings.Enabled -and $settings.Enabled -ne 'true') -or
      ($triggers[0].Enabled -and $triggers[0].Enabled -ne 'true')) {
    throw 'Existing desktop task differs; no overwrite'
  }
}
"""

TASK_SCRIPT = TASK_ASSERT + r"""
$ErrorActionPreference='Stop'
$c=[Console]::In.ReadToEnd() | ConvertFrom-Json
$name='Vincula-Desktop-' + $c.sid
$existing=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing) {
  [xml]$x=Export-ScheduledTask -TaskName $name
  Assert-VinculaDesktopTask $x $c
} elseif ($c.operation -eq 'install') {
  $action=New-ScheduledTaskAction -Execute $c.python -Argument $c.arguments -WorkingDirectory $c.root
  $principal=New-ScheduledTaskPrincipal -UserId $c.sid -LogonType Interactive -RunLevel Limited
  $trigger=New-ScheduledTaskTrigger -AtLogOn -User $c.sid
  $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description 'Vincula optional graphical supervisor; idle until an authorized group starts' | Out-Null
} else { throw 'Desktop task not installed' }
if ($c.operation -eq 'start') { Start-ScheduledTask -TaskName $name }
@{ok=$true;operation=$c.operation} | ConvertTo-Json -Compress
"""


def task(operation):
    import base64
    config = verify_installed()
    root = desktop_node.home()
    sid = desktop_security.WindowsSecurity().user_sid
    request = {"operation": operation, "sid": sid, "python": config["python"], "root": str(root),
               "arguments": subprocess.list2cmdline(["-IB", str(root / "desktop_session.py"), "serve"])}
    require(not any(c in str(root) + config["python"] for c in ("%", "\r", "\n", "\0")),
            "Task paths cannot contain environment expansions or control characters.")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
               base64.b64encode(TASK_SCRIPT.encode("utf-16le")).decode("ascii")]
    result = subprocess.run(command, input=json.dumps(request).encode("utf-8"), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    require(result.returncode == 0 and len(result.stdout) <= 4096, "Desktop task setup rejected or unconfirmed.")
    require(desktop_ssh.decode(result.stdout).get("ok") is True, "Desktop task setup failed.")


def user_manager_environment(runtime=None):
    # Connect to the existing owner bus. A system worker or SSH session may not
    # inherit XDG_RUNTIME_DIR; this neither creates a bus nor invents a display.
    runtime = Path("/run/user") / str(os.getuid()) if runtime is None else Path(runtime)
    directory, bus = runtime.lstat(), (runtime / "bus").lstat()
    require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid()
            and not directory.st_mode & 0o077 and stat.S_ISSOCK(bus.st_mode) and bus.st_uid == os.getuid(),
            "The existing owner desktop bus is unavailable.")
    return dict(os.environ, XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(runtime / "bus"))


def start():
    config = verify_installed()
    root = desktop_node.home()
    if sys.platform == "win32":
        task("start")
    else:
        # A user-manager unit inherits its existing graphical environment. Never
        # invent DISPLAY, Xauthority or a new D-Bus session on an SSH login.
        environment = user_manager_environment()
        observed = subprocess.run(["systemctl", "--user", "show-environment"], env=environment, capture_output=True, timeout=5)
        values = dict(line.split("=", 1) for line in observed.stdout.decode("utf-8").splitlines() if "=" in line)
        require(observed.returncode == 0 and (values.get("DISPLAY") or values.get("WAYLAND_DISPLAY")),
                "The graphical user manager has no desktop environment; use the local login autostart.")
        result = subprocess.run(["systemd-run", "--user", "--collect", "--unit=vincula-desktop-runtime",
                                 "--property=Restart=no", config["python"], "-IB", str(root / "desktop_session.py"), "serve"],
                                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        require(result.returncode == 0, "Desktop supervisor start rejected or already owned.")
    return {"start_requested": True, "engine_started": False}


def serve():
    verify_installed()
    if sys.platform == "win32":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        session = wintypes.DWORD()
        require(api.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) and session.value != 0,
                "The desktop supervisor must run in an interactive user session.")
    else:
        require(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
                "The desktop supervisor needs the real graphical session environment.")
    desktop_supervisor.serve(desktop_ssh.default_directory(), lease_seconds=8, guardian_lease=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "install", "start", "serve", "status"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--approved-sha256")
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "plan":
            require(args.source is not None, "Supply the release support directory.")
            digest, _, _ = bundle(args.source)
            result = {"bundle_sha256": digest, "files": list(SUPPORT_FILES), "installed": False}
        elif args.action == "install":
            require(args.source is not None and args.registry is not None and args.approved_sha256,
                    "Supply source, external pin and local catalog.")
            result = install(args.source, args.approved_sha256, args.registry, sys.executable)
        elif args.action == "serve":
            serve()
            return 0
        elif args.action == "start":
            result = start()
        else:
            result = desktop_supervisor.request(desktop_ssh.default_directory(), {"action": "status"})
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({"ok": False, "error": "Desktop setup rejected or unavailable; existing services were preserved."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
