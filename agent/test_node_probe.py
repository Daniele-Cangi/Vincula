# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from time import monotonic
import unittest
from unittest.mock import patch
from uuid import uuid4

from node_enrollment import EnrollmentConflict
import node_probe
import nodes


class NodeProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "LOCALAPPDATA": str(self.home / "AppData/Local"), "XDG_CONFIG_HOME": str(self.home / ".config")}
        self.config = (self.home / "AppData/Local/Vincula" if sys.platform == "win32"
                       else self.home / ".config/vincula")
        self.root = (self.home / "AppData/Local/Vincula-Jobs" if sys.platform == "win32"
                     else self.home / ".local/share/vincula-jobs")
        self.path = self.config / "core/nodes.json"
        self.local_id, self.peer_id = str(uuid4()), str(uuid4())
        self.family = "windows" if sys.platform == "win32" else "linux"
        nodes.execute({"action": "add", "local": True,
                       "node": {"id": self.local_id, "label": "Origin", "family": self.family}}, self.path)
        self.identity = self.path.parent / "origin.pub"
        self.identity.write_text("public identity selection\n")
        self.pin = self.path.parent / "peer.known_hosts"
        self.pin.write_text("public host key pin\n")
        nodes.execute({"action": "add", "node": {"id": self.peer_id, "label": "Peer", "family": "linux",
                       "aliases": ["peer"], "ssh": {"host": "peer.example", "user": "worker", "port": 22,
                       "identity_file": str(self.identity), "known_hosts_file": str(self.pin), "network": "Test"}}}, self.path)
        self.nonce = "0123456789abcdef" * 2
        self.hashes = node_probe._source_hashes()

    def check(self, node_id=None, **changes):
        arguments = {"node_id": node_id or self.peer_id, "expected_revision": nodes.snapshot(self.path)["revision"]}
        arguments.update(changes)
        return node_probe.check_node(self.path, **arguments)

    def payload(self):
        return {"format": "vincula-node-probe", "version": 1, "nonce": self.nonce,
                "catalogs": [{"source": "core", "state": "observed", "node_id": self.peer_id},
                             {"source": "desktop", "state": "missing", "node_id": None}],
                "helpers": dict(self.hashes), "policy": {"state": "observed", "mode": "full", "native_enabled": True}}

    def wire(self, payload=None):
        return json.dumps(self.payload() if payload is None else payload).encode("utf-8")

    def install_fixture(self, *, database=True):
        self.root.mkdir(parents=True)
        for name in node_probe.HELPERS:
            self.root.joinpath(name).write_bytes(Path(node_probe.__file__).with_name(name).read_bytes())
        if database:
            with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
                db.executescript("CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('ask');"
                                 "CREATE TABLE direct_execution_policy(id INTEGER, enabled INTEGER);"
                                 "INSERT INTO direct_execution_policy VALUES(1,0);")

    def run_source(self, hashes=None, *, selected=None):
        source = node_probe._source(self.nonce, self.hashes if hashes is None else hashes, selected)
        status, raw = node_probe._capture([sys.executable, "-IBS", "-"], source, env=self.env)
        self.assertEqual(status, 0)
        return node_probe._payload(raw, self.nonce, local=selected is not None)

    def test_authenticated_observations_are_bound_to_origin_revision_and_route(self):
        before = nodes.snapshot(self.path)
        with patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", return_value=(0, self.wire())) as capture:
            result = self.check()
        self.assertEqual(result["registry"], before["registry"])
        report = result["node_check"]
        self.assertEqual(report["origin_id"], self.local_id)
        self.assertEqual(report["node_id"], self.peer_id)
        self.assertEqual(report["revision"], before["revision"])
        self.assertRegex(report["route_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["host_pin_sha256"], sha256(self.pin.read_bytes()).hexdigest())
        self.assertGreaterEqual(report["elapsed_seconds"], 0)
        checks = report["checks"]
        self.assertEqual(checks["ssh"], {"state": "authenticated"})
        self.assertEqual(checks["identity"], {"state": "match"})
        self.assertEqual(checks["helper"]["state"], "compatible_on_disk")
        self.assertEqual(checks["policy"], {"state": "observed", "mode": "full", "native_enabled": True})
        self.assertEqual(checks["worker"], {"state": "unverified"})
        self.assertEqual(checks["desktop"], {"state": "unchecked"})
        argv, source = capture.call_args.args
        self.assertEqual(argv[-1], "python3 -IBS -")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertLessEqual(len(source), node_probe.MAX_REQUEST_BYTES)
        self.assertNotIn(str(self.path).encode(), source)

    def test_local_probe_reads_real_fixture_without_claiming_ssh_or_worker_readiness(self):
        self.install_fixture()
        before_db = (self.root / "jobs.sqlite3").read_bytes()
        before_files = set(self.root.iterdir())
        with patch.object(node_probe.node_ssh, "environment", return_value=self.env):
            report = self.check(self.local_id)["node_check"]
        self.assertEqual(report["checks"]["ssh"], {"state": "unchecked", "reason": "local_node"})
        self.assertEqual(report["checks"]["identity"], {"state": "match"})
        self.assertEqual(report["checks"]["policy"], {"state": "observed", "mode": "ask", "native_enabled": False})
        self.assertEqual(report["checks"]["worker"], {"state": "unverified"})
        self.assertEqual((self.root / "jobs.sqlite3").read_bytes(), before_db)
        self.assertEqual(set(self.root.iterdir()), before_files)

    def test_windows_peer_uses_direct_python_stdin_and_never_consolehost(self):
        nodes.execute({"action": "update", "node_id": self.peer_id, "changes": {"family": "windows"}}, self.path)
        with patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", return_value=(0, self.wire())) as capture:
            self.check()
        self.assertEqual(capture.call_args.args[0][-1], "py.exe -3 -IBS -")
        self.assertNotIn("powershell", " ".join(str(value) for value in capture.call_args.args[0]).lower())

    def test_python_probe_never_reads_the_origin_identity_file(self):
        original = Path.open
        original_os_open = os.open

        def no_identity_read(path, *arguments, **keywords):
            if path == self.identity:
                raise AssertionError("Python must not read SSH identity bytes.")
            return original(path, *arguments, **keywords)

        def no_identity_descriptor(path, *arguments, **keywords):
            if Path(path) == self.identity:
                raise AssertionError("Python must not open an SSH identity descriptor.")
            return original_os_open(path, *arguments, **keywords)

        with patch.object(Path, "open", no_identity_read), patch.object(node_probe.os, "open", no_identity_descriptor), \
                patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", return_value=(0, self.wire())):
            self.assertEqual(self.check()["node_check"]["checks"]["ssh"]["state"], "authenticated")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files are POSIX-only")
    def test_fifo_pin_does_not_block_a_snapshot_or_start_ssh(self):
        self.pin.unlink()
        os.mkfifo(self.pin)
        started = monotonic()
        with patch.object(node_probe, "_capture", side_effect=AssertionError("must not connect")):
            report = self.check()["node_check"]
        self.assertEqual(report["checks"]["ssh"], {"state": "unchecked", "reason": "host_pin_unavailable"})
        self.assertLess(monotonic() - started, 1)

    def test_windows_device_and_non_symlink_reparse_files_are_rejected_before_open(self):
        for path in (r"C:\CON", r"C:\NUL.txt", r"\\.\pipe\vincula", r"\\?\GLOBALROOT\Device\x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                node_probe._check_windows_file(PureWindowsPath(path))
        original = Path.lstat

        def reparse(path):
            value = original(path)
            if path == self.pin:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value

        with patch.object(Path, "lstat", reparse):
            with self.assertRaises(ValueError):
                node_probe._check_windows_file(self.pin)

    def test_regular_pin_symlink_is_followed_like_the_configured_ssh_route(self):
        target = self.path.parent / "public pin target"
        target.write_bytes(self.pin.read_bytes())
        self.pin.unlink()
        try:
            self.pin.symlink_to(target)
        except OSError as error:
            if sys.platform == "win32" and getattr(error, "winerror", None) == 1314:
                self.skipTest("The Windows account cannot create symlinks.")
            raise
        # Also exercise the Windows guard on the real link when running POSIX.
        node_probe._check_windows_file(self.pin)
        with patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", return_value=(0, self.wire())):
            report = self.check()["node_check"]
        self.assertEqual(report["checks"]["ssh"]["state"], "authenticated")
        self.assertEqual(report["host_pin_sha256"], sha256(target.read_bytes()).hexdigest())

    def test_disagreeing_or_unreadable_persistent_catalogs_are_ambiguous(self):
        for other in [str(uuid4()), "invalid"]:
            desktop = self.config / "nodes.json"
            if other == "invalid":
                desktop.write_text("not valid JSON")
            else:
                desktop.write_text(json.dumps({"local": other, "nodes": [
                    {"id": other, "label": "Different installation", "family": self.family}]}))
            with self.subTest(other=other), patch.object(node_probe.node_ssh, "environment", return_value=self.env):
                report = self.check(self.local_id)["node_check"]
            self.assertEqual(report["checks"]["identity"]["state"], "ambiguous")

    def test_identity_mismatch_and_unverified_helpers_never_become_readiness(self):
        payload = self.payload()
        payload["catalogs"][0]["node_id"] = str(uuid4())
        payload["helpers"]["jobs.py"] = "0" * 64
        with patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", return_value=(0, self.wire(payload))):
            checks = self.check()["node_check"]["checks"]
        self.assertEqual(checks["ssh"]["state"], "authenticated")
        self.assertEqual(checks["identity"]["state"], "mismatch")
        self.assertEqual(checks["helper"]["state"], "unverified")
        self.assertEqual(checks["policy"], {"state": "unchecked", "reason": "helpers_unverified"})
        self.assertEqual(checks["worker"]["state"], "unverified")

    def test_helper_bytes_are_hashed_without_importing_or_executing_them(self):
        self.install_fixture()
        sentinel = self.home / "must-not-exist"
        source = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\nraise RuntimeError('must not load')\n"
        for name in node_probe.HELPERS:
            self.root.joinpath(name).write_bytes(source.encode("utf-8"))
        hashes = {name: sha256(source.encode()).hexdigest() for name in node_probe.HELPERS}
        value = self.run_source(hashes)
        self.assertEqual(value["helpers"], hashes)
        self.assertEqual(value["policy"]["state"], "observed")
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.root / "__pycache__").exists())

    def test_missing_database_and_helpers_are_never_initialized_or_updated(self):
        self.install_fixture(database=False)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        value = self.run_source()
        self.assertEqual(value["policy"], {"state": "unchecked", "reason": "policy_unavailable"})
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
        self.root.joinpath("jobs.py").unlink()
        value = self.run_source()
        self.assertEqual(value["policy"], {"state": "unchecked", "reason": "helpers_unverified"})
        self.assertFalse((self.root / "jobs.py").exists())
        self.assertFalse((self.root / "jobs.sqlite3").exists())

    def test_invalid_or_busy_policy_is_unchecked_without_changing_it(self):
        self.install_fixture()
        database = self.root / "jobs.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE policy SET mode='invalid'")
        value = self.run_source()
        self.assertEqual(value["policy"], {"state": "unchecked", "reason": "policy_invalid"})
        connection = sqlite3.connect(database)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            value = self.run_source()
            self.assertEqual(value["policy"], {"state": "unchecked", "reason": "policy_unavailable"})
        finally:
            connection.rollback()
            connection.close()
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT mode FROM policy").fetchone()[0], "invalid")

    def test_expensive_policy_query_is_interrupted_within_its_own_budget(self):
        self.install_fixture()
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.executescript("DROP TABLE policy; CREATE VIEW policy AS "
                             "WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM numbers WHERE n<1000000000) "
                             "SELECT 'ask' AS mode FROM numbers ORDER BY n DESC LIMIT 1;")
        started = monotonic()
        value = self.run_source()
        self.assertEqual(value["policy"], {"state": "unchecked", "reason": "policy_unavailable"})
        self.assertLess(monotonic() - started, 3)

    def test_invalid_response_nonce_schema_and_types_are_sanitized(self):
        variants = []
        for key, value in [("nonce", "forged"), ("version", True), ("unexpected", "private path")]:
            payload = self.payload()
            payload[key] = value
            variants.append(self.wire(payload))
        for location, value in [(("policy", "native_enabled"), 1), (("policy", "mode"), "root"),
                                (("helpers", "jobs.py"), True), (("catalogs", 0, "node_id"), "peer"),
                                (("catalogs", 1, "node_id"), self.peer_id)]:
            payload = self.payload()
            cursor = payload
            for part in location[:-1]:
                cursor = cursor[part]
            cursor[location[-1]] = value
            variants.append(self.wire(payload))
        variants.extend([b'{"nonce":"duplicate","nonce":"duplicate"}', b"NaN", b"\xff",
                         b"x" * (node_probe.MAX_RESPONSE_BYTES + 1)])
        for raw in variants:
            with self.subTest(raw=raw[:70]), patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                    patch.object(node_probe, "_capture", return_value=(0, raw)):
                checks = self.check()["node_check"]["checks"]
            self.assertEqual(checks["ssh"], {"state": "failed", "reason": "invalid_response"})
            self.assertEqual(checks["policy"]["state"], "unchecked")

    def test_network_failures_never_return_stderr_commands_or_exception_paths(self):
        secret = "PRIVATE_DIAGNOSTIC /sensitive/operator/path"
        for failure in [OSError(secret), subprocess.CalledProcessError(255, secret, stderr=secret),
                        node_probe._TransportFailure("timeout")]:
            with self.subTest(failure=type(failure).__name__), patch.object(node_probe, "_capture", side_effect=failure):
                report = self.check()["node_check"]
            self.assertEqual(report["checks"]["ssh"]["state"], "failed")
            self.assertNotIn(secret, json.dumps(report))
        with patch.object(node_probe, "_capture", return_value=(255, secret.encode())):
            report = self.check()["node_check"]
        self.assertEqual(report["checks"]["ssh"], {"state": "failed", "reason": "probe_failed"})

    def test_stale_revision_and_noncanonical_selector_prevent_network_access(self):
        revision = nodes.snapshot(self.path)["revision"]
        nodes.execute({"action": "update", "node_id": self.local_id, "changes": {"label": "Changed"}}, self.path)
        with patch.object(node_probe, "_capture", side_effect=AssertionError("must not connect")):
            with self.assertRaises(EnrollmentConflict):
                self.check(expected_revision=revision)
            for bad in ("peer", self.peer_id.upper(), None, True):
                with self.subTest(node_id=bad), self.assertRaises(ValueError):
                    node_probe.check_node(self.path, node_id=bad, expected_revision=revision)
            with self.assertRaises(ValueError):
                self.check(expected_revision=None)

    def test_registry_edit_during_network_is_not_blocked_and_invalidates_result(self):
        finished = threading.Event()
        failures = []

        def edit():
            try:
                nodes.execute({"action": "update", "node_id": self.local_id, "changes": {"label": "Concurrent"}}, self.path)
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        def capture(*arguments, **keywords):
            editor = threading.Thread(target=edit, daemon=True)
            editor.start()
            self.assertTrue(finished.wait(2), "The network phase must not hold the registry mutex.")
            editor.join(timeout=1)
            self.assertEqual(failures, [])
            return 0, self.wire()

        with patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", side_effect=capture):
            with self.assertRaises(EnrollmentConflict):
                self.check()

    def test_pin_rotation_with_unchanged_registry_revision_invalidates_result(self):
        before = nodes.snapshot(self.path)["revision"]

        def capture(*arguments, **keywords):
            self.pin.write_bytes(b"different host pin\n")
            self.assertEqual(nodes.snapshot(self.path)["revision"], before)
            return 0, self.wire()

        with patch.object(node_probe.secrets, "token_hex", return_value=self.nonce), \
                patch.object(node_probe, "_capture", side_effect=capture):
            with self.assertRaises(EnrollmentConflict):
                self.check()

    def test_node_removed_during_network_invalidates_result(self):
        def capture(*arguments, **keywords):
            nodes.execute({"action": "remove", "node_id": self.peer_id}, self.path)
            return 0, self.wire()

        with patch.object(node_probe, "_capture", side_effect=capture):
            with self.assertRaises(EnrollmentConflict):
                self.check()

    def test_legacy_missing_route_identity_and_pin_are_not_network_targets(self):
        with patch.object(node_probe, "_capture", side_effect=AssertionError("must not connect")):
            self.identity.unlink()
            self.assertEqual(self.check()["node_check"]["checks"]["ssh"]["reason"], "local_identity_unavailable")
            self.pin.unlink()
            self.assertEqual(self.check()["node_check"]["checks"]["ssh"]["reason"], "host_pin_unavailable")
            nodes.execute({"action": "update", "node_id": self.peer_id, "changes": {"ssh": None}}, self.path)
            self.assertEqual(self.check()["node_check"]["checks"]["ssh"]["reason"], "explicit_route_missing")
            legacy = "ubuntu" if sys.platform == "win32" else "windows"
            nodes.execute({"action": "update", "node_id": self.peer_id,
                           "changes": {"legacy_route": legacy, "family": "linux" if legacy == "ubuntu" else "windows"}}, self.path)
            self.assertEqual(self.check()["node_check"]["checks"]["ssh"]["reason"], "legacy_route_requires_explicit")

    def test_absent_registry_check_does_not_create_directories_or_lock_files(self):
        missing = self.home / "absent config/nested/nodes.json"
        with self.assertRaises(ValueError):
            node_probe.check_node(missing, node_id=self.peer_id, expected_revision="0" * 64)
        self.assertFalse(missing.parent.exists())


class BoundedCaptureTests(unittest.TestCase):
    def capture(self, code, source=b""):
        return node_probe._capture([sys.executable, "-IBS", "-c", code], source, env=os.environ.copy())

    def test_exact_input_and_output_limits_work_with_a_real_subprocess(self):
        source = b"a" * node_probe.MAX_REQUEST_BYTES
        status, raw = self.capture("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", source)
        self.assertEqual(status, 0)
        self.assertEqual(raw, source)
        with patch.object(node_probe.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(ValueError):
                self.capture("", source + b"a")

    def test_oversized_stdout_and_stderr_are_stopped_and_owned_process_reaped(self):
        original = subprocess.Popen
        for stream, limit, reason in [("stdout", node_probe.MAX_RESPONSE_BYTES, "response_too_large"),
                                      ("stderr", node_probe.MAX_DIAGNOSTICS_BYTES, "diagnostics_too_large")]:
            owned = []

            def spawn(*arguments, **keywords):
                child = original(*arguments, **keywords)
                owned.append(child)
                return child

            with self.subTest(stream=stream), patch.object(node_probe.subprocess, "Popen", side_effect=spawn):
                code = f"import sys,time; sys.{stream}.buffer.write(b'x'*{limit + 1}); sys.{stream}.flush(); time.sleep(5)"
                with self.assertRaises(node_probe._TransportFailure) as caught:
                    self.capture(code)
            self.assertEqual(caught.exception.reason, reason)
            self.assertIsNotNone(owned[0].poll())
            self.assertTrue(owned[0].stdout.closed)
            self.assertTrue(owned[0].stderr.closed)

    def test_timeout_reaps_owned_process_within_the_capture_deadline(self):
        original = subprocess.Popen
        owned = []

        def spawn(*arguments, **keywords):
            child = original(*arguments, **keywords)
            owned.append(child)
            return child

        started = monotonic()
        with patch.object(node_probe, "PROBE_TIMEOUT", 0.4), patch.object(node_probe.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(node_probe._TransportFailure) as caught:
                self.capture("import time; time.sleep(5)")
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(monotonic() - started, 1.2)
        self.assertIsNotNone(owned[0].poll())

    def test_unconfirmed_reap_is_an_explicit_failure_and_never_a_success(self):
        original = subprocess.Popen
        owned = []

        def spawn(*arguments, **keywords):
            child = original(*arguments, **keywords)
            original_wait = child.wait

            def lose_ack(*arguments, **keywords):
                original_wait(*arguments, **keywords)
                raise subprocess.TimeoutExpired("owned probe", 0)

            child.wait = lose_ack
            owned.append(child)
            return child

        with patch.object(node_probe, "PROBE_TIMEOUT", 0.4), patch.object(node_probe.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(node_probe._TransportFailure) as caught:
                self.capture("import time; time.sleep(5)")
        self.assertEqual(caught.exception.reason, "termination_unconfirmed")
        self.assertIsNotNone(owned[0].poll())
        self.assertTrue(owned[0].stdout.closed)
        self.assertTrue(owned[0].stderr.closed)


if __name__ == "__main__":
    unittest.main()
