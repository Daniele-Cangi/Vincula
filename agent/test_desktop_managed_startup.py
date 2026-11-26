# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cancellation and rollback use fake processes/FDs; no engine or service runs."""

from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import os
import signal
import unittest
from unittest.mock import MagicMock, Mock, patch

from desktop_backend import GroupManifest, Screen
import desktop_managed as managed
import desktop_process as guardian


@unittest.skipUnless(os.name != "nt", "POSIX synthetic startup/process-group tests; Windows requires its native Job Object gate")
class ManagedStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="vincula-managed-startup-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.binary = root / "approved-core"
        self.binary.write_bytes(b"fixture; never executable or launched")
        self.settings = root / "Deskflow.conf"
        self.settings.write_text("[fixture]\n", encoding="utf-8")
        primary = "11111111-1111-4111-8111-111111111111"
        secondary = "22222222-2222-4222-8222-222222222222"
        group = GroupManifest(
            "99999999-9999-4999-8999-999999999999", primary,
            (Screen(primary, 0, 0), Screen(secondary, 1, 0)), True,
            ((primary, "a" * 64), (secondary, "b" * 64)),
            ((primary, "c" * 64), (secondary, "d" * 64)),
        )
        self.configuration = {
            "group": group.to_document(), "node_id": primary,
            "settings_file": str(self.settings), "state_file": str(root / "Deskflow.state"),
            "binary_sha256": "f" * 64, "owner_consent": True,
        }
        self.argv = [str(self.binary), "server", "--settings", str(self.settings)]
        self.pinned = self.binary.stat()

    def engine(self):
        return managed.ManagedEngine(self.configuration, self.argv, stop_event=Event())

    def startup_mocks(self, stack, *, pipes=((201, 202), (203, 204))):
        """No real OS pipe, child, signal, sleep or background thread is allowed."""
        stack.enter_context(patch("desktop_security.private_directory"))
        stack.enter_context(patch("desktop_security.private_file"))
        stack.enter_context(patch.object(managed.sys, "platform", "linux"))
        pipe = stack.enter_context(patch.object(managed.os, "pipe", side_effect=pipes))
        close = stack.enter_context(patch.object(managed.os, "close"))
        popen = stack.enter_context(patch.object(managed.subprocess, "Popen"))
        killpg = stack.enter_context(patch.object(managed.os, "killpg"))
        sleep = stack.enter_context(patch.object(managed, "sleep"))
        thread = stack.enter_context(patch.object(managed, "Thread"))
        return pipe, close, popen, killpg, sleep, thread

    def assert_closed_once_and_detached(self, engine, close, descriptors):
        self.assertEqual(Counter(call.args[0] for call in close.call_args_list), Counter(descriptors))
        self.assertIsNone(engine.reader)
        self.assertIsNone(engine.writer)
        before = list(close.call_args_list)
        # A subsequent owner close must not close descriptors another owner may reuse.
        engine.close()
        engine.close()
        self.assertEqual(close.call_args_list, before)

    def test_stop_while_hashing_closes_original_pin_and_never_spawns(self):
        engine = self.engine()
        with ExitStack() as stack:
            pipe, close, popen, killpg, sleep, thread = self.startup_mocks(stack)
            stack.enter_context(patch.object(managed, "artifact_approval"))
            opened = stack.enter_context(patch.object(managed.os, "open", return_value=100))
            stack.enter_context(patch.object(managed.os, "fstat", return_value=self.pinned))

            def cancelled_read(_descriptor, _count):
                engine.stopped.set()
                return b"hashing was in progress"

            read = stack.enter_context(patch.object(managed.os, "read", side_effect=cancelled_read))
            with self.assertRaisesRegex(OSError, "startup was cancelled"):
                engine.start()
            opened.assert_called_once()
            read.assert_called_once_with(100, 65536)
            pipe.assert_not_called()
            popen.assert_not_called()
            killpg.assert_not_called()
            sleep.assert_not_called()
            thread.assert_not_called()
            self.assert_closed_once_and_detached(engine, close, [100])

    def test_stop_as_pin_finishes_prevents_popen_and_rolls_back_pipes(self):
        engine = self.engine()

        def completed_pin():
            engine.stopped.set()
            return 100, self.pinned

        with ExitStack() as stack:
            _pipe, close, popen, killpg, sleep, thread = self.startup_mocks(stack)
            stack.enter_context(patch.object(engine, "_pin_binary", side_effect=completed_pin))
            with self.assertRaisesRegex(OSError, "startup was cancelled"):
                engine.start()
            popen.assert_not_called()
            killpg.assert_not_called()
            sleep.assert_not_called()
            thread.assert_not_called()
            self.assert_closed_once_and_detached(engine, close, [100, 201, 202, 203, 204])

    def test_stop_immediately_after_popen_kills_group_before_reaping_original_child(self):
        for term_disappears in (False, True):
            with self.subTest(term_disappears=term_disappears), ExitStack() as stack:
                engine = self.engine()
                _pipe, close, popen, killpg, sleep, thread = self.startup_mocks(stack)
                stack.enter_context(patch.object(engine, "_pin_binary", return_value=(100, self.pinned)))
                process = Mock(spec=["pid", "kill", "terminate", "poll", "wait"])
                process.pid = 4242
                order = []

                def launch(*_args, **_kwargs):
                    order.append("popen-original")
                    engine.stopped.set()
                    return process

                def group_signal(pid, signum):
                    order.append(("group", pid, signum))
                    if term_disappears and signum == signal.SIGTERM:
                        raise ProcessLookupError("group already exited")

                popen.side_effect = launch
                killpg.side_effect = group_signal
                sleep.side_effect = lambda delay: order.append(("grace", delay))
                process.wait.side_effect = lambda **kwargs: order.append(("wait-original", kwargs)) or 0
                with self.assertRaisesRegex(OSError, "startup was cancelled"):
                    engine.start()
                popen.assert_called_once()
                kwargs = popen.call_args.kwargs
                self.assertTrue(kwargs["start_new_session"])
                self.assertTrue(kwargs["close_fds"])
                self.assertEqual(kwargs["pass_fds"], (201, 204))
                self.assertIs(engine.process, process)
                self.assertEqual(order, [
                    "popen-original", ("group", 4242, signal.SIGTERM), ("grace", 0.15),
                    ("group", 4242, signal.SIGKILL), ("wait-original", {"timeout": 3}),
                ])
                process.kill.assert_not_called()
                process.terminate.assert_not_called()
                process.poll.assert_not_called()
                thread.assert_not_called()
                self.assert_closed_once_and_detached(engine, close, [100, 201, 202, 203, 204])

    def test_each_pipe_allocation_failure_closes_only_owned_descriptors(self):
        cases = (([OSError("first pipe allocation failed")], [100]),
                 ([(201, 202), OSError("second pipe allocation failed")], [100, 201, 202]))
        for pipes, owned in cases:
            with self.subTest(pipes=pipes), ExitStack() as stack:
                engine = self.engine()
                _pipe, close, popen, killpg, sleep, thread = self.startup_mocks(stack, pipes=pipes)
                stack.enter_context(patch.object(engine, "_pin_binary", return_value=(100, self.pinned)))
                with self.assertRaisesRegex(OSError, "pipe allocation failed"):
                    engine.start()
                self.assertIsNone(engine.process)
                popen.assert_not_called()
                killpg.assert_not_called()
                sleep.assert_not_called()
                thread.assert_not_called()
                self.assert_closed_once_and_detached(engine, close, owned)

    def test_popen_failure_closes_original_pin_and_all_four_pipe_ends(self):
        engine = self.engine()
        with ExitStack() as stack:
            _pipe, close, popen, killpg, sleep, thread = self.startup_mocks(stack)
            stack.enter_context(patch.object(engine, "_pin_binary", return_value=(100, self.pinned)))
            popen.side_effect = OSError("spawn failed before returning original handle")
            with self.assertRaisesRegex(OSError, "spawn failed"):
                engine.start()
            self.assertIsNone(engine.process)
            popen.assert_called_once()
            killpg.assert_not_called()
            sleep.assert_not_called()
            thread.assert_not_called()
            self.assert_closed_once_and_detached(engine, close, [100, 201, 202, 203, 204])

    def assert_thread_start_failure_rolls_back(self, failed_index):
        engine = self.engine()
        with ExitStack() as stack:
            _pipe, close, popen, killpg, sleep, thread = self.startup_mocks(stack)
            stack.enter_context(patch.object(engine, "_pin_binary", return_value=(100, self.pinned)))
            process = Mock(spec=["pid", "kill", "terminate", "poll", "wait"])
            process.pid = 4242
            popen.return_value = process
            workers = [Mock(spec=["start"]), Mock(spec=["start"])]
            workers[failed_index].start.side_effect = RuntimeError("thread startup failed")
            thread.side_effect = workers
            order = []
            killpg.side_effect = lambda pid, signum: order.append(("group", pid, signum))
            sleep.side_effect = lambda delay: order.append(("grace", delay))
            process.wait.side_effect = lambda **kwargs: order.append(("wait-original", kwargs)) or 0
            with self.assertRaisesRegex(RuntimeError, "thread startup failed"):
                engine.start()
            self.assertTrue(engine.stopped.is_set())
            self.assertIs(engine.process, process)
            popen.assert_called_once()
            self.assertEqual(thread.call_count, failed_index + 1)
            self.assertEqual(thread.call_args_list[0].kwargs["name"], "managed-engine-read")
            workers[0].start.assert_called_once_with()
            if failed_index == 1:
                self.assertEqual(thread.call_args_list[1].kwargs["name"], "managed-engine-challenge")
                workers[1].start.assert_called_once_with()
            else:
                workers[1].start.assert_not_called()
            self.assertEqual(order, [
                ("group", 4242, signal.SIGTERM), ("grace", 0.15),
                ("group", 4242, signal.SIGKILL), ("wait-original", {"timeout": 3}),
            ])
            process.kill.assert_not_called()
            process.terminate.assert_not_called()
            process.poll.assert_not_called()
            self.assert_closed_once_and_detached(engine, close, [100, 201, 202, 203, 204])

    def test_first_thread_start_failure_kills_group_and_closes_owned_fds_once(self):
        self.assert_thread_start_failure_rolls_back(0)

    def test_second_thread_start_failure_kills_group_and_closes_owned_fds_once(self):
        self.assert_thread_start_failure_rolls_back(1)

    def test_guardian_event_thread_failure_happens_before_any_engine_start(self):
        specification = {"argv": self.argv, "cwd": None, "lease_seconds": 2,
                         "heartbeat_token": "e" * 64, "managed": self.configuration}
        heartbeat = MagicMock()
        heartbeat.__enter__.return_value = heartbeat
        heartbeat.__exit__.return_value = False
        opened_log = MagicMock()
        opened_log.__exit__.return_value = False
        with ExitStack() as stack:
            stack.enter_context(patch.object(guardian.sys, "platform", "linux"))
            stack.enter_context(patch.object(guardian.os, "waitid", create=True))
            stack.enter_context(patch.object(guardian.os, "WNOWAIT", 0, create=True))
            stack.enter_context(patch.object(guardian, "inherited_spec_pipe", return_value=300))
            stack.enter_context(patch.object(guardian, "read_private_spec", return_value=specification))
            stack.enter_context(patch.object(guardian, "Thread"))
            stack.enter_context(patch.object(guardian.desktop_security, "private_directory"))
            stack.enter_context(patch.object(guardian.desktop_security, "protect_new_file"))
            stack.enter_context(patch.object(guardian.socket, "socket", return_value=heartbeat))
            stack.enter_context(patch.object(guardian.os, "open", return_value=301))
            stack.enter_context(patch.object(guardian.os, "fdopen", return_value=opened_log))
            events = stack.enter_context(patch.object(guardian, "ManagedEvents",
                                                     side_effect=RuntimeError("event writer startup failed")))
            engine = stack.enter_context(patch.object(guardian.desktop_managed, "ManagedEngine"))
            popen = stack.enter_context(patch.object(guardian.subprocess, "Popen"))
            with self.assertRaisesRegex(RuntimeError, "event writer startup failed"):
                guardian.run()
            events.assert_called_once_with()
            engine.assert_not_called()
            engine.return_value.start.assert_not_called()
            popen.assert_not_called()
            heartbeat.__exit__.assert_called_once()
            opened_log.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
