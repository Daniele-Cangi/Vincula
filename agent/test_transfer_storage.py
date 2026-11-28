# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real temporary files, synthetic contents; no database, SSH or process launch."""
from contextlib import contextmanager
from hashlib import sha256
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import transfer_storage as storage


class TransferStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-large-storage-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.base = self.home / "private"
        self.source = self.home / "input.bin"
        self.target = self.home / "result.bin"
        self.export_id, self.import_id = str(uuid4()), str(uuid4())
        self.records, self.received = {}, {}
        self.request_sha = sha256(b"immutable export request").hexdigest()
        self.commits = 0

    def source_file(self, size):
        block = bytes(range(256)) * (storage.BLOCK_SIZE // 256)
        with self.source.open("wb") as output:
            remaining = size
            while remaining:
                data = block[:min(len(block), remaining)]
                output.write(data)
                remaining -= len(data)
        return storage.source_identity(self.source)

    def checkpoint(self, record):
        self.records[record["index"]] = dict(record)

    def prepare(self, size=None, **kwargs):
        identity = self.source_file(size) if size is not None else storage.source_identity(self.source)
        return storage.prepare_export(self.source, self.base, self.export_id, self.request_sha, identity,
            get_block=self.records.get, checkpoint=kwargs.pop("checkpoint", self.checkpoint), **kwargs)

    def receive(self, header):
        for index in range(header["block_count"]):
            data = storage.read_export_block(self.base, header, index, self.records.get)
            storage.store_block(self.base, self.import_id, header, index, data, self.records[index],
                checkpoint=lambda record: self.received.__setitem__(record["index"], dict(record)))

    def commit(self):
        self.commits += 1

    def finalize(self, header, **kwargs):
        return storage.finalize_import(self.base, self.import_id, header, self.target,
            get_block=self.records.get, authorize=kwargs.pop("authorize", lambda: None),
            commit_point=kwargs.pop("commit_point", self.commit), **kwargs)

    def pending(self):
        return self.target.parent / (storage.STAGE_PREFIX + ".large-" + self.import_id)

    def test_real_file_larger_than_legacy_limit_has_streamed_hashes_and_roundtrips(self):
        header = self.prepare(64 * 1024 * 1024 + 19)
        self.assertEqual(header["block_count"], 17)
        self.assertEqual(set(header), storage.HEADER_FIELDS)
        expected_root = sha256()
        for index, record in self.records.items():
            expected_root.update(f"{index}:{record['size']}:{record['sha256']}\n".encode("ascii"))
        self.assertEqual(header["blocks_sha256"], expected_root.hexdigest())
        self.receive(header)
        self.assertTrue(all(storage.verify_stored_block(self.base, self.import_id, header, i, record)
                            for i, record in self.records.items()))
        receipt = self.finalize(header)
        self.assertTrue(receipt["published"])
        self.assertEqual(receipt["size"], self.target.stat().st_size)
        digest = sha256()
        with self.target.open("rb") as stream:
            while data := stream.read(storage.BLOCK_SIZE):
                digest.update(data)
        self.assertEqual(digest.hexdigest(), header["sha256"])
        self.assertTrue(os.path.samefile(self.pending(), self.target))
        self.assertEqual(self.commits, 1)
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        storage.cleanup_private(self.base, "imports", self.import_id)
        storage.cleanup_private(self.base, "exports", self.export_id)
        self.assertTrue(self.target.is_file())
        self.assertEqual(self.target.stat().st_size, header["size"])

    def test_export_resume_hashes_prefix_and_discards_only_uncheckpointed_tail(self):
        identity = self.source_file(2 * storage.BLOCK_SIZE + 29)
        def stop(record):
            self.checkpoint(record)
            if record["index"] == 0:
                raise storage.Cancelled("synthetic crash after durable checkpoint")
        with self.assertRaises(storage.Cancelled):
            storage.prepare_export(self.source, self.base, self.export_id, self.request_sha, identity,
                get_block=self.records.get, checkpoint=stop)
        partial = self.base / "exports" / self.export_id / "payload.part"
        with partial.open("ab") as stream:
            stream.write(b"untrusted trailing bytes")
        header = self.prepare()
        self.assertEqual(header["size"], identity["size"])
        self.assertFalse(partial.exists())
        with self.source.open("rb") as stream:
            self.assertEqual(storage.read_export_block(self.base, header, 0, self.records.get), stream.read(storage.BLOCK_SIZE))

    def test_corrupt_checkpointed_export_is_never_skipped_or_overwritten(self):
        identity = self.source_file(storage.BLOCK_SIZE + 1)
        def stop(record):
            self.checkpoint(record)
            raise storage.Cancelled("stop")
        with self.assertRaises(storage.Cancelled):
            storage.prepare_export(self.source, self.base, self.export_id, self.request_sha, identity,
                get_block=self.records.get, checkpoint=stop)
        partial = self.base / "exports" / self.export_id / "payload.part"
        with partial.open("r+b") as stream:
            stream.write(b"X")
        before = partial.stat().st_size
        with self.assertRaisesRegex(ValueError, "prefix is corrupt"):
            self.prepare()
        self.assertEqual(partial.stat().st_size, before)
        self.assertFalse(partial.with_name("payload").exists())

    def test_source_identity_change_prevents_sealing_and_old_identity_replay(self):
        identity = self.source_file(storage.BLOCK_SIZE + 101)
        def mutate(record):
            self.checkpoint(record)
            if record["index"] == 0:
                with self.source.open("ab") as stream:
                    stream.write(b"source changed")
        with self.assertRaisesRegex(ValueError, "Source identity changed"):
            storage.prepare_export(self.source, self.base, self.export_id, self.request_sha, identity,
                get_block=self.records.get, checkpoint=mutate)
        self.assertFalse((self.base / "exports" / self.export_id / "payload").exists())
        with self.assertRaisesRegex(ValueError, "Source identity changed"):
            storage.prepare_export(self.source, self.base, self.export_id, self.request_sha, identity,
                get_block=self.records.get, checkpoint=self.checkpoint)

    def test_store_rechecks_existing_data_and_recovers_lost_checkpoint(self):
        header = self.prepare(8193)
        data = storage.read_export_block(self.base, header, 0, self.records.get)
        with self.assertRaisesRegex(ValueError, "checksum"):
            storage.store_block(self.base, self.import_id, header, 0, data + b"x", self.records[0], checkpoint=lambda row: None)
        def lost(record):
            raise RuntimeError("checkpoint acknowledgment lost")
        with self.assertRaisesRegex(RuntimeError, "lost"):
            storage.store_block(self.base, self.import_id, header, 0, io.BytesIO(data), self.records[0], checkpoint=lost)
        self.assertTrue(storage.verify_stored_block(self.base, self.import_id, header, 0, self.records[0]))
        block = self.base / "imports" / self.import_id / "block-00000000"
        with block.open("r+b") as stream:
            stream.write(b"X")
        self.assertFalse(storage.verify_stored_block(self.base, self.import_id, header, 0, self.records[0]))
        storage.store_block(self.base, self.import_id, header, 0, data, self.records[0], checkpoint=lambda row: self.received.update({0: row}))
        self.assertTrue(storage.verify_stored_block(self.base, self.import_id, header, 0, self.records[0]))
        self.assertEqual(self.received[0], self.records[0])

    def test_checkpoint_follows_data_fsync_and_authorization_can_prevent_io(self):
        self.source_file(19)
        synced = []
        real_sync = storage.os.fsync
        def sync(descriptor):
            real_sync(descriptor)
            synced.append(descriptor)
        def checkpoint(record):
            self.assertTrue(synced)
            self.checkpoint(record)
        with patch.object(storage.os, "fsync", side_effect=sync):
            self.prepare(checkpoint=checkpoint)
        second = str(uuid4())
        def denied():
            raise PermissionError("revoked")
        with self.assertRaises(PermissionError):
            storage.prepare_export(self.source, self.base, second, self.request_sha, storage.source_identity(self.source),
                get_block=self.records.get, checkpoint=self.checkpoint, authorize=denied)
        self.assertFalse((self.base / "exports" / second).exists())

    def test_stop_before_publication_retains_attested_pending_for_scoped_cleanup(self):
        header = self.prepare(storage.BLOCK_SIZE + 3)
        self.receive(header)
        def stopped(record):
            raise storage.Cancelled("operator stop after fsync")
        with self.assertRaises(storage.Cancelled):
            self.finalize(header, checkpoint=stopped)
        self.assertFalse(self.target.exists())
        self.assertTrue(self.pending().exists())
        with self.assertRaisesRegex(ValueError, "cleanup must be confirmed"):
            storage.cleanup_private(self.base, "imports", self.import_id)
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        self.assertFalse(self.pending().exists())
        self.assertTrue(self.finalize(header)["published"])

    def test_enospc_before_publication_leaves_no_final_and_can_be_cleaned(self):
        header = self.prepare(16384)
        self.receive(header)
        real_write = storage._write_all
        def no_space(stream, data):
            if len(data) > 4096:
                stream.write(data[:3])
                raise OSError(28, "Synthetic ENOSPC")
            return real_write(stream, data)
        with patch.object(storage, "_write_all", side_effect=no_space):
            with self.assertRaises(OSError):
                self.finalize(header)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.commits, 0)
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        self.assertTrue(self.finalize(header)["published"])

    def test_lost_publication_reply_reconciles_original_inode_without_republishing(self):
        header = self.prepare(65537)
        self.receive(header)
        real_sync = storage._PublicationDirectory.sync
        def lost(directory):
            real_sync(directory)
            if self.target.exists():
                raise RuntimeError("publication acknowledgment lost")
        with patch.object(storage._PublicationDirectory, "sync", new=lost):
            with self.assertRaisesRegex(RuntimeError, "lost"):
                self.finalize(header)
        before = self.target.stat()
        reconciled = []
        def permit():
            self.assertTrue(self.target.exists())
            self.assertTrue(os.path.samefile(self.pending(), self.target))
            reconciled.append(True)
            self.commit()
        receipt = self.finalize(header, commit_point=permit)
        self.assertEqual(receipt["target_identity"], {"dev": before.st_dev, "ino": before.st_ino})
        self.assertEqual(self.commits, 2)
        self.assertEqual(reconciled, [True])
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        self.assertEqual(self.target.stat().st_ino, before.st_ino)

    def test_reconciliation_requires_new_commit_permission_without_deleting_final(self):
        header = self.prepare(103)
        self.receive(header)
        self.finalize(header)
        before = self.target.stat()
        def denied():
            raise PermissionError("Reconciliation permit denied after Stop")
        with self.assertRaises(PermissionError):
            self.finalize(header, commit_point=denied)
        self.assertEqual(self.target.stat().st_ino, before.st_ino)
        self.assertTrue(os.path.samefile(self.pending(), self.target))

    def test_existing_final_and_replaced_pending_are_never_deleted_or_adopted(self):
        header = self.prepare(17)
        self.receive(header)
        self.target.write_bytes(b"owner's existing final")
        with self.assertRaises(FileExistsError):
            self.finalize(header)
        self.assertEqual(self.target.read_bytes(), b"owner's existing final")
        self.target.unlink()  # Test owns this synthetic preexisting file.
        with self.assertRaises(storage.Cancelled):
            self.finalize(header, checkpoint=lambda row: (_ for _ in ()).throw(storage.Cancelled("stop")))
        replacement = self.home / "replacement"
        replacement.write_bytes(b"foreign pending replacement")
        old = self.pending().stat()
        self.assertNotEqual((old.st_dev, old.st_ino), (replacement.stat().st_dev, replacement.stat().st_ino))
        os.replace(replacement, self.pending())
        with self.assertRaisesRegex(ValueError, "replaced pending"):
            storage.cleanup_pending(self.base, self.import_id, self.target)
        self.assertEqual(self.pending().read_bytes(), b"foreign pending replacement")
        self.assertFalse(self.target.exists())

    def test_empty_file_budget_and_symlink_guards(self):
        header = self.prepare(0)
        self.assertEqual(header["block_count"], 0)
        self.assertEqual(header["sha256"], sha256(b"").hexdigest())
        self.assertEqual(self.finalize(header)["size"], 0)
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        self.source_file(10)
        with self.assertRaisesRegex(ValueError, "byte budget"):
            self.prepare(max_bytes=9)
        link = self.home / "linked-private"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                return
            raise
        with self.assertRaisesRegex(ValueError, "Links"):
            storage.PrivateFiles(link, "imports", str(uuid4()))

    def test_block_root_and_commit_authorization_are_checked_before_publication(self):
        header = self.prepare(71)
        self.receive(header)
        wrong = dict(header, blocks_sha256=sha256(b"foreign block order").hexdigest())
        with self.assertRaisesRegex(ValueError, "block-root"):
            self.finalize(wrong)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.commits, 0)
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        def denied():
            self.assertFalse(self.target.exists())
            self.assertEqual(self.pending().stat().st_size, header["size"])
            raise PermissionError("commit authorization revoked")
        with self.assertRaises(PermissionError):
            self.finalize(header, commit_point=denied)
        self.assertFalse(self.target.exists())
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))

    def test_cleanup_refuses_unknown_files_and_export_link_interruption_is_reconciled(self):
        header = self.prepare(31)
        export = self.base / "exports" / self.export_id
        os.link(export / "payload", export / "payload.part")
        self.assertEqual(self.prepare(), header)
        self.assertFalse((export / "payload.part").exists())
        foreign = export / "operator-file.txt"
        foreign.write_bytes(b"must remain")
        with self.assertRaisesRegex(ValueError, "Unknown private"):
            storage.cleanup_private(self.base, "exports", self.export_id)
        self.assertTrue((export / "payload").exists())
        self.assertEqual(foreign.read_bytes(), b"must remain")

    def test_pre_epoch_source_timestamp_is_a_valid_signed_identity(self):
        self.source_file(1)
        try:
            os.utime(self.source, ns=(-1, -1))
        except (OSError, OverflowError):
            self.skipTest("The temporary filesystem does not support timestamps before the epoch.")
        if storage.source_identity(self.source)["mtime_ns"] >= 0:
            self.skipTest("The temporary filesystem rounded the timestamp to the epoch.")
        self.assertEqual(self.prepare()["size"], 1)

    def test_path_ctime_difference_does_not_change_descriptor_based_source_identity(self):
        self.source_file(19)
        expected = storage.source_identity(self.source)
        original = Path.lstat
        def by_path(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.source:
                fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                fields["st_ctime_ns"] -= 100
                return SimpleNamespace(**fields)
            return info
        with patch.object(Path, "lstat", new=by_path):
            self.assertEqual(storage.source_identity(self.source), expected)
            self.assertEqual(self.prepare()["size"], 19)

    def test_descriptor_ctime_change_alone_still_prevents_export_sealing(self):
        identity = self.source_file(19)
        changed = False
        original = storage.os.fstat
        def by_descriptor(descriptor):
            info = original(descriptor)
            if changed and (info.st_dev, info.st_ino) == (identity["dev"], identity["ino"]):
                fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                fields["st_ctime_ns"] += 100
                return SimpleNamespace(**fields)
            return info
        def checkpoint(record):
            nonlocal changed
            self.checkpoint(record)
            changed = True
        with patch.object(storage.os, "fstat", side_effect=by_descriptor):
            with self.assertRaisesRegex(ValueError, "Source identity changed"):
                storage.prepare_export(self.source, self.base, self.export_id, self.request_sha, identity,
                    get_block=self.records.get, checkpoint=checkpoint)
        self.assertFalse((self.base / "exports" / self.export_id / "payload").exists())

    def test_parent_swap_after_commit_check_never_publishes_through_a_symlink(self):
        header = self.prepare(19)
        self.receive(header)
        parent = self.home / "grant" / "nested"
        parent.mkdir(parents=True)
        outside = self.home / "outside"
        outside.mkdir()
        moved = outside / "moved"
        self.target = parent / "result.bin"
        def swap():
            parent.rename(moved)  # Windows's held directory handles deny this rename.
            parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.finalize(header, commit_point=swap)
        self.assertFalse((moved / "result.bin").exists())
        self.assertFalse(self.target.exists())

    def test_interrupted_owner_link_and_partial_cleanup_are_reconciled(self):
        header = self.prepare(31)
        self.receive(header)
        with self.assertRaises(storage.Cancelled):
            self.finalize(header, checkpoint=lambda row: (_ for _ in ()).throw(storage.Cancelled("stop")))
        directory = self.base / "imports" / self.import_id
        scratch = directory / (".pending-owner-" + uuid4().hex + ".tmp")
        os.link(directory / "pending-owner.json", scratch)
        self.assertTrue(self.finalize(header)["published"])
        self.assertFalse(scratch.exists())
        real_unlink = storage._unlink_owned
        def crash(path, info, **kwargs):
            real_unlink(path, info, **kwargs)
            if Path(path).name == self.pending().name:
                raise RuntimeError("crash after pending unlink")
        with patch.object(storage, "_unlink_owned", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                storage.cleanup_pending(self.base, self.import_id, self.target)
        self.assertFalse(self.pending().exists())
        self.assertTrue(self.target.exists())
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        self.assertTrue(self.target.exists())
        self.assertFalse((directory / "pending-owner.json").exists())

    def test_owner_recovery_uses_fresh_path_stat_when_direntry_identifiers_are_zero(self):
        header = self.prepare(31)
        self.receive(header)
        with self.assertRaises(storage.Cancelled):
            self.finalize(header, checkpoint=lambda row: (_ for _ in ()).throw(storage.Cancelled("stop")))
        directory = self.base / "imports" / self.import_id
        scratch = directory / (".pending-owner-" + uuid4().hex + ".tmp")
        os.link(directory / "pending-owner.json", scratch)
        original, cached_stats = storage.os.scandir, []
        @contextmanager
        def windows_entries(path):
            with original(path) as entries:
                def values():
                    for entry in entries:
                        cached = Mock(return_value=SimpleNamespace(st_mode=0o100600, st_dev=0, st_ino=0, st_nlink=0))
                        cached_stats.append(cached)
                        yield SimpleNamespace(name=entry.name, path=entry.path, stat=cached)
                yield values()
        with patch.object(storage.os, "scandir", side_effect=windows_entries):
            self.assertTrue(self.finalize(header)["published"])
        self.assertTrue(cached_stats)
        self.assertTrue(all(not cached.called for cached in cached_stats))
        self.assertFalse(scratch.exists())
        self.assertTrue(storage.cleanup_pending(self.base, self.import_id, self.target))
        self.assertTrue(self.target.exists())


class WindowsCreationContractTests(unittest.TestCase):
    def test_private_dacl_is_passed_at_file_and_directory_creation(self):
        token, native_handle = Mock(), MagicMock()
        security = SimpleNamespace(TokenUser=1, SDDL_REVISION_1=1,
            OpenProcessToken=Mock(return_value=token), GetTokenInformation=Mock(return_value=("account",)),
            ConvertSidToStringSid=Mock(return_value="S-1-5-21-1000"),
            ConvertStringSecurityDescriptorToSecurityDescriptor=Mock(side_effect=lambda descriptor, revision: descriptor))
        winfile = SimpleNamespace(CreateFile=Mock(return_value=native_handle), CreateDirectory=Mock())
        constants = SimpleNamespace(GENERIC_READ=1, GENERIC_WRITE=2, FILE_SHARE_READ=1,
            FILE_SHARE_WRITE=2, FILE_SHARE_DELETE=4, CREATE_NEW=1, OPEN_EXISTING=2)
        modules = {"ntsecuritycon": SimpleNamespace(TOKEN_QUERY=8), "win32api": SimpleNamespace(GetCurrentProcess=lambda: -1),
            "win32security": security, "pywintypes": SimpleNamespace(SECURITY_ATTRIBUTES=SimpleNamespace),
            "win32file": winfile, "win32con": constants,
            "msvcrt": SimpleNamespace(open_osfhandle=Mock(return_value=123))}
        fake_os = SimpleNamespace(name="nt", O_BINARY=0x8000, O_RDWR=2, O_RDONLY=0)
        with patch.dict(sys.modules, modules), patch.object(storage, "os", fake_os):
            self.assertEqual(storage._descriptor("private.bin", 0, write=True, create=True), 123)
            storage._make_directory(Path("private-directory"))
        file_attributes = winfile.CreateFile.call_args.args[3]
        directory_attributes = winfile.CreateDirectory.call_args.args[1]
        for attributes in (file_attributes, directory_attributes):
            self.assertFalse(attributes.bInheritHandle)
            self.assertTrue(attributes.SECURITY_DESCRIPTOR.startswith("D:P"))
            self.assertIn("S-1-5-21-1000", attributes.SECURITY_DESCRIPTOR)
            self.assertIn("S-1-5-18", attributes.SECURITY_DESCRIPTOR)
            self.assertIn("S-1-5-32-544", attributes.SECURITY_DESCRIPTOR)
            self.assertNotIn(";;;WD)", attributes.SECURITY_DESCRIPTOR)
        self.assertIn("OICI", directory_attributes.SECURITY_DESCRIPTOR)
        self.assertEqual(winfile.CreateFile.call_args.args[2], 7)
        native_handle.Detach.assert_called_once()
        native_handle.Close.assert_not_called()

    def test_windows_ancestor_guards_omit_delete_sharing_and_close_original_handles(self):
        handles = []
        def create(*args):
            handle = Mock()
            handles.append(handle)
            self.assertEqual(args[2], 3)  # READ | WRITE, deliberately no DELETE.
            self.assertEqual(args[5], 0x02200000)
            return handle
        modules = {"win32file": SimpleNamespace(CreateFile=Mock(side_effect=create)),
                   "win32con": SimpleNamespace(FILE_SHARE_READ=1, FILE_SHARE_WRITE=2, OPEN_EXISTING=3)}
        with TemporaryDirectory(prefix="vincula-parent-guard-") as temporary:
            directory = Path(temporary).resolve()
            with patch.dict(sys.modules, modules), patch.object(storage, "os", SimpleNamespace(name="nt")):
                with storage._PublicationDirectory(directory):
                    self.assertEqual(len(handles), len(directory.parents) + 1)
                    self.assertTrue(all(not handle.Close.called for handle in handles))
            self.assertTrue(all(handle.Close.call_count == 1 for handle in handles))


if __name__ == "__main__":
    unittest.main()
