# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic facts test the private protocol, not a real desktop or clipboard."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_backend import GroupManifest, Screen
import desktop_managed as managed


def configuration(primary=True, clipboard=True):
    ids = sorted(str(uuid4()) for _ in range(3))
    group = GroupManifest(str(uuid4()), ids[0], tuple(Screen(node, i, 0) for i, node in enumerate(ids)),
                          clipboard, tuple((node, str(i + 1) * 64) for i, node in enumerate(ids)),
                          tuple((node, str(i + 4) * 64) for i, node in enumerate(ids)))
    return {"group": group.to_document(), "node_id": ids[0] if primary else ids[1],
            "settings_file": "/private/profilo-è-😀/Deskflow.conf", "state_file": "/private/profilo-è-😀/Deskflow.state",
            "binary_sha256": "f" * 64, "owner_consent": True}


def snapshot(expected, challenge, sequence=1):
    primary, node = expected["primary"], expected["node_id"]
    peers = set(expected["members"]) - {node} if node == primary else {primary}
    return {"type": "snapshot", "protocol": 1, "session_id": expected["session_id"], "node_id": node,
            "engine_nonce": expected["engine_nonce"], "manifest_sha256": expected["manifest_sha256"],
            "challenge": challenge, "sequence": sequence, "revoked": False, "reason": "",
            "role": "primary" if node == primary else "client", "input_ready": True, "clipboard_ready": True,
            "peers": [{"node_id": peer, "pin_sha256": expected["tls_pins"][peer], "handshake": True} for peer in sorted(peers)],
            "local_pin_sha256": expected["tls_pins"][node]}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ManagedProtocolTests(unittest.TestCase):
    def test_runtime_environment_uses_the_real_desktop_without_ambient_loaders(self):
        original = {"LD_PRELOAD": "/external/preload.so", "LD_LIBRARY_PATH": "/external/lib",
                    "QT_PLUGIN_PATH": "/external/qt", "QT_QPA_PLATFORM": "offscreen", "OPENSSL_CONF": "/external/ssl.conf",
                    "SSH_AUTH_SOCK": "/credentials/agent.sock", "DISPLAY": ":7", "XAUTHORITY": "/local/session/auth",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "XDG_SESSION_TYPE": "x11"}
        with patch.object(managed.sys, "platform", "linux"):
            result = managed.runtime_environment(original)
            self.assertEqual(result["QT_QPA_PLATFORM"], "xcb")
            for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH", "OPENSSL_CONF", "SSH_AUTH_SOCK"):
                self.assertNotIn(key, result)
            self.assertEqual(result["XAUTHORITY"], original["XAUTHORITY"])
            self.assertEqual(result["DBUS_SESSION_BUS_ADDRESS"], original["DBUS_SESSION_BUS_ADDRESS"])
            self.assertEqual(managed.runtime_environment(dict(original, XDG_SESSION_TYPE="wayland", WAYLAND_DISPLAY="wayland-2"))["QT_QPA_PLATFORM"], "wayland")
        self.assertEqual(original["QT_QPA_PLATFORM"], "offscreen")
        with patch.object(managed.sys, "platform", "win32"):
            self.assertEqual(managed.runtime_environment(original)["QT_QPA_PLATFORM"], "windows")

    def test_frame_roundtrip_unicode_and_windows_paths(self):
        value = {"z": 'C:\\profilo è😀\\"quote"/config', "a": True}
        encoded = managed.frame(value)
        self.assertIn("è😀".encode(), encoded)
        self.assertEqual(encoded[4:], json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
        reader, writer = os.pipe()
        try:
            os.write(writer, encoded)
            self.assertEqual(managed.read_frame(reader), value)
        finally:
            os.close(reader)
            os.close(writer)

    def test_malformed_frames_are_rejected_without_waiting_for_eof(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'\xff', b'{'):
            with self.subTest(raw=raw):
                reader, writer = os.pipe()
                try:
                    os.write(writer, len(raw).to_bytes(4, "big") + raw)
                    with self.assertRaises((ValueError, UnicodeError)):
                        managed.read_frame(reader)
                finally:
                    os.close(reader)
                    os.close(writer)
        for size in (0, managed.MAX_FRAME + 1):
            reader, writer = os.pipe()
            try:
                os.write(writer, size.to_bytes(4, "big"))
                with self.assertRaises(ValueError):
                    managed.read_frame(reader)
            finally:
                os.close(reader)
                os.close(writer)

    def test_truncated_pipe_and_oversized_output_fail(self):
        reader, writer = os.pipe()
        os.write(writer, b'\0\0\0\x09{}')
        os.close(writer)
        try:
            with self.assertRaises(EOFError):
                managed.read_frame(reader)
        finally:
            os.close(reader)
        with self.assertRaises(ValueError):
            managed.frame({"data": "x" * managed.MAX_FRAME})

    def test_bootstrap_is_bound_to_common_manifest_and_local_role(self):
        for primary in (True, False):
            config = configuration(primary)
            expected = managed.bootstrap(config, "a" * 64)
            group = GroupManifest.from_document(config["group"])
            self.assertEqual(expected["manifest_sha256"], group.digest)
            self.assertEqual(set(expected["tls_pins"]), set(expected["members"]))
            self.assertTrue(managed.validate_snapshot(snapshot(expected, "b" * 64), expected, "b" * 64, 0))
            role = "server" if primary else "client"
            managed.validate_argv(config, ["/approved/deskflow-core", role, "--settings", config["settings_file"]])
            for argv in (["/approved/deskflow-core", role], ["/approved/deskflow-core", role, "--settings", "/different"],
                         ["/usr/bin/python3", "-c", "invented", config["settings_file"]]):
                with self.assertRaises(ValueError):
                    managed.validate_argv(config, argv)

    def test_owner_consent_and_public_configuration_are_required(self):
        for changes in ({"owner_consent": False}, {"owner_consent": 1}, {"node_id": str(uuid4())},
                        {"settings_file": "relative"}, {"binary_sha256": "invented"}, {"extra": True}):
            config = configuration()
            config.update(changes)
            with self.assertRaises(ValueError):
                managed.validate_configuration(config)

    def test_peer_identity_and_session_cannot_be_substituted(self):
        expected = managed.bootstrap(configuration(), "a" * 64)
        good = snapshot(expected, "b" * 64)
        substitutions = [("session_id", str(uuid4())), ("node_id", str(uuid4())), ("engine_nonce", "c" * 64),
                         ("manifest_sha256", "d" * 64), ("local_pin_sha256", "e" * 64),
                         ("challenge", "f" * 64), ("role", "client"), ("sequence", True), ("protocol", True)]
        for key, value in substitutions:
            with self.subTest(field=key):
                bad = dict(good, **{key: value})
                with self.assertRaises(ValueError):
                    managed.validate_snapshot(bad, expected, "b" * 64, 0)
        for modification in ("duplicate", "unknown", "wrong-pin", "extra"):
            bad = deepcopy(good)
            if modification == "duplicate":
                bad["peers"][1] = dict(bad["peers"][0])
            elif modification == "unknown":
                bad["peers"][0]["node_id"] = str(uuid4())
            elif modification == "wrong-pin":
                bad["peers"][0]["pin_sha256"] = bad["peers"][1]["pin_sha256"]
            else:
                bad["peers"][0]["forged"] = True
            with self.assertRaises(ValueError):
                managed.validate_snapshot(bad, expected, "b" * 64, 0)

    def test_missing_peer_grant_and_requested_clipboard_are_not_ready(self):
        expected = managed.bootstrap(configuration(), "a" * 64)
        for field, value in (("peers", []), ("input_ready", False), ("clipboard_ready", False), ("revoked", True)):
            bad = dict(snapshot(expected, "b" * 64), **{field: value})
            self.assertFalse(managed.validate_snapshot(bad, expected, "b" * 64, 0))
        expected = managed.bootstrap(configuration(clipboard=False), "a" * 64)
        no_clipboard = dict(snapshot(expected, "b" * 64), clipboard_ready=False)
        self.assertTrue(managed.validate_snapshot(no_clipboard, expected, "b" * 64, 0))

    def test_challenge_freshness_and_terminal_revocation(self):
        clock = Clock()
        expected = managed.bootstrap(configuration(primary=False), "a" * 64)
        tracker = managed.SnapshotTracker(expected, clock)
        self.assertFalse(tracker.report()["ready"])
        request = tracker.request()
        with self.assertRaises(ValueError):
            tracker.request()
        self.assertTrue(tracker.accept(snapshot(expected, request["challenge"])))
        self.assertTrue(tracker.report()["ready"])
        clock.now += 0.5
        request = tracker.request()
        lost = dict(snapshot(expected, request["challenge"], 2), peers=[])
        self.assertFalse(tracker.accept(lost))
        self.assertIsNotNone(tracker.report()["failure"])
        with self.assertRaises(ValueError):
            tracker.request()

    def test_late_or_old_sequence_cannot_refresh_snapshot(self):
        for late in (True, False):
            clock = Clock()
            expected = managed.bootstrap(configuration(), "a" * 64)
            tracker = managed.SnapshotTracker(expected, clock)
            request = tracker.request()
            if late:
                clock.now += managed.SNAPSHOT_TIMEOUT
            else:
                tracker.sequence = 1
            with self.assertRaises(ValueError):
                tracker.accept(snapshot(expected, request["challenge"]))
        clock = Clock()
        tracker = managed.SnapshotTracker(expected, clock)
        request = tracker.request()
        tracker.accept(snapshot(expected, request["challenge"]))
        clock.now += managed.SNAPSHOT_TIMEOUT
        self.assertFalse(tracker.report()["ready"])
        self.assertIsNotNone(tracker.report()["failure"])


if __name__ == "__main__":
    unittest.main()
