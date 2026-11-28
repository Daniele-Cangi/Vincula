# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fallback policy, exact transport and native read-only Windows API fixtures."""
import ast
from contextlib import ExitStack
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import node_registry
import node_ssh
import server
import windows_native_status as native


class NativeStatusTests(unittest.TestCase):
    def setUp(self):
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.spawn = self.scope.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("No process")))
        self.addCleanup(self.spawn.assert_not_called)
        self.node = node_registry.Node("windows", "Windows", "windows")

    @staticmethod
    def base_metrics():
        return {"os": "Windows", "os_version": "10.0.26100", "uptime_seconds": 543,
                "cpu_percent": 83.33, "memory_total_bytes": 8192, "memory_available_bytes": 4096,
                "cpu_logical": 8, "architecture": "AMD64", "gpus": None, "volumes": []}

    def kernel(self):
        api = SimpleNamespace(**{name: Mock() for name in (
            "GlobalMemoryStatusEx", "GetSystemTimes", "GetTickCount64", "GetLogicalDrives", "GetDriveTypeW")})
        def memory(pointer):
            self.assertEqual(pointer._obj.length, 64)
            pointer._obj.total, pointer._obj.available = 8192, 4096
            return 1
        times = iter(((100, 300, 200), (150, 500, 300)))
        def cpu(idle, kernel, user):
            for pointer, ticks in zip((idle, kernel, user), next(times)):
                pointer._obj.low, pointer._obj.high = ticks & 0xffffffff, ticks >> 32
            return 1
        api.GlobalMemoryStatusEx.side_effect = memory
        api.GetSystemTimes.side_effect = cpu
        api.GetTickCount64.return_value = 543210
        api.GetLogicalDrives.return_value = (1 << 2) | (1 << 3) | (1 << 13)
        api.GetDriveTypeW.side_effect = {"C:\\": 3, "D:\\": 2, "N:\\": 4}.__getitem__
        return api

    def native_environment(self, api):
        self.scope.enter_context(patch.object(native.sys, "platform", "win32"))
        self.scope.enter_context(patch.object(native.sys, "getwindowsversion", create=True,
                                              return_value=SimpleNamespace(platform_version=(10, 0, 26100))))
        self.scope.enter_context(patch.object(native.ctypes, "WinDLL", create=True, return_value=api))
        self.scope.enter_context(patch.object(native.os, "cpu_count", return_value=8))
        self.scope.enter_context(patch.dict(native.os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}))
        self.sleep = self.scope.enter_context(patch.object(native.time, "sleep"))
        return self.scope.enter_context(patch.object(native.shutil, "disk_usage",
            return_value=SimpleNamespace(total=1000, free=400)))

    def test_real_helper_logic_uses_correct_abi_sample_and_only_fixed_disks(self):
        api = self.kernel(); usage = self.native_environment(api)
        result = native.read_status()
        self.assertEqual(ctypes.sizeof(native._MemoryStatus), 64)
        self.assertEqual(ctypes.sizeof(native._FileTime), 8)
        self.assertIs(api.GetTickCount64.restype, ctypes.c_uint64)
        self.assertEqual(api.GetSystemTimes.argtypes, [ctypes.POINTER(native._FileTime)] * 3)
        self.assertEqual(result["cpu_percent"], 83.33)
        self.assertEqual(result["uptime_seconds"], 543)
        self.assertEqual((result["memory_total_bytes"], result["memory_available_bytes"]), (8192, 4096))
        self.assertEqual(result["volumes"], [{"path": "C:\\", "total_bytes": 1000, "available_bytes": 400}])
        usage.assert_called_once_with("C:\\")
        self.sleep.assert_called_once_with(0.05)
        self.assertIsNone(result["gpus"])
        server.Metrics.model_validate(result)

    def test_cpu_unknown_is_not_synthetic_zero_and_high_filetime_bits_are_kept(self):
        api = self.kernel()
        api.GetSystemTimes.side_effect = None;api.GetSystemTimes.return_value = 0
        self.assertIsNone(native._cpu_percent(api))
        self.assertEqual(native._FileTime(1, 2).ticks(), (2 << 32) + 1)
        with patch.object(native, "_cpu_times", side_effect=((100, 50), (100, 50))), patch.object(native.time, "sleep"):
            self.assertIsNone(native._cpu_percent(api))

    def test_memory_and_drive_api_failures_are_not_available_zero_metrics(self):
        api = self.kernel();self.native_environment(api)
        api.GlobalMemoryStatusEx.side_effect = None;api.GlobalMemoryStatusEx.return_value = 0
        with self.assertRaises(OSError):
            native.read_status()
        api = self.kernel();api.GetLogicalDrives.return_value = 0
        with self.assertRaises(OSError):
            native._volumes(api)

    def test_powershell_success_uses_original_command_budget_and_no_fallback(self):
        status = {"os": "Windows edition", "build": "26100", "uptime": 543, "cpu": 10,
                  "ram_total": 8192, "ram_free": 4096, "disks": []}
        with patch.object(server.windows_ssh, "local_status", return_value=status) as powershell, \
             patch.object(server, "native_windows_metrics", side_effect=AssertionError("No fallback")):
            self.assertEqual(server.windows_metrics(self.node, True).cpu_percent, 10)
        powershell.assert_called_once_with(timeout=12, command=server.WINDOWS_STATUS)

    def test_only_powershell_timeout_runs_actual_native_helper_logic(self):
        api = self.kernel();self.native_environment(api)
        with patch.object(server.windows_ssh, "local_status", side_effect=subprocess.TimeoutExpired("fixed", 12)) as powershell:
            result = server.windows_metrics(self.node, True)
        self.assertEqual(result.cpu_percent, 83.33)
        self.assertEqual(result.memory_available_bytes, 4096)
        powershell.assert_called_once_with(timeout=12, command=server.WINDOWS_STATUS)
        self.assertIsNone(result.gpus)

    def test_other_powershell_errors_do_not_trigger_fallback(self):
        failures = (server.windows_ssh.LinkError("fixed failure"), OSError("fixed failure"),
                    json.JSONDecodeError("fixed failure", "", 0), subprocess.CalledProcessError(1, "fixed"))
        for failure in failures:
            with self.subTest(kind=type(failure).__name__), \
                 patch.object(server.windows_ssh, "local_status", side_effect=failure), \
                 patch.object(server, "native_windows_metrics") as fallback:
                with self.assertRaises(type(failure)) as caught:
                    server.windows_metrics(self.node, True)
                self.assertIs(caught.exception, failure)
                fallback.assert_not_called()
        status = {"os": "Windows", "build": "1", "uptime": 1, "cpu": -1,
                  "ram_total": 1, "ram_free": 1, "disks": []}
        with patch.object(server.windows_ssh, "local_status", return_value=status), \
             patch.object(server, "native_windows_metrics") as fallback:
            with self.assertRaises(ValueError):
                server.windows_metrics(self.node, True)
            fallback.assert_not_called()

    def test_failed_fallback_still_reports_unavailable(self):
        with patch.object(server, "local_node", return_value="windows"), \
             patch.object(server.windows_ssh, "local_status", side_effect=subprocess.TimeoutExpired("fixed", 12)), \
             patch.object(native, "read_status", side_effect=OSError("native query failed")) as fallback:
            result = server.node_status(self.node)
        self.assertEqual(result.availability, "unavailable")
        self.assertIsNone(result.metrics)
        fallback.assert_called_once_with()

    def test_remote_fallback_preserves_route_and_uses_exact_source_bytes_and_short_budget(self):
        route = node_registry.Route("authenticated-peer", ("-o", "StrictHostKeyChecking=yes"))
        peer = node_registry.Node("peer", "Windows peer", "windows", route=route)
        with patch.object(node_ssh, "json_command", side_effect=[subprocess.TimeoutExpired("fixed", 12), self.base_metrics()]) as execute:
            result = server.windows_metrics(peer, False)
        self.assertEqual(result.memory_total_bytes, 8192)
        self.assertEqual(execute.call_count, 2)
        first, second = execute.call_args_list
        self.assertIs(first.args[0], route)
        self.assertEqual(first.args[1], server.windows_ssh._powershell_command(server.WINDOWS_STATUS))
        self.assertEqual(first.kwargs, {})
        self.assertIs(second.args[0], route)
        self.assertEqual(second.kwargs["timeout"], 3)
        self.assertEqual(second.kwargs["output_limit"], 16384)
        source = Path(native.__file__).read_bytes()
        self.assertEqual(second.kwargs["input_data"], source)
        command = second.args[1]
        self.assertTrue(command.startswith('py.exe -3 -IB -c "') and command.endswith('"'))
        code = ast.parse(command[len('py.exe -3 -IB -c "'):-1])
        reads = [node for node in ast.walk(code) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(ast.literal_eval(reads[0].args[0]), len(source))
        self.assertTrue(any(isinstance(node, ast.Assert) for node in ast.walk(code)))
        compile(source, "native-status-source", "exec")
        self.assertNotIn("PowerShell", command)

    def test_json_transport_accepts_only_shorter_finite_timeouts_and_keeps_default(self):
        seen = []
        def run(route, program, flags, args, output, *, timeout, stdin):
            seen.append((timeout, flags, stdin.read() if stdin else None))
            output.write(b'{"ok":true}')
        with patch.object(node_ssh, "run", side_effect=run) as execute:
            self.assertTrue(node_ssh.json_command(None, "fixed")["ok"])
            self.assertTrue(node_ssh.json_command(None, "fixed", timeout=3, input_data=b"source")["ok"])
            self.assertEqual(seen, [(12, ("-nT",), None), (3, ("-T",), b"source")])
            count = execute.call_count
            for timeout in (True, False, 0, -1, 13, float("nan"), float("inf"), "3", None, 10**1000):
                with self.subTest(kind=type(timeout).__name__), self.assertRaises(ValueError):
                    node_ssh.json_command(None, "fixed", timeout=timeout)
            self.assertEqual(execute.call_count, count)

    def test_json_transport_still_enforces_output_bound_with_fallback_budget(self):
        def run(route, program, flags, args, output, **kwargs):
            self.assertEqual(kwargs["timeout"], 3)
            output.write(b"x" * 16385)
        with patch.object(node_ssh, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                node_ssh.json_command(None, "fixed", output_limit=16384, timeout=3)


@unittest.skipUnless(sys.platform == "win32", "Read-only native Windows API")
class NativeWindowsAPITests(unittest.TestCase):
    def test_real_fallback_after_synthetic_powershell_timeout_reads_os_without_process(self):
        node = node_registry.Node("windows", "Windows", "windows")
        started = time.monotonic()
        with patch.object(subprocess, "Popen", side_effect=AssertionError("Native fallback starts no process")), \
             patch.object(server.windows_ssh, "local_status", side_effect=subprocess.TimeoutExpired("fixed", 12)):
            result = server.windows_metrics(node, True)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.os, "Windows")
        self.assertGreater(result.memory_total_bytes, 0)
        self.assertLessEqual(result.memory_available_bytes, result.memory_total_bytes)
        self.assertGreaterEqual(result.uptime_seconds, 0)
        self.assertIsNone(result.gpus)
        api = native._api()
        self.assertTrue(all(api.GetDriveTypeW(volume.path) == 3 for volume in result.volumes))


if __name__ == "__main__":
    unittest.main()
