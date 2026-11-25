# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Native execution lifecycle in a disposable account-state directory."""

from argparse import Namespace
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


class ProcessRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-process-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / ("AppData/Vincula-Jobs" if jobs.WINDOWS else ".local/share/vincula-jobs")
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        LOCALAPPDATA=str(self.home / "AppData"))
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        self.workers = []
        self.addCleanup(self.cleanup_workers)
        jobs.operator(Namespace(command="process-access", access="enable"))
        self.start_worker()

    def cleanup_workers(self):
        for worker in self.workers:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)

    def start_worker(self):
        worker = subprocess.Popen([sys.executable, "-IB", str(self.root / "jobs.py"), "worker"], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.workers.append(worker)
        self.wait(lambda: self.has_environment())
        return worker

    def has_environment(self):
        with jobs.transaction() as db:
            return db.execute("SELECT 1 FROM worker_environment").fetchone() is not None

    def wait(self, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail("The disposable native execution did not reach its expected state.")
            time.sleep(0.025)

    def request(self, code="print('hello')", *, job_id=None, argv=None, **fields):
        return jobs.dispatch("process", operation="execute", protocol_version=1,
                             job_id=str(uuid4()) if job_id is None else job_id,
                             argv=argv or [sys.executable, "-c", code], cwd=str(self.home), **fields)

    def read(self, key):
        return jobs.dispatch("process", operation="read", protocol_version=1, job_id=key)

    def completed(self, key, expected="completed"):
        deadline = time.monotonic() + 12
        while True:
            value = self.read(key)
            if value["state"] == expected:
                return value
            if value["state"] not in jobs.ACTIVE or time.monotonic() >= deadline:
                self.fail(json.dumps(value, ensure_ascii=True))
            time.sleep(0.025)

    def full(self):
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")

    @staticmethod
    def alive(pid):
        if not jobs.WINDOWS:
            try:
                return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] != "Z"
            except FileNotFoundError:
                return False
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status))) and status.value == 259
        finally:
            kernel.CloseHandle(handle)

    def long_process(self, **fields):
        marker = self.home / (uuid4().hex + ".json")
        code = ("import json,os,subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); "
                "time.sleep(120)")
        result = self.request(code, **fields)
        self.wait(marker.exists)
        return result, json.loads(marker.read_text())

    def test_full_two_new_parameter_variants_utf8_input_env_cwd_and_native_exit(self):
        self.full()
        for argument in ("é🙂 first", "different second"):
            code = ("import os,sys; sys.stdout.reconfigure(encoding='utf-8'); "
                    "print(sys.argv[1]); print(os.getcwd()); print(os.environ['PROBE']); "
                    "print(sys.stdin.buffer.read().decode('utf-8')); "
                    "print('error log',file=sys.stderr); sys.exit(17)")
            queued = self.request(argv=[sys.executable, "-c", code, argument],
                                  env={"PROBE": "é🙂 value"}, stdin_text="é🙂 stdin\x00EOF")
            result = self.completed(queued["id"])
            self.assertEqual(result["exit_code"], 17)
            self.assertEqual(result["outcome"]["program_exit_code"], 17)
            self.assertTrue(result["outcome"]["started"])
            self.assertEqual(result["outcome"]["reason"], "exit")
            self.assertEqual(result["stdout"]["text"].splitlines(),
                             [argument, str(self.home), "é🙂 value", "é🙂 stdin\x00EOF"])
            self.assertIn("error log", result["stderr"]["text"])
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM programs").fetchone()[0], 0)

    def test_ask_requires_exact_preview_digest_and_legacy_approve_is_insufficient(self):
        marker = self.home / "effect"
        code = f"from pathlib import Path; Path({str(marker)!r}).write_text('done')"
        request = self.request(code)
        self.assertEqual(request["state"], "pending")
        with jobs.transaction() as db:
            preview = [dict(row) for row in jobs.pending_jobs(db)]
            self.assertEqual(json.loads(preview[0]["argv"]), request["manifest"])
            with self.assertRaisesRegex(ValueError, "exact process manifest"):
                jobs.decide(db, request["id"], True)
            db.execute("UPDATE jobs SET state='queued' WHERE id=?", (request["id"],))
        result = self.completed(request["id"], "failed")
        self.assertFalse(result["outcome"]["started"])
        self.assertFalse(marker.exists())
        request = self.request(code)
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
        self.completed(request["id"])
        self.assertTrue(marker.exists())

    def test_stop_terminates_process_tree_and_reports_stop(self):
        self.full()
        queued, pids = self.long_process()
        jobs.rpc("stop", queued["id"])
        result = self.completed(queued["id"], "cancelled")
        self.assertEqual(result["outcome"]["reason"], "stop")
        self.wait(lambda: not any(self.alive(pid) for pid in pids))

    def test_timeout_terminates_process_tree_and_is_distinct_from_program_exit(self):
        self.full()
        queued, pids = self.long_process(timeout_seconds=1)
        result = self.completed(queued["id"], "failed")
        self.assertEqual(result["outcome"]["reason"], "timeout")
        self.assertTrue(result["outcome"]["started"])
        self.wait(lambda: not any(self.alive(pid) for pid in pids))

    def test_confirmed_launch_error_has_no_program_exit_code(self):
        directory = self.home / "disappeared"
        directory.mkdir()
        request = jobs.dispatch("process", operation="execute", protocol_version=1, job_id=str(uuid4()),
                                argv=[sys.executable, "-V"], cwd=str(directory))
        directory.rmdir()
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
        result = self.completed(request["id"], "failed")
        self.assertEqual(result["outcome"]["reason"], "launch_error")
        self.assertFalse(result["outcome"]["started"])
        self.assertIsNone(result["exit_code"])

    def test_long_unicode_cwd_removed_before_approval_is_confirmed_launch_error(self):
        base = Path("\\\\?\\" + str(self.home)) if jobs.WINDOWS else self.home
        start = base / ("🙂" * 40)
        directory = start
        for _ in range(19):
            directory /= "🙂" * 40
        directory.mkdir(parents=True)
        self.assertTrue(directory.is_dir())
        self.assertGreater(len(str(directory).encode("utf-8")), 3000)
        request = jobs.dispatch("process", operation="execute", protocol_version=1, job_id=str(uuid4()),
                                argv=[sys.executable, "-V"], cwd=str(directory))
        shutil.rmtree(start)
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
        result = self.completed(request["id"], "failed")
        self.assertEqual(result["outcome"]["reason"], "launch_error")
        self.assertFalse(result["outcome"]["started"])
        self.assertIsNone(result["exit_code"])
        diagnostic = (self.root / (request["id"] + ".err")).read_text(encoding="utf-8")
        self.assertTrue(diagnostic.strip())
        if not jobs.WINDOWS:
            self.assertIn("Traceback", diagnostic)
            self.assertIn("diagnostic truncated; see stderr", diagnostic)

    def test_disable_between_claim_and_launch_prevents_effects(self):
        marker = self.home / "must-not-start"
        request = self.request(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (request["id"],))
            row = db.execute("SELECT id,program,argv FROM jobs WHERE id=?", (request["id"],)).fetchone()
            jobs.direct_execution.set_enabled(db, False)
        jobs.run_one(row)
        result = self.read(request["id"])
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["outcome"]["started"])
        self.assertFalse(marker.exists())

    def test_late_coordinator_observation_preserves_confirmed_native_exit(self):
        request = self.request("import sys; sys.exit(17)", timeout_seconds=1)
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (request["id"],))
            row = db.execute("SELECT id,program,argv FROM jobs WHERE id=?", (request["id"],)).fetchone()
        with patch.object(jobs, "exited", return_value=False):
            jobs.run_one(row)
        result = self.read(request["id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["exit_code"], 17)
        self.assertEqual(result["outcome"]["reason"], "exit")

    @unittest.skipUnless(sys.platform == "linux", "Large native argv fits Linux OS limits")
    def test_inherited_environment_does_not_consume_the_public_request_bound(self):
        request = self.request(argv=[sys.executable, "-c", "pass"] + ["x" * 1024] * 254)
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (request["id"],))
            spec = jobs.direct_execution.launch_spec(db, request["id"],
                                                     dict(os.environ, NORMAL_INHERITED="x" * 4096))
        self.assertLess(len(jobs.direct_execution.encode(request["manifest"]["original"]).encode()), 262144)
        self.assertGreater(len(jobs.direct_execution.encode(spec).encode()), 262144)
        with jobs.launch_context({"protocol_version": 1, "request": spec}) as (argv, options):
            completed = subprocess.run(argv, **options, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0)

    @unittest.skipUnless(sys.platform == "win32", "Windows ready-file sharing semantics")
    def test_ready_file_open_during_child_exit_does_not_lose_native_result(self):
        original = jobs.read_launch_status
        held = []
        reads = 0

        def observe(path):
            nonlocal reads
            value = original(path)
            if value and value.get("started") is True:
                reads += 1
                if reads == 1:
                    held.append(path.open("rb"))
                else:
                    for stream in held:
                        stream.close()
            return value

        spec = {"protocol_version": 1, "request": {
            "argv": [sys.executable, "-c", "import sys,time; time.sleep(.3); sys.exit(17)"],
            "cwd": str(self.home), "env": dict(os.environ), "stdin_text": None, "timeout_seconds": 5}}
        try:
            with (self.home / "hold.out").open("wb") as out, (self.home / "hold.err").open("wb") as err, \
                    patch.object(jobs, "read_launch_status", side_effect=observe):
                with jobs.program_process(spec, out, err) as process:
                    self.wait(lambda: jobs.exited(process))
            self.assertEqual(process.returncode, 17)
            self.assertGreaterEqual(reads, 2)
        finally:
            for stream in held:
                stream.close()

    def test_legacy_terminal_helper_exit_is_unconfirmed_and_retry_does_not_revive_it(self):
        request = self.request()
        with jobs.transaction() as db:
            db.execute("UPDATE jobs SET state='completed',exit_code=1,finished=1 WHERE id=?", (request["id"],))
        result = self.read(request["id"])
        self.assertEqual(result["state"], "unconfirmed")
        self.assertIsNone(result["exit_code"])
        self.assertIsNone(result["outcome"])
        retry = self.request(job_id=request["id"])
        self.assertEqual(retry, result)

    def test_lost_native_ack_after_effects_reports_unknown_launch_and_cleans_tree(self):
        marker = self.home / "ack-effects.json"
        code = ("import json,os,subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(120)")
        request = self.request(code)
        with jobs.transaction() as db:
            jobs.decide(db, request["id"], True, request["manifest"]["sha256"])
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (request["id"],))
            row = db.execute("SELECT id,program,argv FROM jobs WHERE id=?", (request["id"],)).fetchone()
        wrapper = self.home / "lost-native-ack.py"
        wrapper.write_text(
            "import runpy,sys,time,os,json\nfrom pathlib import Path\n"
            f"module=runpy.run_path({jobs.__file__!r},run_name='native_ack_test')\n"
            "def lose_ack():\n"
            "    deadline=time.monotonic()+5\n"
            f"    while not Path({str(marker)!r}).exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "    raise OSError('synthetic lost native launch acknowledgment')\n"
            "if sys.platform=='win32':\n"
            "    original=module['write_launch_status']\n"
            "    def broken(value,**options):\n"
            "        if value.get('started') is True: lose_ack()\n"
            "        return original(value,**options)\n"
            "    module['windows_launch'].__globals__['write_launch_status']=broken\n"
            "    module['windows_launch'](json.load(sys.stdin))\n"
            "else:\n"
            "    original=os.write\n"
            "    def broken(fd,data):\n"
            "        if data==b'{\"started\":true}\\n': lose_ack()\n"
            "        return original(fd,data)\n"
            "    os.write=broken\n"
            "    module['supervise_linux'](int(sys.argv[2]),int(sys.argv[3]))\n", encoding="utf-8")
        with patch.object(jobs, "__file__", str(wrapper)):
            jobs.run_one(row)
        self.assertTrue(marker.exists())
        pids = json.loads(marker.read_text())
        self.wait(lambda: not any(self.alive(pid) for pid in pids))
        result = self.read(request["id"])
        self.assertEqual(result["state"], "unconfirmed")
        self.assertIsNone(result["outcome"]["started"])
        self.assertIsNone(result["exit_code"])

    def test_launcher_rejects_forged_ack_path_before_program_or_file_effects(self):
        effect = self.home / "forged-program-effect"
        forged = self.home / "outside-ack.json"
        payload = {"protocol_version": 1, "request": {
            "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(effect)!r}).touch()"],
            "cwd": str(self.home), "env": dict(os.environ), "stdin_text": None, "timeout_seconds": 5},
            "status_path": str(forged)}
        with patch.object(jobs.subprocess, "Popen") as create:
            with self.assertRaisesRegex(ValueError, "native launcher protocol"):
                jobs.windows_launch(payload)
            create.assert_not_called()
        self.assertFalse(effect.exists())
        self.assertFalse(forged.exists())
        if jobs.WINDOWS:
            rejected = subprocess.run([sys.executable, "-I", str(self.root / "jobs.py"), "_launch"],
                                      input=json.dumps(payload).encode(), cwd=self.home, env=self.env,
                                      capture_output=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b"Unsupported native launcher protocol", rejected.stderr)
            self.assertFalse(effect.exists())
            self.assertFalse(forged.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows private launcher directory")
    def test_helper_ack_directory_is_parent_owned_and_program_keeps_approved_cwd(self):
        ready_paths = []
        original = jobs.confirm_windows_launch

        def observe(process, status):
            ready_paths.append(status)
            self.assertEqual(status.name, "ready.json")
            self.assertEqual(status.parent.parent, self.root)
            self.assertTrue(status.parent.name.startswith("native-launch-"))
            return original(process, status)

        spec = {"protocol_version": 1, "request": {
            "argv": [sys.executable, "-c", "import os,sys; print(os.getcwd()); sys.exit(17)"],
            "cwd": str(self.home), "env": dict(os.environ), "stdin_text": None, "timeout_seconds": 5}}
        with (self.home / "private.out").open("wb") as out, (self.home / "private.err").open("wb") as err, \
                patch.object(jobs, "confirm_windows_launch", side_effect=observe):
            with jobs.program_process(spec, out, err) as process:
                self.wait(lambda: jobs.exited(process))
        self.assertEqual(process.returncode, 17)
        self.assertEqual((self.home / "private.out").read_text().strip(), str(self.home))
        self.assertEqual(len(ready_paths), 1)
        self.assertFalse(ready_paths[0].parent.exists())

    @unittest.skipUnless(sys.platform == "linux", "Linux SIGKILL parent-death cleanup")
    def test_sigkill_worker_then_restart_cleans_tree_without_reexecution(self):
        self.full()
        queued, pids = self.long_process()
        self.workers[0].kill()
        self.workers[0].wait(timeout=5)
        self.start_worker()
        result = self.completed(queued["id"], "unconfirmed")
        self.assertIsNone(result["exit_code"])
        self.wait(lambda: not any(self.alive(pid) for pid in pids))
        manifest = queued["manifest"]["original"]
        retry = jobs.dispatch("process", operation="execute", protocol_version=1, job_id=queued["id"], **manifest)
        self.assertEqual(retry["state"], "unconfirmed")
        with jobs.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM direct_process_jobs").fetchone()[0], 1)


class LaunchAcknowledgmentEncodingTests(unittest.TestCase):
    def test_error_budget_counts_json_escapes_and_newline_in_encoded_bytes(self):
        for message in ("🙂" * 4000, "é" * 10000, "\"\\\n\x00\ud800" * 4000):
            with self.subTest(kind=repr(message[:8])):
                self.assertGreater(len(json.dumps({"error": message}).encode("utf-8")), 8192)
                packet = jobs.encode_launch_status({"error": message})
                self.assertLessEqual(len(packet), 8192)
                self.assertTrue(packet.endswith(b"\n"))
                result = json.loads(packet)
                self.assertEqual(set(result), {"error"})
                self.assertIn("diagnostic truncated; see stderr", result["error"])
        self.assertEqual(json.loads(jobs.encode_launch_status({"error": "short failure"})),
                         {"error": "short failure"})

    def test_oversize_nonerror_receipt_is_rejected_instead_of_becoming_an_ack(self):
        with self.assertRaisesRegex(ValueError, "byte budget"):
            jobs.encode_launch_status({"started": "x" * 9000})


if __name__ == "__main__":
    unittest.main()
