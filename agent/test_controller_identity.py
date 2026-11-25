# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controller transactions and isolated OpenSSH; never installs a service or contacts peers."""
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import controller_identity as ci


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux controller")
class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vci-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = ci.Paths(self.root / "etc", self.root / "system", self.root / "lib/controller.py",
                              self.root / "run", self.root, os.getuid())
        self.account = SimpleNamespace(pw_name="tester", pw_uid=os.getuid(), pw_dir=str(self.root), pw_gid=os.getgid())
        self.calls = []
        self.active = False
        self.fail_verify = False
        self.fail_stop = False
        self.provider = ci.Controller("tester", "host", paths=self.paths, runner=self.runner, account=self.account)

    def runner(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if "--version" in argv:
            return 0, "systemd 259 (fixture)"
        if argv[0].endswith("systemctl") and "show" in argv:
            return 0, ("LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nMainPID=2\nFragmentPath=" + str(self.provider.unit)
                       if self.active else "LoadState=not-found\nActiveState=inactive\nUnitFileState=\nMainPID=0\nFragmentPath=")
        if argv[0].endswith("ssh-keygen"):
            private = Path(argv[-1])
            private.write_text("synthetic fixture, not a key")
            private.with_suffix(".pub").write_text("ssh-ed25519 QUJDRA== fixture\n")
        if argv[0].endswith("systemd-creds") and "encrypt" in argv:
            Path(argv[-1]).write_bytes(b"synthetic encrypted envelope")
        if argv[0].endswith("systemd-analyze") and self.fail_verify:
            return 1, "not exported"
        if argv[0].endswith("systemctl") and "enable" in argv:
            self.active = True
        if argv[0].endswith("systemctl") and "disable" in argv:
            if self.fail_stop:
                return 1, "not exported"
            self.active = False
        return 0, ""

    def install(self):
        with patch.object(ci.os, "geteuid", return_value=0), patch.object(self.provider, "signature", return_value=True), patch.object(self.provider, "process_ready", return_value=True):
            return self.provider.install()

    def test_plan_does_not_create_files_or_call_key_tools(self):
        plan = self.provider.plan()
        self.assertFalse(plan["already_installed"])
        self.assertFalse(plan["routes_modified"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(any(c[0][0].endswith("ssh-keygen") for c in self.calls))

    def test_explicit_provider_and_owner_required(self):
        for owner in (None, "", "-root", "user\nroot"):
            with self.assertRaises(ValueError):
                ci.Controller(owner, "host", account=self.account)
        provider = ci.Controller("tester", paths=self.paths, runner=self.runner, account=self.account)
        with self.assertRaises(ValueError):
            provider.plan()

    def test_old_systemd_rejected_before_generation(self):
        self.provider.runner = lambda argv, **kw: (0, "systemd 249")
        with self.assertRaises(ValueError):
            self.provider.plan()
        self.assertFalse(self.provider.public.exists())

    def test_partial_provider_requires_recovery_without_rotating_key(self):
        self.provider.public.parent.mkdir(parents=True)
        self.provider.public.write_text("partial operator state")
        with self.assertRaises((OSError, ValueError)):
            self.provider.plan()
        self.assertFalse(any(c[0][0].endswith("ssh-keygen") for c in self.calls))

    def test_shared_helper_in_writable_or_symlink_parent_rejected_before_key_creation(self):
        self.paths.helper.parent.mkdir(mode=0o755)
        self.paths.helper.write_bytes(Path(ci.__file__).read_bytes())
        self.paths.helper.chmod(0o644)
        self.paths.helper.parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "Unprotected"):
            self.provider.plan()
        self.paths.helper.parent.chmod(0o755)
        moved = self.root / "moved-helper"
        self.paths.helper.parent.rename(moved)
        self.paths.helper.parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Unprotected"):
            self.provider.plan()
        self.assertFalse(any(c[0][0].endswith("ssh-keygen") for c in self.calls))

    def test_new_install_preserves_only_ciphertext_and_public(self):
        result = self.install()
        self.assertTrue(result["installed"])
        self.assertTrue(result["signature_valid"])
        self.assertEqual(self.provider.cipher.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.provider.public.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.paths.runtime.glob("vincula-controller-setup-*")), [])
        for _, kwargs in self.calls:
            self.assertNotIn("SSH_AUTH_SOCK", kwargs.get("env", {}))
        for path, (fd, *_rest) in self.provider.created.items():
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.provider.verify_installed()

    def test_idempotent_install_does_not_rotate_identity(self):
        self.install()
        public, cipher = self.provider.public.read_bytes(), self.provider.cipher.read_bytes()
        self.calls.clear()
        result = self.install()
        self.assertTrue(result["already_installed"])
        self.assertEqual(self.provider.public.read_bytes(), public)
        self.assertEqual(self.provider.cipher.read_bytes(), cipher)
        self.assertFalse(any(c[0][0].endswith("ssh-keygen") for c in self.calls))

    def test_idempotent_failure_retains_identity_for_enrolled_peers(self):
        self.install()
        before = self.provider.cipher.read_bytes()
        with patch.object(ci.os, "geteuid", return_value=0), patch.object(self.provider, "signature", return_value=False):
            with self.assertRaises(ValueError):
                self.provider.install()
        self.assertEqual(self.provider.cipher.read_bytes(), before)
        self.assertTrue(self.provider.unit.exists())

    def test_verification_failure_cleans_only_new_files_and_closes_pins(self):
        self.fail_verify = True
        with self.assertRaises(ValueError):
            self.install()
        for path, (fd, *_rest) in self.provider.created.items():
            self.assertFalse(path.exists())
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertFalse(any("disable" in c[0] for c in self.calls))

    def test_failed_readiness_stops_new_owned_service_before_cleanup(self):
        with patch.object(ci.os, "geteuid", return_value=0), patch.object(self.provider, "signature", return_value=False):
            with self.assertRaises(ValueError):
                self.provider.install()
        self.assertTrue(any("disable" in c[0] for c in self.calls))
        self.assertFalse(self.provider.cipher.exists())

    def test_failed_stop_retains_cipher_and_unit_for_recovery(self):
        self.fail_stop = True
        with patch.object(ci.os, "geteuid", return_value=0), patch.object(self.provider, "signature", return_value=False):
            with self.assertRaises(ValueError):
                self.provider.install()
        self.assertTrue(self.provider.cipher.exists())
        self.assertTrue(self.provider.unit.exists())
        self.assertTrue(self.provider.receipt.exists())

    def test_replaced_unit_is_not_disabled_or_deleted(self):
        self.fail_verify = True
        original = self.provider._rollback
        def replace_then_rollback():
            alternate = self.provider.unit.with_suffix(".replacement")
            alternate.write_bytes(self.provider.unit.read_bytes())
            os.replace(alternate, self.provider.unit)
            original()
        with patch.object(self.provider, "_rollback", side_effect=replace_then_rollback):
            with self.assertRaises(ValueError):
                self.install()
        self.assertTrue(self.provider.unit.exists())
        self.assertFalse(any("disable" in c[0] for c in self.calls))

    def test_partial_publish_failure_closes_unrecorded_fd(self):
        before = len(list(Path("/proc/self/fd").iterdir()))
        with patch.object(ci.os, "link", side_effect=OSError("fixture")):
            with self.assertRaises(ValueError):
                self.install()
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)
        self.assertEqual(self.provider.created, {})

    def test_private_plaintext_never_read_by_python(self):
        read = Path.read_bytes
        def guarded(path):
            if path.name == "identity":
                self.fail("Python read a plaintext private identity")
            return read(path)
        with patch.object(Path, "read_bytes", guarded):
            self.install()

    def test_nonroot_plan_skips_cipher_read_but_checks_metadata(self):
        self.install()
        read = self.provider._public_file
        def guarded(path, **kwargs):
            if path == self.provider.cipher:
                self.fail("Non-root plan attempted to read private envelope")
            return read(path, **kwargs)
        with patch.object(ci.os, "geteuid", return_value=1001), patch.object(self.provider, "_public_file", side_effect=guarded):
            self.assertFalse(self.provider.plan()["cipher_digest_verified"])

    def test_existing_modified_file_or_symlink_refused(self):
        self.install()
        for path in (self.provider.public, self.provider.cipher, self.provider.unit, self.paths.helper):
            before = path.read_bytes()
            path.write_bytes(b"foreign")
            with self.assertRaises(ValueError):
                self.provider.verify_installed()
            path.write_bytes(before)
        target = self.root / "foreign"
        target.write_bytes(self.provider.public.read_bytes())
        self.provider.public.unlink()
        self.provider.public.symlink_to(target)
        with self.assertRaises(OSError):
            self.provider.verify_installed()

    def test_no_fallback_to_environment_agent(self):
        with patch.dict(ci.os.environ, {"SSH_AUTH_SOCK": "/wrong/desktop.sock", "DISPLAY": ":bad"}):
            self.assertNotIn("DISPLAY", self.provider.environment(agent=True))
            self.assertEqual(self.provider.environment(agent=True)["SSH_AUTH_SOCK"], str(self.provider.socket))
            self.assertFalse(self.provider.signature())

    def test_unit_keeps_expansion_contexts_separate(self):
        self.assertEqual(ci.unit_word("/path/$literal%"), '"/path/$literal%%"')
        self.assertEqual(ci.unit_word("/path/$literal%", True), '"/path/$$literal%%"')
        unit = self.provider.unit_bytes().decode()
        self.assertIn("LoadCredentialEncrypted=", unit)
        self.assertIn("RuntimeDirectoryMode=0700", unit)
        self.assertIn("User=tester", unit)
        self.assertNotIn("--user", unit)


@unittest.skipUnless(sys.platform.startswith("linux") and all(shutil.which(x) for x in ("ssh-keygen", "ssh-agent", "ssh-add")), "OpenSSH Linux tools")
class OpenSSHIntegration(unittest.TestCase):
    def test_load_sign_restart_and_no_private_python_read(self):
        import pwd
        account = pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory(prefix="vci-ssh-") as directory, ExitStack() as cleanup:
            root = Path(directory)
            paths = ci.Paths(root / "etc", root / "units", root / "loader.py", root / "run", root, os.getuid())
            provider = ci.Controller(account.pw_name, paths=paths, account=account)
            provider.public.parent.mkdir(parents=True)
            provider.socket.parent.mkdir(parents=True, mode=0o700)
            creds = root / "credentials"
            creds.mkdir(mode=0o700)
            private = creds / ci.CREDENTIAL_NAME
            subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
            provider.public.write_bytes(private.with_suffix(".pub").read_bytes())
            original_read = Path.read_bytes
            def guarded(path):
                if path == private:
                    self.fail("private identity read by Python")
                return original_read(path)
            with patch.object(Path, "read_bytes", guarded), patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(creds), "SSH_AUTH_SOCK": "/wrong/desktop", "DISPLAY": ":invalid"}):
                for cycle in range(2):
                    process = subprocess.Popen(["/usr/bin/ssh-agent", "-D", "-a", str(provider.socket)],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        deadline = time.monotonic() + 5
                        while not provider.socket.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(provider.process_ready({"MainPID": str(process.pid), "FragmentPath": str(provider.unit)}))
                        self.assertFalse(provider.signature())
                        self.assertTrue(provider.load()["loaded"])
                        self.assertTrue(provider.signature())
                    finally:
                        process.terminate()
                        process.wait(timeout=5)
                    provider.socket.unlink(missing_ok=True)
                self.assertFalse(provider.signature())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd parser")
    def test_native_systemd_parser(self):
        import pwd
        account = pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory(prefix="vci-unit-") as directory:
            root = Path(directory)
            paths = ci.Paths(root / "config space%", root / "units", root / "loader space%.py", root / "run", root, os.getuid())
            provider = ci.Controller(account.pw_name, paths=paths, account=account)
            provider.unit.parent.mkdir()
            provider.unit.write_bytes(provider.unit_bytes())
            paths.helper.touch()
            result = subprocess.run(["/usr/bin/systemd-analyze", "verify", str(provider.unit)], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout.decode())


if __name__ == "__main__":
    unittest.main()
