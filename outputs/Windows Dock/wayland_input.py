# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Share Ubuntu's input on Windows through Deskflow and the existing SSH link."""

import fcntl
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic
from uuid import uuid4

import windows_ssh as windows

RUNTIME = Path.home() / ".local/share/linux-dock/vnc"
PROFILE = RUNTIME / "deskflow-wayland"
CLIPBOARD_RUNTIME = PROFILE / "clipboard-native"
PEM = RUNTIME / "input-profile/SSL/Barrier.pem"
CLIENT_TOOL = Path(__file__).with_name("StartWaylandInput.ps1")
STARTED = b"VinculaWaylandClientStarted"
CONNECTED = b'client "dock-windows" has connected'
DISCONNECTED = b'client "dock-windows" has disconnected'
DIRECTIONS = {"east": ("right", "left"), "west": ("left", "right"),
              "north": ("up", "down"), "south": ("down", "up")}


def server_configuration(directory, edge, clipboard):
    tls = directory / "tls"
    tls.mkdir(mode=0o700)
    fingerprint = (PROFILE / "settings/tls/trusted-servers").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"v2:sha256:[0-9a-f]{64}", fingerprint):
        raise windows.LinkError("Manca l’impronta Deskflow verificata di Windows.")
    (tls / "trusted-clients").write_text(fingerprint + "\n", encoding="ascii")
    direction, opposite = DIRECTIONS[edge]
    layout = directory / "layout.conf"
    layout.write_text(
        f"section: links\n dock-ubuntu:\n  {direction} = dock-windows\n"
        f" dock-windows:\n  {opposite} = dock-ubuntu\nend\n"
        "section: options\n keystroke(control+F12) = switchToNextScreen\nend\n",
        encoding="utf-8",
    )
    config = directory / "server.conf"
    config.write_text(
        "[core]\ncomputerName=dock-ubuntu\ninterface=127.0.0.1\nport=24800\nprocessMode=1\n"
        f'[server]\nexternalConfig=true\nexternalConfigFile="{layout}"\n'
        f"protocol=barrier\nenableClipboard={str(clipboard).lower()}\nclipboardSize={3 if clipboard else 0}\n"
        f'[security]\ntlsEnabled=true\ncheckPeerFingerprints=true\ncertificate="{PEM}"\n'
        "[log]\nlevel=DEBUG\n"
        "[computer_dock-ubuntu]\nname=dock-ubuntu\n[computer_dock-windows]\nname=dock-windows\n",
        encoding="utf-8",
    )
    return config


def windows_directory():
    name = f"VinculaWayland-{uuid4().hex}"
    reply = windows._powershell(
        f"$path = Join-Path ([IO.Path]::GetTempPath()) '{name}'\n"
        "[void][IO.Directory]::CreateDirectory($path)\n$path",
        timeout=10,
    )
    windows._require_success(reply)
    path = windows._file_path(windows._text(reply.stdout))
    if path.name != name:
        raise windows.LinkError("Windows ha restituito una cartella di sessione inattesa.")
    return path


def require_running(process, log, fallback):
    if process.poll() is not None:
        size = os.fstat(log.fileno()).st_size
        message = windows._text(os.pread(log.fileno(), 8192, max(0, size - 8192)))
        raise windows.LinkError(message or fallback)


def monitor(server, tunnel, server_log, ssh_log, ready, stop, opened):
    deadline = monotonic() + 45
    lost_at = None
    connected_once = False
    while not stop.wait(0.2):
        require_running(server, server_log, "Deskflow si è chiuso su Ubuntu.")
        require_running(tunnel, ssh_log, "La connessione Deskflow con Windows si è chiusa.")
        now = monotonic()
        size = os.fstat(server_log.fileno()).st_size
        start = max(0, size - 65536)
        recent = os.pread(server_log.fileno(), 65536, start)
        connected = recent.rfind(CONNECTED)
        disconnected = recent.rfind(DISCONNECTED)
        if opened is not None:
            windows_ready = STARTED in os.pread(ready.fileno(), 8192, 0)
            if connected > disconnected and windows_ready:
                opened()
                opened = None
                connected_once = True
            elif now >= deadline:
                message = windows._text(recent[-8192:])
                raise windows.LinkError("Deskflow non si è connesso. Controlla il consenso di GNOME.\n" + message)
        if connected_once and disconnected > connected:
            lost_at = now if lost_at is None else lost_at
            if now - lost_at >= 5:
                raise windows.LinkError("Windows si è scollegato durante la condivisione.")
        elif connected >= 0:
            lost_at = None


def share_input(stop, edge, opened, managed_process):
    if not all(path.is_file() for path in (PROFILE / "bin/deskflow-core", PEM, CLIENT_TOOL)):
        raise windows.LinkError("Installa il componente Deskflow Wayland per la condivisione da Ubuntu.")
    with (RUNTIME / "desktop-session.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise windows.LinkError("Chiudi la condivisione già attiva fra i due PC.") from None
        with TemporaryDirectory(prefix="vincula-input-") as directory:
            clipboard_engine = CLIPBOARD_RUNTIME / "deskflow-core"
            clipboard = clipboard_engine.is_file()
            engine_path = clipboard_engine if clipboard else PROFILE / "bin/deskflow-core"
            config = server_configuration(Path(directory), edge, clipboard)
            remote = windows_directory()
            with windows._remote_cleanup(remote, directory=True):
                script = PureWindowsPath(remote) / CLIENT_TOOL.name
                windows.upload(CLIENT_TOOL, str(script))
                call_client = f"& {windows._ps_string(script)}"
                command = windows._powershell_command(call_client)
                ssh = ["ssh", "-T", *windows._ssh_options(), "-o", "ExitOnForwardFailure=yes",
                       "-R", "127.0.0.1:24802:127.0.0.1:24800", windows.DESTINATION, command]
                env = dict(os.environ, LD_LIBRARY_PATH=str(PROFILE / "lib"),
                           QT_PLUGIN_PATH=str(PROFILE / "lib/qt6/plugins"), QT_QPA_PLATFORM="xcb")
                if clipboard:
                    env.update(LD_LIBRARY_PATH=os.pathsep.join(map(str, (CLIPBOARD_RUNTIME / "lib", PROFILE / "lib"))),
                               VINCULA_REMOTE_DESKTOP_CLIPBOARD="1")
                server = [str(engine_path), "server", "--new-instance", "-s", str(config)]
                try:
                    with TemporaryFile() as server_log, TemporaryFile() as ssh_log, TemporaryFile() as ready:
                        with managed_process(server, env=env, stdout=server_log, stderr=server_log) as engine:
                            with managed_process(ssh, stdin=subprocess.PIPE, stderr=ssh_log, stdout=ready) as tunnel:
                                try:
                                    monitor(engine, tunnel, server_log, ssh_log, ready, stop, opened)
                                finally:
                                    tunnel.stdin.close()
                                    try:
                                        tunnel.wait(timeout=6)
                                    except subprocess.TimeoutExpired:
                                        tunnel.terminate()
                finally:
                    # A broken SSH connection may terminate PowerShell before its own finally runs.
                    windows._require_success(windows._powershell(call_client + " -Stop", timeout=10))
