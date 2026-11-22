# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from argparse import Namespace
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import warnings
from unittest.mock import patch
from uuid import uuid4

import jobs


class FileAccessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows reserved file names")
    def test_windows_reserved_names_without_deprecation_warnings(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            for name in ("CON", "NUL.txt", "sub/COM1", "LPT9"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    jobs.files.components(name)
            self.assertEqual(jobs.files.components("sub/report.txt"), ["sub", "report.txt"])

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        parent = Path(temp.name).resolve()
        self.root = parent / "state"
        self.share = parent / "share"
        self.share.mkdir()
        (self.share / "message.txt").write_text("hello", encoding="utf-8")
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()
        jobs.operator(Namespace(command="files-grant", alias="shared", directory=str(self.share), write=True))

    def request(self, operation, path, uid=None):
        return jobs.dispatch("files", operation="request", request_id=uid or str(uuid4()),
                             operation_kind=operation, root="shared", relative=path)

    def full(self):
        with jobs.transaction() as db:
            jobs.set_mode(db, "full")

    def test_scoped_approval_and_snapshot(self):
        key = str(uuid4())
        pending = self.request("read", "message.txt", key)
        self.assertEqual(pending["state"], "pending")
        self.assertIsNone(pending["result"])
        with jobs.transaction() as db:
            jobs.files.decide(db, key, True)
        result = self.request("read", "message.txt", key)
        self.assertEqual(result["result"]["text"], "hello")
        (self.share / "message.txt").write_text("changed")
        self.assertEqual(self.request("read", "message.txt", key), result)
        jobs.operator(Namespace(command="files-revoke", alias="shared"))
        self.assertEqual(jobs.dispatch("files", operation="status", request_id=key)["state"], "denied")

    def test_traversal_symlink_and_readonly_folders(self):
        self.full()
        for name in ("../outside", "/abs", "a/../../b", "C:/file", "sub\\file"):
            with self.assertRaises(ValueError):
                self.request("read", name)
        remote = self.share / "jump"
        try:
            remote.symlink_to(self.share / "message.txt")
        except OSError:
            pass  # Creating a symlink requires additional privileges on some Windows hosts.
        else:
            with self.assertRaisesRegex(ValueError, "Symbolic links"):
                self.request("read", "jump")
        jobs.operator(Namespace(command="files-grant", alias="shared", directory=str(self.share), write=False))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            self.request("import", "new.txt")

    def test_transfer_checksum_and_atomic_non_overwrite(self):
        self.full()
        receipt = {"size": 5, "sha256": sha256(b"hello").hexdigest()}
        key = self.request("import", "new.txt")["id"]
        payload = Path(jobs.dispatch("files", operation="stage", request_id=key)["path"])
        payload.write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "checksum"):
            jobs.dispatch("files", operation="commit", request_id=key, expected=receipt)
        self.assertFalse((self.share / "new.txt").exists())
        payload.write_bytes(b"hello")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with jobs.transaction() as db:
                jobs.files.commit(db, self.root, key, receipt)
                raise RuntimeError("interrupted publication response")
        self.assertEqual(jobs.dispatch("files", operation="commit", request_id=key, expected=receipt), receipt)
        jobs.dispatch("files", operation="cleanup", request_id=key)
        self.assertEqual(jobs.dispatch("files", operation="commit", request_id=key, expected=receipt), receipt)
        self.assertEqual((self.share / "new.txt").read_bytes(), b"hello")
        duplicate = self.request("import", "new.txt")["id"]
        with self.assertRaisesRegex(ValueError, "already exists"):
            jobs.dispatch("files", operation="commit", request_id=duplicate, expected=receipt)

    def test_read_pages_keep_utf8_characters_intact(self):
        self.full()
        text = "a" * (jobs.files.READ_SIZE - 1) + "éx"
        (self.share / "message.txt").write_text(text, encoding="utf-8")
        first = self.request("read", "message.txt")["result"]
        second = jobs.dispatch("files", operation="request", request_id=str(uuid4()),
                               operation_kind="read", root="shared", relative="message.txt",
                               offset=first["next_offset"])["result"]
        self.assertEqual(first["text"] + second["text"], text)

    def test_switch_to_ask_freezes_unconsumed_automatic_approval(self):
        self.full()
        key = self.request("export", "message.txt")["id"]
        with jobs.transaction() as db:
            jobs.set_mode(db, "ask")
        with self.assertRaisesRegex(ValueError, "needs authorization"):
            jobs.dispatch("files", operation="stage", request_id=key)

    def test_switch_to_ask_does_not_reopen_completed_export(self):
        self.full()
        key = self.request("export", "message.txt")["id"]
        jobs.dispatch("files", operation="stage", request_id=key)
        jobs.dispatch("files", operation="cleanup", request_id=key)
        with jobs.transaction() as db:
            jobs.set_mode(db, "ask")
            self.assertEqual(jobs.files.permission_items(db), [])
        self.assertEqual(jobs.dispatch("files", operation="status", request_id=key)["state"], "completed")


if __name__ == "__main__":
    unittest.main()
