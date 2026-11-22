# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise Linux worker supervision in a private HOME with actual processes."""

from argparse import Namespace
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


@unittest.skipUnless(sys.platform == "linux", "Linux worker parent-death supervision")
class WorkerCrashTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / ".local/share/vincula-jobs"
        self.env = dict(os.environ, HOME=str(self.home))
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        self.workers = []
        self.addCleanup(self.stop_workers)
        self.fixture = self.home / "fixture.py"
        self.pids = self.home / "pids.json"
        self.fixture.write_text(
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],\n"
            "                         start_new_session=(sys.argv[3] == 'yes'))\n"
            "print('fixture-start', flush=True)\n"
            "print('fixture-stderr', file=sys.stderr, flush=True)\n"
            "marker = Path(sys.argv[1])\n"
            "stage = marker.with_suffix('.tmp')\n"
            "stage.write_text(json.dumps([os.getpid(), child.pid]))\n"
            "stage.replace(marker)\n"
            "time.sleep(float(sys.argv[2]))\n"
            "sys.exit(7)\n"
        )
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")

    def stop_workers(self):
        for process in self.workers:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def start_worker(self):
        process = subprocess.Popen(
            [sys.executable, "-I", str(self.root / "jobs.py"), "worker"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.workers.append(process)
        return process

    def register(self, *argv):
        jobs.operator(Namespace(command="register", name="fixture", argv=list(argv)))
        job_id = str(uuid4())
        jobs.rpc("start", job_id, "fixture")
        return job_id

    def start_fixture(self, seconds=120, detached_child=False):
        key = self.register(sys.executable, str(self.fixture), str(self.pids), str(seconds),
                            "yes" if detached_child else "no")
        worker = self.start_worker()
        self.wait_until(self.pids.exists)
        self.children = json.loads(self.pids.read_text())
        return key, worker

    def wait_until(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("The isolated worker did not reach the expected state.")
            time.sleep(0.025)

    @staticmethod
    def alive(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def assert_children_stopped(self):
        self.wait_until(lambda: not any(self.alive(pid) for pid in self.children))

    def test_normal_exit_preserves_code_and_stops_remaining_children(self):
        key, _ = self.start_fixture(seconds=0.2)
        self.wait_until(lambda: jobs.rpc("read", key)["state"] == "completed")
        self.assertEqual(jobs.rpc("read", key)["exit_code"], 7)
        self.assertIn("fixture-stderr", jobs.rpc("read", key)["stderr"]["text"])
        self.assert_children_stopped()

    def test_stop_preserves_cancelled_state_and_stops_children(self):
        key, _ = self.start_fixture()
        jobs.rpc("stop", key)
        self.wait_until(lambda: jobs.rpc("read", key)["state"] == "cancelled")
        self.assertIsNotNone(jobs.rpc("read", key)["exit_code"])
        self.assert_children_stopped()

    def test_program_signal_exit_is_preserved(self):
        key = self.register(sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.start_worker()
        self.wait_until(lambda: jobs.rpc("read", key)["state"] == "completed")
        self.assertEqual(jobs.rpc("read", key)["exit_code"], -signal.SIGTERM)

    def test_launch_failure_is_failed_and_has_no_exit_code(self):
        executable = self.home / "removed-executable"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        key = self.register(str(executable))
        executable.unlink()
        self.start_worker()
        self.wait_until(lambda: jobs.rpc("read", key)["state"] == "failed")
        result = jobs.rpc("read", key)
        self.assertIsNone(result["exit_code"])
        self.assertIn("removed-executable", result["stderr"]["text"])

    def test_sigkill_then_restart_reconciles_without_relaunching(self):
        key, worker = self.start_fixture()
        initial = jobs.rpc("read", key)
        worker.kill()
        worker.wait(timeout=5)
        deadline = time.monotonic() + 8
        restarted = self.start_worker()
        while jobs.rpc("read", key)["state"] != "unconfirmed":
            if restarted.poll() is not None:
                restarted = self.start_worker()
            if time.monotonic() >= deadline:
                self.fail("Restart could not acquire the worker lock after crash cleanup.")
            time.sleep(0.025)
        self.assertFalse(any(self.alive(pid) for pid in self.children))
        result = jobs.rpc("start", key, "fixture")
        self.assertEqual(result["state"], "unconfirmed")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["created"], initial["created"])
        self.assertEqual(jobs.rpc("read", key)["stdout"]["text"].count("fixture-start"), 1)

    def test_supervisor_keeps_the_worker_lock_during_program_execution(self):
        import fcntl

        with ExitStack() as stack:
            ownership = stack.enter_context(jobs.exclusive_worker())
            output = stack.enter_context((self.home / "out").open("wb"))
            errors = stack.enter_context((self.home / "err").open("wb"))
            stack.enter_context(jobs.program_process(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                output, errors, ownership.fileno(),
            ))
            ownership.close()
            with (self.root / "worker.lock").open("a+b") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (self.root / "worker.lock").open("a+b") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_detached_descendant_is_adopted_and_stopped_after_worker_crash(self):
        _, worker = self.start_fixture(detached_child=True)
        worker.kill()
        worker.wait(timeout=5)
        self.assert_children_stopped()

    def test_supervisor_death_before_ack_is_unconfirmed(self):
        helper = self.home / "broken-supervisor.py"
        helper.write_text("raise SystemExit(73)\n")
        key = self.register(sys.executable, "--version")
        with jobs.transaction() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
            row = db.execute("SELECT id,program,argv FROM jobs WHERE id=?", (key,)).fetchone()
        with patch.object(jobs, "__file__", str(helper)):
            jobs.run_one(row)
        result = jobs.rpc("read", key)
        self.assertEqual(result["state"], "unconfirmed")
        self.assertIsNone(result["exit_code"])
        self.assertIn("before confirming", result["stderr"]["text"])

    def test_worker_death_before_supervisor_initialization_does_not_launch(self):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as ready:
            process = subprocess.Popen(
                [sys.executable, "-I", str(self.root / "jobs.py"), "_supervise",
                 str(os.getpid() + 1), str(writer)],
                env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(writer,),
            )
            os.close(writer)
            _, errors = process.communicate(json.dumps([
                sys.executable, str(self.fixture), str(self.pids), "120",
            ]).encode(), timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"worker exited before supervision", errors)
            self.assertEqual(ready.read(), b"")
            self.assertFalse(self.pids.exists())

    def test_supervisor_failure_after_launch_is_unconfirmed_and_stops_children(self):
        helper = self.home / "failing-supervisor.py"
        helper.write_text(
            "import runpy, sys, time\nfrom pathlib import Path\n"
            f"module = runpy.run_path({jobs.__file__!r}, run_name='supervisor_test')\n"
            "def fail_after_start(proc):\n"
            f"    while not Path({str(self.pids)!r}).exists(): time.sleep(0.025)\n"
            "    raise RuntimeError('supervisor fixture failure')\n"
            "module['supervise_linux'].__globals__['exited'] = fail_after_start\n"
            "module['supervise_linux'](int(sys.argv[2]), int(sys.argv[3]))\n"
        )
        key = self.register(sys.executable, str(self.fixture), str(self.pids), "120", "no")
        with jobs.transaction() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
            row = db.execute("SELECT id,program,argv FROM jobs WHERE id=?", (key,)).fetchone()
        with patch.object(jobs, "__file__", str(helper)):
            jobs.run_one(row)
        self.children = json.loads(self.pids.read_text())
        self.assert_children_stopped()
        result = jobs.rpc("read", key)
        self.assertEqual(result["state"], "unconfirmed")
        self.assertIsNone(result["exit_code"])
        self.assertIn("could not confirm", result["stderr"]["text"])

    def test_lost_or_malformed_ack_after_effects_is_unconfirmed(self):
        for response in (None, b"malformed\n", b"[]\n"):
            with self.subTest(response=response):
                self.pids.unlink(missing_ok=True)
                helper = self.home / "handoff-failure.py"
                helper.write_text(
                    "import runpy, sys, time, os\nfrom pathlib import Path\n"
                    f"module = runpy.run_path({jobs.__file__!r}, run_name='supervisor_test')\n"
                    "original = os.write\n"
                    "def fail_handoff(fd, data):\n"
                    "    if data == b'{\"started\":true}\\n':\n"
                    f"        while not Path({str(self.pids)!r}).exists(): time.sleep(0.01)\n"
                    f"        response = {response!r}\n"
                    "        if response is None: raise OSError('fixture handoff lost after launch')\n"
                    "        return original(fd, response)\n"
                    "    return original(fd, data)\n"
                    "os.write = fail_handoff\n"
                    "module['supervise_linux'](int(sys.argv[2]), int(sys.argv[3]))\n"
                )
                key = self.register(sys.executable, str(self.fixture), str(self.pids), "120", "no")
                with jobs.transaction() as db:
                    db.execute("UPDATE jobs SET state='running' WHERE id=?", (key,))
                    row = db.execute("SELECT id,program,argv FROM jobs WHERE id=?", (key,)).fetchone()
                with patch.object(jobs, "__file__", str(helper)):
                    jobs.run_one(row)
                self.children = json.loads(self.pids.read_text())
                self.assert_children_stopped()
                result = jobs.rpc("read", key)
                self.assertEqual(result["state"], "unconfirmed")
                self.assertIsNone(result["exit_code"])
                self.assertIn("fixture-start", result["stdout"]["text"])
                self.assertEqual(jobs.rpc("start", key, "fixture")["state"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
