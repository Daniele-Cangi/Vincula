# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Single bounded binary block over an existing operator-owned SSH route.

The SSH helper is trusted to emit bounded frames. A regular stdout temporary
file avoids Windows OpenSSH pipe lifetime problems; the response size is
checked before parsing/acceptance, not enforced as an OS disk-write quota.
This protocol does not alter the legacy wire_framing transport's limits.
"""

from hashlib import sha256
import json
import math
import re
from tempfile import TemporaryFile

PROTOCOL_VERSION = 2
HEADER_BYTES = 9
BLOCK_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_FRAME_BYTES = HEADER_BYTES + MAX_METADATA_BYTES + BLOCK_BYTES
BLOCK_TIMEOUT_SECONDS = 30
HEADER = re.compile(rb"[0-9a-f]{8}\n")
DIGEST = re.compile(r"[0-9a-f]{64}")
ENVELOPE_KEYS = {"protocol_version", "metadata", "payload_size", "payload_sha256"}
LINUX_COMMAND = 'python3 -IB "$HOME/.local/share/vincula-jobs/jobs.py" large-files-stdin'
WINDOWS_BOOTSTRAP = (
    "import os,runpy,sys;"
    "sys.argv=['jobs.py','large-files-stdin'];"
    "runpy.run_path(os.path.join(os.environ['LOCALAPPDATA'],'Vincula-Jobs','jobs.py'),"
    "run_name='__main__')"
)
WINDOWS_COMMAND = f'py.exe -3 -IB -c "{WINDOWS_BOOTSTRAP}"'


def fixed_command(family):
    if family == "linux":
        return LINUX_COMMAND
    if family == "windows":
        return WINDOWS_COMMAND
    raise ValueError("Unsupported large-file destination platform.")


def _checked_header(value):
    if (not isinstance(value, dict) or set(value) != ENVELOPE_KEYS
            or type(value["protocol_version"]) is not int or value["protocol_version"] != PROTOCOL_VERSION
            or not isinstance(value["metadata"], dict)
            or type(value["payload_size"]) is not int or not 0 <= value["payload_size"] <= BLOCK_BYTES
            or not isinstance(value["payload_sha256"], str) or DIGEST.fullmatch(value["payload_sha256"]) is None):
        raise ValueError("Invalid large-file frame metadata.")
    return value


def pack(metadata, payload=b""):
    if not isinstance(metadata, dict) or not isinstance(payload, bytes) or len(payload) > BLOCK_BYTES:
        raise ValueError("Invalid large-file metadata or binary block size.")
    envelope = {"protocol_version": PROTOCOL_VERSION, "metadata": metadata,
                "payload_size": len(payload), "payload_sha256": sha256(payload).hexdigest()}
    try:
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Large-file metadata must be finite UTF-8 JSON.") from None
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("Large-file frame metadata exceeds its size limit.")
    return f"{len(raw):08x}\n".encode("ascii") + raw + payload


def _exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(min(64 * 1024, size - len(result)))
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(result):
            raise ValueError("Incomplete large-file binary frame.")
        result.extend(chunk)
    return bytes(result)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate large-file metadata key.")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite large-file metadata number.")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite large-file metadata number.")
    return number


def read_header(stream):
    raw_header = _exact(stream, HEADER_BYTES)
    if HEADER.fullmatch(raw_header) is None:
        raise ValueError("Invalid large-file frame length header.")
    size = int(raw_header[:8], 16)
    if size > MAX_METADATA_BYTES:
        raise ValueError("Large-file frame metadata exceeds its size limit.")
    raw = _exact(stream, size)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_constant, parse_float=_float)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid large-file JSON metadata.") from None
    return _checked_header(value)


def read_payload(stream, header):
    header = _checked_header(header)
    payload = _exact(stream, header["payload_size"])
    if sha256(payload).hexdigest() != header["payload_sha256"]:
        raise ValueError("Large-file binary block SHA-256 mismatch.")
    return payload


def read(stream):
    header = read_header(stream)
    return header["metadata"], read_payload(stream, header)


def write(stream, metadata, payload=b""):
    remaining = memoryview(pack(metadata, payload))
    while remaining:
        size = stream.write(remaining)
        if type(size) is not int or not 0 < size <= len(remaining):
            raise ValueError("Incomplete large-file frame write.")
        remaining = remaining[size:]


def remote_exchange(route, family, request, payload=None, *, check_cancelled=None, spool_dir=None):
    """Exchange one exact frame; cancellation never reads credentials or kills saved PIDs."""
    # Receiver-only installs need the frame API without the controller's SSH adapter.
    import node_ssh

    command = fixed_command(family)
    if check_cancelled is not None:
        check_cancelled()
    packet = pack(request, b"" if payload is None else payload)
    # spool_dir is operator-local storage, never a path taken from request metadata.
    with TemporaryFile(dir=spool_dir) as source, TemporaryFile(dir=spool_dir) as output:
        source.write(packet)
        source.seek(0)
        try:
            node_ssh.run(route, "ssh", ("-T",), (command,), output, timeout=BLOCK_TIMEOUT_SECONDS, stdin=source)
        finally:
            if check_cancelled is not None:
                check_cancelled()
        size = output.seek(0, 2)
        if size > MAX_FRAME_BYTES:
            raise ValueError("Large-file SSH response exceeds its size limit.")
        output.seek(0)
        reply, body = read(output)
        if output.tell() != size:
            raise ValueError("Unexpected trailing data in large-file SSH response.")
        return reply, body
