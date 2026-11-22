# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import ctypes
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

import desktop_supervisor as desktop
import desktop_security


def alive(pid):
    if sys.platform == "win32":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


def wait_until(predicate, timeout=6):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = predicate()
        if result:
            return result
        sleep(0.025)
    raise AssertionError("The isolated desktop lifecycle did not reach its expected state.")


class DesktopSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="vincula-desktop-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = desktop_security.private_directory(Path(self.temporary.name) / "control")
        self.daemons = []
        self.addCleanup(self.cleanup_daemons)

    def start(self, lease=5):
        process = subprocess.Popen([sys.executable, "-S", desktop.__file__, "serve", "--directory", str(self.directory),
                                    "--lease-seconds", str(lease), "--guardian-lease", "0.7"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        self.daemons.append(process)
        reply = json.loads(process.stdout.readline())
        self.assertTrue(reply["ok"], reply)
        return process

    def cleanup_daemons(self):
        for process in reversed(self.daemons):
            if process.poll() is None:
                try:
                    desktop.request(self.directory, {"action": "shutdown"})
                    process.wait(timeout=5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def plan(self, marker="children.json", extra=None):
        code = ("import json,os,subprocess,sys,threading;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']);"
                f"Path({str(self.directory / marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));"
                "print('fake component ready',flush=True);threading.Event().wait()")
        local = str(uuid4())
        commands = [{"name": "input", "argv": [str(Path(sys.executable).resolve()), "-c", code]}]
        if extra:
            commands.append(extra)
        return {"session_id": str(uuid4()), "revision": "a" * 64, "local_id": local,
                "primary": local, "members": [local], "commands": commands}

    def call(self, action, **fields):
        reply = desktop.request(self.directory, {"action": action, **fields})
        self.assertTrue(reply["ok"], reply)
        return reply["session"]

    def state(self):
        return self.call("status")

    def children(self, marker="children.json"):
        path = self.directory / marker

        def read():
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                return None

        return wait_until(read)

    def run_plan(self, plan):
        self.call("prepare", plan=plan)
        self.call("commit", session_id=plan["session_id"])

        def running():
            status = self.state()
            if status["state"] == "running":
                return True
            self.call("heartbeat", session_id=plan["session_id"])
            return False

        wait_until(running)
        return self.children()

    def test_authenticated_stop_works_with_blocked_components_and_slow_clients(self):
        self.start()
        plan = self.plan()
        prepared = self.call("prepare", plan=plan)
        self.assertEqual(prepared["state"], "prepared")
        self.assertFalse((self.directory / "children.json").exists())
        self.call("commit", session_id=plan["session_id"])
        children = self.children()
        wait_until(lambda: self.state()["state"] == "running")
        endpoint = json.loads((self.directory / "endpoint.json").read_text())
        with socket.create_connection(("127.0.0.1", endpoint["port"])) as wrong:
            wrong.sendall(b'{"action":"stop","token":"wrong"}\n')
            self.assertFalse(json.loads(wrong.makefile("rb").readline())["ok"])
        self.assertEqual(self.state()["state"], "running")
        # A stalled request/file-transfer surrogate cannot hold the control lock.
        blocked = [socket.create_connection(("127.0.0.1", endpoint["port"])) for _ in range(3)]
        for stream in blocked:
            stream.sendall(b'{"partial":')
        foreign = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            started = monotonic()
            self.call("stop")
            self.assertLess(monotonic() - started, 0.7)
            wait_until(lambda: self.state()["state"] == "stopped")
            wait_until(lambda: not any(alive(pid) for pid in children))
            self.assertIsNone(foreign.poll(), "Stop must affect only owned components.")
        finally:
            for stream in blocked:
                stream.close()
            foreign.kill()
            foreign.wait(timeout=3)

    def test_expiry_and_heartbeat_are_tied_to_the_exact_session(self):
        self.start(lease=0.8)
        plan = self.plan()
        children = self.run_plan(plan)
        wrong = desktop.request(self.directory, {"action": "heartbeat", "session_id": str(uuid4())})
        self.assertFalse(wrong["ok"])
        for _ in range(3):
            sleep(0.3)
            self.call("heartbeat", session_id=plan["session_id"])
        self.assertEqual(self.state()["state"], "running")
        wait_until(lambda: self.state()["state"] == "expired")
        wait_until(lambda: not any(alive(pid) for pid in children))
        self.assertFalse(desktop.request(self.directory, {"action": "heartbeat", "session_id": plan["session_id"]})["ok"])

    def test_supervisor_crash_eof_cleans_children_and_restart_never_adopts_old_pids(self):
        process = self.start()
        plan = self.plan()
        children = self.run_plan(plan)
        old_endpoint = json.loads((self.directory / "endpoint.json").read_text())
        process.kill()
        process.wait(timeout=3)
        wait_until(lambda: not any(alive(pid) for pid in children))
        foreign = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            previous = json.loads((self.directory / "state.json").read_text())
            previous["components"] = [{"pid": foreign.pid, "guardian_pid": foreign.pid}]
            (self.directory / "state.json").write_text(json.dumps(previous))
            self.start()
            current = self.state()
            self.assertEqual(current["state"], "idle")
            self.assertEqual(current["previous_session"], plan["session_id"])
            self.assertEqual(current["components"], [])
            self.assertNotEqual(old_endpoint["token"], json.loads((self.directory / "endpoint.json").read_text())["token"])
            self.call("stop")
            self.assertIsNone(foreign.poll())
        finally:
            foreign.kill()
            foreign.wait(timeout=3)

    def test_component_failure_rolls_back_other_components_and_keeps_exit_code(self):
        self.start()
        # Wait for the first component's marker before the second exits.
        failure = {"name": "transport", "argv": [str(Path(sys.executable).resolve()), "-c",
            "from pathlib import Path;import time;"
            + f"marker=Path({str(self.directory / 'children.json')!r});"
            + "exec('while not marker.exists(): time.sleep(.01)');raise SystemExit(7)"]}
        plan = self.plan(extra=failure)
        self.call("prepare", plan=plan)
        self.call("commit", session_id=plan["session_id"])
        children = self.children()
        wait_until(lambda: self.state()["state"] == "failed")
        wait_until(lambda: not any(alive(pid) for pid in children))
        result = wait_until(lambda: next((item for item in self.state()["components"]
                                         if item["name"] == "transport" and item["exit_code"] is not None), None))
        self.assertEqual(result["exit_code"], 7)
        history = self.directory / plan["session_id"] / "state.json"
        saved = history.read_bytes()
        self.assertEqual(next(item for item in json.loads(saved)["components"] if item["name"] == "transport")["exit_code"], 7)
        self.call("shutdown")
        self.daemons[-1].wait(timeout=4)
        self.start()
        self.assertEqual(history.read_bytes(), saved)
        self.assertEqual(self.state()["state"], "idle")

    def test_missing_guardian_heartbeats_fail_closed_even_while_control_pipe_is_open(self):
        plan = self.plan()
        spec = self.directory / "guardian.json"
        spec.write_text(json.dumps({"argv": plan["commands"][0]["argv"], "lease_seconds": 0.5,
                                    "heartbeat_token": "b" * 64, "log": str(self.directory / "input.log")}))
        guardian = subprocess.Popen([sys.executable, "-S", str(Path(desktop.__file__).with_name("desktop_process.py")), str(spec)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
        try:
            self.assertEqual(json.loads(guardian.stdout.readline())["event"], "started")
            children = self.children()
            guardian.wait(timeout=4)
            self.assertEqual(guardian.returncode, 0, guardian.stderr.read())
            wait_until(lambda: not any(alive(pid) for pid in children))
            event = json.loads(guardian.stdout.readline())
            self.assertEqual(event["reason"], "guardian_lease_expired")
        finally:
            if guardian.poll() is None:
                guardian.stdin.close()
                guardian.wait(timeout=4)
            if not guardian.stdin.closed:
                guardian.stdin.close()
            guardian.stdout.close()
            guardian.stderr.close()

    def test_second_owner_cannot_replace_the_live_endpoint(self):
        self.start()
        before = (self.directory / "endpoint.json").read_bytes()
        result = subprocess.run([sys.executable, "-S", desktop.__file__, "serve", "--directory", str(self.directory)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual((self.directory / "endpoint.json").read_bytes(), before)

    def test_stop_during_startup_never_leaves_a_late_spawn_running(self):
        self.start()
        plan = self.plan()
        self.call("prepare", plan=plan)
        self.call("commit", session_id=plan["session_id"])
        self.call("stop")
        wait_until(lambda: self.state()["state"] == "stopped")
        for component in self.state()["components"]:
            self.assertFalse(alive(component["guardian_pid"]))
            if component["pid"] is not None:
                self.assertFalse(alive(component["pid"]))
        marker = self.directory / "children.json"
        if marker.exists():
            self.assertFalse(any(alive(pid) for pid in json.loads(marker.read_text())))

    def test_prepared_session_expires_without_ever_starting_commands(self):
        self.start(lease=0.5)
        plan = self.plan()
        self.call("prepare", plan=plan)
        wait_until(lambda: self.state()["state"] == "expired")
        self.assertFalse((self.directory / "children.json").exists())
        self.assertEqual(self.state()["components"], [])
        self.assertFalse(desktop.request(self.directory, {"action": "commit", "session_id": plan["session_id"]})["ok"])

    def test_invalid_previous_identity_is_diagnostic_and_never_adopted(self):
        (self.directory / "state.json").write_text(json.dumps({"session_id": "invalid", "components": [{"pid": os.getpid()}]}))
        self.start()
        current = self.state()
        self.assertEqual(current["state"], "idle")
        self.assertIsNone(current["previous_session"])
        self.assertEqual(current["components"], [])

    def test_commit_and_heartbeat_reject_expiry_at_the_boundary_before_monitor_runs(self):
        for action, state in (("commit", "prepared"), ("heartbeat", "prepared"),
                              ("heartbeat", "starting"), ("heartbeat", "running")):
            for instant in (100.5, 100.501):
                with self.subTest(action=action, state=state, instant=instant):
                    # Disable only the monitor thread and advance a deterministic clock;
                    # no process can start even if the entry point regresses.
                    with patch.object(desktop, "Thread") as thread, patch.object(desktop, "monotonic", return_value=100.0) as clock:
                        supervisor = desktop.Supervisor(self.directory / (action + state + str(instant)), lease_seconds=0.5)
                        try:
                            plan = self.plan()
                            supervisor.prepare(plan)
                            supervisor.state = state
                            clock.return_value = instant
                            with self.assertRaisesRegex(ValueError, "expired"):
                                getattr(supervisor, action)(plan["session_id"])
                            self.assertEqual(supervisor.state, "stopping")
                            self.assertEqual(supervisor.reason, "expired")
                            self.assertTrue(supervisor.stop_event.is_set())
                            self.assertIsNone(supervisor.deadline)
                            self.assertIsNone(supervisor.worker)
                            self.assertEqual(thread.call_count, 1, "No launch worker may be constructed.")
                        finally:
                            supervisor.closed.set()
                            supervisor.heartbeat_socket.close()

    def test_accepted_shutdown_releases_owner_even_when_ack_write_fails(self):
        for failure, token, extra, expected in (
                (BrokenPipeError, "owner", {}, ["stop", "shutdown"]),
                (socket.timeout, "owner", {}, ["stop", "shutdown"]),
                (BrokenPipeError, "wrong", {}, []),
                (BrokenPipeError, "owner", {"unexpected": True}, [])):
            with self.subTest(failure=failure, token=token, extra=extra):
                calls = []
                handler = desktop.Handler.__new__(desktop.Handler)
                handler.request = SimpleNamespace(settimeout=lambda _timeout: None)
                handler.rfile = BytesIO((json.dumps({"action": "shutdown", "token": token, **extra}) + "\n").encode())
                handler.wfile = SimpleNamespace(write=lambda _value: (_ for _ in ()).throw(failure("lost ack")))
                handler.server = SimpleNamespace(token="owner", shutdown=lambda: calls.append("shutdown"),
                    supervisor=SimpleNamespace(stop=lambda: calls.append("stop") or {"state": "stopping"}))
                with patch.object(desktop, "Thread", side_effect=lambda target, daemon: SimpleNamespace(start=target)):
                    handler.handle()
                self.assertEqual(calls, expected)

    def test_expiry_during_commit_or_staging_cannot_start_a_component(self):
        for delayed in ("control", "persist", "staging"):
            with self.subTest(delayed=delayed), patch.object(desktop, "Thread") as thread, patch.object(desktop, "monotonic", return_value=100.0) as clock:
                supervisor = desktop.Supervisor(self.directory / delayed, lease_seconds=0.5)
                try:
                    plan = self.plan()
                    supervisor.prepare(plan)
                    if delayed == "staging":
                        supervisor.state = "starting"
                        original = desktop.atomic_json

                        def slow_stage(path, value):
                            original(path, value)
                            if path.name == "input.json":
                                clock.return_value = 100.6

                        with patch.object(desktop, "atomic_json", side_effect=slow_stage), patch.object(desktop.subprocess, "Popen") as spawn:
                            supervisor.launch()
                            spawn.assert_not_called()
                    else:
                        name = "require_private_control" if delayed == "control" else "persist"
                        original = getattr(supervisor, name)

                        def slow_io():
                            original()
                            clock.return_value = 100.6

                        with patch.object(supervisor, name, side_effect=slow_io):
                            with self.assertRaisesRegex(ValueError, "expired"):
                                supervisor.commit(plan["session_id"])
                    self.assertEqual(supervisor.state, "stopping")
                    self.assertEqual(supervisor.reason, "expired")
                    self.assertTrue(supervisor.stop_event.is_set())
                    self.assertIsNone(supervisor.worker)
                    self.assertEqual(thread.call_count, 1)
                finally:
                    supervisor.closed.set()
                    supervisor.heartbeat_socket.close()

    def test_owner_can_stop_or_shutdown_after_control_permissions_broaden(self):
        for action in ("stop", "shutdown"):
            for target in ("directory", "token"):
                with self.subTest(action=action, target=target):
                    self.directory = desktop_security.private_directory(Path(self.temporary.name) / (action + target))
                    daemon = self.start()
                    children = self.run_plan(self.plan())
                    endpoint = json.loads((self.directory / "endpoint.json").read_text())
                    path = self.directory if target == "directory" else self.directory / "endpoint.json"
                    if sys.platform == "win32":
                        self.broaden_windows_acl(path)
                    else:
                        path.chmod(0o755 if target == "directory" else 0o644)
                    self.assertTrue(desktop.request(self.directory, {"action": action})["ok"])
                    wait_until(lambda: not any(alive(pid) for pid in children))
                    if action == "stop":
                        self.assertTrue(desktop.request(self.directory, {"action": "shutdown"})["ok"])
                    daemon.wait(timeout=4)
                    self.assertEqual(daemon.returncode, 0)
                    self.assertFalse((self.directory / "endpoint.json").exists())

    def broaden_windows_acl(self, path):
        """Test-only fixture mutation, using fixed SID identifiers in temporary storage."""
        from ctypes import wintypes
        security = desktop_security.WindowsSecurity()
        descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        sddl = (f"O:{security.user_sid}D:P(A;OICI;FA;;;{security.user_sid})"
                "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;GR;;;WD)")
        self.assertTrue(security.api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        try:
            self.assertTrue(security.api.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
            self.assertEqual(security.api.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None), 0)
        finally:
            security.kernel.LocalFree(descriptor)

    def windows_acl(self, path):
        from ctypes import wintypes
        security = desktop_security.WindowsSecurity()
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        self.assertEqual(security.api.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                         ctypes.byref(dacl), None, ctypes.byref(descriptor)), 0)
        try:
            security.api.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self.assertTrue(security.api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
            size = ctypes.c_ushort.from_address(dacl.value + 2).value
            return control.value, ctypes.string_at(dacl.value, size)
        finally:
            security.kernel.LocalFree(descriptor)

    @unittest.skipUnless(sys.platform == "win32", "Windows DACL protection")
    def test_windows_creates_protected_control_directory_and_token(self):
        self.directory = Path(self.temporary.name) / "new-protected-control"
        self.start()
        for path in (self.directory, self.directory / "endpoint.json"):
            self.assertTrue(self.windows_acl(path)[0] & 0x1000)
            desktop_security.WindowsSecurity().verify(path)

    @unittest.skipUnless(sys.platform == "win32", "Windows DACL protection")
    def test_windows_rejects_broad_directory_without_repair(self):
        self.broaden_windows_acl(self.directory)
        before = self.windows_acl(self.directory)
        result = subprocess.run([sys.executable, "-S", desktop.__file__, "serve", "--directory", str(self.directory)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe Windows ACL", json.loads(result.stdout)["error"])
        self.assertFalse((self.directory / "endpoint.json").exists())
        self.assertEqual(self.windows_acl(self.directory), before)

    @unittest.skipUnless(sys.platform == "win32", "Windows DACL protection")
    def test_windows_rejects_existing_broad_token_without_repair(self):
        endpoint = self.directory / "endpoint.json"
        endpoint.write_text("{}")
        self.broaden_windows_acl(endpoint)
        before = self.windows_acl(endpoint)
        result = subprocess.run([sys.executable, "-S", desktop.__file__, "serve", "--directory", str(self.directory)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe Windows ACL", json.loads(result.stdout)["error"])
        self.assertEqual(endpoint.read_text(), "{}")
        self.assertEqual(self.windows_acl(endpoint), before)

    @unittest.skipUnless(sys.platform == "win32", "Windows DACL protection")
    def test_windows_refuses_commit_if_token_acl_has_broadened(self):
        self.start()
        plan = self.plan()
        self.call("prepare", plan=plan)
        endpoint_path = self.directory / "endpoint.json"
        endpoint = json.loads(endpoint_path.read_text())
        self.broaden_windows_acl(endpoint_path)
        before = self.windows_acl(endpoint_path)
        with socket.create_connection(("127.0.0.1", endpoint["port"])) as stream:
            stream.sendall((json.dumps({"action": "commit", "session_id": plan["session_id"], "token": endpoint["token"]}) + "\n").encode())
            with stream.makefile("rb") as reader:
                reply = json.loads(reader.readline())
        self.assertFalse(reply["ok"])
        self.assertIn("Unsafe Windows ACL", reply["error"])
        self.assertFalse((self.directory / "children.json").exists())
        self.assertEqual(self.windows_acl(endpoint_path), before)


if __name__ == "__main__":
    unittest.main()
