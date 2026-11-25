# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep network observations outside the registry mutex used by local edits."""
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import node_enrollment
import node_registry
import nodes


class NodeCheckCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "nodes.json"
        self.node_id = str(uuid4())
        self.document = {"local": self.node_id, "nodes": [{"id": self.node_id, "label": "Origin",
            "family": "windows" if sys.platform == "win32" else "linux", "aliases": []}]}
        self.before = nodes.save(self.path, self.document)
        self.request = {"action": "enrollment_check", "node_id": self.node_id,
                        "expected_revision": self.before["revision"]}
        self.depth = 0
        self.module = ModuleType("node_probe")
        self.module.check_node = Mock(side_effect=self.observe)

    @contextmanager
    def guard(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def observe(self, path, **arguments):
        self.assertEqual(self.depth, 0, "Registry mutex held during network observation")
        self.assertEqual(path, self.path)
        self.assertEqual(arguments, {"node_id": self.node_id, "expected_revision": self.before["revision"]})
        return {**self.before, "node_check": {"node_id": self.node_id}}

    def test_dispatch_releases_outer_guard_before_probe(self):
        with patch.dict(sys.modules, {"node_probe": self.module}), \
                patch.object(node_registry, "registry_guard", self.guard):
            reply = nodes.execute(self.request, self.path)
        self.assertEqual(reply["node_check"]["node_id"], self.node_id)
        self.module.check_node.assert_called_once()
        self.assertEqual(self.path.read_text(), json.dumps(self.document, ensure_ascii=False, indent=2) + "\n")

    def test_main_does_not_reintroduce_the_process_wide_guard(self):
        output = io.StringIO()
        with patch.dict(sys.modules, {"node_probe": self.module}), \
                patch.object(node_registry, "registry_guard", self.guard), \
                patch.object(nodes, "registry_path", return_value=self.path), \
                patch.object(sys, "argv", ["nodes.py"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(self.request))), redirect_stdout(output):
            exit_code = nodes.main()
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.module.check_node.assert_called_once()

    def test_required_revision_and_closed_fields_precede_probe(self):
        cases = [{"action": "enrollment_check", "node_id": self.node_id},
                 {**self.request, "argv": ["arbitrary-command"]},
                 {**self.request, "registry_path": str(self.root / "other.json")}]
        with patch.dict(sys.modules, {"node_probe": self.module}):
            for request in cases:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    nodes.execute(request, self.path)
        self.module.check_node.assert_not_called()

    def test_probe_conflict_becomes_the_current_cli_conflict_class(self):
        self.module.check_node.side_effect = node_enrollment.EnrollmentConflict("The route changed.")
        with patch.dict(sys.modules, {"node_probe": self.module}):
            with self.assertRaises(nodes.ConflictError):
                nodes.execute(self.request, self.path)

    def test_main_serializes_conflict_without_a_traceback(self):
        self.module.check_node.side_effect = node_enrollment.EnrollmentConflict("The route changed.")
        output = io.StringIO()
        with patch.dict(sys.modules, {"node_probe": self.module}), \
                patch.object(nodes, "registry_path", return_value=self.path), \
                patch.object(sys, "argv", ["nodes.py"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(self.request))), redirect_stdout(output):
            exit_code = nodes.main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "conflict")

    def test_incomplete_runtime_keeps_manual_catalog_and_requests_update_for_checks(self):
        runtime = self.root / "minimal-runtime"
        runtime.mkdir()
        for name in ("nodes.py", "node_registry.py", "desktop_layout.py", "node_enrollment.py"):
            shutil.copyfile(Path(nodes.__file__).with_name(name), runtime / name)
        environment = dict(os.environ, VINCULA_NODE_REGISTRY=str(self.path))
        command = [sys.executable, "-E", "-S", "-B", str(runtime / "nodes.py")]
        for request in ({"action": "list"}, self.request):
            with self.subTest(action=request["action"]):
                result = subprocess.run(command, input=json.dumps(request), capture_output=True,
                                        text=True, encoding="utf-8", env=environment, timeout=15)
                reply = json.loads(result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                if request["action"] == "list":
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(reply, self.before)
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(reply["error"]["code"], "invalid_request")
                    self.assertIn("Update Vincula", reply["error"]["message"])


if __name__ == "__main__":
    unittest.main()
