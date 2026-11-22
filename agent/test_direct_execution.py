# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import direct_execution as direct


class DirectExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('ask');
            CREATE TABLE jobs(id TEXT PRIMARY KEY,program TEXT,argv TEXT,state TEXT,exit_code INTEGER,
                              created REAL,finished REAL);
            CREATE TABLE worker_environment(id INTEGER PRIMARY KEY,path TEXT,pathext TEXT);
        """)
        self.db.executescript(direct.SCHEMA)
        self.db.execute("INSERT INTO worker_environment VALUES(1,?,?)",
                        (str(Path(sys.executable).parent), ".EXE;.COM;.BAT;.CMD"))
        self.argv = [sys.executable, "-c", "print('one')"]

    def queue(self, job_id=None, access="ask", **kwargs):
        key = str(uuid4()) if job_id is None else job_id
        value = direct.queue(self.db, access, key, kwargs.pop("argv", self.argv), home=self.home, **kwargs)
        return key, value

    def state(self, key):
        return self.db.execute("SELECT state FROM jobs WHERE id=?", (key,)).fetchone()[0]

    def running(self, key):
        self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))

    def test_existing_installation_opt_in_stays_disabled_across_initialization(self):
        self.assertFalse(direct.support(self.db)["enabled"])
        with self.assertRaisesRegex(ValueError, "explicitly enable"):
            self.queue(access="full")
        direct.set_enabled(self.db, True)
        self.db.executescript(direct.SCHEMA)
        self.assertTrue(direct.support(self.db)["enabled"])
        self.assertFalse(direct.support(self.db)["interactive"])

    def test_ask_preview_and_approval_are_bound_to_exact_immutable_manifest(self):
        direct.set_enabled(self.db, True)
        key, manifest = self.queue(cwd=str(self.home), env={"VINCULA_PROBE": "Ω"}, stdin_text="hello\n")
        self.assertEqual(self.state(key), "pending")
        self.assertEqual(direct.read(self.db, key), manifest)
        self.assertEqual(manifest["sha256"], direct.digest(manifest))
        with self.assertRaisesRegex(ValueError, "exact process manifest"):
            direct.approve(self.db, key, "0" * 64)
        self.running(key)  # A legacy approve/worker claim cannot authorize native direct execution.
        with self.assertRaisesRegex(ValueError, "no matching destination authorization"):
            direct.launch_spec(self.db, key, {})
        self.db.execute("UPDATE jobs SET state='pending' WHERE id=?", (key,))
        direct.approve(self.db, key, manifest["sha256"])
        self.running(key)
        spec = direct.launch_spec(self.db, key, {"BASE": "inherited"})
        self.assertEqual(spec["env"], {"BASE": "inherited", "VINCULA_PROBE": "Ω"})
        self.assertEqual(spec["stdin_text"], "hello\n")

    def test_full_queues_autonomously_and_mode_downgrade_requires_fresh_approval(self):
        direct.set_enabled(self.db, True)
        self.db.execute("UPDATE policy SET mode='full'")
        key, manifest = self.queue(access="full")
        self.assertEqual(self.state(key), "queued")
        self.running(key)
        direct.launch_spec(self.db, key, {})
        self.db.execute("UPDATE policy SET mode='ask'")
        with self.assertRaisesRegex(ValueError, "no matching destination authorization"):
            direct.launch_spec(self.db, key, {})
        self.db.execute("UPDATE jobs SET state='pending' WHERE id=?", (key,))
        direct.approve(self.db, key, manifest["sha256"])
        self.running(key)
        direct.launch_spec(self.db, key, {})

    def test_one_native_api_executes_new_variants_without_any_program_registration(self):
        direct.set_enabled(self.db, True)
        self.db.execute("UPDATE policy SET mode='full'")
        for expected in ("first", "second Ω"):
            key, manifest = self.queue(access="full", argv=[sys.executable, "-c",
                "import os,sys; sys.stdout.reconfigure(encoding='utf-8'); print(sys.argv[1]); print(os.getcwd()); print(os.environ['PROBE']); "
                "print(sys.stdin.buffer.read().decode('utf-8')); sys.exit(7)", expected],
                cwd=str(self.home), env={"PROBE": "custom"}, stdin_text="input Ω")
            self.running(key)
            spec = direct.launch_spec(self.db, key, os.environ)
            result = subprocess.run(spec["argv"], cwd=spec["cwd"], env=spec["env"],
                                    input=spec["stdin_text"].encode("utf-8"), capture_output=True,
                                    timeout=spec["timeout_seconds"])
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout.decode("utf-8").splitlines(),
                             [expected, str(self.home), "custom", "input Ω"])

    def test_identical_retry_uses_saved_resolution_even_after_path_or_policy_changes(self):
        direct.set_enabled(self.db, True)
        bare = Path(sys.executable).name
        key, original = self.queue(argv=[bare, "-V"])
        self.db.execute("UPDATE worker_environment SET path='' WHERE id=1")
        direct.set_enabled(self.db, False)
        replay_key, replay = self.queue(job_id=key, argv=[bare, "-V"])
        self.assertEqual((replay_key, replay), (key, original))
        self.assertEqual(self.state(key), "cancelled")
        for changed in ({"argv": [bare, "-c", "print(2)"]}, {"argv": [bare, "-V"], "cwd": str(self.home)},
                        {"argv": [bare, "-V"], "env": {}}, {"argv": [bare, "-V"], "stdin_text": ""},
                        {"argv": [bare, "-V"], "timeout_seconds": 1}):
            with self.assertRaisesRegex(ValueError, "different process request"):
                self.queue(job_id=key, **changed)

    def test_disable_cancels_only_pending_and_queued_and_blocks_running_launch_race(self):
        direct.set_enabled(self.db, True)
        pending, _ = self.queue()
        queued, _ = self.queue(access="full")
        running, _ = self.queue(access="full")
        self.running(running)
        self.db.execute("INSERT INTO jobs VALUES('legacy','registered','[]','queued',NULL,0,NULL)")
        direct.set_enabled(self.db, False)
        self.assertEqual([self.state(key) for key in (pending, queued, running, "legacy")],
                         ["cancelled", "cancelled", "running", "queued"])
        with self.assertRaisesRegex(ValueError, "enabled destination worker"):
            direct.launch_spec(self.db, running, {})
        with self.assertRaisesRegex(ValueError, "disabled"):
            direct.approve(self.db, pending, direct.read(self.db, pending)["sha256"])

    def test_worker_metadata_is_required_and_relative_executable_and_cwd_are_rejected(self):
        direct.set_enabled(self.db, True)
        for values in ({"argv": ["./python"]}, {"cwd": "relative"}, {"cwd": str(self.home / "absent")}):
            with self.assertRaises(ValueError):
                self.queue(**values)
        self.db.execute("DELETE FROM worker_environment")
        with self.assertRaisesRegex(ValueError, "Start the destination worker"):
            self.queue()

    def test_request_validates_utf8_limits_timeout_and_env_without_running_anything(self):
        invalid = [([], {}), ([True], {}), (["x\x00y"], {}), (["x"], {"env": {"a=b": "x"}}),
                   (["x"], {"env": {"x": 1}}), (["x"], {"stdin_text": "Ω" * 32769}),
                   (["x"], {"stdin_text": "\ud800"}), (["x"], {"timeout_seconds": True}),
                   (["x"], {"timeout_seconds": 0}), (["x"], {"timeout_seconds": 3601}),
                   (["x", "a" * direct.MAX_REQUEST_BYTES], {})]
        for argv, values in invalid:
            with self.subTest(argv=str(argv)[:32], values=str(values)[:32]), self.assertRaises(ValueError):
                direct.request(argv, **values)
        self.assertEqual(direct.request(["x"], stdin_text="a\x00b")["stdin_text"], "a\x00b")
        self.assertEqual(len(direct.request(["x"], stdin_text="Ω" * 32768)["stdin_text"].encode("utf-8")), 65536)
        with patch.object(direct, "WINDOWS", True), self.assertRaisesRegex(ValueError, "unique"):
            direct.request(["x"], env={"Path": "one", "PATH": "two"})

    def test_manifest_corruption_marker_collision_and_unapproved_launch_fail_closed(self):
        direct.set_enabled(self.db, True)
        key, manifest = self.queue()
        with self.assertRaisesRegex(ValueError, "approved process"):
            direct.launch_spec(self.db, key, {})
        self.db.execute("UPDATE jobs SET argv='[]' WHERE id=?", (key,))
        with self.assertRaisesRegex(ValueError, "marker"):
            direct.read(self.db, key)
        self.db.execute("UPDATE jobs SET argv=? WHERE id=?", (direct.LEGACY_ARGV, key))
        manifest["resolved"]["argv"].append("unapproved")
        self.db.execute("UPDATE direct_process_jobs SET manifest=? WHERE id=?", (direct.encode(manifest), key))
        with self.assertRaisesRegex(ValueError, "immutable process manifest"):
            direct.read(self.db, key)
        other = str(uuid4())
        self.db.execute("INSERT INTO jobs VALUES(?,'legacy','[]','pending',NULL,0,NULL)", (other,))
        with self.assertRaisesRegex(ValueError, "outside direct execution"):
            self.queue(job_id=other)

    def test_legacy_marker_is_not_an_executable_argv(self):
        direct.set_enabled(self.db, True)
        key, _ = self.queue()
        row = self.db.execute("SELECT program,argv FROM jobs WHERE id=?", (key,)).fetchone()
        self.assertEqual(row[0], "@process-v1")
        with self.assertRaises((TypeError, ValueError)):
            subprocess.Popen(json.loads(row[1]))

    def test_windows_batch_paths_require_an_explicit_interpreter(self):
        with patch.object(direct, "WINDOWS", True):
            for suffix in (".bat", ".cmd"):
                path = self.home / ("script" + suffix)
                path.write_text("@echo synthetic\n")
                with self.assertRaisesRegex(ValueError, "explicit cmd.exe"):
                    direct.executable(str(path), str(self.home), ".EXE;.CMD;.BAT")

    def test_windows_bare_versioned_name_uses_saved_pathext(self):
        path = self.home / "app.v2.exe"
        path.write_bytes(b"synthetic")
        with patch.object(direct, "WINDOWS", True):
            self.assertEqual(direct.executable("app.v2", str(self.home), ".exe;.com"), str(path.resolve()))

    def test_resolved_paths_do_not_consume_the_original_request_size_bound(self):
        direct.set_enabled(self.db, True)
        bare = Path(sys.executable).name
        argv = [bare, "-c", "pass", ""]
        initial = direct.request(argv)
        argv[-1] = "x" * (direct.MAX_REQUEST_BYTES - len(direct.encode(initial).encode()))
        key, manifest = self.queue(argv=argv)
        self.assertEqual(len(direct.encode(manifest["original"]).encode()), direct.MAX_REQUEST_BYTES)
        self.assertGreater(len(direct.encode(manifest["resolved"]).encode()), direct.MAX_REQUEST_BYTES)
        self.assertEqual(direct.read(self.db, key), manifest)


if __name__ == "__main__":
    unittest.main()
