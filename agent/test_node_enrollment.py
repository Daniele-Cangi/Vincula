# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import node_enrollment as enrollment
import nodes


def public_key(seed=1):
    wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    encoded = base64.b64encode(wire).decode("ascii")
    fingerprint = "SHA256:" + base64.b64encode(sha256(wire).digest()).decode("ascii").rstrip("=")
    return f"ssh-ed25519 {encoded} public-comment", fingerprint


class NodeEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.path = self.base / "origin one" / "nodes.json"
        self.ids = [str(uuid4()) for _ in range(4)]
        self.family = "windows" if sys.platform == "win32" else "linux"
        self.initialize(self.path, self.ids[0], self.ids[:3])
        self.source = self.base / "new peer" / "nodes.json"
        self.initialize(self.source, self.ids[3], [self.ids[3]])
        self.endpoints = [{"host": "192.168.77.4", "user": "worker", "port": 22, "network": "Ethernet"},
                          {"host": "peer.example", "user": "other", "port": 2222, "network": "Wi-Fi"}]
        key, self.fingerprint = public_key()
        self.invitation = enrollment.create_invitation(self.source, self.endpoints, key)
        self.connection = {"identity_file": str(self.base / "origin one" / "identity")}

    def initialize(self, path, local, identities):
        for node_id in identities:
            nodes.execute({"action": "add", "local": node_id == local,
                           "node": {"id": node_id, "label": "PC " + node_id[:8], "family": self.family}}, path)

    def request(self, path=None, document=None, **changes):
        path = path or self.path
        arguments = {"expected_revision": nodes.snapshot(path)["revision"],
                     "expected_fingerprint": self.fingerprint, "endpoint_index": 0,
                     "local_connection": self.connection}
        arguments.update(changes)
        return enrollment.import_invitation(path, document or self.invitation, **arguments)

    def pin(self, path=None):
        return (path or self.path).parent / "enrollment-known-hosts" / (self.ids[3] + ".known_hosts")

    def test_export_uses_persisted_uuid_and_only_public_fields(self):
        self.assertEqual(self.invitation["node"]["id"], self.ids[3])
        self.assertEqual(self.invitation["ssh_host_key"]["fingerprint"], self.fingerprint)
        self.assertEqual(set(self.invitation["node"]), {"id", "label", "family"})
        self.assertNotIn(str(self.base), str(self.invitation))
        self.assertNotIn("public-comment", str(self.invitation))
        again = enrollment.create_invitation(self.source, self.endpoints, public_key()[0])
        self.assertEqual(again, self.invitation)
        self.assertEqual(nodes.snapshot(self.source)["registry"]["local"], self.ids[3])

    def test_export_requires_an_existing_local_registry_and_a_public_key(self):
        missing = self.base / "absent" / "nodes.json"
        with self.assertRaises(ValueError):
            enrollment.create_invitation(missing, self.endpoints, public_key()[0])
        self.assertFalse(missing.exists())
        for key in ["-----BEGIN OPENSSH PRIVATE KEY-----", public_key()[0] + "\n", "ssh-rsa AAAA", 123]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                enrollment.create_invitation(self.source, self.endpoints, key)

    def test_fourth_uuid_has_independent_routes_from_two_origins(self):
        other = self.base / "origin two" / "nodes.json"
        self.initialize(other, self.ids[1], [self.ids[1], self.ids[0], self.ids[2]])
        first = self.request()
        second = self.request(other, endpoint_index=1,
                              local_connection={"identity_file": str(other.parent / "other identity"),
                                                "bind_address": "192.168.1.20"})
        self.assertEqual([node["id"] for node in first["registry"]["nodes"]], self.ids)
        self.assertEqual(second["registry"]["nodes"][-1]["id"], self.ids[3])
        route1, route2 = (item["registry"]["nodes"][-1]["ssh"] for item in (first, second))
        self.assertEqual(route1["host"], "192.168.77.4")
        self.assertEqual(route2["host"], "peer.example")
        self.assertNotEqual(route1["identity_file"], route2["identity_file"])
        self.assertNotEqual(route1["known_hosts_file"], route2["known_hosts_file"])
        self.assertEqual(self.pin().read_bytes(), self.pin(other).read_bytes())
        compiled = nodes.validate(first["registry"]).get(self.ids[3]).route
        self.assertIn("HostKeyAlias=vincula-" + self.ids[3], compiled.options)
        self.assertIn("HostKeyAlgorithms=ssh-ed25519", compiled.options)
        self.assertIn("UpdateHostKeys=no", compiled.options)
        for result in (first, second):
            self.assertEqual(result["state"], "registered")
            self.assertEqual(result["checks"], dict.fromkeys(("ssh", "runtime", "desktop"), "unchecked"))
            self.assertEqual(result["node_id"], self.ids[3])
            self.assertTrue(result["next_steps"])

    def test_existing_core_known_hosts_file_and_other_pins_are_preserved(self):
        existing = self.path.parent / "known_hosts"
        existing.write_bytes(b"existing core host trust\n")
        directory = self.pin().parent
        directory.mkdir()
        other_pin = directory / (self.ids[1] + ".known_hosts")
        other_pin.write_bytes(b"another pin\n")
        self.request()
        self.assertEqual(existing.read_bytes(), b"existing core host trust\n")
        self.assertEqual(other_pin.read_bytes(), b"another pin\n")
        self.assertEqual(self.pin().read_text(), "vincula-" + self.ids[3] + " ssh-ed25519 "
                         + self.invitation["ssh_host_key"]["key"] + "\n")

    def test_identical_retry_accepts_old_revision_without_rewriting_any_file(self):
        revision = nodes.snapshot(self.path)["revision"]
        first = self.request()
        with patch.object(nodes, "save", side_effect=AssertionError("must not rewrite registry")):
            again = self.request(expected_revision=revision)
        self.assertEqual(first, again)

    def test_same_uuid_different_pin_metadata_or_local_route_conflicts(self):
        self.request()
        before, pin = self.path.read_bytes(), self.pin().read_bytes()
        changed_label = deepcopy(self.invitation)
        changed_label["node"]["label"] = "Another PC"
        changed_family = deepcopy(self.invitation)
        changed_family["node"]["family"] = "windows" if self.family == "linux" else "linux"
        changed_key = enrollment.create_invitation(self.source, self.endpoints, public_key(2)[0])
        for document, overrides in [
            (changed_label, {}), (changed_family, {}),
            (changed_key, {"expected_fingerprint": public_key(2)[1]}),
            (self.invitation, {"endpoint_index": 1}),
            (self.invitation, {"local_connection": {"identity_file": str(self.base / "different")}}),
        ]:
            with self.subTest(overrides=overrides, document=document), self.assertRaises(enrollment.EnrollmentConflict):
                self.request(document=document, **overrides)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.pin().read_bytes(), pin)

    def test_preexisting_wrong_pin_is_never_rotated_or_deleted(self):
        self.pin().parent.mkdir()
        self.pin().write_bytes(b"foreign pin\n")
        before = self.path.read_bytes()
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request()
        self.assertEqual(self.pin().read_bytes(), b"foreign pin\n")
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_revision_does_not_publish_a_new_pin_or_replace_other_edits(self):
        old = nodes.snapshot(self.path)["revision"]
        nodes.execute({"action": "update", "node_id": self.ids[0], "changes": {"label": "Other app"}}, self.path)
        before = self.path.read_bytes()
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request(expected_revision=old)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.pin().exists())

    def test_missing_pin_never_silently_changes_trust_for_an_existing_uuid(self):
        old = nodes.snapshot(self.path)["revision"]
        imported = self.request()
        self.pin().unlink()
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request(expected_revision=old)
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request(expected_revision=imported["revision"])
        changed_key = enrollment.create_invitation(self.source, self.endpoints, public_key(2)[0])
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request(document=changed_key, expected_fingerprint=public_key(2)[1])
        self.assertFalse(self.pin().exists())

    def test_concurrent_different_invitations_obey_cas_without_an_orphan_loser_pin(self):
        old = nodes.snapshot(self.path)["revision"]
        other = deepcopy(self.invitation)
        other["node"]["id"] = str(uuid4())

        def attempt(invitation):
            try:
                self.request(document=invitation, expected_revision=old)
                return "registered", invitation["node"]["id"]
            except enrollment.EnrollmentConflict:
                return "conflict", invitation["node"]["id"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, (self.invitation, other)))
        self.assertEqual(sorted(state for state, _ in results), ["conflict", "registered"])
        registered = nodes.snapshot(self.path)["registry"]["nodes"]
        self.assertEqual(len(registered), 4)
        for state, node_id in results:
            pin = self.pin().parent / (node_id + ".known_hosts")
            self.assertEqual(pin.exists(), state == "registered")

    def test_registry_publishes_only_after_complete_pin_is_available(self):
        original = nodes.save

        def observe(path, document):
            self.assertEqual(self.pin().read_text().split()[0], "vincula-" + self.ids[3])
            self.assertEqual(self.pin().read_text().split()[2], self.invitation["ssh_host_key"]["key"])
            return original(path, document)

        with patch.object(nodes, "save", side_effect=observe):
            self.request()

    def test_failed_save_cleans_owned_unreferenced_pin_and_allows_retry(self):
        before = self.path.read_bytes()
        with patch.object(nodes, "save", side_effect=OSError("save before commit")):
            with self.assertRaisesRegex(OSError, "save before commit"):
                self.request()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.pin().exists())
        self.assertEqual(list(self.pin().parent.glob(".pin-*")), [])
        self.request()
        self.assertTrue(self.pin().exists())

    def test_lost_save_acknowledgement_keeps_referenced_pin_and_retry_recovers(self):
        revision = nodes.snapshot(self.path)["revision"]
        original = nodes.save

        def commit_then_fail(path, document):
            original(path, document)
            raise OSError("lost commit acknowledgement")

        with patch.object(nodes, "save", side_effect=commit_then_fail):
            with self.assertRaisesRegex(OSError, "lost commit acknowledgement"):
                self.request(expected_revision=revision)
        self.assertTrue(self.pin().is_file())
        self.assertEqual(nodes.snapshot(self.path)["registry"]["nodes"][-1]["id"], self.ids[3])
        result = self.request(expected_revision=revision)
        self.assertEqual(result["state"], "registered")

    def test_uncertain_save_outcome_preserves_pin_for_recovery(self):
        original = nodes.snapshot
        calls = 0

        def uncertain(path):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("cannot read outcome")
            return original(path)

        with patch.object(nodes, "snapshot", side_effect=uncertain), patch.object(nodes, "save", side_effect=OSError("save failed")):
            with self.assertRaisesRegex(OSError, "save failed"):
                # Supply revision directly: request() would add another read.
                enrollment.import_invitation(self.path, self.invitation,
                    expected_revision=original(self.path)["revision"], expected_fingerprint=self.fingerprint,
                    endpoint_index=0, local_connection=self.connection)
        self.assertTrue(self.pin().is_file())
        self.request()

    def test_cleanup_does_not_probe_registry_paths_and_preserves_ambiguous_pin(self):
        untrusted = self.base / "untrusted network mount" / "known_hosts"
        route = {**self.endpoints[0], **self.connection, "known_hosts_file": str(untrusted)}
        nodes.execute({"action": "update", "node_id": self.ids[1], "changes": {"ssh": route}}, self.path)
        before = self.path.read_bytes()

        def forbid_probe(method):
            original = getattr(Path, method)

            def guarded(path, *arguments, **keywords):
                if path == untrusted or path == untrusted.parent or untrusted in path.parents:
                    raise AssertionError("Cleanup must not explore paths taken from the registry.")
                return original(path, *arguments, **keywords)

            return guarded

        with patch.object(Path, "resolve", forbid_probe("resolve")), \
                patch.object(Path, "stat", forbid_probe("stat")), \
                patch.object(Path, "lstat", forbid_probe("lstat")), \
                patch.object(Path, "open", forbid_probe("open")), \
                patch.object(nodes, "save", side_effect=OSError("save before commit")):
            with self.assertRaisesRegex(OSError, "save before commit"):
                self.request()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.pin().is_file())
        # The orphan is verified public trust, reused by an explicit retry.
        self.request()
        self.assertEqual(nodes.snapshot(self.path)["registry"]["nodes"][-1]["id"], self.ids[3])

    def test_cleanup_preserves_pin_referenced_through_a_real_directory_alias(self):
        self.pin().parent.mkdir()
        alias = self.base / "alias to pin directory"
        try:
            alias.symlink_to(self.pin().parent, target_is_directory=True)
        except OSError as error:
            if sys.platform == "win32" and getattr(error, "winerror", None) == 1314:
                self.skipTest("The Windows account cannot create symlinks.")
            raise
        reference = alias / self.pin().name
        route = {**self.endpoints[0], **self.connection, "known_hosts_file": str(reference)}
        nodes.execute({"action": "update", "node_id": self.ids[1], "changes": {"ssh": route}}, self.path)
        with patch.object(nodes, "save", side_effect=OSError("save before commit")):
            with self.assertRaisesRegex(OSError, "save before commit"):
                self.request()
        self.assertTrue(self.pin().is_file())
        self.assertEqual(reference.read_bytes(), self.pin().read_bytes())

    def test_cleanup_preserves_pin_when_legacy_route_references_are_implicit(self):
        legacy = "ubuntu" if sys.platform == "win32" else "windows"
        family = "linux" if legacy == "ubuntu" else "windows"
        nodes.execute({"action": "update", "node_id": self.ids[1],
                       "changes": {"family": family, "legacy_route": legacy}}, self.path)
        with patch.object(nodes, "save", side_effect=OSError("save before commit")):
            with self.assertRaisesRegex(OSError, "save before commit"):
                self.request()
        self.assertTrue(self.pin().is_file())

    def test_matching_orphan_pin_is_reused_and_not_removed_on_failed_registry_save(self):
        self.request()
        pin_bytes = self.pin().read_bytes()
        nodes.execute({"action": "remove", "node_id": self.ids[3]}, self.path)
        with patch.object(nodes, "save", side_effect=OSError("save failed")):
            with self.assertRaises(OSError):
                self.request()
        self.assertEqual(self.pin().read_bytes(), pin_bytes)
        self.request()

    def test_pin_link_publication_failure_leaves_registry_unchanged(self):
        before = self.path.read_bytes()
        with patch.object(os, "link", side_effect=OSError("pin publication failed")):
            with self.assertRaisesRegex(OSError, "pin publication failed"):
                self.request()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.pin().exists())
        self.assertEqual(list(self.pin().parent.glob(".pin-*")), [])

    def test_rejects_unverified_fingerprint_indices_revision_and_local_secret_fields(self):
        for overrides in [
            {"expected_fingerprint": None}, {"expected_fingerprint": "SHA256:wrong"},
            {"expected_fingerprint": "SHA256:é"}, {"endpoint_index": True}, {"endpoint_index": -1},
            {"endpoint_index": 2}, {"expected_revision": True}, {"expected_revision": "stale"},
            {"local_connection": {"identity_file": "relative"}},
            {"local_connection": {"identity_file": self.connection["identity_file"], "password": "secret"}},
            {"local_connection": {"identity_file": self.connection["identity_file"] + "\t"}},
            {"local_connection": {"identity_file": self.connection["identity_file"], "bind_address": "bad"}},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.request(**overrides)
        self.assertFalse(self.pin().exists())

    def test_rejects_local_identity_replacement(self):
        invitation = enrollment.create_invitation(self.path, self.endpoints, public_key()[0])
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request(document=invitation)

    def test_public_invitation_rejects_extra_private_fields_at_each_level(self):
        for location in [(), ("node",), ("endpoints", 0), ("ssh_host_key",)]:
            for field in ("password", "private_key", "identity_file", "token"):
                document = deepcopy(self.invitation)
                cursor = document
                for item in location:
                    cursor = cursor[item]
                cursor[field] = "not public"
                with self.subTest(location=location, field=field), self.assertRaises(ValueError):
                    enrollment.validate_invitation(document)

    def test_malformed_public_key_and_structure_are_rejected(self):
        changes = [
            (("version",), True), (("version",), 2), (("format",), "another"),
            (("node", "id"), self.ids[3].upper()), (("node", "id"), "not-a-uuid"),
            (("node", "label"), " "), (("node", "label"), "line\nbreak"),
            (("node", "family"), "macos"), (("endpoints",), []),
            (("endpoints", 0, "port"), True), (("endpoints", 0, "user"), "-oProxyCommand"),
            (("endpoints", 0, "network"), "line\nbreak"),
            (("ssh_host_key", "type"), "ssh-rsa"), (("ssh_host_key", "key"), "AAAA"),
            (("ssh_host_key", "key"), self.invitation["ssh_host_key"]["key"] + "="),
            (("ssh_host_key", "fingerprint"), "SHA256:wrong"),
        ]
        # Correct length but invalid embedded SSH lengths/type must also fail.
        for wire in [b"x" * 51, b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x21" + bytes(32),
                     b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(31)]:
            changes.append((("ssh_host_key", "key"), base64.b64encode(wire).decode("ascii")))
        for location, value in changes:
            document = deepcopy(self.invitation)
            cursor = document
            for item in location[:-1]:
                cursor = cursor[item]
            cursor[location[-1]] = value
            with self.subTest(location=location, value=value), self.assertRaises(ValueError):
                enrollment.validate_invitation(document)

    def test_validated_document_is_independent_of_caller_mutation(self):
        validated = enrollment.validate_invitation(self.invitation)
        validated["endpoints"][0]["host"] = "different.example"
        self.assertEqual(self.invitation["endpoints"][0]["host"], "192.168.77.4")

    def test_symlink_pin_and_directory_are_rejected_without_following_them(self):
        target = self.base / "unrelated"
        target.write_bytes(b"do not change\n")
        self.pin().parent.mkdir()
        try:
            self.pin().symlink_to(target)
        except OSError as error:
            if sys.platform == "win32" and getattr(error, "winerror", None) == 1314:
                self.skipTest("The Windows account cannot create symlinks.")
            raise
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request()
        self.pin().unlink()
        self.pin().parent.rmdir()
        self.pin().parent.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(enrollment.EnrollmentConflict):
            self.request()
        self.assertEqual(target.read_bytes(), b"do not change\n")

    def test_reparse_metadata_rejects_directory_and_pin_before_read_or_write(self):
        self.pin().parent.mkdir()
        self.pin().write_bytes(b"preserve existing file\n")
        original_lstat, original_read = Path.lstat, Path.read_bytes
        before = self.path.read_bytes()

        def no_pin_read(path):
            if path == self.pin():
                raise AssertionError("A reparse point must not be read.")
            return original_read(path)

        for redirected in (self.pin().parent, self.pin()):
            def metadata(path, *arguments, **keywords):
                result = original_lstat(path, *arguments, **keywords)
                if path == redirected:
                    # A junction can still have an ordinary directory st_mode.
                    return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
                return result

            with self.subTest(redirected=redirected), patch.object(Path, "lstat", metadata), \
                    patch.object(Path, "read_bytes", no_pin_read), \
                    patch.object(nodes, "save", side_effect=AssertionError("must not write registry")):
                with self.assertRaises(enrollment.EnrollmentConflict):
                    self.request()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.pin().read_bytes(), b"preserve existing file\n")

    @unittest.skipUnless(sys.platform == "win32", "Native NTFS junction test")
    def test_windows_junction_directory_does_not_redirect_pin_publication(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetVolumePathNameW.restype = wintypes.BOOL
        kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                                ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetVolumeInformationW.restype = wintypes.BOOL
        volume, filesystem = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(256)
        if not kernel.GetVolumePathNameW(str(self.base), volume, len(volume)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
            raise ctypes.WinError(ctypes.get_last_error())
        if filesystem.value.upper() != "NTFS":
            self.skipTest("NTFS junctions are not supported by this temporary volume: " + filesystem.value)
        target = self.base / "outside pin directory"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_bytes(b"do not change\n")
        junction = self.pin().parent
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            self.assertTrue(junction.lstat().st_file_attributes & 0x400)
            before = self.path.read_bytes()
            with self.assertRaises(enrollment.EnrollmentConflict):
                self.request()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(target.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_bytes(), b"do not change\n")
        finally:
            junction.rmdir()

    @unittest.skipUnless(sys.platform == "linux", "Explicit identity_agent is Linux-only")
    def test_explicit_local_agent_route_uses_public_identity_only(self):
        connection = {"identity_file": str(self.base / "service.pub"),
                      "identity_agent": str(self.base / "agent.sock")}
        result = self.request(local_connection=connection)
        self.assertEqual(result["registry"]["nodes"][-1]["ssh"]["identity_agent"], connection["identity_agent"])
        self.assertNotIn("identity_agent", self.invitation["endpoints"][0])


if __name__ == "__main__":
    unittest.main()
