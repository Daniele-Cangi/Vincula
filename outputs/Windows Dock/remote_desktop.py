"""Share the Windows desktop or input across the existing SSH link."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryFile
from time import monotonic

from windows_ssh import DESTINATION, LinkError, _powershell_command, _ssh_options


RUNTIME = Path.home() / ".local/share/linux-dock/vnc"
LOCAL = ("127.0.0.1", 15900)
FORWARD = f"{LOCAL[0]}:{LOCAL[1]}:127.0.0.1:5909"
FORWARD_READY = b"WindowsDockForwardReady"


@contextmanager
def _managed_process(command, *, stdin=subprocess.DEVNULL, **options):
    with subprocess.Popen(command, stdin=stdin, **options) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()


def _log(file):
    file.seek(0)
    return file.read().decode("utf-8", errors="replace").strip()


def _ready(tunnel, stop, log, confirmation):
    deadline = monotonic() + 10
    while monotonic() < deadline and not stop.is_set():
        if tunnel.poll() is not None:
            raise LinkError(_log(log) or "Il tunnel SSH si è chiuso.")
        if FORWARD_READY not in os.pread(confirmation.fileno(), 4096, 0).splitlines():
            stop.wait(0.15)
            continue
        try:
            with socket.create_connection(LOCAL, timeout=0.4) as channel:
                with channel.makefile("rb") as banner:
                    if banner.read(4) == b"RFB " and tunnel.poll() is None:
                        return True
        except OSError:
            pass
        stop.wait(0.15)
    if not stop.is_set():
        raise LinkError("Il server desktop Windows non risponde. Verifica che la sessione Windows sia aperta.")
    return False


def _run_windows_client(command, stop, *, env=None, opened=None):
    with (RUNTIME / "desktop-session.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LinkError("Chiudi la condivisione attiva sul PC Windows.") from error
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(LOCAL)
        except OSError as error:
            raise LinkError("La porta locale 15900 è occupata. Chiudi la sessione precedente.") from error

        # SSH executes the remote command only after binding its local forward.
        confirm = _powershell_command(
            f"[Console]::WriteLine('{FORWARD_READY.decode('ascii')}')\n"
            "[void][Console]::In.ReadToEnd()"
        )
        tunnel_command = ["ssh", "-T", *_ssh_options(), "-o", "ExitOnForwardFailure=yes",
                          "-L", FORWARD, DESTINATION, confirm]
        with TemporaryFile() as ssh_log, TemporaryFile() as client_log, TemporaryFile() as confirmation:
            with _managed_process(tunnel_command, stdin=subprocess.PIPE,
                                  stderr=ssh_log, stdout=confirmation) as tunnel:
                if not _ready(tunnel, stop, ssh_log, confirmation):
                    return
                with _managed_process(command, env=env, stderr=client_log) as client:
                    deadline = monotonic() + 8
                    while client.poll() is None and not stop.wait(0.2):
                        if tunnel.poll() is not None:
                            raise LinkError(_log(ssh_log) or "Il tunnel SSH si è interrotto.")
                        if opened is not None:
                            if b"Desktop name" in os.pread(client_log.fileno(), 8192, 0):
                                opened()
                                opened = None
                            elif monotonic() >= deadline:
                                raise LinkError("Windows non ha completato il collegamento per mouse e tastiera.")
                    if not stop.is_set() and (client.returncode or opened is not None):
                        raise LinkError(_log(client_log) or "La connessione a Windows si è chiusa con un errore.")


def show_windows(stop):
    viewer = RUNTIME / "bin/xtigervncviewer"
    password = RUNTIME / "windows.pass"
    if not viewer.is_file() or not password.is_file():
        raise LinkError("Il visualizzatore o il profilo del desktop remoto non è disponibile.")
    command = [str(viewer), "-SecurityTypes", "VncAuth", "-passwd", str(password),
               "-AcceptClipboard=0", "-SendClipboard=0", "-RemoteResize=0",
               f"{LOCAL[0]}::{LOCAL[1]}"]
    viewer_env = dict(os.environ, LD_LIBRARY_PATH=str(RUNTIME / "lib"))
    _run_windows_client(command, stop, env=viewer_env)


def share_input(stop, edge, opened):
    if os.environ.get("XDG_SESSION_TYPE") != "x11" or not os.environ.get("DISPLAY"):
        raise LinkError("Per condividere mouse e tastiera, accedi a Ubuntu con una sessione X11.")
    if edge not in ("east", "west", "north", "south"):
        raise LinkError("Seleziona il lato in cui si trova lo schermo di Windows.")

    bridge = RUNTIME / "bin/x2vnc"
    password = RUNTIME / "windows.pass"
    if not bridge.is_file() or not password.is_file():
        raise LinkError("Il componente per condividere mouse e tastiera non è disponibile.")
    command = [str(bridge), f"-{edge}", "-edgewidth", "2", "-resurface", "-noreconnect",
               "-hotkey", "Control-F12", "-noclipboard", "-passwdfile", str(password),
               f"{LOCAL[0]}:{LOCAL[1]}"]
    _run_windows_client(command, stop, opened=opened)
