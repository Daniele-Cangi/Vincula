# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only acknowledgment handling; fake clocks, no workers/program retries."""

from contextlib import ExitStack
import errno
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import jobs


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def failure(code=None):
    error = PermissionError(errno.EACCES, "PRIVATE_ERROR_DETAIL", "PRIVATE_CREDENTIAL_PATH")
    if code is not None:
        error.winerror = code
    return error


class WindowsAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-ack-read-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ready.json"
        self.clock = Clock()
        self.process = Mock()
        self.process.poll.return_value = None
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(jobs, "WINDOWS", True))
        self.patches.enter_context(patch.object(jobs.time, "monotonic", side_effect=self.clock.monotonic))
        self.patches.enter_context(patch.object(jobs.time, "sleep", side_effect=self.clock.sleep))
        self.create = self.patches.enter_context(patch.object(jobs.subprocess, "Popen",
            side_effect=AssertionError("Acknowledgment reads must never relaunch a program")))
        self.addCleanup(self.create.assert_not_called)

    def assert_private_diagnostic(self, error, category, code=None):
        message = str(error)
        self.assertIn(category, message)
        self.assertNotIn("PRIVATE_ERROR_DETAIL", message)
        self.assertNotIn("PRIVATE_CREDENTIAL_PATH", message)
        if code is not None:
            self.assertIn("winerror=" + str(code), message)
            self.assertIn("errno=" + str(errno.EACCES), message)

    def test_known_sharing_and_lock_violations_reread_only_the_same_ack(self):
        for code in (32, 33):
            with self.subTest(winerror=code):
                self.clock.now, self.clock.sleeps = 0.0, []
                with patch.object(jobs, "read_launch_status", side_effect=[failure(code), {"started": True}]) as read:
                    self.assertIsNone(jobs.confirm_windows_launch(self.process, self.path))
                self.assertEqual(read.call_count, 2)
                self.assertTrue(all(call.args == (self.path,) for call in read.call_args_list))
                self.assertEqual(self.clock.sleeps, [.025])

    def test_persistent_sharing_violation_expires_with_safe_error_codes(self):
        with patch.object(jobs, "read_launch_status", side_effect=failure(32)) as read:
            with self.assertRaises(jobs.UnconfirmedLaunchError) as caught:
                jobs.confirm_windows_launch(self.process, self.path)
        self.assert_private_diagnostic(caught.exception, "PermissionError", 32)
        self.assertGreater(read.call_count, 1)
        self.assertLess(read.call_count, 1000)
        self.assertGreaterEqual(self.clock.now, 10)
        self.assertLessEqual(self.clock.now, 10.05)
        self.assertTrue(all(call.args == (self.path,) for call in read.call_args_list))

    def test_access_denied_is_immediate_and_never_treated_as_sharing(self):
        with patch.object(jobs, "read_launch_status", side_effect=failure(5)) as read:
            with self.assertRaises(jobs.UnconfirmedLaunchError) as caught:
                jobs.confirm_windows_launch(self.process, self.path)
        self.assert_private_diagnostic(caught.exception, "PermissionError", 5)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_invalid_json_is_immediate_and_does_not_echo_ack_contents(self):
        self.path.write_text('{"PRIVATE_CREDENTIAL_PATH":INVALID}', encoding="utf-8")
        original = jobs.read_launch_status
        with patch.object(jobs, "read_launch_status", wraps=original) as read:
            with self.assertRaises(jobs.UnconfirmedLaunchError) as caught:
                jobs.confirm_windows_launch(self.process, self.path)
        self.assert_private_diagnostic(caught.exception, "JSONDecodeError")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_oversized_ack_is_immediate_even_if_its_json_would_be_valid(self):
        self.path.write_bytes(json.dumps({"started": True}).encode() + b" " * jobs.LAUNCH_ACK_BYTES)
        original = jobs.read_launch_status
        with patch.object(jobs, "read_launch_status", wraps=original) as read:
            with self.assertRaises(jobs.UnconfirmedLaunchError) as caught:
                jobs.confirm_windows_launch(self.process, self.path)
        self.assert_private_diagnostic(caught.exception, "ValueError")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_owned_ack_can_be_reread_after_original_helper_already_exited(self):
        self.process.poll.return_value = 0
        with patch.object(jobs, "read_launch_status", side_effect=[failure(32), {"started": True}]) as read:
            jobs.confirm_windows_launch(self.process, self.path)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.clock.sleeps, [.025])

    def test_posix_or_uncategorized_access_errors_are_not_retried(self):
        for windows, error in ((False, failure(32)), (True, failure())):
            with self.subTest(windows=windows), patch.object(jobs, "WINDOWS", windows), \
                    patch.object(jobs, "read_launch_status", side_effect=error) as read:
                with self.assertRaises(jobs.UnconfirmedLaunchError) as caught:
                    jobs.confirm_windows_launch(self.process, self.path)
                self.assert_private_diagnostic(caught.exception, "PermissionError")
                self.assertEqual(read.call_count, 1)
                self.assertEqual(self.clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
