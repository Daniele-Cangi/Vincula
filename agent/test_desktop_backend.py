# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import unittest

from desktop_backend import (ArtifactManifest, GroupManifest, PreflightCheck, PreflightReport,
                             Screen, VerifiedArtifact, canonical_json, verify_artifact_manifest)


IDS = tuple(f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 6))
SESSION = "10000000-0000-4000-8000-000000000001"
UPSTREAM = "081f6478e654a112168879e67a41ce7d7e4ae3af"
CAPABILITIES = ("backend_consent_v1", "managed_profile_v1", "mutual_tls_uuid_v1", "private_attestation_v1")


def group(clipboard=True, count=3):
    return GroupManifest(
        SESSION, IDS[0], tuple(Screen(IDS[i], i, 0) for i in range(count)), clipboard,
        tuple((IDS[i], str(i + 1) * 64) for i in range(count)),
        tuple((IDS[i], str(i + 5) * 64) for i in range(count)))


def artifact(platform="linux", managed=True, **changes):
    manifest = ArtifactManifest(
        "deskflow", "https://github.com/deskflow/deskflow", UPSTREAM, "1.27.0",
        "a" * 64 if managed else None, platform, "x86_64",
        "bin/deskflow-core.exe" if platform == "windows" else "bin/deskflow-core",
        (("bin/deskflow-core.exe" if platform == "windows" else "bin/deskflow-core", "b" * 64),
         ("lib/dependency.dll" if platform == "windows" else "lib/dependency.so", "c" * 64)),
        CAPABILITIES if managed else ())
    if changes:
        manifest = replace(manifest, **changes)
    raw = canonical_json(manifest.to_document())
    return verify_artifact_manifest(raw, approved_sha256=hashlib.sha256(raw).hexdigest())


class GroupManifestTests(unittest.TestCase):
    def test_common_digest_is_origin_independent_and_contains_no_local_paths(self):
        first = group()
        second = replace(first, screens=tuple(reversed(first.screens)), tls_pins=tuple(reversed(first.tls_pins)),
                         revisions=tuple(reversed(first.revisions)))
        self.assertEqual(first.to_document(), second.to_document())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(GroupManifest.from_document(first.to_document()), first)
        self.assertEqual(set(first.to_document()), {"schema_version", "session_id", "primary", "members",
                                                   "clipboard", "tls_pins", "revisions"})
        self.assertFalse(any(token in canonical_json(first.to_document()) for token in
                             (b"artifact_root", b"certificate_reference", b"credential", b"argv")))

    def test_layout_clipboard_and_local_revision_are_bound_to_the_digest(self):
        base = group()
        changed_revision = replace(base, revisions=((IDS[0], "f" * 64), *base.revisions[1:]))
        changed_layout = replace(base, screens=tuple(Screen(screen.node_id, screen.x, screen.y + 1) for screen in base.screens))
        changed_primary = replace(base, primary=IDS[1])
        for value in (changed_revision, changed_layout, changed_primary, replace(base, clipboard=False)):
            with self.subTest(value=value):
                self.assertNotEqual(base.digest, value.digest)

    def test_fourth_uuid_needs_no_platform_or_name_special_case(self):
        value = group(count=4)
        self.assertEqual(len(value.to_document()["members"]), 4)
        self.assertEqual(set(dict(value.tls_pins)), set(IDS[:4]))
        self.assertEqual(GroupManifest.from_document(value.to_document()), value)

    def test_invalid_members_and_disconnected_layouts_are_rejected(self):
        base = group()
        changes = [
            {"primary": IDS[4]},
            {"screens": (base.screens[0], base.screens[0])},
            {"screens": (base.screens[0], Screen(IDS[1], 0, 0))},
            {"screens": (base.screens[0], Screen(IDS[1], 2, 0))},
            {"screens": list(base.screens)},
            {"clipboard": 1},
            {"tls_pins": ((IDS[0], "a" * 64), (IDS[1], "a" * 64), (IDS[2], "b" * 64))},
            {"revisions": ((IDS[0], "a" * 64),)},
            {"tls_pins": ((IDS[0], "a" * 64), (IDS[0], "b" * 64), (IDS[2], "c" * 64))},
        ]
        for fields in changes:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(base, **fields)

    def test_schema_is_closed_bounded_and_rejects_bool_coordinates(self):
        base = group().to_document()
        values = [base | {"artifact_root": "/private/path"}, base | {"schema_version": True},
                  base | {"members": base["members"] * 30}, base | {"tls_pins": {}},
                  base | {"revisions": []}, base | {"members": [True, False]}]
        altered = deepcopy(base)
        altered["members"][0]["x"] = True
        values.append(altered)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                GroupManifest.from_document(value)
        for uuid in (1, None, IDS[0].upper().replace("4000", "A000"), "not-a-uuid"):
            with self.subTest(uuid=uuid), self.assertRaises(ValueError):
                Screen(uuid, 0, 0)
        for coordinate in (True, 65, -65, float("nan"), "1"):
            with self.subTest(coordinate=coordinate), self.assertRaises(ValueError):
                Screen(IDS[0], coordinate, 0)

    def test_manifest_is_immutable(self):
        value = group()
        with self.assertRaises(FrozenInstanceError):
            value.clipboard = False
        with self.assertRaises(FrozenInstanceError):
            value.screens[0].x = 9


class ArtifactTests(unittest.TestCase):
    def test_external_pin_required_and_modification_cannot_self_approve(self):
        approved = artifact()
        raw = canonical_json(approved.manifest.to_document())
        self.assertEqual(verify_artifact_manifest(raw, approved_sha256=approved.manifest_sha256), approved)
        changed = approved.manifest.to_document()
        changed["capabilities"] = []
        altered = canonical_json(changed)
        with self.assertRaises(ValueError):
            verify_artifact_manifest(altered, approved_sha256=approved.manifest_sha256)
        with self.assertRaises(ValueError):
            verify_artifact_manifest(raw, approved_sha256="f" * 64)
        with self.assertRaises(ValueError):
            VerifiedArtifact(approved.manifest, "f" * 64)

    def test_json_must_be_canonical_closed_and_have_no_duplicate_keys(self):
        base = canonical_json(artifact().manifest.to_document())
        duplicate = base[:-1] + b',"backend":"deskflow"}'
        extra = canonical_json(artifact().manifest.to_document() | {"trusted": True})
        malformed = [base + b"\n", duplicate, extra, b"[]", b"{", b"\xff", b"{}" * 600000]
        for raw in malformed:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                verify_artifact_manifest(raw, approved_sha256=hashlib.sha256(raw).hexdigest())

    def test_paths_cannot_traverse_alias_or_select_a_directory_as_file(self):
        base = artifact().manifest
        paths = ["../secret", "/absolute", "bin//engine", "bin/./engine", "bin\\engine",
                 "bin/a b", "bin/engine:stream", "bin/engine\n"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                replace(base, engine_file=path, files=((path, "a" * 64),))
        for files in [((base.engine_file, "a" * 64), (base.engine_file, "b" * 64)),
                      ((base.engine_file, "a" * 64), ("bin", "b" * 64))]:
            with self.subTest(files=files), self.assertRaises(ValueError):
                replace(base, files=files)
        windows = artifact("windows").manifest
        for name in ("bin/CON.exe", "NUL/engine.exe", "bin/name.", "bin/name "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                replace(windows, engine_file=name, files=((name, "a" * 64),))
        with self.assertRaises(ValueError):
            replace(windows, files=((windows.engine_file, "a" * 64), ("BIN/DESKFLOW-CORE.EXE", "b" * 64)))

    def test_metadata_and_containers_are_validated(self):
        base = artifact().manifest
        cases = [
            {"upstream_repository": "https://user:password@example.test/repo"},
            {"upstream_repository": "https://example.test/repo?token=secret"},
            {"upstream_repository": "file:///tmp/repo"},
            {"upstream_commit": "a" * 7},
            {"platform": []},
            {"architecture": True},
            {"capabilities": ["managed_profile_v1"]},
            {"capabilities": ("managed_profile_v1", "managed_profile_v1")},
            {"files": []},
            {"patch_sha256": "A" * 64},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(base, **change)


class PreflightContractTests(unittest.TestCase):
    def test_configuration_valid_never_means_launch_or_desktop_readiness(self):
        report = PreflightReport((PreflightCheck("artifact", "ok"),))
        self.assertTrue(report.configuration_valid)
        self.assertFalse(report.launchable)
        self.assertFalse(report.desktop_ready)
        self.assertEqual(report.to_document(), {"configuration_valid": True, "launchable": False,
                                              "desktop_ready": False, "checks": [{"code": "artifact", "status": "ok"}]})

    def test_empty_or_ambiguous_checks_cannot_pass(self):
        for checks in ((), [], (True,), (PreflightCheck("artifact", "ok"), PreflightCheck("artifact", "missing"))):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                PreflightReport(checks)
        for code, status in (("", "ok"), ("message\n", "ok"), ("artifact", []), ("artifact", "ready")):
            with self.subTest(code=code, status=status), self.assertRaises(ValueError):
                PreflightCheck(code, status)


if __name__ == "__main__":
    unittest.main()
