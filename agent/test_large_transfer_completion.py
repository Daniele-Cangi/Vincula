# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Completion races against protocol-shaped endpoints; no node or database I/O."""
import asyncio
from collections import OrderedDict
from copy import deepcopy
import unittest
from unittest.mock import patch

import server
from test_large_transfer_server import Endpoints


TERMINAL_IO_ERROR = "Endpoint I/O is stopped or terminal."


class LargeTransferCompletionTests(unittest.TestCase):
    def setUp(self):
        self.network = Endpoints(count=1)
        self.manifest = self.network.manifest
        self.exchange_hook = None
        for mocker in (
                patch.object(server, "large_exchange", side_effect=self.exchange),
                patch.object(server, "_large_cursors", OrderedDict()),
                patch.object(server.jobs, "dispatch", side_effect=AssertionError("No real coordinator database")),
                patch.object(server, "destination_request", side_effect=AssertionError("No real node transport"))):
            mocker.start()
            self.addCleanup(mocker.stop)

    def exchange(self, node, operation, **fields):
        result = self.network.exchange(node, operation, **fields)
        if self.exchange_hook is not None:
            replacement = self.exchange_hook(node, operation)
            if replacement is not None:
                return replacement
        return result

    def publish(self):
        destination = self.network.destination
        destination.update(state="published", publication_committed=True, publication_epoch=destination["epoch"],
                           receipt=dict(self.network.header, import_id=destination["id"], published=True))

    def advance(self):
        return asyncio.run(server._advance_large(self.manifest, 1))

    def interrupt_after(self, operation, error=TERMINAL_IO_ERROR, *, publish=True):
        def interrupt(node, name):
            if node == self.network.destination_node and name == operation:
                if publish:
                    self.publish()
                raise server.ToolError(error)
        self.exchange_hook = interrupt

    def test_publishing_waits_without_reserving_binding_or_queueing_again(self):
        self.network.destination["state"] = "publishing"
        report = self.advance()
        self.assertEqual(report.state, "publishing")
        self.assertEqual(report.bytes_copied_this_call, 0)
        self.assertEqual(report.blocks_checked_this_call, 0)
        self.assertEqual([operation for _, operation, _, _ in self.network.calls], ["status", "status"])

    def test_publication_between_report_and_reservation_recovers_the_durable_receipt(self):
        self.interrupt_after("reserve")
        report = self.advance()
        self.assertEqual(report.state, "completed")
        self.assertEqual(report.destination["receipt"], self.network.destination["receipt"])
        self.assertTrue(report.publication_may_exist)
        self.assertEqual(report.bytes_copied_this_call, 0)
        self.assertEqual(self.network.operations("block-write"), [])
        self.assertEqual(self.network.operations("queue"), [])
        self.assertEqual([side for side, _ in self.network.operations("reserve")], ["source", "destination"])

    def test_bind_observes_another_coordinator_publication_without_copying_again(self):
        for phase in ("publishing", "published"):
            with self.subTest(phase=phase):
                self.network.destination.update(state="receiving", receipt=None, publication_committed=False)
                self.network.calls.clear()
                def observe_publication(node, operation):
                    if node == self.network.destination_node and operation == "bind":
                        if phase == "published":
                            self.publish()
                        else:
                            self.network.destination["state"] = phase
                        return deepcopy(self.network.destination), b""
                    return None
                self.exchange_hook = observe_publication
                report = self.advance()
                self.assertEqual(report.state, "completed" if phase == "published" else "publishing")
                self.assertEqual(report.bytes_copied_this_call, 0)
                self.assertEqual(self.network.operations("block-read"), [])
                self.assertEqual(self.network.operations("block-write"), [])
                self.assertEqual(self.network.operations("queue"), [])

    def test_completion_reconciliation_preserves_acknowledged_copy_counters(self):
        self.interrupt_after("queue")
        report = self.advance()
        self.assertEqual(report.state, "completed")
        self.assertEqual(report.bytes_copied_this_call, len(self.network.block))
        self.assertEqual(report.blocks_checked_this_call, 1)
        self.assertEqual(report.coordinator_next_block, 1)
        self.assertEqual(len(self.network.operations("block-write")), 1)
        self.assertEqual(len(self.network.operations("queue")), 1)

    def test_authentication_transport_and_integrity_errors_are_not_hidden_by_a_receipt(self):
        for error in ("Permission denied (publickey).",
                      "Large-file service unavailable. Preserve the transfer handle and retry after checking its status.",
                      "The destination did not confirm the expected block."):
            with self.subTest(error=error):
                self.network.destination.update(state="receiving", receipt=None, publication_committed=False)
                self.network.calls.clear()
                self.interrupt_after("reserve", error)
                with self.assertRaises(server.ToolError) as caught:
                    self.advance()
                self.assertEqual(str(caught.exception), error)
                self.assertEqual(self.network.operations("block-write"), [])

    def test_terminal_rejection_without_a_receipt_does_not_invent_completion(self):
        self.interrupt_after("reserve", publish=False)
        with self.assertRaises(server.ToolError) as caught:
            self.advance()
        self.assertEqual(str(caught.exception), TERMINAL_IO_ERROR)
        self.assertIsNone(self.network.destination["receipt"])
        self.assertEqual(self.network.operations("block-write"), [])

    def test_reconciliation_still_rejects_a_corrupt_receipt(self):
        def corrupt_receipt(node, operation):
            if node == self.network.destination_node and operation == "reserve":
                self.publish()
                self.network.destination["receipt"]["sha256"] = "0" * 64
                raise server.ToolError(TERMINAL_IO_ERROR)
        self.exchange_hook = corrupt_receipt
        with self.assertRaisesRegex(server.ToolError, "receipt differs"):
            self.advance()

    def test_reconciliation_still_rejects_cross_endpoint_content_disagreement(self):
        original = deepcopy(self.network.destination["header"])
        def corrupt_binding(node, operation):
            if node == self.network.destination_node and operation == "reserve":
                self.publish()
                self.network.destination["header"]["sha256"] = "0" * 64
                self.network.destination["receipt"]["sha256"] = "0" * 64
                raise server.ToolError(TERMINAL_IO_ERROR)
        self.exchange_hook = corrupt_binding
        with self.assertRaisesRegex(server.ToolError, "bindings disagree"):
            self.advance()
        self.assertEqual(self.network.source["header"], original)


if __name__ == "__main__":
    unittest.main()
