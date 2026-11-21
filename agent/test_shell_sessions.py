# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from argparse import Namespace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import jobs


class ShellTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        jobs.operator(Namespace(command="register", name="version", argv=[sys.executable, "--version"]))
        self.session = str(uuid4())
        jobs.shell_rpc("open", self.session)

    def test_ask_then_close_and_resume_history(self):
        first = str(uuid4())
        self.assertEqual(jobs.shell_rpc("send", self.session, first, "version")["state"], "pending")
        with self.assertRaisesRegex(ValueError, "current command"):
            jobs.shell_rpc("send", self.session, str(uuid4()), "version")
        with jobs.transaction() as db:
            jobs.decide(db, first, True)
        self.assertEqual(jobs.shell_rpc("read", self.session, first)["output"]["state"], "queued")
        closed = jobs.shell_rpc("close", self.session)
        self.assertEqual(closed["commands"][0]["job"]["state"], "cancelled")
        self.assertEqual(jobs.shell_rpc("open", self.session), closed)
        self.assertEqual(jobs.shell_rpc("send", self.session, first, "version")["id"], first)
        with self.assertRaisesRegex(ValueError, "closed"):
            jobs.shell_rpc("send", self.session, str(uuid4()), "version")

    def test_full_allows_only_registered_names_and_enforces_session_membership(self):
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")
        with self.assertRaisesRegex(ValueError, "single registered"):
            jobs.shell_rpc("send", self.session, str(uuid4()), "version && echo extra")
        with self.assertRaisesRegex(ValueError, "not authorized"):
            jobs.shell_rpc("send", self.session, str(uuid4()), "unknown")
        foreign = str(uuid4())
        jobs.rpc("start", foreign, "version")
        with self.assertRaisesRegex(ValueError, "outside this session"):
            jobs.shell_rpc("send", self.session, foreign, "version")
        with self.assertRaisesRegex(ValueError, "Unknown command"):
            jobs.shell_rpc("read", self.session, foreign)
        self.assertEqual(jobs.shell_rpc("send", self.session, str(uuid4()), "version")["state"], "queued")

    def test_discovery_uses_worker_path_and_paginates(self):
        folder = self.root / "bin"
        folder.mkdir()
        names = ["vincula-a.exe", "vincula-b.exe"]
        for name in names:
            file = folder / name
            file.touch()
            file.chmod(0o700)
        with jobs.transaction() as db:
            db.execute("INSERT INTO worker_environment VALUES(1,?,?)", (str(folder), ".EXE"))
        first = jobs.discover_tools("VINCULA", limit=1)
        self.assertEqual(first["source"], "worker")
        self.assertEqual(first["commands"], names[:1])
        second = jobs.discover_tools("vincula", first["next_offset"], 1)
        self.assertEqual(second["commands"], names[1:])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(jobs.rpc("list")["programs"], ["version"])

    def test_empty_pathext_entry_does_not_advertise_extensionless_files(self):
        (self.root / "tool.exe").touch()
        (self.root / "notes").touch()
        with patch.object(jobs, "WINDOWS", True):
            self.assertEqual(jobs.executable_names(str(self.root), ".EXE; "), {"tool.exe"})


if __name__ == "__main__":
    unittest.main()
