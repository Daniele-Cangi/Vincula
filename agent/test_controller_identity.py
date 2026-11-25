# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controller transactions and isolated OpenSSH; never installs a service or contacts peers."""
from contextlib import ExitStack
import ast
import errno
import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import controller_identity as ci


CREDENTIAL_PARSE_PROGRAM = r'''
import json,os,sys
try:import dbus
except ImportError:
 print(json.dumps({'skip':'OS Python D-Bus bindings unavailable'}));raise SystemExit(77)
address=os.environ.get('DBUS_SESSION_BUS_ADDRESS');runtime=os.environ.get('XDG_RUNTIME_DIR')
if not address or not runtime:
 print(json.dumps({'skip':'Existing user bus/runtime unavailable'}));raise SystemExit(77)
try:
 bus=dbus.bus.BusConnection(address)
 if not bus.name_has_owner('org.freedesktop.systemd1'):
  bus.close();print(json.dumps({'skip':'Existing user manager unavailable'}));raise SystemExit(77)
 manager=dbus.Interface(bus.get_object('org.freedesktop.systemd1','/org/freedesktop/systemd1'),'org.freedesktop.systemd1.Manager')
except dbus.DBusException:
 print(json.dumps({'skip':'Existing user manager inaccessible'}));raise SystemExit(77)
entries=json.load(sys.stdin);refs=[];results=[]
try:
 for entry in entries:
  name=entry['name'];manager.RefUnit(name);refs.append(name)
  obj=bus.get_object('org.freedesktop.systemd1',manager.GetUnit(name))
  props=dbus.Interface(obj,'org.freedesktop.DBus.Properties')
  active=str(props.Get('org.freedesktop.systemd1.Unit','ActiveState'))
  refused=bool(props.Get('org.freedesktop.systemd1.Unit','RefuseManualStart'))
  main=int(props.Get('org.freedesktop.systemd1.Service','MainPID'))
  executed=int(props.Get('org.freedesktop.systemd1.Service','ExecMainStartTimestampMonotonic'))
  assert active=='inactive' and refused and main==0 and executed==0
  parsed=[[str(a),str(b)] for a,b in props.Get('org.freedesktop.systemd1.Service','LoadCredentialEncrypted')]
  results.append({'parsed':parsed,'active':active,'refuse_start':refused,'pid':main,'exec_timestamp':executed})
finally:
 try:
  for name in reversed(refs):manager.UnrefUnit(name)
 finally:bus.close()
print(json.dumps({'properties':results}))
'''


def run_credential_parser(cases, environment):
    if not environment.get('DBUS_SESSION_BUS_ADDRESS') or not environment.get('XDG_RUNTIME_DIR'):
        return SimpleNamespace(returncode=77, stdout=json.dumps({'skip':'Existing user bus/runtime unavailable'})), True
    directory = Path(environment['XDG_RUNTIME_DIR']) / 'systemd/user'
    if not directory.is_dir():
        return SimpleNamespace(returncode=77, stdout=json.dumps({'skip':'Existing user runtime unit directory unavailable'})), True
    created = []
    with ExitStack() as pins:
        try:
            for case in cases:
                assert Path(case['name']).name == case['name']
                path = directory / case['name']
                raw = ('[Unit]\nRefuseManualStart=yes\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n' + case['line'] + '\n').encode()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                pins.callback(os.close, fd)
                info = os.fstat(fd)
                created.append((path, fd, (info.st_dev, info.st_ino), hashlib.sha256(raw).hexdigest()))
                with os.fdopen(os.dup(fd), 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            result = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', CREDENTIAL_PARSE_PROGRAM],
                                    input=json.dumps(cases), text=True, capture_output=True, timeout=30, env=environment)
        finally:
            for path, fd, identity, wanted in reversed(created):
                if not os.path.lexists(path):
                    continue
                info, pinned = path.lstat(), os.fstat(fd)
                assert stat.S_ISREG(info.st_mode)
                assert info.st_uid == os.getuid()
                assert info.st_nlink == 1
                assert (info.st_dev, info.st_ino) == identity == (pinned.st_dev, pinned.st_ino)
                assert hashlib.sha256(path.read_bytes()).hexdigest() == wanted
                path.unlink()
        removed = all(not os.path.lexists(path) for path, *_ in created)
    return result, removed


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

    def credential_acl(self, entries=None, *, version=2):
        if entries is None:
            entries = [(1, 4, 0xFFFFFFFF), (2, 4, self.account.pw_uid), (4, 0, 0xFFFFFFFF),
                       (16, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
        return struct.pack("<I", version) + b"".join(struct.pack("<HHI", *entry) for entry in entries)

    def credential_fixture(self, mode=0o400):
        directory = self.root / "credentials"
        directory.mkdir(mode=0o700)
        path = directory / ci.CREDENTIAL_NAME
        path.write_text("public metadata-only fixture\n")
        path.chmod(mode)
        return path

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

    def test_restrictive_umask_sets_only_new_directory_modes_and_preserves_existing(self):
        self.paths.configuration.mkdir(mode=0o750)
        self.paths.configuration.chmod(0o750)
        preserved = {path: path.stat().st_mode & 0o777 for path in (self.root, self.paths.configuration)}
        previous = os.umask(0o077)
        try:
            result = self.install()
        finally:
            os.umask(previous)
        self.assertTrue(result["installed"])
        for directory in (self.paths.runtime, self.paths.system, self.paths.helper.parent, self.provider.directory):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        for path, mode in preserved.items():
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.assertEqual(self.provider.public.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.paths.helper.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.provider.cipher.stat().st_mode & 0o777, 0o600)

    def test_private_staging_and_cipher_publication_ignore_permissive_umask(self):
        runner, link = self.provider.runner, os.link
        observed = []

        def inspect_staging(argv, **kwargs):
            if argv[0].endswith("ssh-keygen") or "encrypt" in argv:
                stage = Path(argv[-1]).parent
                self.assertEqual(stage.stat().st_uid, self.paths.admin_uid)
                self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
                self.assertEqual(stage.parent, self.paths.runtime)
                observed.append(stage)
            return runner(argv, **kwargs)

        def inspect_publication(source, destination, *args, **kwargs):
            if Path(destination) == self.provider.cipher:
                self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
                observed.append(Path(destination))
            return link(source, destination, *args, **kwargs)

        self.provider.runner = inspect_staging
        previous = os.umask(0o000)
        try:
            with patch.object(ci.os, "link", side_effect=inspect_publication):
                self.install()
        finally:
            os.umask(previous)
        self.assertEqual(len(observed), 3)
        self.assertFalse(observed[0].exists())
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[2], self.provider.cipher)
        self.assertEqual(stat.S_IMODE(self.provider.directory.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.provider.cipher.stat().st_mode), 0o600)

    def test_verification_rejects_exposed_cipher_without_changing_permissions(self):
        self.install()
        try:
            for mode in (0o640, 0o644, 0o666):
                self.provider.cipher.chmod(mode)
                for verify_digest in (False, True):
                    with self.subTest(mode=mode, digest=verify_digest), self.assertRaises(ValueError):
                        self.provider.verify_installed(cipher_digest=verify_digest)
                    self.assertEqual(stat.S_IMODE(self.provider.cipher.stat().st_mode), mode)
        finally:
            self.provider.cipher.chmod(0o600)

    def test_preexisting_private_admin_directory_is_not_widened_for_other_owner(self):
        self.root.chmod(0o755)
        self.paths.helper.parent.mkdir(mode=0o700)
        self.paths.helper.parent.chmod(0o700)
        self.paths.helper.write_bytes(Path(ci.__file__).read_bytes())
        self.paths.helper.chmod(0o644)
        self.provider.account = SimpleNamespace(pw_name="tester", pw_uid=os.getuid() + 1, pw_dir=str(self.root), pw_gid=os.getgid())
        with self.assertRaisesRegex(ValueError, "searchable.*inspect"):
            self.provider.plan()
        self.assertEqual(self.paths.helper.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(any(c[0][0].endswith("ssh-keygen") for c in self.calls))
        self.paths.helper.parent.chmod(0o750)
        self.assertFalse(self.provider.plan()["already_installed"])
        self.assertEqual(self.paths.helper.parent.stat().st_mode & 0o777, 0o750)

    def test_replaced_new_directory_is_not_chmodded_and_descriptor_is_closed(self):
        destination = self.root / "new-directory"
        foreign = self.root / "foreign-directory"
        foreign.mkdir(mode=0o700); foreign.chmod(0o700)
        original_open = os.open
        def replace_before_open(path, flags, *args, **kwargs):
            if Path(path) == destination:
                self.assertTrue(flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW)
                destination.rename(self.root / "original-directory")
                foreign.rename(destination)
            return original_open(path, flags, *args, **kwargs)
        before = len(list(Path("/proc/self/fd").iterdir()))
        with patch.object(ci.os, "open", side_effect=replace_before_open), self.assertRaisesRegex(ValueError, "identity changed"):
            self.provider._parents(destination)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)

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

    def test_credential_source_is_literal_and_only_escapes_specifier_percent(self):
        for path in ('/config with spaces/identity.cred', '/config%q/identity.cred',
                     '/config"quote/identity.cred', '/config\\backslash/identity.cred', '/config$literal/identity.cred'):
            with self.subTest(path=path):
                self.assertEqual(ci.credential_source("identity", Path(path)), "identity:" + path.replace("%", "%%"))
        expected = "LoadCredentialEncrypted=" + ci.CREDENTIAL_NAME + ":" + str(self.provider.cipher)
        self.assertIn(expected + "\n", self.provider.unit_bytes().decode())

    def test_credential_source_rejects_controls_or_relative_paths(self):
        for path in ('relative', '/config\nfile', '/config\0file', '/config\tfile', '/config\x7ffile'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ci.credential_source("identity", Path(path))
        with self.assertRaises(ValueError):
            ci.credential_source("identity\n", Path('/absolute'))

    def test_systemd_credential_acl_accepts_only_the_service_read_grant(self):
        raw = self.credential_acl()
        ci._systemd_credential_acl(raw, self.account.pw_uid, 0o440)
        entries = list(struct.iter_unpack("<HHI", raw[4:]))
        invalid = [raw[:-1], raw + b"\0", self.credential_acl(version=1)]
        changes = [(0, (1, 6, 0xFFFFFFFF)), (1, (2, 4, self.account.pw_uid + 1)),
                   (1, (2, 6, self.account.pw_uid)), (1, (8, 4, self.account.pw_gid)),
                   (2, (4, 4, 0xFFFFFFFF)), (3, (16, 6, 0xFFFFFFFF)),
                   (4, (32, 4, 0xFFFFFFFF)), (2, (128, 0, 0xFFFFFFFF)),
                   (0, (1, 4, 0)), (1, (0x1002, 4, self.account.pw_uid))]
        for index, replacement in changes:
            changed = list(entries); changed[index] = replacement
            invalid.append(self.credential_acl(changed))
        invalid.append(self.credential_acl([*entries, entries[1]]))
        for candidate in invalid:
            with self.subTest(acl=candidate.hex()), self.assertRaises(ValueError):
                ci._systemd_credential_acl(candidate, self.account.pw_uid, 0o440)
        with self.assertRaises(ValueError):
            ci._systemd_credential_acl(raw, self.account.pw_uid, 0o400)

    def test_credential_without_acl_keeps_the_existing_private_mode_guard(self):
        path = self.credential_fixture()
        for absent in (errno.ENODATA, errno.ENOTSUP):
            with patch.object(ci.os, "getxattr", side_effect=OSError(absent, "synthetic no ACL")):
                for mode in (0o400, 0o600, 0o700):
                    path.chmod(mode)
                    ci.check_system_credential(path, self.account.pw_uid)
                for mode in (0o440, 0o640, 0o644, 0o401):
                    path.chmod(mode)
                    with self.subTest(errno=absent, mode=mode), self.assertRaises(ValueError):
                        ci.check_system_credential(path, self.account.pw_uid)

    def test_credential_acl_metadata_rejects_default_malformed_and_unknown_errors(self):
        path = self.credential_fixture(0o440)
        descriptors = []
        original_open = os.open
        def capture_open(*args, **kwargs):
            fd = original_open(*args, **kwargs); descriptors.append(fd); return fd
        for default, access in ((b"unexpected default ACL", self.credential_acl()), (None, b"invalid"),
                                (None, b""), (None, OSError(errno.EIO, "synthetic metadata failure")),
                                (None, OSError("synthetic unknown metadata failure"))):
            def metadata(_fd, name):
                value = default if name == "system.posix_acl_default" else access
                if value is None:
                    raise OSError(errno.ENODATA, "synthetic absent ACL")
                if isinstance(value, OSError):
                    raise value
                return value
            with patch.object(ci.os, "open", side_effect=capture_open), patch.object(ci.os, "getxattr", side_effect=metadata):
                with self.assertRaises((ValueError, OSError)):
                    ci.check_system_credential(path, self.account.pw_uid)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_load_accepts_real_acl_metadata_without_reading_fixture_contents(self):
        private = self.credential_fixture()
        try:
            os.setxattr(private, "system.posix_acl_access", self.credential_acl())
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest("Fixture filesystem has no POSIX ACL support")
            raise
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o440)
        self.assertEqual(os.getxattr(private, "system.posix_acl_access"), self.credential_acl())
        with patch.dict(ci.os.environ, {"CREDENTIALS_DIRECTORY": str(private.parent)}), \
                patch.object(self.provider, "socket_owned", return_value=True), \
                patch.object(self.provider, "signature", return_value=True), \
                patch.object(ci.os, "read", side_effect=AssertionError("Credential bytes read by Python")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("Credential bytes read by Python")), \
                patch.object(Path, "open", side_effect=AssertionError("Credential bytes opened by Python stream")):
            self.assertTrue(self.provider.load()["loaded"])
        self.assertEqual([call[0] for call in self.calls], [["/usr/bin/ssh-add", str(private)]])

    def test_credential_metadata_refuses_symlink_hardlinks_and_foreign_owner(self):
        path = self.credential_fixture()
        link = self.root / "credential-link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            ci.check_system_credential(link, self.account.pw_uid)
        link.unlink(); os.link(path, link)
        with self.assertRaises(ValueError):
            ci.check_system_credential(path, self.account.pw_uid)
        link.unlink()
        original = os.fstat
        def foreign(fd):
            info = original(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=self.account.pw_uid + 1)
        with patch.object(ci.os, "fstat", side_effect=foreign), self.assertRaises(ValueError):
            ci.check_system_credential(path, self.account.pw_uid)

    def test_credential_metadata_refuses_path_replacement(self):
        path = self.credential_fixture()
        replacement = self.root / "replacement"
        replacement.write_text("foreign public fixture\n")
        def metadata(_fd, name):
            if name == "system.posix_acl_access":
                os.replace(replacement, path)
            raise OSError(errno.ENODATA, "synthetic absent ACL")
        with patch.object(ci.os, "getxattr", side_effect=metadata), self.assertRaisesRegex(ValueError, "identity changed"):
            ci.check_system_credential(path, self.account.pw_uid)
        self.assertEqual(path.read_text(), "foreign public fixture\n")

    def test_user_parser_closes_connection_when_unref_disconnects(self):
        tree = ast.parse(CREDENTIAL_PARSE_PROGRAM)
        transaction = next(node for node in tree.body if isinstance(node, ast.Try) and node.finalbody)
        cleanup = ast.Module(body=transaction.finalbody, type_ignores=[])
        program = compile(cleanup, '<parser-cleanup-fixture>', 'exec')
        observations = []
        disconnected = ConnectionError('synthetic user bus disconnected')
        def unref(_name):
            observations.append('unref')
            raise disconnected
        namespace = {'manager': SimpleNamespace(UnrefUnit=unref),
                     'bus': SimpleNamespace(close=lambda: observations.append('close')), 'refs': ['fixture.service']}
        with self.assertRaises(ConnectionError) as raised:
            exec(program, namespace)
        self.assertEqual(observations, ['unref', 'close'])
        self.assertIs(raised.exception, disconnected)

    def test_user_parser_parent_cleans_owned_file_and_closes_pin_after_timeout(self):
        directory = self.root / 'runtime/systemd/user'
        directory.mkdir(parents=True)
        environment = {'XDG_RUNTIME_DIR': str(self.root / 'runtime'), 'DBUS_SESSION_BUS_ADDRESS': 'synthetic-unconnected-address'}
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                name = 'vincula-credential-parser-' + uuid4().hex + '.service'
                path, descriptors = directory / name, []
                open_file = os.open
                def capture_open(candidate, *args, **kwargs):
                    descriptor = open_file(candidate, *args, **kwargs)
                    if Path(candidate) == path:
                        descriptors.append(descriptor)
                    return descriptor
                timeout = subprocess.TimeoutExpired('synthetic parser helper', 30)
                def helper_timeout(*_args, **_kwargs):
                    self.assertIn('RefuseManualStart=yes', path.read_text())
                    self.assertEqual(len(descriptors), 1)
                    self.assertEqual(os.fstat(descriptors[0]).st_ino, path.stat().st_ino)
                    if replaced:
                        foreign = path.with_suffix('.foreign')
                        foreign.write_text('foreign public fixture')
                        os.replace(foreign, path)
                    raise timeout
                with patch.object(os, 'open', side_effect=capture_open), patch.object(subprocess, 'run', side_effect=helper_timeout):
                    with self.assertRaises(AssertionError if replaced else subprocess.TimeoutExpired) as raised:
                        run_credential_parser([{'name': name, 'line': 'LoadCredentialEncrypted=fixture:/nonexistent.cred'}], environment)
                if replaced:
                    self.assertEqual(path.read_text(), 'foreign public fixture')
                    self.assertIs(raised.exception.__context__, timeout)
                else:
                    self.assertFalse(os.path.lexists(path))
                    self.assertIs(raised.exception, timeout)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)


@unittest.skipUnless(sys.platform.startswith("linux") and Path('/usr/bin/python3').is_file(), "Linux user-manager property parser")
class CredentialDirectiveParserTests(unittest.TestCase):
    def test_user_manager_parses_actual_directive_exactly_without_start(self):
        import pwd
        owner = pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory(prefix='vci-property-') as temporary:
            root = Path(temporary)
            cases = []
            for suffix in ('plain', 'spaces %q "quote" \\backslash $literal'):
                paths = ci.Paths(root / suffix, root / 'units', root / 'loader.py', root / 'run', root, os.getuid())
                provider = ci.Controller(owner.pw_name, paths=paths, account=owner)
                line = next(line for line in provider.unit_bytes().decode().splitlines() if line.startswith('LoadCredentialEncrypted='))
                cases.append({'name':'vincula-credential-parser-'+uuid4().hex+'.service', 'line':line,
                              'expected_source':str(provider.cipher)})
            environment = {key: os.environ[key] for key in ('DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR') if key in os.environ}
            environment.update(PATH='/usr/bin:/bin', LANG='C.UTF-8')
            result, parent_removed = run_credential_parser(cases, environment)
            if result.returncode == 77:
                self.skipTest(json.loads(result.stdout)['skip'])
            self.assertEqual(result.returncode, 0, result.stderr)
            proof = json.loads(result.stdout)
            self.assertTrue(parent_removed)
            self.assertEqual(len(proof['properties']), len(cases))
            for case, parsed in zip(cases, proof['properties']):
                self.assertEqual(parsed['parsed'], [[ci.CREDENTIAL_NAME, case['expected_source']]])
                self.assertEqual((parsed['active'], parsed['pid'], parsed['exec_timestamp']), ('inactive', 0, 0))
                self.assertTrue(parsed['refuse_start'])


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
