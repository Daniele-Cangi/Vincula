# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reject stale/foreign provisioning before opening a local TLS profile."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import uuid4

import desktop_node as node


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.local, self.peer = str(uuid4()), str(uuid4())
        self.before = {"revision": "a" * 64, "registry": {"local": self.local,
                       "nodes": [{"id": self.local, "family": "linux", "label": "Local"},
                                 {"id": self.peer, "family": "windows", "label": "Peer"}]}}
        self.request = {"action": "provision", "node_id": self.local, "session_id": str(uuid4()),
                        "expected_revision": "b" * 64, "desktop": {"primary": self.peer,
                        "clipboard": True, "members": [{"node_id": self.local, "x": -1, "y": 0},
                                                       {"node_id": self.peer, "x": 0, "y": 0}]}}

    def test_stale_revision_never_provisions_or_edits_catalog(self):
        with patch.object(node, "catalog", return_value=deepcopy(self.before)), \
             patch.object(node, "deployment") as deployment, patch.object(node.nodes, "execute") as execute:
            with self.assertRaises(ValueError):
                node.execute(self.request)
            deployment.assert_not_called()
            execute.assert_not_called()

    def test_route_uuid_is_checked_before_any_desktop_artifact_access(self):
        with patch.object(node.nodes, "snapshot", return_value=deepcopy(self.before)), \
             patch.object(node, "deployment") as deployment:
            with self.assertRaises(ValueError):
                node.execute({"action": "inspect", "node_id": self.peer})
            deployment.assert_not_called()

    def test_caller_cannot_choose_executable_profile_path_or_private_credentials(self):
        for field in ("argv", "artifact_root", "profile", "certificate", "identity", "owner_consent"):
            with self.subTest(field=field), patch.object(node, "catalog") as catalog:
                with self.assertRaises(ValueError):
                    node.execute(dict(self.request, **{field: "caller-controlled"}))
                catalog.assert_not_called()

    def test_operations_do_not_expose_arbitrary_execution(self):
        for action in ("shell", "stop", "install", "unknown", None):
            with self.subTest(action=action), self.assertRaises(ValueError):
                node.execute({"action": action})


if __name__ == "__main__":
    unittest.main()
