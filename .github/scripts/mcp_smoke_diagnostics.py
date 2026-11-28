# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""CI-only observer for the first, unchanged local Windows status probe.

No command, output text, path, account or environment value enters a record.
The proxy replaces only windows_ssh's module reference, never subprocess.run
globally. It delegates with the original argument objects and preserves results
and exception instances. Observation failure must not change probe behavior.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from threading import local
from time import monotonic


ENV_NAMES = ("SystemRoot", "PSModulePath", "PATH", "TEMP", "USERPROFILE", "LOCALAPPDATA", "PROGRAMDATA")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


def _blob(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogatepass")
    if not isinstance(value, bytes):
        return {"bytes": None, "sha256": None}
    return {"bytes": len(value), "sha256": sha256(value).hexdigest()}


def _emit(stream, phase, started, *, environment=None, timeout=None, result=None, error=None):
    try:
        environment = os.environ if environment is None else environment
        names = {name.casefold() for name in environment}
        cause = (error.__cause__ or error.__context__) if error is not None else None
        record = {
            "kind": "vincula_ci_windows_status", "version": 1, "phase": phase,
            "outcome": "error" if error is not None else "success",
            "duration_ms": max(0, round((monotonic() - started) * 1000)),
            "exception_type": type(error).__name__ if error is not None else None,
            "cause_type": type(cause).__name__ if cause is not None else None,
            "timeout_seconds": _number(timeout),
            "returncode": _number(getattr(error if error is not None else result, "returncode", None)),
            "errno": _number(getattr(error, "errno", None)),
            "winerror": _number(getattr(error, "winerror", None)),
            "stdout": _blob(getattr(error if error is not None else result, "stdout", None)),
            "stderr": _blob(getattr(error if error is not None else result, "stderr", None)),
            "environment_present": {name: name.casefold() in names for name in ENV_NAMES},
        }
        stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
    except Exception:
        # Diagnostics must not mask the original exception or change success.
        pass


class _SubprocessProxy:
    def __init__(self, original, scope, stream):
        self.original, self.scope, self.stream = original, scope, stream

    def __getattr__(self, name):
        return getattr(self.original, name)

    def run(self, *args, **kwargs):
        if not getattr(self.scope, "observing", False):
            return self.original.run(*args, **kwargs)
        started = monotonic()
        details = {"environment": kwargs.get("env"), "timeout": kwargs.get("timeout")}
        try:
            result = self.original.run(*args, **kwargs)
        except BaseException as error:
            _emit(self.stream, "powershell_run", started, error=error, **details)
            raise
        _emit(self.stream, "powershell_run", started, result=result, **details)
        return result


@contextmanager
def observe_local_windows(server, stream=None):
    """Wrap only the probe on this server; restore both references on exit."""
    stream = sys.stderr if stream is None else stream
    scope = local()
    original_metrics = server.read_metrics
    original_subprocess = server.windows_ssh.subprocess

    def metrics(node, is_local):
        if not is_local or node.family != "windows":
            return original_metrics(node, is_local)
        previous = getattr(scope, "observing", False)
        scope.observing = True
        started = monotonic()
        try:
            result = original_metrics(node, is_local)
        except BaseException as error:
            _emit(stream, "metrics", started, error=error)
            raise
        else:
            _emit(stream, "metrics", started)
            return result
        finally:
            scope.observing = previous

    server.read_metrics = metrics
    server.windows_ssh.subprocess = _SubprocessProxy(original_subprocess, scope, stream)
    try:
        yield
    finally:
        server.read_metrics = original_metrics
        server.windows_ssh.subprocess = original_subprocess


@contextmanager
def diagnostic_server(check_module, agent, directory):
    """CI factory override only; the product check.py and server stay unchanged."""
    agent = Path(agent).resolve()
    wrapper = Path(directory) / "mcp-smoke-observer.py"
    source = ("import sys\n"
              f"sys.path[:0] = {[str(agent), str(Path(__file__).resolve().parent)]!r}\n"
              "import server\n"
              "from mcp_smoke_diagnostics import observe_local_windows\n"
              "with observe_local_windows(server):\n"
              "    server.local_node()\n"
              "    server.mcp.run(transport='stdio')\n")
    descriptor = os.open(wrapper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(source)
    original_factory = check_module.StdioServerParameters

    def parameters(*args, **kwargs):
        if kwargs.get("args") == [str(agent / "server.py")]:
            kwargs = dict(kwargs, args=[str(wrapper)])
        return original_factory(*args, **kwargs)

    check_module.StdioServerParameters = parameters
    try:
        yield
    finally:
        check_module.StdioServerParameters = original_factory
