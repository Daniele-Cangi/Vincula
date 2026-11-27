# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure artifact/test-result guards; no network, native compiler or desktop engine."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("managed_build_recipe", Path(__file__).with_name("build.py"))
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


class BuildArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="vincula-build-guards-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = self.root / "engine"
        self.engine.mkdir()
        (self.engine / "deskflow-core.exe").write_bytes(b"synthetic engine; never executed")
        (self.engine / "Qt6Core.dll").write_bytes(b"synthetic runtime")
        self.inputs = json.loads((recipe.RECIPE / "inputs.json").read_text(encoding="utf-8"))

    def test_canonical_manifest_is_reproducible_relative_and_schema_compatible(self):
        first, second = self.root / "one.json", self.root / "two.json"
        digest = recipe.write_artifact_manifest(self.engine, first, self.inputs)
        self.assertEqual(recipe.write_artifact_manifest(self.engine, second, self.inputs), digest)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        manifest = recipe.verify_artifact_manifest(first.read_bytes(), approved_sha256=digest).manifest
        self.assertEqual(dict(manifest.files), {
            "Qt6Core.dll": recipe.sha256(self.engine / "Qt6Core.dll"),
            "deskflow-core.exe": recipe.sha256(self.engine / "deskflow-core.exe"),
        })
        self.assertEqual(manifest.platform, "windows")
        self.assertEqual(set(manifest.capabilities), recipe.MANAGED_CAPABILITIES)
        self.assertEqual(first.with_suffix(".sha256").read_text(), digest + "\n")
        self.assertNotIn(str(self.root).encode(), first.read_bytes())

    def test_runtime_mutation_changes_manifest_pin(self):
        output = self.root / "manifest.json"
        original = recipe.write_artifact_manifest(self.engine, output, self.inputs)
        (self.engine / "Qt6Core.dll").write_bytes(b"changed")
        self.assertNotEqual(recipe.write_artifact_manifest(self.engine, output, self.inputs), original)

    def test_linked_file_is_rejected(self):
        original = Path.is_symlink

        def linked(path):
            return path.name == "Qt6Core.dll" or original(path)

        with patch.object(Path, "is_symlink", linked), self.assertRaisesRegex(ValueError, "link"):
            recipe.write_artifact_manifest(self.engine, self.root / "manifest.json", self.inputs)

    def test_unpinned_download_is_removed_before_any_use(self):
        item = {"filename": "tool.exe", "url": "https://example.invalid/tool.exe", "sha256": "0" * 64}
        with patch.object(recipe.urllib.request, "urlopen", return_value=io.BytesIO(b"unapproved tool")):
            with self.assertRaisesRegex(ValueError, "locked SHA256"):
                recipe.download(item, self.root)
        self.assertFalse((self.root / "tool.exe").exists())
        self.assertFalse((self.root / "tool.exe.partial").exists())

    def xml(self, *, remove=None, skip=None, duplicate=None):
        root = ET.Element("TestCase")
        for name in sorted(recipe.MANAGED_TESTS - ({remove} if remove else set())):
            function = ET.SubElement(root, "TestFunction", name=name)
            ET.SubElement(function, "Incident", type="pass")
            if name == skip:
                ET.SubElement(function, "Message", type="skip")
            if name == duplicate:
                ET.SubElement(root, "TestFunction", name=name)
        path = self.root / "managed.xml"
        ET.ElementTree(root).write(path, encoding="utf-8")
        return path

    def test_all_native_managed_cases_must_pass_without_skips(self):
        self.assertEqual(recipe.verify_managed_tests(self.xml()), 16)
        for changes in ({"remove": "provisionIsExclusiveAndSessionSpecific"},
                        {"skip": "privateControlBindsChallengeAndStops"},
                        {"duplicate": "privateControlRequiresLiveBackendLoop"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                recipe.verify_managed_tests(self.xml(**changes))

    def test_qt_license_texts_are_copied_from_corresponding_source(self):
        source = self.root / "source.tar"
        with tarfile.open(source, "w") as archive:
            for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt", "BSD-3-Clause.txt"):
                raw = ("fixture " + name).encode()
                member = tarfile.TarInfo("qtbase-6.10.2/LICENSES/" + name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        destination = self.root / "licenses"
        recipe.qt_licenses(source, destination)
        self.assertEqual(len(list(destination.iterdir())), 3)
        self.assertEqual((destination / "LGPL-3.0-only.txt").read_bytes(), b"fixture LGPL-3.0-only.txt")


if __name__ == "__main__":
    unittest.main()
