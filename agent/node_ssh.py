# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Execute fixed Vincula operations over an operator-configured OpenSSH route."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryFile

WINDOWS = sys.platform == "win32"
EMPTY_CONFIG = "NUL" if WINDOWS else "/dev/null"
PROBE_TIMEOUT = 12
OPTIONS = (
    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=6", "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
    "-o", "PreferredAuthentications=publickey", "-o", "NumberOfPasswordPrompts=0",
)


def executable(program):
    if WINDOWS:
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        return system / "System32" / "OpenSSH" / (program + ".exe")
    return program


def environment():
    env = os.environ.copy()
    if not WINDOWS or env.get("PROGRAMDATA"):
        return env
    # MCP clients may filter ProgramData, which Windows OpenSSH requires.
    from ctypes import byref, c_ubyte, c_wchar_p, windll
    from uuid import UUID

    folder_id = (c_ubyte * 16).from_buffer_copy(UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97").bytes_le)
    path = c_wchar_p()
    try:
        if windll.shell32.SHGetKnownFolderPath(byref(folder_id), 0, None, byref(path)) != 0:
            raise OSError("Could not locate Windows ProgramData.")
        env["PROGRAMDATA"] = path.value
    finally:
        windll.ole32.CoTaskMemFree(path)
    return env


def run(route, program, flags, args, stdout, *, timeout, cwd=None):
    if route is None or not route.configured():
        raise ValueError("The registered node has no configured SSH route.")
    # Use regular files for Windows OpenSSH, which can leave a captured pipe open.
    with TemporaryFile() as stderr:
        subprocess.run([executable(program), *flags, *OPTIONS, *route.options, route.destination, *args],
                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, cwd=cwd, env=environment(),
                       creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0,
                       timeout=timeout, check=True)


def json_command(route, command):
    with TemporaryFile() as output:
        run(route, "ssh", ("-nT",), (command,), output, timeout=PROBE_TIMEOUT)
        output.seek(0)
        return json.loads(output.read(256 * 1024))


def copy(route, command, directory):
    with ExitStack() as cleanup:
        with NamedTemporaryFile(delete=False) as batch:
            path = Path(batch.name)
            cleanup.callback(path.unlink, missing_ok=True)
            batch.write((command + "\n").encode("utf-8"))
        run(route, "sftp", ("-q", "-b", path), (), subprocess.DEVNULL, cwd=directory, timeout=90)
