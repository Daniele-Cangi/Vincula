# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Public invitation round trips through the same JSON CLI used by both GUIs."""
import base64
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4


class EnrollmentCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.family = "windows" if sys.platform == "win32" else "linux"
        self.source_id = str(uuid4())
        self.registry = self.profile("source", self.source_id)
        name = b"ssh-ed25519"
        wire = struct.pack(">I", len(name)) + name + struct.pack(">I", 32) + bytes(range(32))
        self.host_key = "ssh-ed25519 " + base64.b64encode(wire).decode() + " public-fixture"
        self.endpoint = {"host": "192.0.2.60", "user": "operator", "port": 22, "network": "Ethernet"}

    def profile(self, name, local_id):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        path = directory / "nodes.json"
        path.write_text(json.dumps({"local": local_id, "nodes": [
            {"id": local_id, "label": name, "family": self.family, "aliases": []}]}), encoding="utf-8")
        return path

    def call(self, request, path=None):
        env = dict(os.environ, VINCULA_NODE_REGISTRY=str(path or self.registry), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-s", "-B", str(Path(__file__).with_name("nodes.py"))],
                                input=json.dumps(request), capture_output=True, text=True, encoding="utf-8",
                                env=env, timeout=15)
        reply = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if reply["ok"] else 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return reply

    def export(self):
        reply = self.call({"action": "enrollment_export", "endpoints": [self.endpoint],
                           "public_host_key": self.host_key})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["invitation"]["node"]["id"], self.source_id)
        return reply["invitation"]

    def test_export_preview_import_and_idempotent_retry_keep_public_identity(self):
        invitation = self.export()
        preview = self.call({"action": "enrollment_preview", "invitation_text": json.dumps(invitation)})
        self.assertEqual(preview, {"ok": True, "invitation": invitation})
        destination = self.profile("destination", str(uuid4()))
        before = self.call({"action": "list"}, destination)
        request = {"action": "enrollment_import", "invitation": invitation,
                   "expected_revision": before["revision"],
                   "expected_fingerprint": invitation["ssh_host_key"]["fingerprint"], "endpoint_index": 0,
                   "local_connection": {"identity_file": str(destination.parent / "controller.pub")}}
        imported = self.call(request, destination)
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["node_id"], self.source_id)
        self.assertEqual(imported["state"], "registered")
        self.assertEqual(imported["checks"], {"ssh": "unchecked", "runtime": "unchecked", "desktop": "unchecked"})
        self.assertEqual(len(imported["registry"]["nodes"]), 2)
        retry = self.call(request, destination)
        self.assertTrue(retry["ok"])
        self.assertEqual(retry["revision"], imported["revision"])
        request["local_connection"]["identity_file"] = str(destination.parent / "different.pub")
        conflict = self.call(request, destination)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "conflict")

    def test_preview_rejects_duplicate_fields_constants_and_oversized_input(self):
        for text in ('{"version":1,"version":1}', '{"version":NaN}', '{"version":Infinity}', " " * 32769):
            with self.subTest(text=text[:70]):
                result = self.call({"action": "enrollment_preview", "invitation_text": text})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_request")

    def test_import_requires_explicit_fingerprint_and_expected_revision(self):
        invitation = self.export()
        request = {"action": "enrollment_import", "invitation": invitation, "endpoint_index": 0,
                   "local_connection": {"identity_file": str(self.root / "controller.pub")}}
        result = self.call(request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
