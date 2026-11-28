# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Execute fixed Vincula operations over an operator-configured OpenSSH route."""

from contextlib import ExitStack
import json
import math
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


def run(route, program, flags, args, stdout, *, timeout, cwd=None, stdin=None):
    if route is None or not route.configured():
        raise ValueError("The registered node has no configured SSH route.")
    # Use regular files for Windows OpenSSH, which can leave a captured pipe open.
    with TemporaryFile() as stderr:
        subprocess.run([executable(program), *flags, *OPTIONS, *route.options, route.destination, *args],
                       stdin=subprocess.DEVNULL if stdin is None else stdin,
                       stdout=stdout, stderr=stderr, cwd=cwd, env=environment(),
                       creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0,
                       timeout=timeout, check=True)


def json_command(route, command, *, input_data=None, output_limit=256 * 1024, timeout=PROBE_TIMEOUT):
    if input_data is not None and (not isinstance(input_data, bytes) or len(input_data) > 512 * 1024):
        raise ValueError("SSH input exceeds the bounded journal transport.")
    if type(output_limit) is not int or not 1 <= output_limit <= 1024 * 1024:
        raise ValueError("Invalid SSH JSON output limit.")
    if type(timeout) not in (int, float) or not 0 < timeout <= PROBE_TIMEOUT or not math.isfinite(timeout):
        raise ValueError("SSH JSON timeout must be positive and no longer than the probe budget.")
    with ExitStack() as cleanup:
        output = cleanup.enter_context(TemporaryFile())
        source = None
        if input_data is not None:
            source = cleanup.enter_context(TemporaryFile())
            source.write(input_data)
            source.seek(0)
        flags = ("-T",) if source is not None else ("-nT",)
        run(route, "ssh", flags, (command,), output, timeout=timeout, stdin=source)
        output.seek(0)
        raw = output.read(output_limit + 1)
        if len(raw) > output_limit:
            raise ValueError("SSH JSON response exceeds its limit.")
        return json.loads(raw)


def copy(route, command, directory):
    with ExitStack() as cleanup:
        with NamedTemporaryFile(delete=False) as batch:
            path = Path(batch.name)
            cleanup.callback(path.unlink, missing_ok=True)
            batch.write((command + "\n").encode("utf-8"))
        run(route, "sftp", ("-q", "-b", path), (), subprocess.DEVNULL, cwd=directory, timeout=90)
