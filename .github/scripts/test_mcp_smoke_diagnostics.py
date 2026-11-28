# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure observers and owned TEMP launcher text; no actual child/SDK/host probe."""
import ast
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import mcp_smoke_diagnostics as diagnostics


class SmokeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.child_guard = patch.object(subprocess, "Popen", side_effect=AssertionError("No native child"))
        self.spawn = self.child_guard.start()
        self.addCleanup(self.child_guard.stop)
        self.addCleanup(self.spawn.assert_not_called)
        self.output = io.StringIO()
        self.node = SimpleNamespace(family="windows")
        self.secret = "SENSITIVE_PRIVATE_ACCOUNT_PATH_COMMAND_INPUT"

    def fixture(self, *, result=None, error=None, metrics_error=None):
        run = Mock(return_value=result, side_effect=error)
        original_subprocess = SimpleNamespace(run=run, CREATE_NO_WINDOW=123)
        dock = SimpleNamespace(subprocess=original_subprocess)
        argv = [self.secret, "-EncodedCommand", self.secret]
        environment = {"SYSTEMROOT": self.secret, "PSModulePath": self.secret, "SECRET_VALUE": self.secret}
        kwargs = {"stdin": subprocess.DEVNULL, "capture_output": True, "creationflags": 123,
                  "timeout": 12, "env": environment}
        status = object()

        def metrics(node, is_local):
            dock.subprocess.run(argv, **kwargs)
            if metrics_error is not None:
                raise metrics_error
            return status

        server = SimpleNamespace(read_metrics=metrics, windows_ssh=dock)
        return server, original_subprocess, run, argv, kwargs, status

    def records(self):
        self.assertNotIn(self.secret, self.output.getvalue())
        values = [json.loads(line) for line in self.output.getvalue().splitlines()]
        for value in values:
            self.assertEqual(value["kind"], "vincula_ci_windows_status")
            self.assertNotIn("args", value)
            self.assertNotIn("command", value)
            self.assertNotIn("message", value)
        return values

    def test_success_preserves_return_args_environment_and_global_subprocess(self):
        response = subprocess.CompletedProcess([self.secret], 0, self.secret.encode(), self.secret.encode())
        server, original, run, argv, kwargs, status = self.fixture(result=response)
        function, global_run = server.read_metrics, subprocess.run
        with diagnostics.observe_local_windows(server, self.output):
            self.assertIs(server.read_metrics(self.node, True), status)
            self.assertIs(subprocess.run, global_run)
            self.assertEqual(server.windows_ssh.subprocess.CREATE_NO_WINDOW, 123)
        self.assertIs(server.read_metrics, function)
        self.assertIs(server.windows_ssh.subprocess, original)
        self.assertEqual(run.call_count, 1)
        self.assertIs(run.call_args.args[0], argv)
        self.assertIs(run.call_args.kwargs["env"], kwargs["env"])
        self.assertEqual(run.call_args.kwargs, kwargs)
        values = self.records()
        self.assertEqual([x["phase"] for x in values], ["powershell_run", "metrics"])
        self.assertTrue(all(x["outcome"] == "success" for x in values))
        self.assertEqual(values[0]["timeout_seconds"], 12)
        self.assertEqual(values[0]["stdout"], {"bytes": len(self.secret), "sha256": sha256(self.secret.encode()).hexdigest()})
        self.assertEqual(values[0]["stderr"], values[0]["stdout"])
        self.assertTrue(values[0]["environment_present"]["SystemRoot"])
        self.assertTrue(values[0]["environment_present"]["PSModulePath"])
        self.assertNotIn("SECRET_VALUE", values[0]["environment_present"])

    def test_timeout_rethrows_identical_exception_once_without_sensitive_output(self):
        failure = subprocess.TimeoutExpired([self.secret], 12, output=self.secret.encode(), stderr=self.secret.encode())
        failure.__cause__ = OSError(13, self.secret)
        server, original, run, argv, kwargs, _ = self.fixture(error=failure)
        function = server.read_metrics
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            with diagnostics.observe_local_windows(server, self.output):
                server.read_metrics(self.node, True)
        self.assertIs(caught.exception, failure)
        self.assertIs(caught.exception.__cause__, failure.__cause__)
        self.assertIs(server.read_metrics, function)
        self.assertIs(server.windows_ssh.subprocess, original)
        self.assertEqual(run.call_count, 1)
        self.assertIs(run.call_args.args[0], argv)
        self.assertEqual(run.call_args.kwargs, kwargs)
        values = self.records()
        self.assertEqual([x["exception_type"] for x in values], ["TimeoutExpired", "TimeoutExpired"])
        self.assertTrue(all(x["cause_type"] == "PermissionError" for x in values))
        self.assertEqual(values[0]["stderr"]["sha256"], sha256(self.secret.encode()).hexdigest())

    def test_nonzero_result_and_metrics_error_remain_distinct_without_error_text(self):
        response = subprocess.CompletedProcess([self.secret], 1, b"", self.secret.encode())
        failure = ValueError(self.secret)
        server, _, run, _, _, _ = self.fixture(result=response, metrics_error=failure)
        with diagnostics.observe_local_windows(server, self.output):
            with self.assertRaises(ValueError) as caught:
                server.read_metrics(self.node, True)
        self.assertIs(caught.exception, failure)
        self.assertEqual(run.call_count, 1)
        first, second = self.records()
        self.assertEqual((first["returncode"], first["exception_type"]), (1, None))
        self.assertEqual(second["exception_type"], "ValueError")
        self.assertIsNone(second["stderr"])

    def test_nonlocal_and_linux_metrics_are_not_observed(self):
        server, _, run, _, _, status = self.fixture(result=SimpleNamespace())
        with diagnostics.observe_local_windows(server, self.output):
            self.assertIs(server.read_metrics(self.node, False), status)
            self.assertIs(server.read_metrics(SimpleNamespace(family="linux"), True), status)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.output.getvalue(), "")

    def test_diagnostic_sink_failure_does_not_change_success_or_original_error(self):
        stream = Mock()
        stream.write.side_effect = OSError(self.secret)
        server, _, _, _, _, status = self.fixture(result=SimpleNamespace())
        with diagnostics.observe_local_windows(server, stream):
            self.assertIs(server.read_metrics(self.node, True), status)
        failure = subprocess.TimeoutExpired(self.secret, 12)
        server, _, _, _, _, _ = self.fixture(error=failure)
        with diagnostics.observe_local_windows(server, stream):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                server.read_metrics(self.node, True)
        self.assertIs(caught.exception, failure)

    def test_factory_restores_on_success_and_failure_without_changing_other_parameters(self):
        for fail in (False, True):
            with self.subTest(fail=fail), TemporaryDirectory(prefix="vincula-ci-observer-fake-") as directory:
                agent = Path(directory).resolve() / "agent"
                factory = Mock(side_effect=lambda *args, **kwargs: (args, kwargs))
                module = SimpleNamespace(StdioServerParameters=factory)
                environment = {"USERPROFILE": self.secret}
                try:
                    with diagnostics.diagnostic_server(module, agent, directory):
                        result = module.StdioServerParameters(command=self.secret, args=[str(agent / "server.py")], env=environment)
                        self.assertEqual(result[1]["command"], self.secret)
                        self.assertIs(result[1]["env"], environment)
                        wrapper = Path(result[1]["args"][0])
                        self.assertEqual(wrapper.parent, Path(directory))
                        ast.parse(wrapper.read_text())
                        self.assertNotIn("EncodedCommand", wrapper.read_text())
                        if os.name != "nt":
                            self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o600)
                        # Calls other than this exact server invocation are unchanged.
                        supplied = [self.secret]
                        other = module.StdioServerParameters(command=self.secret, args=supplied, env=environment)
                        self.assertIs(other[1]["args"], supplied)
                        if fail:
                            raise ValueError("synthetic setup failure")
                except ValueError:
                    self.assertTrue(fail)
                self.assertIs(module.StdioServerParameters, factory)

    def test_number_fields_never_serialize_booleans_or_nonfinite_values(self):
        for value in (True, False, float("inf"), float("nan"), self.secret):
            self.assertIsNone(diagnostics._number(value))
        self.assertEqual(diagnostics._number(12), 12)


if __name__ == "__main__":
    unittest.main()
