# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

import desktop_layout
import nodes


class DesktopLayoutTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(4)]
        self.document = {"local": self.ids[0], "nodes": [
            {"id": node_id, "label": "Untrusted label #\nend", "family":
             "windows" if index == 0 and sys.platform == "win32" else "linux"}
            for index, node_id in enumerate(self.ids)]}
        self.document["desktop"] = {"primary": self.ids[0], "clipboard": True, "members": [
            {"node_id": node_id, "x": index % 2, "y": index // 2}
            for index, node_id in enumerate(self.ids)]}

    def test_deterministic_reciprocal_connected_grid_with_uuid_names(self):
        result = desktop_layout.compile_layout(nodes.validate(self.document))
        reversed_document = deepcopy(self.document)
        reversed_document["nodes"].reverse()
        reversed_document["desktop"]["members"].reverse()
        self.assertEqual(result, desktop_layout.compile_layout(nodes.validate(reversed_document)))
        opposites = {"left": "right", "right": "left", "up": "down", "down": "up"}
        for node_id, neighbors in result["links"].items():
            for direction, peer in neighbors.items():
                self.assertEqual(result["links"][peer][opposites[direction]], node_id)
        self.assertNotIn("section: screens", result["layout"])
        for name in result["names"].values():
            self.assertEqual(result["server_options"]["computer_" + name], {"name": name})
        self.assertNotIn("Untrusted", result["layout"])
        self.assertEqual(result["names"][self.ids[0]], "vincula-" + self.ids[0])
        self.assertTrue(result["server_options"]["security"]["checkPeerFingerprints"])
        self.assertFalse(result["launchable"])

    def test_disconnected_diagonal_gaps_and_ambiguous_cells_rejected(self):
        for x, y in ((2, 0), (1, 1)):
            document = deepcopy(self.document)
            document["desktop"]["members"] = [
                {"node_id": self.ids[0], "x": 0, "y": 0},
                {"node_id": self.ids[1], "x": x, "y": y}]
            with self.subTest(position=(x, y)), self.assertRaisesRegex(ValueError, "Unreachable"):
                desktop_layout.compile_layout(nodes.validate(document))
        self.document["desktop"]["members"][1]["x"] = 0
        with self.assertRaisesRegex(ValueError, "unique"):
            nodes.validate(self.document)

    def test_preview_is_read_only_and_invalid_save_preserves_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nodes.json"
            path.write_text(__import__("json").dumps(self.document), encoding="utf-8")
            before = path.read_bytes()
            snapshot = nodes.snapshot(path)
            candidate = deepcopy(self.document["desktop"])
            candidate["clipboard"] = False
            result = nodes.execute({"action": "desktop_preview", "desktop": candidate,
                                    "expected_revision": snapshot["revision"]}, path)
            self.assertFalse(result["desktop_preview"]["server_options"]["server"]["enableClipboard"])
            self.assertEqual(result["desktop_preview"]["server_options"]["server"]["clipboardSize"], 0)
            self.assertEqual(path.read_bytes(), before)
            candidate["members"][1]["x"] = 20
            with self.assertRaisesRegex(ValueError, "Unreachable"):
                nodes.execute({"action": "desktop", "desktop": candidate}, path)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(nodes.ConflictError):
                nodes.execute({"action": "desktop_preview", "expected_revision": "stale"}, path)

    def test_preflight_never_confuses_files_or_ssh_with_verified_desktop_tls(self):
        with patch.object(desktop_layout, "local_engine", return_value="/test/deskflow-core"):
            report = desktop_layout.preview(nodes.validate(self.document))["preflight"]
        self.assertFalse(report["ready"])
        self.assertFalse(report["active"])
        checks = report["checks"]
        self.assertTrue(any(check["status"] == "present" for check in checks))
        self.assertTrue(all(check["status"] == "unverified" for check in checks if check["code"].startswith("tls_")))
        self.assertTrue(any(check["code"] == "session_supervisor" and check["status"] == "missing" for check in checks))

    def test_legacy_route_placeholder_does_not_claim_ssh_files_present(self):
        peer = self.document["nodes"][1]
        peer["family"] = "linux" if sys.platform == "win32" else "windows"
        peer["legacy_route"] = "ubuntu" if sys.platform == "win32" else "windows"
        checks = desktop_layout.preview(nodes.validate(self.document))["preflight"]["checks"]
        ssh = [check for check in checks if check["node_id"] == peer["id"] and check["code"].startswith("ssh_")]
        self.assertEqual(len(ssh), 1)
        self.assertEqual(ssh[0]["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
