# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""OpenSSH transport for the direct Ethernet link to Windows."""

import base64
from contextlib import contextmanager, suppress
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from errno import EINVAL, ENOSYS, EOPNOTSUPP, EPERM
import json
import os
from pathlib import Path, PureWindowsPath
import re
import selectors
import signal
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4
from xml.etree import ElementTree


HOST = os.environ.get("VINCULA_WINDOWS_HOST", "192.168.77.2")
SOURCE = os.environ.get("VINCULA_LINUX_ADDRESS", "192.168.77.1")
USER = os.environ.get("VINCULA_WINDOWS_USER", "admin")
DESTINATION = f"{USER}@{HOST}"
IDENTITY = Path.home() / ".ssh" / "id_ed25519_windows_dock"
HOST_KEY = Path(__file__).with_name("host.known_hosts").resolve()
POWERSHELL_RUNNER = Path(__file__).with_name("PowerShellRunner.cs")
SCRIPT_TIMEOUT_SECONDS = 600
OUTPUT_LIMIT = 256 * 1024
OUTPUT_TAIL = 4096
TRUNCATED = "\n[… output troncato …]\n".encode("utf-8")

POWERSHELL_SETUP = (
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
    "$ErrorActionPreference = 'Stop'\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
)

PRIVATE_SCRIPT_DIRECTORY = r"""
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owners = @($sid, 'S-1-5-18', 'S-1-5-32-544')
$security = New-Object Security.AccessControl.DirectorySecurity
$security.SetSecurityDescriptorSddlForm("O:${sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;$sid)")

function New-DockDirectory([string]$path) {
    $item = [IO.Directory]::CreateDirectory($path, $security)
    $item.Refresh()
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Cartella PowerShell non valida: $path"
    }
    $acl = $item.GetAccessControl()
    $sidType = [Security.Principal.SecurityIdentifier]
    $rules = @($acl.GetAccessRules($true, $true, $sidType))
    $valid = @($rules | Where-Object {
        $_.AccessControlType -eq 'Allow' -and $_.FileSystemRights -eq 'FullControl' -and
        $_.InheritanceFlags -eq 3 -and $_.PropagationFlags -eq 'None' -and !$_.IsInherited
    } | ForEach-Object { $_.IdentityReference.Value })
    if (!$acl.AreAccessRulesProtected -or $acl.GetOwner($sidType).Value -notin $owners -or
        $rules.Count -ne $owners.Count -or @(Compare-Object $owners $valid).Count -ne 0) {
        throw "Permessi della cartella PowerShell non validi: $path"
    }
    $item.FullName
}
$base = [Environment]::GetFolderPath('ProgramFiles')
if (!$base -or ![IO.Directory]::Exists($base) -or
    ([IO.File]::GetAttributes($base) -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Cartella Program Files non valida.'
}
$root = New-DockDirectory (Join-Path $base 'Windows Dock PowerShell')
New-DockDirectory (Join-Path $root $directoryName)
"""

STATUS_COMMAND = r"""
$os = Get-CimInstance Win32_OperatingSystem
$cpu = $null
try {
    $total = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
    if ($null -ne $total.PercentProcessorTime) {
        $cpu = [Math]::Min(100, [double]$total.PercentProcessorTime)
    }
} catch {
    # Keep the rest of the report available if performance counters are unavailable.
}
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [ordered]@{ drive = $_.DeviceID; total = $_.Size; free = $_.FreeSpace }
})
[ordered]@{
    host = $env:COMPUTERNAME
    user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    profile = $env:USERPROFILE
    os = $os.Caption
    build = $os.BuildNumber
    uptime = [int64]((Get-Date) - $os.LastBootUpTime).TotalSeconds
    cpu = $cpu
    ram_total = [int64]$os.TotalVisibleMemorySize * 1024
    ram_free = [int64]$os.FreePhysicalMemory * 1024
    disks = $disks
} | ConvertTo-Json -Compress -Depth 4
"""


class LinkError(Exception):
    """A connection or operation failed."""


def _ssh_options():
    return [
        "-F", "/dev/null",
        "-i", os.fspath(IDENTITY),
        "-o", f'UserKnownHostsFile="{HOST_KEY}"',
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "HostKeyAlgorithms=ssh-ed25519",
        "-o", "UpdateHostKeys=no",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", f"BindAddress={SOURCE}",
        "-o", "ConnectTimeout=6",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
    ]


def _capture(process, data, timeout):
    deadline = monotonic() + timeout
    buffers = {process.stdout: (bytearray(), bytearray()), process.stderr: (bytearray(), bytearray())}
    truncated = set()
    pending = memoryview(data or b"")
    with selectors.DefaultSelector() as selector:
        for stream in buffers:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        if pending:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
        while selector.get_map():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, events in selector.select(remaining):
                stream = key.fileobj
                try:
                    if events & selectors.EVENT_WRITE:
                        try:
                            pending = pending[os.write(key.fd, pending[:65536]):]
                        except BrokenPipeError:
                            pending = pending[len(pending):]
                        if pending:
                            continue
                    else:
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            prefix, tail = buffers[stream]
                            available = OUTPUT_LIMIT - len(prefix)
                            prefix.extend(chunk[:available])
                            if len(chunk) > available:
                                # Keep the end of each stream, including the remote exit record.
                                tail.extend(chunk[available:])
                                if len(tail) > OUTPUT_TAIL:
                                    truncated.add(stream)
                                    del tail[:-OUTPUT_TAIL]
                            continue
                except BlockingIOError:
                    continue
                selector.unregister(stream)
                stream.close()
    process.wait(timeout=max(0, deadline - monotonic()))
    return [bytes(prefix) + (TRUNCATED if stream in truncated else b"") + bytes(tail)
            for stream, (prefix, tail) in buffers.items()]


def _run(command, *, data=None, cwd=None, timeout):
    try:
        with subprocess.Popen(
            command, stdin=subprocess.PIPE if data else subprocess.DEVNULL, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        ) as process:
            try:
                stdout, stderr = _capture(process, data, timeout)
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as error:
        raise LinkError(f"Tempo limite raggiunto ({timeout} s).") from error
    except FileNotFoundError as error:
        raise LinkError(f"Programma richiesto non trovato: {command[0]}.") from error


def _text(data):
    return data.decode("utf-8", errors="replace").strip()


def _powershell_errors(data):
    text = _text(data)
    marker = "#< CLIXML"
    if marker not in text:
        return text
    plain, _, xml = text.partition(marker)
    try:
        root = ElementTree.fromstring(xml.strip())
    except ElementTree.ParseError:
        return text
    messages = [
        node.text.replace("_x000D_", "\r").replace("_x000A_", "\n").strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "S" and node.text
    ]
    return "\n".join(part for part in (plain.strip(), *messages) if part)


def _require_success(result):
    if result.returncode == 0:
        return
    detail = _powershell_errors(result.stderr) or _text(result.stdout)
    if "Permission denied (publickey" in detail or "agent refused operation" in detail:
        detail += "\nVerifica che la chiave SSH sia sbloccata in ssh-agent."
    raise LinkError(detail or f"Operazione terminata con codice {result.returncode}.")


def _powershell_arguments(command):
    encoded = base64.b64encode((POWERSHELL_SETUP + command).encode("utf-16-le")).decode("ascii")
    return ["-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded]


def _powershell_command(command):
    return " ".join(["powershell.exe", *_powershell_arguments(command)])


def _powershell(command, *, timeout=35):
    return _run(["ssh", "-T", *_ssh_options(), DESTINATION,
                 _powershell_command(command)], timeout=timeout)


def _ps_string(value):
    encoded = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
    return f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')))"


def _file_path(value):
    path = PureWindowsPath(value.strip())
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":":
        raise LinkError("Usa un percorso assoluto di Windows, ad esempio C:\\Users\\Admin\\note.txt.")
    if not path.name or any(
        part == ".."
        or part.endswith((".", " "))
        or any(char in '<>:"|?*' or ord(char) < 32 for char in part)
        for part in path.parts[1:]
    ):
        raise LinkError("Il percorso Windows deve identificare un file con un nome valido.")
    return path


def _sftp_name(path):
    # OpenSSH treats glob metacharacters inside quotes as literal characters.
    return f'"/{path.as_posix()}"'


def _sftp(command, *, cwd=None, timeout=900):
    result = _run(["sftp", "-q", "-b", "-", *_ssh_options(), DESTINATION],
                  data=(command + "\n").encode("utf-8"), cwd=cwd, timeout=timeout)
    _require_success(result)


def _read_status(result):
    _require_success(result)
    try:
        return json.loads(_text(result.stdout))
    except json.JSONDecodeError as error:
        raise LinkError("Windows ha restituito dati di stato in un formato inatteso.") from error


def status(*, timeout=35):
    return _read_status(_powershell(STATUS_COMMAND, timeout=timeout))


def local_status(*, timeout=35):
    """Use the same status probe on Windows without an SSH connection."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return _read_status(subprocess.run(
        [powershell, *_powershell_arguments(STATUS_COMMAND)], stdin=subprocess.DEVNULL,
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout,
    ))


def _local_file(value):
    try:
        path = Path(value).resolve()
    except RuntimeError as error:
        raise LinkError("Il percorso locale contiene un ciclo di collegamenti simbolici.") from error
    if not path.is_file():
        raise LinkError("Seleziona un file locale esistente.")
    return path


def _put(source, destination):
    # A fixed local name keeps arbitrary Linux filenames out of the SFTP command parser.
    with TemporaryDirectory(prefix="windows-dock-upload-") as directory:
        Path(directory, "payload").symlink_to(source)
        _sftp(f"put payload {_sftp_name(destination)}", cwd=directory)


def _remove_temporary(path, directory):
    literal = _ps_string(path)
    remove_flags = " -Recurse" if directory else ""
    result = _powershell(
        f"if (Test-Path -LiteralPath {literal}) "
        f"{{ Remove-Item -LiteralPath {literal} -Force{remove_flags} }}", timeout=15,
    )
    _require_success(result)


@contextmanager
def _remote_cleanup(path, *, directory=False, cleanup_on_success=True):
    try:
        yield
    except Exception as error:
        try:
            _remove_temporary(path, directory)
        except (OSError, LinkError) as cleanup_error:
            raise LinkError(f"{error}\nFile temporaneo su Windows: {path}\n{cleanup_error}") from error
        raise
    else:
        if directory and cleanup_on_success:
            _remove_temporary(path, True)


def upload(local, remote):
    source = _local_file(local)
    destination = _file_path(remote)
    temporary = destination.with_name(f".windows-dock-{uuid4().hex}.part")
    literal = _ps_string(destination)
    check = _powershell(
        f"if (Test-Path -LiteralPath {literal}) {{ throw 'Il file di destinazione esiste gia.' }}"
    )
    _require_success(check)
    with _remote_cleanup(temporary):
        _put(source, temporary)
        move = _powershell(f"[IO.File]::Move({_ps_string(temporary)}, {literal})")
        _require_success(move)


def _save_without_overwrite(source, destination):
    try:
        destination.hardlink_to(source)
        return
    except OSError as error:
        if error.errno not in (EPERM, EOPNOTSUPP, ENOSYS):
            raise

    # FAT and exFAT do not support hard links. Publish with an atomic no-replace rename.
    rename = getattr(CDLL(None, use_errno=True), "renameat2", None)
    if rename is not None:
        rename.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        rename.restype = c_int
        at_fdcwd, rename_noreplace = -100, 1
        if rename(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), rename_noreplace) == 0:
            return
        error = get_errno()
        if error not in (EINVAL, ENOSYS, EOPNOTSUPP):
            raise OSError(error, os.strerror(error), destination)
    raise LinkError("La destinazione non supporta il salvataggio atomico senza sovrascrivere file.")


def download(remote, local):
    source = _file_path(remote)
    destination = Path(local).expanduser().absolute()
    if os.path.lexists(destination):
        raise LinkError("Il file di destinazione esiste già. Scegli un altro nome.")
    with TemporaryDirectory(prefix=".windows-dock-", dir=destination.parent) as directory:
        _sftp(f"get {_sftp_name(source)} payload", cwd=directory)
        _save_without_overwrite(Path(directory, "payload"), destination)


def run_script(local):
    source = _local_file(local)
    if source.suffix.lower() != ".ps1":
        raise LinkError("Seleziona uno script PowerShell con estensione .ps1.")
    if not POWERSHELL_RUNNER.is_file():
        raise LinkError("PowerShellRunner.cs non è disponibile nella cartella di Windows Dock.")
    directory_name = f"windows-dock-{uuid4().hex}"
    make_directory = _powershell(
        f"$directoryName = {_ps_string(directory_name)}\n" + PRIVATE_SCRIPT_DIRECTORY
    )
    _require_success(make_directory)
    folder = PureWindowsPath(_text(make_directory.stdout))
    remote = _file_path(str(folder / "script.ps1"))

    with _remote_cleanup(folder, directory=True, cleanup_on_success=False):
        _put(source, remote)
        _put(POWERSHELL_RUNNER, folder / "runner.cs")

    token = "WindowsDockScript_" + uuid4().hex
    timeout = SCRIPT_TIMEOUT_SECONDS
    try:
        result = _powershell(
            f"$folder = {_ps_string(folder)}\n"
            "$runner = [IO.File]::ReadAllText((Join-Path $folder 'runner.cs'))\n"
            "$previousTemp = $env:TEMP; $previousTmp = $env:TMP\n"
            "try { $env:TEMP = $folder; $env:TMP = $folder; Add-Type -TypeDefinition $runner }\n"
            "finally { $env:TEMP = $previousTemp; $env:TMP = $previousTmp }\n"
            "$pwsh = Join-Path $PSHOME 'powershell.exe'\n"
            f"$result = [PowerShellRunner]::Run($pwsh, $folder, {int(timeout * 1000)})\n"
            f"$outcome = \"`n{token}:{{0}}:{{1}}\" -f [int]$result.TimedOut, $result.ExitCode\n"
            "[Console]::WriteLine($outcome)\n"
            "exit 0", timeout=timeout + 30,
        )
    except LinkError as error:
        raise LinkError(f"Esito PowerShell non confermato su Windows.\n{error}\nCartella di lavoro: {folder}") from error
    errors = _powershell_errors(result.stderr)
    marker = re.search(rb"\r?\n" + token.encode("ascii") + rb":([01]):(-?[0-9]+)\r?\n\Z", result.stdout)
    if result.returncode != 0 or marker is None:
        detail = errors or _text(result.stdout)
        message = "Collegamento SSH interrotto." if result.returncode == 255 else "Risposta remota incompleta."
        raise LinkError("\n".join(filter(None, (
            "Esito PowerShell non confermato su Windows.", message, detail, f"Cartella di lavoro: {folder}",
        ))))
    if marker[1] == b"1":
        notice = f"Tempo limite raggiunto ({timeout} s). Windows ha arrestato i processi dello script."
        errors = "\n".join(filter(None, (errors, notice)))
    try:
        _remove_temporary(folder, True)
    except (LinkError, OSError) as error:
        warning = f"Pulizia non confermata per {folder}\n{error}"
        errors = "\n".join(filter(None, (errors, warning)))
    return int(marker[2]), _text(result.stdout[:marker.start()]), errors
