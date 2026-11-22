# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from io import BytesIO
import unittest

import wire_framing


class FramingTests(unittest.TestCase):
    def test_utf8_body_is_read_exactly_without_eof_or_consuming_a_second_frame(self):
        raw = '{"message":"é🙂"}'.encode("utf-8")
        packet = wire_framing.pack(raw, 100)
        stream = BytesIO(packet + b"not this request")
        self.assertEqual(wire_framing.read(stream, 100), raw)
        self.assertEqual(stream.read(), b"not this request")

        class NoEOF(BytesIO):
            def read(self, size=-1):
                if self.tell() == len(packet):
                    raise AssertionError("The receiver waited for EOF after its complete frame.")
                return super().read(min(size, 3))

        self.assertEqual(wire_framing.read(NoEOF(packet), 100), raw)

    def test_truncated_malformed_and_oversize_headers_or_bodies_are_rejected(self):
        invalid = (b"", b"00000002", b"0000000G\n{}", b"0000000A\n0123456789",
                   b"00000002\r{}", b"00000002\n{", b"00000100\n")
        for packet in invalid:
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                wire_framing.read(BytesIO(packet), 100)
        with self.assertRaises(ValueError):
            wire_framing.pack(b"x" * 101, 100)
        with self.assertRaises(ValueError):
            wire_framing.pack(b"x" * wire_framing.MAX_WIRE_BYTES, wire_framing.MAX_WIRE_BYTES)


if __name__ == "__main__":
    unittest.main()
