# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover fsynced blocks after a lost SQL checkpoint; TEMP-only real workers."""

import sys
import unittest
from unittest.mock import patch

import jobs
import test_large_files_runtime as runtime_tests


@unittest.skipUnless(sys.platform in {"linux", "win32"}, "Qualified native worker platforms")
class LargeFileCheckpointTests(unittest.TestCase):
    def setUp(self):
        # Composition avoids collecting all of the imported fixture's tests a
        # second time. Transfer synchronous cleanups to this real TestCase so
        # cleanup failures are reported here and original workers are drained.
        fixture = runtime_tests.LargeFilesRuntimeTests("runTest")
        self.fixture = fixture
        try:
            fixture.setUp()
        finally:
            self._cleanups.extend(fixture._cleanups)
            fixture._cleanups.clear()
        fixture.start_worker()

    def received_without_checkpoint(self):
        fixture = self.fixture
        exported, imported, size, digest = fixture.request_pair(size=8193)
        reply, data = fixture.rpc("block-read", exported, index=0)
        block = reply["block"]
        with patch.object(jobs.transfer_state, "checkpoint", side_effect=RuntimeError("Synthetic lost SQL checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "lost SQL checkpoint"):
                fixture.rpc("block-write", imported, index=0, expected=block, payload=data)
        self.assert_missing(imported)
        path = jobs.large_files.base(jobs.LOCAL_RUNTIME) / "imports" / imported["id"] / "block-00000000"
        self.assertEqual(path.stat().st_size, size)
        self.assertTrue(jobs.transfer_storage.verify_stored_block(jobs.large_files.base(jobs.LOCAL_RUNTIME),
            imported["id"], exported["header"], 0, block))
        return exported, imported, size, digest, block, path

    def assert_missing(self, imported):
        with jobs.transaction() as db:
            self.assertIsNone(jobs.transfer_state.get_block(db, imported["id"], 0))
            value = jobs.transfer_state.status(db, imported["id"])
        self.assertEqual((value["verified_blocks"], value["verified_bytes"]), (0, 0))

    def assert_single_checkpoint(self, imported, block):
        with jobs.transaction() as db:
            self.assertEqual(jobs.transfer_state.get_block(db, imported["id"], 0), block)
            count = db.execute("SELECT COUNT(*) FROM large_file_blocks WHERE endpoint_id=?", (imported["id"],)).fetchone()[0]
            value = jobs.transfer_state.status(db, imported["id"])
        self.assertEqual(count, 1)
        self.assertEqual((value["verified_blocks"], value["verified_bytes"]), (1, block["size"]))

    def test_valid_block_with_missing_ledger_repairs_once_and_publishes(self):
        fixture = self.fixture
        exported, imported, size, digest, block, path = self.received_without_checkpoint()
        original = (path.stat().st_dev, path.stat().st_ino)
        for _ in range(2):
            self.assertEqual(fixture.rpc("verify-block", imported, index=0, expected=block), ({"valid": True}, b""))
            self.assert_single_checkpoint(imported, block)
        self.assertEqual((path.stat().st_dev, path.stat().st_ino), original)
        fixture.publish_and_cleanup(exported, imported, size, digest)

    def test_stop_after_disk_verification_prevents_checkpoint_until_explicit_resume(self):
        fixture = self.fixture
        exported, imported, size, digest, block, path = self.received_without_checkpoint()
        verify = jobs.transfer_storage.verify_stored_block
        stopped = []

        def verify_then_stop(*args, **kwargs):
            valid = verify(*args, **kwargs)
            self.assertTrue(valid)
            with jobs.transaction() as db:
                stopped.append(jobs.transfer_state.stop(db, imported["id"]))
            return valid

        with patch.object(jobs.transfer_storage, "verify_stored_block", side_effect=verify_then_stop):
            with self.assertRaisesRegex(ValueError, "epoch|stopped"):
                fixture.rpc("verify-block", imported, index=0, expected=block)
        self.assertEqual(len(stopped), 1)
        self.assert_missing(imported)
        self.assertEqual(stopped[0]["reserved_bytes"], 2 * size)
        with self.assertRaisesRegex(ValueError, "epoch|stopped"):
            fixture.rpc("verify-block", imported, index=0, expected=block)
        resumed = fixture.rpc("resume", stopped[0])[0]
        self.assertGreater(resumed["epoch"], stopped[0]["epoch"])
        self.assertEqual(fixture.rpc("verify-block", resumed, index=0, expected=block), ({"valid": True}, b""))
        self.assert_single_checkpoint(resumed, block)
        fixture.publish_and_cleanup(exported, resumed, size, digest)

    def test_replaced_history_after_verification_cannot_receive_a_checkpoint(self):
        fixture = self.fixture
        exported, imported, size, digest, block, path = self.received_without_checkpoint()
        verify = jobs.transfer_storage.verify_stored_block

        def verify_then_replace(*args, **kwargs):
            valid = verify(*args, **kwargs)
            self.assertTrue(valid)
            fixture.replace_history(imported)
            return valid

        with patch.object(jobs.transfer_storage, "verify_stored_block", side_effect=verify_then_replace):
            with self.assertRaisesRegex(ValueError, "history"):
                fixture.rpc("verify-block", imported, index=0, expected=block)
        self.assert_missing(imported)
        self.assertEqual(path.stat().st_size, size)


if __name__ == "__main__":
    unittest.main()
