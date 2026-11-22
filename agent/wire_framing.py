# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bounded single-request frames that do not depend on OpenSSH stdin EOF."""

import re

HEADER_BYTES = 9
MAX_WIRE_BYTES = 512 * 1024
HEADER = re.compile(rb"[0-9a-f]{8}\n")


def pack(raw, maximum):
    if (not isinstance(raw, bytes) or len(raw) > maximum
            or len(raw) + HEADER_BYTES > MAX_WIRE_BYTES):
        raise ValueError("The framed request exceeds its transport size limit.")
    return f"{len(raw):08x}\n".encode("ascii") + raw


def exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        block = stream.read(min(8192, size - len(parts)))
        if not block:
            raise ValueError("Incomplete framed request.")
        parts.extend(block)
    return bytes(parts)


def read(stream, maximum):
    header = exact(stream, HEADER_BYTES)
    if not HEADER.fullmatch(header):
        raise ValueError("Invalid framed request header.")
    size = int(header[:8], 16)
    if size > maximum or size + HEADER_BYTES > MAX_WIRE_BYTES:
        raise ValueError("The framed request exceeds its transport size limit.")
    return exact(stream, size)
