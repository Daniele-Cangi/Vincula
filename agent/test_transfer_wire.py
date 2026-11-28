# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bounded binary frames and fixed SSH commands; no remote nodes or credentials."""

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import unittest
from unittest.mock import call, patch

import jobs
import node_ssh
import transfer_wire as wire
import wire_framing


def framed_header(value):
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return f"{len(raw):08x}\n".encode("ascii") + raw


def envelope():
    return dict(protocol_version=2, metadata={}, payload_size=0,
                payload_sha256=sha256(b"").hexdigest())


class TransferFramingTests(unittest.TestCase):
    def test_binary_and_utf8_metadata_read_exactly_without_eof_or_next_frame(self):
        metadata = {"path": 'é🙂/Windows\\quote".bin', "index": 4, "size": 256}
        payload = bytes(range(256))
        packet = wire.pack(metadata, payload)
        metadata_size = int(packet[:8], 16)
        self.assertEqual(packet[8:9], b"\n")
        self.assertEqual(json.loads(packet[9:9 + metadata_size])["metadata"], metadata)
        stream = BytesIO(packet + b"next-frame")
        self.assertEqual(wire.read(stream), (metadata, payload))
        self.assertEqual(stream.read(), b"next-frame")

        class NoEOF(BytesIO):
            def read(self, size=-1):
                if self.tell() == len(packet):
                    raise AssertionError("The complete frame waited for EOF")
                if size < 0:
                    raise AssertionError("Unbounded frame read")
                return super().read(min(size, 7))

        self.assertEqual(wire.read(NoEOF(packet)), (metadata, payload))

    def test_maximum_block_and_empty_error_reply_roundtrip(self):
        payload = b"\x00\xff\n\x80" * (wire.BLOCK_BYTES // 4)
        metadata = {"result": {"index": 0, "size": len(payload), "sha256": sha256(payload).hexdigest()}}
        self.assertEqual(wire.read(BytesIO(wire.pack(metadata, payload))), (metadata, payload))
        reply = {"error": "Transfer is cancelled."}
        self.assertEqual(wire.read(BytesIO(wire.pack(reply))), (reply, b""))

    def test_split_header_then_payload_preserves_metadata_and_integrity(self):
        payload = b"\x00\xfe\xff\x80"
        stream = BytesIO(wire.pack({"index": 13, "size": 4}, payload))
        header = wire.read_header(stream)
        self.assertEqual(header["protocol_version"], 2)
        self.assertEqual(header["metadata"], {"index": 13, "size": 4})
        self.assertEqual(header["payload_size"], 4)
        self.assertEqual(header["payload_sha256"], sha256(payload).hexdigest())
        self.assertEqual(wire.read_payload(stream, header), payload)

    def test_truncated_malformed_and_oversize_length_headers_are_rejected(self):
        packets = [b"", b"00000002", b"0000000A\n0123456789", b"0000000g\n", b"00000002\r{}",
                   b"00000002\n{", b"00000000\n", f"{wire.MAX_METADATA_BYTES + 1:08x}\n".encode()]
        for packet in packets:
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                wire.read(BytesIO(packet))

    def test_oversize_header_is_rejected_before_reading_its_body(self):
        class HeaderOnly(BytesIO):
            def read(self, size=-1):
                if self.tell() >= wire.HEADER_BYTES:
                    raise AssertionError("Oversize metadata body was read")
                return super().read(size)

        with self.assertRaises(ValueError):
            wire.read_header(HeaderOnly(f"{wire.MAX_METADATA_BYTES + 1:08x}\n".encode()))

    def test_metadata_schema_types_versions_and_digest_are_strict(self):
        base = envelope()
        malformed = [[], {}, dict(base, extra=True), {key: value for key, value in base.items() if key != "metadata"}]
        for key, value in (("protocol_version", 1), ("protocol_version", 2.0), ("protocol_version", True),
                           ("metadata", []), ("payload_size", True), ("payload_size", -1),
                           ("payload_size", wire.BLOCK_BYTES + 1), ("payload_sha256", "A" * 64),
                           ("payload_sha256", "f" * 63), ("payload_sha256", None)):
            malformed.append(dict(base, **{key: value}))
        for header in malformed:
            with self.subTest(header=header), self.assertRaises(ValueError):
                wire.read_header(BytesIO(framed_header(header)))

    def test_invalid_json_utf8_duplicate_keys_and_nonfinite_values_are_rejected(self):
        good = json.dumps(envelope(), separators=(",", ":")).encode()
        invalid = [b"{", b"\xff", good.replace(b'"metadata":{}', b'"metadata":{"x":1,"x":2}'),
                   good.replace(b'"metadata":{}', b'"metadata":{},"metadata":{}'),
                   good.replace(b'"metadata":{}', b'"metadata":{"x":NaN}'),
                   good.replace(b'"metadata":{}', b'"metadata":{"x":Infinity}'),
                   good.replace(b'"metadata":{}', b'"metadata":{"x":1e999}')]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                wire.read_header(BytesIO(f"{len(raw):08x}\n".encode() + raw))

    def test_truncated_or_changed_binary_and_forged_payload_bounds_are_rejected(self):
        packet = wire.pack({"index": 0}, b"abcdef")
        for raw in (packet[:-1], packet[:-1] + b"X"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                wire.read(BytesIO(raw))
        header = wire.read_header(BytesIO(packet))
        for value in (-1, True, wire.BLOCK_BYTES + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wire.read_payload(BytesIO(), dict(header, payload_size=value))

    def test_pack_rejects_unbounded_or_nonbinary_body_and_invalid_metadata(self):
        for metadata, payload in (({}, b"x" * (wire.BLOCK_BYTES + 1)), ({}, "not binary"), ({}, bytearray(b"x")),
                                  ([], b""), ({"x": "x" * wire.MAX_METADATA_BYTES}, b""),
                                  ({"x": float("nan")}, b""), ({"x": "\ud800"}, b"")):
            with self.subTest(kind=type(payload).__name__), self.assertRaises(ValueError):
                wire.pack(metadata, payload)

    def test_write_handles_partial_writes_and_rejects_stalled_writers(self):
        class ShortWriter(BytesIO):
            def write(self, data):
                return super().write(data[:3])

        stream = ShortWriter()
        wire.write(stream, {"path": "é🙂"}, b"\x00\xff")
        self.assertEqual(wire.read(BytesIO(stream.getvalue())), ({"path": "é🙂"}, b"\x00\xff"))
        for result in (None, 0, -1, True, wire.MAX_FRAME_BYTES + 1):
            with self.subTest(result=result), patch.object(stream, "write", return_value=result), self.assertRaises(ValueError):
                wire.write(stream, {})

    def test_legacy_transport_remains_512_kib_and_is_not_used_for_binary_blocks(self):
        self.assertEqual(wire_framing.MAX_WIRE_BYTES, 512 * 1024)
        with self.assertRaises(ValueError):
            wire_framing.pack(b"x" * (512 * 1024), 512 * 1024)
        payload = b"x" * (512 * 1024 + 1)
        self.assertEqual(wire.read(BytesIO(wire.pack({}, payload)))[1], payload)


class TransferRemoteTests(unittest.TestCase):
    def test_operator_spool_directory_is_used_for_both_files_and_files_close_on_failure(self):
        streams = []

        def failed(route, program, flags, args, output, **options):
            streams.extend((options["stdin"], output))
            raise subprocess.TimeoutExpired("fixed ssh", 30)

        with TemporaryDirectory(prefix="vincula-transfer-spool-") as spool, \
                patch.object(wire, "TemporaryFile", wraps=TemporaryFile) as create, \
                patch.object(node_ssh, "run", side_effect=failed):
            with self.assertRaises(subprocess.TimeoutExpired):
                wire.remote_exchange(object(), "linux", {}, spool_dir=spool)
            self.assertEqual(create.call_args_list, [call(dir=spool), call(dir=spool)])
        self.assertEqual(len(streams), 2)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_both_platforms_use_fixed_commands_and_regular_binary_stdin_stdout(self):
        route = object()
        for family in ("linux", "windows"):
            commands = []
            for index, payload in enumerate((b"", bytes(range(256)) * 3)):
                request = {"action": "large-files", "protocol_version": 2, "operation": "block-write",
                           "path": "é🙂/'$(touch NEVER)'\\file.bin", "index": index, "size": len(payload),
                           "sha256": sha256(payload).hexdigest()}
                reply = {"result": {"index": index, "size": len(payload), "sha256": sha256(payload).hexdigest()}}

                def exchange(actual_route, program, flags, args, output, **options):
                    self.assertIs(actual_route, route)
                    self.assertEqual(program, "ssh")
                    self.assertEqual(flags, ("-T",))
                    self.assertEqual(options["timeout"], 30)
                    self.assertTrue(stat.S_ISREG(os.fstat(output.fileno()).st_mode))
                    self.assertTrue(stat.S_ISREG(os.fstat(options["stdin"].fileno()).st_mode))
                    self.assertEqual(wire.read(options["stdin"]), (request, payload))
                    self.assertEqual(args, (wire.fixed_command(family),))
                    self.assertNotIn(request["path"], args[0])
                    self.assertNotIn("NEVER", args[0])
                    commands.append(args[0])
                    output.write(wire.pack(reply, payload))

                with patch.object(node_ssh, "run", side_effect=exchange):
                    self.assertEqual(wire.remote_exchange(route, family, request, payload), (reply, payload))
            self.assertEqual(commands, [commands[0]] * 2)
            if family == "windows":
                self.assertIn("py.exe -3 -IB -c", commands[0])
                self.assertIn("os.environ['LOCALAPPDATA']", commands[0])
                self.assertNotIn("powershell", commands[0].lower())
                self.assertNotIn("base64", commands[0].lower())
            else:
                self.assertEqual(commands[0], 'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" large-files-stdin')

    def test_unknown_platform_and_oversize_request_never_invoke_ssh(self):
        with patch.object(node_ssh, "run") as execute:
            with self.assertRaises(ValueError):
                wire.remote_exchange(object(), "unknown", {})
            with self.assertRaises(ValueError):
                wire.remote_exchange(object(), "linux", {}, b"x" * (wire.BLOCK_BYTES + 1))
            execute.assert_not_called()

    def test_response_acceptance_cap_is_checked_before_parsing(self):
        def oversized(route, program, flags, args, output, **options):
            output.write(b"x" * (wire.MAX_FRAME_BYTES + 1))

        with patch.object(node_ssh, "run", side_effect=oversized), \
                patch.object(wire, "read", side_effect=AssertionError("Oversize response was parsed")):
            with self.assertRaisesRegex(ValueError, "response exceeds"):
                wire.remote_exchange(object(), "linux", {})

    def test_trailing_truncated_or_tampered_response_is_never_accepted(self):
        valid = wire.pack({"result": {"index": 2}}, b"\x00\xff")
        for response in (valid + b"unexpected", valid[:-1], valid[:-1] + b"X", b"00000002\n{}"):
            def emit(route, program, flags, args, output, **options):
                output.write(response)

            with self.subTest(response=response), patch.object(node_ssh, "run", side_effect=emit), self.assertRaises(ValueError):
                wire.remote_exchange(object(), "windows", {"index": 2})

    def test_cancel_before_and_after_exchange_does_not_parse_or_start_another_request(self):
        class Cancelled(Exception):
            pass

        with patch.object(node_ssh, "run") as execute:
            with self.assertRaises(Cancelled):
                wire.remote_exchange(object(), "linux", {}, check_cancelled=lambda: (_ for _ in ()).throw(Cancelled()))
            execute.assert_not_called()
        calls = []

        def emit(route, program, flags, args, output, **options):
            calls.append("ssh")
            output.write(wire.pack({"result": {}}))

        def after():
            if calls and calls[-1] == "ssh":
                raise Cancelled()
            calls.append("before")

        with patch.object(node_ssh, "run", side_effect=emit), \
                patch.object(wire, "read", side_effect=AssertionError("Cancelled response was parsed")), self.assertRaises(Cancelled):
            wire.remote_exchange(object(), "windows", {}, check_cancelled=after)
        self.assertEqual(calls, ["before", "ssh"])

    def test_network_failure_remains_scoped_and_runs_post_exchange_cancel_check(self):
        checks = []
        failure = subprocess.TimeoutExpired("fixed ssh", 30)
        with patch.object(node_ssh, "run", side_effect=failure), self.assertRaises(subprocess.TimeoutExpired):
            wire.remote_exchange(object(), "linux", {}, check_cancelled=lambda: checks.append(True))
        self.assertEqual(checks, [True, True])


class NativeBinaryStdioTests(unittest.TestCase):
    def test_full_block_python_stdio_finishes_while_stdin_remains_open(self):
        # Exercise native binary stdio (including Windows), with regular stdout
        # instead of a pipe whose inherited writer could hide completion.
        code = ("import runpy,sys;"
                "m=runpy.run_path(sys.argv[1],run_name='transfer_wire_fixture');"
                "metadata,payload=m['read'](sys.stdin.buffer);"
                "m['write'](sys.stdout.buffer,{'reply':metadata},payload);"
                "sys.stdout.buffer.flush()")
        metadata = {"path": "é🙂\\quote\".bin", "index": 7}
        payload = bytes(range(256)) * (wire.BLOCK_BYTES // 256)
        packet = wire.pack(metadata, payload)
        with TemporaryFile() as output, TemporaryFile() as errors:
            with subprocess.Popen([sys.executable, "-I", "-B", "-W", "error", "-c", code,
                                   str(Path(wire.__file__).resolve())],
                                  stdin=subprocess.PIPE, stdout=output, stderr=errors) as process:
                try:
                    process.stdin.write(packet)
                    process.stdin.flush()
                    process.wait(timeout=10)
                    self.assertFalse(process.stdin.closed)
                    errors.seek(0)
                    self.assertEqual(process.returncode, 0, errors.read(4096).decode("utf-8", "replace"))
                    output.seek(0)
                    self.assertEqual(wire.read(output), ({"reply": metadata}, payload))
                finally:
                    if process.poll() is None:
                        process.kill()  # Only the original child created by this fixture.
                        process.wait(timeout=5)


class LargeFilesStdinTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="vincula-large-stdin-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / ("Vincula-Jobs" if jobs.WINDOWS else "jobs")
        change = patch.multiple(jobs, ROOT=self.root, DATABASE=self.root / "jobs.sqlite3")
        change.start()
        self.addCleanup(change.stop)
        jobs.initialize()

    def command(self):
        if jobs.WINDOWS:
            return wire.WINDOWS_COMMAND, dict(os.environ, LOCALAPPDATA=str(self.home))
        # Inject only the test-owned runtime globals, without changing HOME or
        # touching any default-account helper installation.
        code = ("import runpy,sys;from pathlib import Path;"
                "m=runpy.run_path(" + repr(str(self.root / "jobs.py")) + ",run_name='private_binary_receiver');"
                "g=m['main'].__globals__;g['ROOT']=Path(" + repr(str(self.root)) + ");"
                "g['DATABASE']=g['ROOT']/'jobs.sqlite3';"
                "sys.argv=['jobs.py','large-files-stdin'];m['main']()")
        return [sys.executable, "-I", "-B", "-c", code], None

    def counts(self):
        with jobs.transaction() as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {name: db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                    for name in tables}

    def invoke(self, packet, *, keep_open=False):
        command, environment = self.command()
        with TemporaryFile() as output, TemporaryFile() as errors:
            with subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=output, stderr=errors) as process:
                try:
                    process.stdin.write(packet)
                    process.stdin.flush()
                    if not keep_open:
                        process.stdin.close()
                    process.wait(timeout=10)
                    self.assertEqual(process.stdin.closed, not keep_open)
                    errors.seek(0)
                    self.assertEqual(process.returncode, 0, errors.read(4096).decode("utf-8", "replace"))
                    output.seek(0, 2)
                    size = output.tell()
                    output.seek(0)
                    reply, body = wire.read(output)
                    self.assertEqual(output.tell(), size)
                    return reply, body
                finally:
                    if process.poll() is None:
                        process.kill()  # Original disposable fixture child only.
                        process.wait(timeout=5)

    def test_info_real_cli_returns_one_frame_without_waiting_for_stdin_eof(self):
        before = self.counts()
        request = {"action": "large-files", "operation": "info", "protocol_version": 2}
        reply, body = self.invoke(wire.pack(request), keep_open=True)
        self.assertEqual(body, b"")
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["version"], 2)
        self.assertEqual(reply["result"]["block_size"], wire.BLOCK_BYTES)
        self.assertEqual(reply["result"]["mode"], "ask")
        self.assertFalse(reply["result"]["file_worker_fresh"])
        self.assertEqual(self.counts(), before)

    def test_malformed_oversize_wrong_action_and_wrong_protocol_leave_fixture_counts_unchanged(self):
        before = self.counts()
        info = {"action": "large-files", "operation": "info", "protocol_version": 2}
        invalid = [b"00000001\n{", f"{wire.MAX_METADATA_BYTES + 1:08x}\n".encode(),
                   wire.pack({"action": "process", "operation": "info", "protocol_version": 1}),
                   wire.pack(dict(info, protocol_version=1)), wire.pack(info, b"unexpected"),
                   wire.pack(info, b"binary")[:-1] + b"X",
                   framed_header(dict(envelope(), payload_size=wire.BLOCK_BYTES + 1))]
        for packet in invalid:
            with self.subTest(prefix=packet[:9]):
                reply, body = self.invoke(packet)
                self.assertIn("error", reply)
                self.assertEqual(body, b"")
                self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
