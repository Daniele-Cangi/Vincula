# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol-shaped fake endpoints; real server orchestration, no node or database I/O."""
import asyncio
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from threading import RLock
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import large_transfer_plans as plans
import node_registry
import server


class Endpoints:
    def __init__(self, count=12):
        self.authority, self.source_node, self.destination_node = [str(uuid4()) for _ in range(3)]
        self.source_state, self.destination_state = str(uuid4()), str(uuid4())
        self.transfer_id = str(uuid4())
        request = {"source": {"node_id": self.source_node, "root": "documents", "path": "input.bin"},
                   "destination": {"node_id": self.destination_node, "root": "incoming", "path": "output.bin"},
                   "max_bytes": max(1, count * plans.BLOCK_SIZE), "ttl_seconds": 3600}
        self.manifest = plans.make_manifest(self.transfer_id, self.authority, request,
                                           self.source_state, self.destination_state, 1000)
        self.block = b"x" * plans.BLOCK_SIZE
        digest = sha256(self.block).hexdigest()
        self.blocks = [{"index": index, "size": plans.BLOCK_SIZE, "sha256": digest} for index in range(count)]
        whole, root = sha256(), sha256()
        for block in self.blocks:
            whole.update(self.block)
            root.update(f"{block['index']}:{block['size']}:{block['sha256']}\n".encode("ascii"))
        self.header = {"version": 2, "export_id": self.manifest["plan"]["source_id"],
                       "request_sha256": "a" * 64, "size": count * plans.BLOCK_SIZE,
                       "sha256": whole.hexdigest(), "block_size": plans.BLOCK_SIZE,
                       "block_count": count, "blocks_sha256": root.hexdigest()}
        self.source = self.status("source", "ready", "a" * 64)
        self.destination = self.status("destination", "receiving", "b" * 64)
        self.valid, self.offline, self.calls = set(), set(), []
        self.denied_reservations = set()
        self.ask_bind = False
        self.returned_block = self.written_block = None
        self.lock = RLock()

    def status(self, side, state, digest):
        source = side == "source"
        key = self.manifest["plan"]["source_id" if source else "destination_id"]
        return {"id": key, "version": 2,
                "spec": plans.endpoint_spec(self.manifest, "export" if source else "import", None if source else "a" * 64),
                "state": state, "epoch": 1, "request_sha256": digest, "approval_stage": None,
                "approval_sha256": None, "expired": False, "header": deepcopy(self.header), "receipt": None,
                "publication_committed": False, "publication_epoch": None,
                "reserved_bytes": self.header["size"] * (1 if source else 2),
                "verified_blocks": 0, "verified_bytes": 0}

    def exchange(self, node, operation, *, payload=b"", request_id=None, **fields):
        with self.lock:
            side = "source" if node == self.source_node else "destination" if node == self.destination_node else None
            if side is None:
                raise AssertionError("Only the two fake endpoint nodes may be contacted.")
            self.calls.append((side, operation, deepcopy(fields), len(payload)))
            if side in self.offline:
                raise server.ToolError("Synthetic endpoint unavailable.")
            current = self.source if side == "source" else self.destination
            if operation == "request":
                if fields != {"spec": current["spec"]}:
                    raise AssertionError("Request differs from the immutable endpoint specification.")
                return deepcopy(current), b""
            if request_id != current["id"] or fields.pop("expected_state_id", None) != self.manifest["plan"][side + "_state_id"]:
                raise AssertionError("Every endpoint RPC must bind its endpoint and durable state.")
            epoch = fields.pop("epoch", None)
            if operation not in {"status", "blocks"} and epoch != current["epoch"]:
                raise AssertionError("Mutating/block RPC must carry its current endpoint epoch.")
            if operation == "status":
                if fields or payload:
                    raise AssertionError("Status is metadata only.")
                return deepcopy(current), b""
            if operation == "reserve":
                allowed = side not in self.denied_reservations
                return {"reservation": {"reserved": allowed, "reason": None if allowed else "disk_budget"}}, b""
            if operation == "bind":
                if fields["header"] != self.header:
                    raise AssertionError("Bind must preserve exact source content.")
                self.destination["header"] = deepcopy(self.header)
                if self.ask_bind:
                    self.destination.update(approval_stage="content", approval_sha256="c" * 64, state="content_pending")
                return deepcopy(self.destination), b""
            if operation == "blocks":
                offset, limit = fields["offset"], fields["limit"]
                # Metadata pagination does not mutate endpoint state and does
                # not carry epoch in the server's request.
                values = self.blocks[offset:offset + limit]
                return {"blocks": deepcopy(values),
                        "next_offset": offset + limit if offset + limit < len(self.blocks) else None}, b""
            if operation == "verify-block":
                index = fields["index"]
                if fields["expected"] != self.blocks[index]:
                    raise AssertionError("Verification is bound to immutable metadata.")
                return {"valid": index in self.valid}, b""
            if operation == "block-read":
                return {"block": deepcopy(self.returned_block or self.blocks[fields["index"]])}, self.block
            if operation == "block-write":
                index = fields["index"]
                if payload != self.block or fields["expected"] != self.blocks[index]:
                    raise AssertionError("Only matching, bounded fake block bytes may be delivered.")
                self.valid.add(index)
                self.destination.update(verified_blocks=len(self.valid), verified_bytes=len(self.valid) * plans.BLOCK_SIZE)
                return {"block": deepcopy(self.written_block or self.blocks[index])}, b""
            if operation == "queue":
                current["state"] = "publishing" if side == "destination" else "preparing"
                return deepcopy(current), b""
            if operation == "stop":
                current.update(state="stopped", epoch=current["epoch"] + 1)
                return deepcopy(current), b""
            if operation == "resume":
                current.update(state="ready" if side == "source" else "receiving", epoch=current["epoch"] + 1)
                return deepcopy(current), b""
            if operation == "cleanup":
                current["state"] = "cleaned"
                return deepcopy(current), b""
            raise AssertionError("Unexpected fake operation: " + operation)

    def operations(self, operation):
        return [(side, fields) for side, name, fields, _ in self.calls if name == operation]


class LargeTransferServerTests(unittest.TestCase):
    def setUp(self):
        self.network = Endpoints()
        self.manifest = self.network.manifest
        topology = node_registry.Registry(self.network.authority, [node_registry.Node(key, key, "linux")
            for key in (self.network.authority, self.network.source_node, self.network.destination_node)])
        for mocker in (patch.object(server, "large_exchange", side_effect=self.network.exchange),
                       patch.object(server, "registry", return_value=topology),
                       patch.object(server, "_large_cursors", OrderedDict()),
                       patch.object(server, "known_large", AsyncMock(return_value=self.manifest)),
                       patch.object(server.jobs, "dispatch", side_effect=AssertionError("No real coordinator database")),
                       patch.object(server, "destination_request", side_effect=AssertionError("No real destination transport"))):
            mocker.start()
            self.addCleanup(mocker.stop)

    def advance(self, maximum):
        return asyncio.run(server._advance_large(self.manifest, maximum))

    def test_existing_and_missing_blocks_share_one_bounded_examination_budget(self):
        self.network.valid.update({0, 1})
        report = self.advance(3)
        self.assertEqual([fields["index"] for _, fields in self.network.operations("verify-block")], [0, 1, 2])
        self.assertEqual([fields["index"] for _, fields in self.network.operations("block-read")], [2])
        self.assertEqual(report.blocks_checked_this_call, 3)
        self.assertEqual(report.bytes_copied_this_call, plans.BLOCK_SIZE)
        self.assertEqual(report.coordinator_next_block, 3)

    def test_same_process_cursor_does_not_reverify_entire_prefix_on_every_call(self):
        self.network.valid.update(range(len(self.network.blocks)))
        first, second, third = self.advance(3), self.advance(3), self.advance(3)
        self.assertEqual([fields["index"] for _, fields in self.network.operations("verify-block")], list(range(9)))
        self.assertEqual([first.coordinator_next_block, second.coordinator_next_block, third.coordinator_next_block], [3, 6, 9])
        self.assertEqual(self.network.operations("block-write"), [])

    def test_new_coordinator_or_resumed_endpoint_revalidates_the_prefix(self):
        self.network.valid.update(range(len(self.network.blocks)))
        self.advance(3)
        server._large_cursors.clear()  # A new process starts with no performance hints.
        self.network.calls.clear()
        self.advance(2)
        self.assertEqual([fields["index"] for _, fields in self.network.operations("verify-block")], [0, 1])
        self.network.destination["epoch"] += 1
        self.network.calls.clear()
        self.advance(1)
        self.assertEqual([fields["index"] for _, fields in self.network.operations("verify-block")], [0])

    def test_zero_budget_can_bind_but_cannot_copy_or_verify_blocks(self):
        report = self.advance(0)
        self.assertEqual(report.blocks_checked_this_call, 0)
        self.assertEqual(self.network.operations("verify-block"), [])
        self.assertEqual(self.network.operations("block-write"), [])

    def test_source_preparation_waits_for_both_disk_reservations(self):
        self.network.source["header"] = None
        self.network.destination["header"] = None
        self.network.denied_reservations.add("destination")
        self.advance(8)
        self.assertEqual([side for side, _ in self.network.operations("reserve")], ["source", "destination"])
        self.assertEqual(self.network.operations("queue"), [])
        self.network.denied_reservations.clear()
        self.network.calls.clear()
        report = self.advance(8)
        self.assertEqual([side for side, _ in self.network.operations("queue")], ["source"])
        self.assertEqual(self.network.operations("block-read"), [])
        self.assertEqual(report.state, "preparing")

    def test_ask_intent_and_content_pause_before_any_data_delivery(self):
        self.network.source.update(approval_stage="intent", state="pending", approval_sha256="a" * 64)
        self.assertEqual(self.advance(8).state, "awaiting_approval")
        self.assertEqual(self.network.operations("reserve"), [])
        self.network.source.update(approval_stage=None, state="ready", approval_sha256=None)
        self.network.ask_bind = True
        self.assertEqual(self.advance(8).state, "awaiting_approval")
        self.assertEqual(self.network.operations("block-read"), [])

    def test_final_block_queues_publication_once_and_reports_none_receipt_as_in_progress(self):
        self.network.valid.update(range(len(self.network.blocks)))
        self.advance(8)
        report = self.advance(8)
        self.assertEqual(len(self.network.operations("queue")), 1)
        self.assertEqual(report.state, "publishing")
        self.assertIsNone(report.destination["receipt"])

    def test_stop_contacts_reachable_peer_even_when_the_other_peer_is_offline(self):
        self.network.offline.add("source")
        report = asyncio.run(server.large_control(self.manifest["id"], self.manifest["origin"], "stop"))
        self.assertEqual([side for side, _ in self.network.operations("stop")], ["destination"])
        self.assertEqual(report.destination["state"], "stopped")
        self.assertIn("source", report.errors)
        self.assertEqual(report.state, "unavailable")

    def test_stop_after_publication_permit_exposes_uncertainty_without_inventing_receipt(self):
        self.network.destination.update(publication_committed=True, publication_epoch=1)
        report = asyncio.run(server.large_control(self.manifest["id"], self.manifest["origin"], "stop"))
        self.assertEqual(report.state, "stopped")
        self.assertTrue(report.publication_may_exist)
        self.assertIsNone(report.destination["receipt"])
        self.assertIn("absent receipt does not prove", report.stop_semantics)

    def test_wrong_block_metadata_stops_delivery_and_does_not_advance_cursor(self):
        self.network.returned_block = dict(self.network.blocks[0], sha256="f" * 64)
        with self.assertRaises(server.ToolError):
            self.advance(1)
        self.assertEqual(self.network.operations("block-write"), [])
        self.assertEqual(len(server._large_cursors), 0)

    def test_bad_write_ack_retries_by_verification_without_duplicate_delivery(self):
        self.network.written_block = dict(self.network.blocks[0], index=1)
        with self.assertRaises(server.ToolError):
            self.advance(1)
        self.assertEqual(len(server._large_cursors), 0)
        self.assertEqual(self.network.valid, {0})  # The remote write itself committed.
        self.network.written_block = None
        self.network.calls.clear()
        report = self.advance(1)
        self.assertEqual(report.coordinator_next_block, 1)
        self.assertEqual(report.bytes_copied_this_call, 0)
        self.assertEqual(self.network.operations("block-write"), [])
        self.assertEqual(self.network.operations("block-read"), [])

    def test_corrupt_or_noncanonical_receipt_never_claims_completion(self):
        valid = dict(self.network.header, import_id=self.network.destination["id"], published=True)
        self.network.destination["state"] = "published"
        with self.assertRaises(server.ToolError):
            asyncio.run(server.large_report(self.manifest))
        for receipt in ({}, [], False, 0, {"published": True}, dict(valid, sha256="0" * 64), dict(valid, published=1)):
            with self.subTest(receipt=receipt):
                self.network.destination["receipt"] = receipt
                with self.assertRaises(server.ToolError):
                    asyncio.run(server.large_report(self.manifest))
        self.network.destination["receipt"] = valid
        self.assertEqual(asyncio.run(server.large_report(self.manifest)).state, "completed")

    def test_cross_endpoint_request_and_content_bindings_must_agree(self):
        original = deepcopy(self.network.destination)
        for mismatch in ("source_request", "content_header"):
            with self.subTest(mismatch=mismatch):
                self.network.destination = deepcopy(original)
                if mismatch == "source_request":
                    self.network.destination["spec"]["source_sha256"] = "d" * 64
                    self.network.destination["header"]["request_sha256"] = "d" * 64
                else:
                    self.network.destination["header"]["sha256"] = "f" * 64
                with self.assertRaisesRegex(server.ToolError, "bindings disagree"):
                    asyncio.run(server.large_report(self.manifest))
                self.assertEqual(self.network.operations("queue"), [])
                self.assertEqual(self.network.operations("block-write"), [])

    def test_recovered_receipt_is_verified_against_header_even_when_source_is_offline(self):
        self.network.offline.add("source")
        valid = dict(self.network.header, import_id=self.network.destination["id"], published=True)
        self.network.destination.update(state="published", receipt=dict(valid, sha256="0" * 64))
        with self.assertRaisesRegex(server.ToolError, "receipt differs"):
            asyncio.run(server.large_report(self.manifest))
        self.network.destination["receipt"] = valid
        recovered = asyncio.run(server.large_report(self.manifest))
        self.assertEqual(recovered.state, "completed")
        self.assertIn("source", recovered.errors)
        self.network.destination["header"]["request_sha256"] = "d" * 64
        rejected = asyncio.run(server.large_report(self.manifest))
        self.assertEqual(rejected.state, "unavailable")
        self.assertIn("destination", rejected.errors)

    def test_replay_preserves_manifest_and_rejects_changed_budget_before_endpoint_effects(self):
        source = server.LargeFileEndpoint(**self.manifest["request"]["source"])
        destination = server.LargeFileEndpoint(**self.manifest["request"]["destination"])
        with patch.object(server, "large_manifest", AsyncMock(return_value=(self.manifest, False))), \
                patch.object(server, "publish_plan", AsyncMock(return_value={})):
            result = asyncio.run(server.start_large_transfer(self.manifest["id"], self.manifest["origin"],
                source, destination, self.manifest["request"]["max_bytes"], self.manifest["request"]["ttl_seconds"]))
            self.assertEqual(result.manifest, self.manifest)
            self.network.calls.clear()
            with self.assertRaises(server.ToolError):
                asyncio.run(server.start_large_transfer(self.manifest["id"], self.manifest["origin"],
                    source, destination, self.manifest["request"]["max_bytes"] + 1, self.manifest["request"]["ttl_seconds"]))
            self.assertEqual(self.network.calls, [])

    def test_replica_keeps_original_authority_when_another_origin_reads_it(self):
        record = plans.package(self.manifest)
        with patch.object(server.jobs, "dispatch", return_value=None), \
                patch.object(server, "destination_request", return_value=record), \
                patch.object(server, "find_plan_manifest", AsyncMock(return_value=self.manifest)):
            recovered, reachable = asyncio.run(server.large_manifest(self.manifest["id"], self.manifest["origin"]))
            self.assertEqual(recovered, self.manifest)
            self.assertTrue(reachable)
            with self.assertRaises(server.ToolError):
                asyncio.run(server.large_manifest(self.manifest["id"], self.network.destination_node))
            with patch.object(server, "destination_request", side_effect=server.ToolError("Authority offline")):
                recovered, reachable = asyncio.run(server.large_manifest(self.manifest["id"], self.manifest["origin"]))
            self.assertEqual(recovered, self.manifest)
            self.assertFalse(reachable)


if __name__ == "__main__":
    unittest.main()
