# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Public node invitations and local, operator-verified SSH registration.

Registration only publishes topology and a host-key pin. It does not contact a
peer, authorize an SSH identity, grant execution, or establish desktop consent.
"""

import base64
import binascii
from copy import deepcopy
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
from uuid import UUID

import node_registry
import nodes


class EnrollmentConflict(ValueError):
    """The registry revision or an existing identity differs from this request."""


_WIRE_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
_REVISION = re.compile(r"[0-9a-f]{64}")


def _fields(value, names, description):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError(f"Invalid {description} fields.")


def _text(value, minimum, maximum, description):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"Invalid {description}.")
    return value


def _host_key(key):
    _text(key, 1, 100, "Ed25519 public key")
    try:
        wire = base64.b64decode(key, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid base64 Ed25519 public key.") from None
    if (len(wire) != 51 or not wire.startswith(_WIRE_PREFIX)
            or base64.b64encode(wire).decode("ascii") != key):
        raise ValueError("Use the exact SSH wire encoding of an Ed25519 public key.")
    fingerprint = "SHA256:" + base64.b64encode(sha256(wire).digest()).decode("ascii").rstrip("=")
    return {"type": "ssh-ed25519", "key": key, "fingerprint": fingerprint}


def _endpoints(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("Specify between 1 and 32 public SSH endpoints.")
    result = []
    for entry in value:
        _fields(entry, {"host", "user", "port", "network"}, "endpoint")
        host = node_registry.host_address(entry["host"])
        user, port = entry["user"], entry["port"]
        if not isinstance(user, str) or not node_registry.ACCOUNT.fullmatch(user) or user.startswith("-"):
            raise ValueError("Specify a valid SSH account.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("The SSH port must be between 1 and 65535.")
        network = _text(entry["network"], 1, 72, "network label")
        result.append({"host": host, "user": user, "port": port, "network": network})
    return result


def validate_invitation(document):
    """Validate exact public fields and return an independent canonical copy."""
    _fields(document, {"format", "version", "node", "endpoints", "ssh_host_key"}, "invitation")
    if document["format"] != "vincula-node-invitation" or type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("Unsupported node invitation format or version.")
    node = document["node"]
    _fields(node, {"id", "label", "family"}, "public node")
    node_id = node["id"]
    try:
        if not isinstance(node_id, str) or str(UUID(node_id)) != node_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Use an existing canonical UUID as the node ID.") from None
    label = _text(node["label"], 1, 120, "node label")
    if not label.strip() or node["family"] not in ("linux", "windows"):
        raise ValueError("Each node needs a label and a supported OS family.")
    supplied = document["ssh_host_key"]
    _fields(supplied, {"type", "key", "fingerprint"}, "SSH public host key")
    key = _host_key(supplied["key"])
    if supplied != key:
        raise ValueError("The Ed25519 public key and its SHA256 fingerprint must match.")
    return {"format": "vincula-node-invitation", "version": 1,
            "node": {"id": node_id, "label": label, "family": node["family"]},
            "endpoints": _endpoints(document["endpoints"]), "ssh_host_key": key}


def create_invitation(path, endpoints, public_host_key):
    """Export an existing local UUID and a supplied public OpenSSH key line."""
    if not isinstance(public_host_key, str) or "\n" in public_host_key or "\r" in public_host_key:
        raise ValueError("Supply one public OpenSSH Ed25519 key line.")
    pieces = public_host_key.split()
    if len(pieces) < 2 or pieces[0] != "ssh-ed25519":
        raise ValueError("Supply one public OpenSSH Ed25519 key line.")
    with nodes.edit_lock(Path(path).resolve()):
        registry = nodes.snapshot(Path(path).resolve())["registry"]
        if registry is None:
            raise ValueError("Register this computer before exporting an invitation.")
        local = next(entry for entry in registry["nodes"] if entry["id"] == registry["local"])
        return validate_invitation({"format": "vincula-node-invitation", "version": 1,
                                    "node": {name: local[name] for name in ("id", "label", "family")},
                                    "endpoints": endpoints, "ssh_host_key": _host_key(pieces[1])})


def _connection(value):
    if (not isinstance(value, dict) or "identity_file" not in value
            or value.keys() - {"identity_file", "identity_agent", "bind_address"}):
        raise ValueError("Specify only this origin's identity_file, identity_agent and bind_address.")
    result = deepcopy(value)
    for name in ("identity_file", "identity_agent"):
        if name in result:
            _text(result[name], 1, 32768, name)
            result[name] = str(node_registry.operator_path(result[name]))
    if "bind_address" in result:
        if not isinstance(result["bind_address"], str):
            raise ValueError("Specify a local IP bind address.")
        result["bind_address"] = str(ip_address(result["bind_address"]))
    return result


def _redirected(metadata):
    # NTFS junctions (and other reparse points) are not necessarily reported as
    # symlinks by pathlib on supported Python versions, including Python 3.10.
    return (stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _check_pin_directory(path, *, missing_ok=False):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if _redirected(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise EnrollmentConflict("The enrollment pin directory must be a local directory, not a link or reparse point.")


def _pin_bytes(path):
    """Refuse links and special files rather than following a supplied pin."""
    _check_pin_directory(path.parent, missing_ok=True)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or _redirected(metadata):
        raise EnrollmentConflict("The enrollment host-key pin must be a regular file, not a link or reparse point.")
    if metadata.st_size > 1024:
        raise EnrollmentConflict("The enrollment host-key pin differs from the invitation.")
    return path.read_bytes()


def _pin_directory(path):
    _check_pin_directory(path, missing_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    _check_pin_directory(path)


def _sync_directory(path):
    # Windows cannot open directories with os.open; atomic publication still
    # applies there. POSIX also flushes the directory entries across power loss.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish_pin(path, content):
    """Publish a complete file without overwriting an independently created pin."""
    _check_pin_directory(path.parent)
    stage = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".pin-", suffix=".tmp", delete=False) as stream:
            stage = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(stage, path)
        except FileExistsError:
            if _pin_bytes(path) != content:
                raise EnrollmentConflict("The enrollment host-key pin changed during publication.") from None
            return None
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def _cleanup_unreferenced_pin(registry_path, pin, ownership, content):
    """A failed save may already have committed. Uncertainty preserves the pin."""
    if ownership is None:
        return
    try:
        _check_pin_directory(pin.parent)
        registry = nodes.snapshot(registry_path)["registry"]
        if registry is not None and any(
                isinstance(entry.get("ssh"), dict)
                and Path(entry["ssh"]["known_hosts_file"]).resolve() == pin.resolve()
                for entry in registry["nodes"] if "known_hosts_file" in entry.get("ssh", {})):
            return
        metadata = pin.lstat()
        if (not _redirected(metadata) and (metadata.st_dev, metadata.st_ino) == ownership
                and _pin_bytes(pin) == content):
            pin.unlink()
            _sync_directory(pin.parent)
    except (OSError, ValueError, KeyError):
        # Never replace the original failure or remove a potentially live pin.
        return


def _registered(snapshot, node_id):
    return {**snapshot, "node_id": node_id, "state": "registered",
            "checks": {"ssh": "unchecked", "runtime": "unchecked", "desktop": "unchecked"},
            "next_steps": ["Authorize this origin's public SSH identity on the destination if needed.",
                           "Verify authenticated SSH and the destination runtime before using agent capabilities.",
                           "Configure and verify desktop consent separately before enabling shared input."]}


def import_invitation(path, document, *, expected_revision, expected_fingerprint, endpoint_index, local_connection):
    """Publish one verified pin and route, preserving local CAS and identity.

    An old revision is accepted only when both the saved node and pin already
    exactly match this request. Key or route changes require a separate explicit
    operator workflow; enrollment never silently rotates an existing identity.
    """
    invitation = validate_invitation(document)
    key = invitation["ssh_host_key"]
    if (not isinstance(expected_fingerprint, str) or not expected_fingerprint.isascii()
            or not compare_digest(expected_fingerprint, key["fingerprint"])):
        raise ValueError("Explicitly confirm the destination's independently verified SHA256 fingerprint.")
    if expected_revision is not None and (not isinstance(expected_revision, str) or not _REVISION.fullmatch(expected_revision)):
        raise ValueError("Specify the registry revision returned by a previous read.")
    if type(endpoint_index) is not int or not 0 <= endpoint_index < len(invitation["endpoints"]):
        raise ValueError("Select one advertised endpoint by its index.")
    path = Path(path).resolve()
    node_id = invitation["node"]["id"]
    pin = path.parent / "enrollment-known-hosts" / (node_id + ".known_hosts")
    entry = {**invitation["node"], "aliases": [],
             "ssh": {**invitation["endpoints"][endpoint_index], **_connection(local_connection),
                     "known_hosts_file": str(pin)}}
    content = f"vincula-{node_id} ssh-ed25519 {key['key']}\n".encode("ascii")
    with nodes.edit_lock(path):
        before = nodes.snapshot(path)
        if before["registry"] is None:
            raise ValueError("Register this computer before importing a peer.")
        candidate = deepcopy(before["registry"])
        if candidate["local"] == node_id:
            raise EnrollmentConflict("An invitation cannot replace this computer's local identity.")
        existing = next((node for node in candidate["nodes"] if node["id"] == node_id), None)
        if existing is not None and existing != entry:
            raise EnrollmentConflict("This node UUID already has different metadata or a different local route.")
        _check_pin_directory(pin.parent, missing_ok=True)
        previous_pin = _pin_bytes(pin)
        if previous_pin is not None and previous_pin != content:
            raise EnrollmentConflict("This node UUID already has a different host-key pin.")
        if existing == entry and previous_pin == content:
            return _registered(before, node_id)
        if existing is not None:
            raise EnrollmentConflict("The registered node's host-key pin is missing. Restore its verified pin explicitly.")
        if expected_revision != before["revision"]:
            raise EnrollmentConflict("The node registry changed. Reload it before importing this invitation.")
        candidate["nodes"].append(entry)
        nodes.validate(candidate)
        ownership = None
        try:
            _pin_directory(pin.parent)
            if previous_pin is None:
                ownership = _publish_pin(pin, content)
            _sync_directory(pin.parent)
            result = nodes.save(path, candidate)
            _sync_directory(path.parent)
            return _registered(result, node_id)
        except BaseException:
            _cleanup_unreferenced_pin(path, pin, ownership, content)
            raise
