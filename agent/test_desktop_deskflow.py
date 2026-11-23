# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from desktop_deskflow import LocalPaths, NetworkProfile, PreflightFacts, compile_profile, preflight
from test_desktop_backend import IDS, artifact, group


def paths(platform="linux"):
    if platform == "windows":
        return LocalPaths(platform, r"C:\Vincula Engine", r"C:\Users\Admin\Vincula session",
                          r"C:\Users\Admin\private\Deskflow.pem")
    return LocalPaths(platform, "/opt/vincula-engine", "/home/test/Vincula session", "/home/test/private/Deskflow.pem")


def measured(node, package, local, graphics=None):
    return PreflightFacts(
        node, local.artifact_root, local.platform, "x86_64",
        profile_root=local.profile_root, certificate_reference=local.certificate_reference,
        graphics=graphics or ("windows" if local.platform == "windows" else "x11"),
        observed_files=package.manifest.files, executable_usable=True, private_profile_available=True,
        certificate_sha256=dict(group().tls_pins)[node], private_certificate_access=True, interactive_session=True,
        network_authorized=True, owner_input_consent=True, owner_clipboard_consent=True)


def statuses(report):
    return {check.code: check.status for check in report.checks}


class DeskflowCompilerTests(unittest.TestCase):
    def test_windows_primary_preview_uses_pinned_role_settings_and_public_trust(self):
        local, package, manifest = paths("windows"), artifact("windows"), group()
        result = compile_profile(manifest, IDS[0], package, local, NetworkProfile("0.0.0.0", 24800))
        self.assertEqual(result.argv, (r"C:\Vincula Engine\bin\deskflow-core.exe", "server", "--settings",
                                       r"C:\Users\Admin\Vincula session\Deskflow.conf"))
        files = dict(result.files)
        self.assertEqual(files["Deskflow.conf"], f'''[core]
computerName="vincula-{IDS[0]}"
interface="0.0.0.0"
port=24800
enableEnterCommand=false
enableExitCommand=false
useHooks=true

[security]
tlsEnabled=true
checkPeerFingerprints=true
certificate="C:/Users/Admin/private/Deskflow.pem"

[log]
toFile=false

[server]
externalConfig=true
externalConfigFile="C:/Users/Admin/Vincula session/layout.conf"
protocol=barrier
enableClipboard=true
clipboardSize=3
''')
        self.assertEqual(files["tls/trusted-clients"], "v2:sha256:" + "2" * 64 + "\nv2:sha256:" + "3" * 64 + "\n")
        self.assertEqual(files["tls/trusted-servers"], "")
        self.assertIn(f" right = vincula-{IDS[1]}", files["layout.conf"])
        self.assertEqual(result.common_manifest_sha256, manifest.digest)
        self.assertFalse(result.launchable)
        self.assertFalse(result.desktop_ready)

    def test_linux_client_has_one_endpoint_and_only_primary_trust(self):
        manifest = group()
        result = compile_profile(manifest, IDS[1], artifact(), paths(),
                                 NetworkProfile("192.0.2.20", 25000, "192.0.2.10"))
        files = dict(result.files)
        self.assertEqual(result.argv, ("/opt/vincula-engine/bin/deskflow-core", "client", "--settings",
                                       "/home/test/Vincula session/Deskflow.conf"))
        self.assertNotIn("layout.conf", files)
        self.assertIn('[client]\nremoteHost="192.0.2.10"\n', files["Deskflow.conf"])
        self.assertIn('port=25000\n', files["Deskflow.conf"])
        self.assertEqual(files["tls/trusted-servers"], "v2:sha256:" + "1" * 64 + "\n")
        self.assertEqual(files["tls/trusted-clients"], "")
        self.assertNotIn("externalConfig", files["Deskflow.conf"])

    def test_clipboard_off_is_explicit_for_every_role(self):
        manifest = group(False)
        for node in IDS[:3]:
            network = NetworkProfile("192.0.2.10", 24800, None if node == manifest.primary else "192.0.2.1")
            result = compile_profile(manifest, node, artifact(), paths(), network)
            ini = dict(result.files)["Deskflow.conf"]
            self.assertIn("enableClipboard=false\nclipboardSize=0\n", ini)
            self.assertIn("protocol=barrier\n", ini)
            self.assertNotIn("restoreToken", ini)
            self.assertFalse(result.launchable)

    def test_four_uuid_layout_is_reciprocal_and_primary_has_exactly_three_pins(self):
        manifest = group(count=4)
        result = compile_profile(manifest, IDS[0], artifact(), paths(), NetworkProfile("192.0.2.1", 24800))
        files = dict(result.files)
        self.assertEqual(files["layout.conf"].count("section: screens"), 1)
        for node in IDS[:4]:
            self.assertIn(" vincula-" + node + ":", files["layout.conf"])
        self.assertIn("  left = vincula-" + IDS[2], files["layout.conf"])
        self.assertIn("  right = vincula-" + IDS[3], files["layout.conf"])
        self.assertEqual(len(files["tls/trusted-clients"].splitlines()), 3)

    def test_unicode_spaces_and_quotes_stay_data_in_argv_and_ini(self):
        local = LocalPaths("linux", "/opt/engine's build", '/home/test/Schermo "à"', '/home/test/identity "à".pem')
        result = compile_profile(group(), IDS[0], artifact(), local, NetworkProfile("192.0.2.1", 24800))
        self.assertEqual(result.argv[0], "/opt/engine's build/bin/deskflow-core")
        self.assertEqual(result.argv[-1], '/home/test/Schermo "à"/Deskflow.conf')
        self.assertIn('certificate="/home/test/identity \\"à\\".pem"\n', dict(result.files)["Deskflow.conf"])
        self.assertEqual(len(result.argv), 4)
        self.assertNotIn("cmd.exe", result.argv)
        self.assertNotIn("/bin/sh", result.argv)

    def test_source_platform_member_and_network_roles_cannot_silently_fallback(self):
        manifest = group()
        cases = [
            (IDS[4], artifact(), paths(), NetworkProfile("192.0.2.1", 24800)),
            (IDS[0], artifact("windows"), paths(), NetworkProfile("192.0.2.1", 24800)),
            (IDS[0], artifact(version="1.26.0"), paths(), NetworkProfile("192.0.2.1", 24800)),
            (IDS[0], artifact(upstream_commit="f" * 40), paths(), NetworkProfile("192.0.2.1", 24800)),
            (IDS[0], artifact(), paths(), NetworkProfile("192.0.2.1", 24800, "192.0.2.2")),
            (IDS[1], artifact(), paths(), NetworkProfile("192.0.2.1", 24800)),
            (IDS[1], artifact(), paths(), NetworkProfile("0.0.0.0", 24800, "192.0.2.1")),
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                compile_profile(manifest, *values)

    def test_paths_follow_destination_platform_without_normalizing_traversal_or_devices(self):
        for platform, bad in [
            ("linux", "relative"), ("linux", "/x/../profile"), ("linux", "/x/./profile"),
            ("linux", "/x\n[security]\ntlsEnabled=false"), ("linux", "//remote/path"),
            ("windows", r"C:relative"), ("windows", r"\\server\share\profile"),
            ("windows", r"\\?\C:\profile"), ("windows", r"C:\x\..\profile"),
            ("windows", r"C:\x\NUL.txt"), ("windows", r"C:\x\file:stream"),
            ("windows", r"C:\x\trailing."), ("windows", r"C:\x\%ENV%"),
        ]:
            local = paths(platform)
            with self.subTest(platform=platform, bad=bad), self.assertRaises(ValueError):
                replace(local, profile_root=bad)
        local = paths()
        for profile in ("/opt/vincula-engine", "/opt/vincula-engine/profiles", "/opt"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                replace(local, profile_root=profile)

    def test_network_needs_literal_unicast_addresses_and_integer_port(self):
        for fields in [
            {"bind_address": True}, {"bind_address": 1}, {"bind_address": "::1"},
            {"bind_address": "224.0.0.1"}, {"bind_address": "255.255.255.255"},
            {"primary_address": "192.0.2.1,192.0.2.2"}, {"primary_address": "0.0.0.0"},
            {"primary_address": "224.0.0.1"}, {"primary_address": "255.255.255.255"},
            {"port": True}, {"port": 0}, {"port": 65536},
        ]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(NetworkProfile("192.0.2.1", 24800), **fields)

    def test_windows_device_aliases_are_rejected_in_every_local_path_reference(self):
        original = paths("windows")
        for name in ("NUL .txt", "COM1 .session", "CONIN$", "conout$.log"):
            for field in ("artifact_root", "profile_root", "certificate_reference"):
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    replace(original, **{field: "C:\\Users\\Admin\\" + name})


class DeskflowPreflightTests(unittest.TestCase):
    def setUp(self):
        self.group, self.local, self.package = group(), paths(), artifact()
        self.node = IDS[1]
        self.facts = measured(self.node, self.package, self.local)

    def assess(self, facts=None, package=None, manifest=None, approved=None):
        return preflight(manifest or self.group, self.node, package or self.package, self.local, facts or self.facts,
                         approved_artifact_sha256=approved or self.package.manifest_sha256)

    def test_all_declared_prerequisites_pass_but_feature_is_never_ready_or_launchable(self):
        report = self.assess()
        self.assertTrue(report.configuration_valid)
        self.assertTrue(all(value == "ok" for value in statuses(report).values()))
        self.assertFalse(report.launchable)
        self.assertFalse(report.desktop_ready)

    def test_stock_engine_or_wrong_external_approval_cannot_pass(self):
        stock = artifact(managed=False)
        report = self.assess(package=stock, approved=stock.manifest_sha256,
                             facts=measured(self.node, stock, self.local))
        self.assertEqual(statuses(report)["managed_engine"], "unsupported")
        self.assertFalse(report.configuration_valid)
        report = self.assess(approved="f" * 64)
        self.assertEqual(statuses(report)["artifact_approval"], "unsupported")
        self.assertFalse(report.configuration_valid)

    def test_unknown_facts_do_not_mean_false_or_zero_and_missing_artifact_is_explicit(self):
        unknown = PreflightFacts(self.node, self.local.artifact_root, "linux", "x86_64",
                                 self.local.profile_root, self.local.certificate_reference)
        report = preflight(self.group, self.node, None, self.local, unknown,
                           approved_artifact_sha256=self.package.manifest_sha256)
        self.assertTrue(all(value == "missing" for value in statuses(report).values()))
        self.assertFalse(report.configuration_valid)

    def test_content_tls_platform_network_and_owner_decisions_are_checked_individually(self):
        cases = [
            ("artifact_files", {"observed_files": ((self.package.manifest.engine_file, "f" * 64),)}),
            ("tls_identity", {"certificate_sha256": "f" * 64}),
            ("tls_private_access", {"private_certificate_access": False}),
            ("interactive_session", {"interactive_session": False}),
            ("owner_input_consent", {"owner_input_consent": False}),
            ("owner_clipboard_consent", {"owner_clipboard_consent": False}),
            ("data_route", {"network_authorized": False}),
            ("platform", {"architecture": "aarch64"}),
        ]
        for check, fields in cases:
            with self.subTest(check=check):
                report = self.assess(facts=replace(self.facts, **fields))
                self.assertEqual(statuses(report)[check], "unsupported")
                self.assertFalse(report.configuration_valid)
                self.assertFalse(report.launchable)
        with self.assertRaises(ValueError):
            self.assess(facts=replace(self.facts, node_id=IDS[2]))
        with self.assertRaises(ValueError):
            self.assess(facts=replace(self.facts, artifact_root="/different"))

    def test_clipboard_off_needs_no_clipboard_consent_but_input_consent_remains_required(self):
        report = self.assess(manifest=group(False), facts=replace(self.facts, owner_clipboard_consent=None))
        self.assertNotIn("owner_clipboard_consent", statuses(report))
        self.assertTrue(report.configuration_valid)
        report = self.assess(manifest=group(False), facts=replace(self.facts, owner_input_consent=None))
        self.assertEqual(statuses(report)["owner_input_consent"], "missing")
        self.assertFalse(report.configuration_valid)

    def test_facts_cannot_be_reused_for_an_unmeasured_profile_or_certificate(self):
        for platform in ("linux", "windows"):
            local, package = paths(platform), artifact(platform)
            facts = measured(self.node, package, local)
            changed_profile = (r"C:\Users\Admin\unmeasured-profile" if platform == "windows" else "/unmeasured/profile")
            changed_certificate = (r"C:\Users\Admin\unmeasured.pem" if platform == "windows" else "/unmeasured/identity.pem")
            for altered in (replace(local, profile_root=changed_profile),
                            replace(local, certificate_reference=changed_certificate),
                            replace(local, profile_root=changed_profile, certificate_reference=changed_certificate)):
                with self.subTest(platform=platform, altered=altered), self.assertRaises(ValueError):
                    preflight(self.group, self.node, package, altered, facts,
                              approved_artifact_sha256=package.manifest_sha256)
            report = preflight(self.group, self.node, package, local, facts,
                               approved_artifact_sha256=package.manifest_sha256)
            self.assertTrue(report.configuration_valid)
            self.assertFalse(report.launchable)
            self.assertFalse(report.desktop_ready)

    def test_windows_local_context_uses_destination_path_semantics(self):
        local, package = paths("windows"), artifact("windows")
        facts = measured(self.node, package, local)
        equivalent = LocalPaths("windows", local.artifact_root.replace("\\", "/").lower(),
                                local.profile_root.replace("\\", "/").lower(),
                                local.certificate_reference.replace("\\", "/").lower())
        report = preflight(self.group, self.node, package, equivalent, facts,
                           approved_artifact_sha256=package.manifest_sha256)
        self.assertTrue(report.configuration_valid)
        self.assertFalse(report.launchable)
        self.assertFalse(report.desktop_ready)

    def test_wayland_primary_continuous_clipboard_remains_unsupported(self):
        facts = measured(IDS[0], self.package, self.local, "wayland")
        report = preflight(self.group, IDS[0], self.package, self.local, facts,
                           approved_artifact_sha256=self.package.manifest_sha256)
        self.assertEqual(statuses(report)["continuous_wayland_primary_clipboard"], "unsupported")
        self.assertFalse(report.configuration_valid)
        no_clipboard = preflight(group(False), IDS[0], self.package, self.local, facts,
                                 approved_artifact_sha256=self.package.manifest_sha256)
        self.assertNotIn("continuous_wayland_primary_clipboard", statuses(no_clipboard))

    def test_facts_reject_malformed_containers_and_boolean_aliases(self):
        for changes in [{"graphics": []}, {"graphics": "unknown"}, {"platform": []}, {"architecture": True},
                        {"profile_root": None}, {"certificate_reference": "relative.pem"},
                        {"network_authorized": 1}, {"observed_files": []},
                        {"observed_files": (("../escape", "a" * 64),)},
                        {"observed_files": (("a", "b" * 64), ("a", "c" * 64))}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.facts, **changes)

    def test_compile_and_preflight_need_no_file_network_or_process_access(self):
        with patch("builtins.open", side_effect=AssertionError("No files")), \
                patch("os.open", side_effect=AssertionError("No OS files")), \
                patch.object(Path, "write_text", side_effect=AssertionError("No profiles")), \
                patch("subprocess.Popen", side_effect=AssertionError("No process")), \
                patch("socket.create_connection", side_effect=AssertionError("No network")):
            profile = compile_profile(self.group, self.node, self.package, self.local,
                                      NetworkProfile("192.0.2.2", 24800, "192.0.2.1"))
            report = self.assess()
        self.assertEqual(profile.node_id, self.node)
        self.assertTrue(report.configuration_valid)


if __name__ == "__main__":
    unittest.main()
