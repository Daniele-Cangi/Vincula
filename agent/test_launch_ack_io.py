# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acknowledgment reads only: synthetic files/handles, never a program launch."""
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jobs


class Stream:
    def __init__(self):
        self.read = Mock(return_value=b'{"started":true}\n')
        self.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed += 1


class WindowsAckKernelContractTests(unittest.TestCase):
    def setUp(self):
        self.kernel = SimpleNamespace(CreateFileW=Mock(return_value=101), CloseHandle=Mock(return_value=1))
        self.crt = SimpleNamespace(open_osfhandle=Mock(return_value=37))
        self.stream = Stream()
        self.os = SimpleNamespace(name="nt", O_RDONLY=os.O_RDONLY, O_BINARY=0x8000,
            fstat=Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0)),
            fdopen=Mock(return_value=self.stream), close=Mock())
        self.error_code = 32
        def native_error(code):
            result = (FileNotFoundError(errno.ENOENT, "synthetic native missing") if code in {2, 3}
                      else PermissionError(errno.EACCES, "synthetic native denial"))
            result.winerror = code
            return result
        self.winerror = Mock(side_effect=native_error)
        for mocker in (patch.object(ctypes, "WinDLL", Mock(return_value=self.kernel), create=True),
                       patch.object(ctypes, "get_last_error", lambda: self.error_code, create=True),
                       patch.object(ctypes, "WinError", self.winerror, create=True),
                       patch.dict(sys.modules, {"msvcrt": self.crt}), patch.object(jobs, "os", self.os)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_wide_native_open_shares_delete_and_transfers_original_handle_once(self):
        result = jobs.read_launch_status(Path("ready.json"))
        self.assertEqual(result, {"started": True})
        self.kernel.CreateFileW.assert_called_once_with("ready.json", 0x80000000, 7, None, 3, 0x00200000, None)
        self.crt.open_osfhandle.assert_called_once_with(101, os.O_RDONLY | 0x8000)
        self.os.fstat.assert_called_once_with(37)
        self.os.fdopen.assert_called_once_with(37, "rb", buffering=0)
        self.stream.read.assert_called_once_with(8193)
        self.assertEqual(self.stream.closed, 1)
        self.os.close.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_native_sharing_code_survives_without_crt_errno_reclassification(self):
        self.kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
        with self.assertRaises(OSError) as caught:
            jobs.read_launch_status(Path("ready.json"))
        self.assertEqual(caught.exception.winerror, 32)
        self.assertEqual(caught.exception.errno, errno.EACCES)
        self.winerror.assert_called_once_with(32)
        self.crt.open_osfhandle.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_native_missing_file_keeps_the_existing_none_contract(self):
        self.kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
        for code in (2, 3):
            self.error_code = code
            self.assertIsNone(jobs.read_launch_status(Path("ready.json")))
        self.kernel.CloseHandle.assert_not_called()

    def test_crt_transfer_failure_closes_native_handle_without_closing_a_descriptor(self):
        self.crt.open_osfhandle.side_effect = OSError("synthetic transfer failure")
        with self.assertRaises(OSError):
            jobs._read_windows_launch_status(Path("ready.json"))
        self.kernel.CloseHandle.assert_called_once_with(101)
        self.os.close.assert_not_called()
        self.os.fdopen.assert_not_called()

    def test_fstat_and_fdopen_failures_close_only_the_transferred_descriptor(self):
        for step in ("fstat", "fdopen"):
            with self.subTest(step=step):
                self.os.close.reset_mock()
                target = getattr(self.os, step)
                target.side_effect = OSError("synthetic descriptor failure")
                try:
                    with self.assertRaises(OSError):
                        jobs._read_windows_launch_status(Path("ready.json"))
                    self.os.close.assert_called_once_with(37)
                    self.kernel.CloseHandle.assert_not_called()
                finally:
                    target.side_effect = None

    def test_reparse_and_nonregular_files_are_rejected_before_reading_bytes(self):
        for mode, attributes in ((stat.S_IFREG | 0o600, 0x400), (stat.S_IFDIR | 0o700, 0)):
            with self.subTest(mode=mode, attributes=attributes):
                self.os.fstat.return_value = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                self.os.close.reset_mock()
                with self.assertRaisesRegex(ValueError, "regular non-reparse"):
                    jobs._read_windows_launch_status(Path("ready.json"))
                self.os.close.assert_called_once_with(37)
                self.os.fdopen.assert_not_called()
                self.stream.read.assert_not_called()

    def test_read_error_is_closed_by_stream_without_double_close(self):
        self.stream.read.side_effect = OSError("synthetic read failure")
        with self.assertRaises(OSError):
            jobs._read_windows_launch_status(Path("ready.json"))
        self.assertEqual(self.stream.closed, 1)
        self.os.close.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_raw_budget_utf8_and_json_contracts_are_unchanged(self):
        for raw, error in ((b" " * 8193, ValueError), (b"[]", jobs.UnconfirmedLaunchError),
                           (b"not JSON", ValueError), (b'{"error":"\xff"}', ValueError)):
            with self.subTest(raw=repr(raw[:20])):
                self.stream.read.return_value = raw
                with self.assertRaises(error):
                    jobs.read_launch_status(Path("ready.json"))
        self.stream.read.return_value = '{"error":"è🙂"}'.encode("utf-8")
        self.assertEqual(jobs.read_launch_status(Path("ready.json")), {"error": "è🙂"})


class LaunchAckRetryContractTests(unittest.TestCase):
    def test_only_native_sharing_codes_retry_ack_reads_without_launching_anything(self):
        for code in (32, 33, 5, None):
            with self.subTest(code=code):
                error = PermissionError(errno.EACCES, "synthetic read error")
                if code is not None:
                    error.winerror = code
                with patch.object(jobs, "WINDOWS", True), \
                        patch.object(jobs, "read_launch_status", side_effect=[error, {"started": True}]) as read, \
                        patch.object(jobs.time, "monotonic", return_value=0), \
                        patch.object(jobs.time, "sleep") as sleep, patch.object(jobs.subprocess, "Popen") as launch:
                    if code in {32, 33}:
                        jobs.confirm_windows_launch(Mock(), Path("ready.json"))
                        self.assertEqual(read.call_count, 2)
                        sleep.assert_called_once_with(0.025)
                    else:
                        with self.assertRaises(jobs.UnconfirmedLaunchError):
                            jobs.confirm_windows_launch(Mock(), Path("ready.json"))
                        read.assert_called_once()
                        sleep.assert_not_called()
                    launch.assert_not_called()

    def test_native_sharing_read_failure_after_deadline_does_not_retry(self):
        error = PermissionError(errno.EACCES, "synthetic read sharing error")
        error.winerror = 32
        with patch.object(jobs, "WINDOWS", True), patch.object(jobs, "read_launch_status", side_effect=error) as read, \
                patch.object(jobs.time, "monotonic", side_effect=[0, 10]), patch.object(jobs.time, "sleep") as sleep:
            with self.assertRaises(jobs.UnconfirmedLaunchError):
                jobs.confirm_windows_launch(Mock(), Path("ready.json"))
            read.assert_called_once()
            sleep.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX dispatch must ignore a mocked WINDOWS flag")
    def test_mocked_platform_flag_does_not_select_native_io_on_posix(self):
        with TemporaryDirectory(prefix="vincula-ack-posix-") as temporary:
            path = Path(temporary) / "ready.json"
            path.write_bytes(b'{"started":true}')
            with patch.object(jobs, "WINDOWS", True), \
                    patch.object(jobs, "_read_windows_launch_status", side_effect=AssertionError("No native API on POSIX")):
                self.assertEqual(jobs.read_launch_status(path), {"started": True})


@unittest.skipUnless(os.name == "nt", "Native Windows file sharing semantics")
class NativeWindowsAckTests(unittest.TestCase):
    def test_owned_temporary_file_sharing_error_keeps_winerror_and_recovers_after_unlock(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with TemporaryDirectory(prefix="vincula-ack-native-") as temporary:
            path = Path(temporary) / "ready.json"
            self.assertIsNone(jobs.read_launch_status(path))
            self.assertIsNone(jobs.read_launch_status(Path(temporary) / "missing-directory" / "ready.json"))
            path.write_bytes(b'{"started":true}')
            with path.open("rb") as ordinary_reader:
                self.assertFalse(ordinary_reader.closed)
                self.assertEqual(jobs.read_launch_status(path), {"started": True})
            handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                with self.assertRaises(OSError) as caught:
                    jobs.read_launch_status(path)
                self.assertEqual(caught.exception.winerror, 32)
            finally:
                self.assertTrue(kernel.CloseHandle(handle))
            self.assertEqual(jobs.read_launch_status(path), {"started": True})

    def test_native_reparse_file_is_not_followed(self):
        with TemporaryDirectory(prefix="vincula-ack-reparse-") as temporary:
            target, link = Path(temporary) / "target.json", Path(temporary) / "ready.json"
            target.write_bytes(b'{"started":true}')
            try:
                link.symlink_to(target)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("The Windows test account cannot create a synthetic symbolic link.")
                raise
            with self.assertRaises((ValueError, OSError)):
                jobs.read_launch_status(link)


if __name__ == "__main__":
    unittest.main()
