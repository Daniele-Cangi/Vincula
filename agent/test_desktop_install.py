# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Temporary artifact files only. Never starts an engine, service, GUI or connection."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import desktop_backend as backend
import desktop_install as install
import desktop_managed as managed
import desktop_node


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def binary(family="linux", architecture="x86_64"):
    raw = bytearray(128)
    if family == "linux":
        raw[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", raw, 18, 62 if architecture == "x86_64" else 183)
    else:
        raw[:2] = b"MZ"
        struct.pack_into("<I", raw, 60, 64)
        raw[64:68] = b"PE\0\0"
        struct.pack_into("<H", raw, 68, 0x8664 if architecture == "x86_64" else 0xAA64)
    return bytes(raw)


class ArtifactInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private-desktop"
        self.package = self.base / "package";self.package.mkdir()
        (self.package / "bin").mkdir();(self.package / "lib").mkdir()
        self.family = "windows" if sys.platform == "win32" else "linux"
        self.engine_name = "bin/deskflow-core.exe" if self.family == "windows" else "bin/deskflow-core"
        self.engine_bytes = binary(self.family)
        self.dependency = "lib/runtime.dll" if self.family == "windows" else "lib/runtime.so"
        (self.package / self.engine_name).write_bytes(self.engine_bytes)
        (self.package / self.dependency).write_bytes(b"synthetic dependency, never loaded")
        self.manifest = backend.ArtifactManifest("deskflow", install.deskflow.UPSTREAM_REPOSITORY,
            install.deskflow.UPSTREAM_COMMIT, "1.27.0", install.PATCH_SHA256, self.family, "x86_64",
            self.engine_name, ((self.engine_name,digest(self.engine_bytes)),
                (self.dependency,digest((self.package/self.dependency).read_bytes()))),
            tuple(install.deskflow.MANAGED_CAPABILITIES))
        self.manifest_file = self.package / "manifest.json"
        self.write_manifest()
        self.stack = ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(install, "default_root", return_value=self.root))
        self.stack.enter_context(patch.object(install, "host_target", return_value=(self.family,"x86_64")))
        self.stack.enter_context(patch.dict(os.environ, {"VINCULA_DESKTOP_ARTIFACT_ROOT":str(self.root/"engines/approved")}))

    def write_manifest(self):
        self.raw = backend.canonical_json(self.manifest.to_document());self.pin = digest(self.raw)
        self.manifest_file.write_bytes(self.raw)

    def installer(self, **kwargs):
        return install.Installer(self.package,self.manifest_file,self.pin,enabled=True,**kwargs)

    def test_plan_is_read_only_and_reports_changes_not_readiness(self):
        plan=self.installer().plan()
        self.assertFalse(self.root.exists());self.assertFalse(plan["desktop_ready"])
        self.assertFalse(plan["engine_started"]);self.assertFalse(plan["service_installed"])
        self.assertEqual(plan["deployment"]["port"],24807);self.assertTrue(plan["deployment"]["enabled"])
        self.assertEqual(len(plan["changes"]),3)

    def test_isolated_cli_help_imports_helpers_without_creating_state(self):
        working=self.base/"isolated-cwd";working.mkdir()
        result=subprocess.run([sys.executable,"-IB",str(Path(install.__file__).resolve()),"--help"],
            cwd=working,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("--approved-sha256",result.stdout);self.assertIn("--enable",result.stdout)
        self.assertEqual(list(working.iterdir()),[]);self.assertFalse(self.root.exists())

    def test_install_matches_real_deployment_and_approval_readers(self):
        candidate=self.installer(port=24809);result=candidate.install()
        self.assertTrue(result["installed"]);self.assertTrue(result["already_installed"])
        self.assertEqual(managed.artifact_approval(digest(self.engine_bytes),candidate.engine),candidate.approval)
        with patch.object(desktop_node,"home",return_value=self.root):
            deployment, artifact, engine=desktop_node.deployment()
        self.assertEqual(deployment,candidate.deployment);self.assertEqual(artifact.manifest_sha256,self.pin)
        self.assertEqual(engine,candidate.engine)
        if sys.platform != "win32":
            for directory in (self.root,candidate.version,candidate.files_root):
                self.assertEqual(directory.stat().st_mode & 0o777,0o700)
            self.assertEqual(candidate.engine.stat().st_mode & 0o777,0o700)
            self.assertEqual(candidate.approval_path.stat().st_mode & 0o777,0o600)

    def test_identical_retry_verifies_without_replacing_files(self):
        candidate=self.installer();candidate.install()
        paths=(candidate.engine,candidate.approval_path,self.root/"deployment.json")
        before=[(p.stat().st_ino,p.read_bytes()) for p in paths]
        self.assertTrue(candidate.install()["already_installed"])
        self.assertEqual(before,[(p.stat().st_ino,p.read_bytes()) for p in paths])

    def test_wrong_external_pin_is_rejected_before_any_mutation(self):
        with self.assertRaises(ValueError):install.Installer(self.package,self.manifest_file,"0"*64,enabled=True)
        self.assertFalse(self.root.exists())

    def test_noncanonical_manifest_even_with_its_own_digest_is_rejected(self):
        self.manifest_file.write_bytes(json.dumps(self.manifest.to_document()).encode())
        with self.assertRaises(ValueError):install.Installer(self.package,self.manifest_file,digest(self.manifest_file.read_bytes()),enabled=True)
        self.assertFalse(self.root.exists())

    def test_owner_enable_and_explicit_port_validation(self):
        with self.assertRaises(ValueError):install.Installer(self.package,self.manifest_file,self.pin,enabled=False)
        for port in (True,0,1023,65536):
            with self.subTest(port=port),self.assertRaises(ValueError):self.installer(port=port)
        self.assertFalse(self.root.exists())

    def test_wrong_platform_arch_source_patch_or_capabilities_are_rejected(self):
        original=self.manifest
        for changes in ({"platform":"linux" if self.family=="windows" else "windows"},
                        {"architecture":"aarch64"},{"upstream_commit":"0"*40},
                        {"upstream_repository":"https://example.invalid/deskflow"},{"patch_sha256":"0"*64},
                        {"patch_sha256":None},{"capabilities":()}):
            self.manifest=replace(original,**changes);self.write_manifest()
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.installer()
        self.assertFalse(self.root.exists())

    def test_engine_header_must_match_even_when_the_hash_is_approved(self):
        (self.package/self.engine_name).write_bytes(binary(self.family,"aarch64"))
        files=dict(self.manifest.files);files[self.engine_name]=digest((self.package/self.engine_name).read_bytes())
        self.manifest=replace(self.manifest,files=tuple(files.items()));self.write_manifest()
        with self.assertRaises(ValueError):self.installer().install()
        self.assertFalse(self.root.exists())

    def test_changed_dependency_or_unlisted_file_refused_before_root_creation(self):
        dependency=self.package/self.dependency;before=dependency.read_bytes();dependency.write_bytes(b"changed")
        with self.assertRaises(ValueError):self.installer().install()
        dependency.write_bytes(before);(self.package/"extra.dll").write_bytes(b"shadow library")
        with self.assertRaises(ValueError):self.installer().install()
        self.assertFalse(self.root.exists())

    def test_package_symlink_and_hardlink_are_rejected(self):
        dependency=self.package/self.dependency;original=self.base/"dependency";dependency.rename(original)
        try:dependency.symlink_to(original)
        except OSError:self.skipTest("No symlink permission")
        with self.assertRaises(ValueError):self.installer().install()
        dependency.unlink();os.link(original,dependency)
        with self.assertRaises(ValueError):self.installer().install()
        self.assertFalse(self.root.exists())

    def test_case_aliases_are_rejected(self):
        if self.family=="windows":self.skipTest("Case-insensitive native filesystem")
        (self.package/"LIB").mkdir();(self.package/"LIB/other.so").write_bytes(b"alias")
        self.manifest=replace(self.manifest,files=(*self.manifest.files,("LIB/other.so",digest(b"alias"))))
        self.write_manifest()
        with self.assertRaises(ValueError):self.installer().install()
        self.assertFalse(self.root.exists())

    def test_file_and_total_size_limits_apply_before_mutating(self):
        with patch.object(install,"MAX_FILE_BYTES",64),self.assertRaises(ValueError):self.installer().install()
        with patch.object(install,"MAX_TOTAL_BYTES",128),self.assertRaises(ValueError):self.installer().install()
        self.assertFalse(self.root.exists())

    def test_existing_deployment_conflict_preserves_everything(self):
        candidate=self.installer();candidate.install();before=(self.root/"deployment.json").read_bytes()
        with self.assertRaises(ValueError):self.installer(port=24808).install()
        self.assertEqual((self.root/"deployment.json").read_bytes(),before)

    def test_different_approval_receipt_is_never_overwritten(self):
        candidate=self.installer();candidate.install()
        candidate.approval_path.write_bytes(b'{"protocol":1}')
        before=candidate.approval_path.read_bytes()
        with self.assertRaises(ValueError):candidate.install()
        self.assertEqual(candidate.approval_path.read_bytes(),before)

    def test_tampered_installed_dependency_prevents_idempotent_success(self):
        candidate=self.installer();candidate.install()
        (candidate.files_root/self.dependency).write_bytes(b"edited")
        with self.assertRaises(ValueError):candidate.install()

    def test_publish_failure_rolls_back_only_created_payloads(self):
        candidate=self.installer();link=os.link
        def fail(source,target,**kwargs):
            if Path(target)==self.root/"deployment.json":raise OSError("synthetic failure")
            return link(source,target,**kwargs)
        with patch.object(install.os,"link",side_effect=fail),self.assertRaises(OSError):candidate.install()
        self.assertFalse((self.root/"deployment.json").exists())
        self.assertFalse(candidate.approval_path.exists());self.assertFalse(candidate.version.exists())
        self.assertTrue(candidate.install()["installed"])

    def test_replaced_owned_inode_is_not_removed_during_rollback(self):
        candidate=self.installer();link=os.link;replaced=[]
        def fail(source,target,**kwargs):
            if Path(target)==self.root/"deployment.json":
                original=install.plain(candidate.approval_path)
                replacement=candidate.approval_path.with_name("replacement.json")
                replacement.write_bytes(candidate.approval_path.read_bytes())
                self.assertNotEqual(install.plain(replacement),original)
                os.replace(replacement,candidate.approval_path);replaced.append(candidate.approval_path.read_bytes())
                raise OSError("synthetic replacement")
            return link(source,target,**kwargs)
        with patch.object(install.os,"link",side_effect=fail),self.assertRaises(OSError):candidate.install()
        self.assertEqual(candidate.approval_path.read_bytes(),replaced[0])

    @unittest.skipIf(sys.platform=="win32","POSIX mode guard")
    def test_existing_broad_root_is_not_repaired(self):
        self.root.mkdir(mode=0o755)
        with self.assertRaises(ValueError):self.installer().install()
        self.assertEqual(self.root.stat().st_mode & 0o777,0o755)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_windows_descriptor_has_numeric_sid_medium_and_no_owner_rights(self):
        value=install.windows_sddl("S-1-5-21-123-456-789-1001")
        self.assertIn("S-1-5-18",value);self.assertIn("S-1-5-32-544",value)
        self.assertIn("S:(ML;OICI;NW;;;S-1-16-8192)",value)
        self.assertNotIn(";;;OW",value);self.assertNotIn("S-1-3-4",value)

    def test_install_lock_blocks_another_original_open(self):
        candidate=self.installer();candidate.install()
        with candidate.lock(), self.assertRaises(OSError):
            with self.installer().lock():pass

    @unittest.skipIf(sys.platform=="win32","POSIX parent integrity")
    def test_shared_writable_parent_is_refused_without_permission_repair(self):
        self.base.chmod(0o777)
        with self.assertRaises(ValueError):self.installer().install()
        self.assertFalse(self.root.exists());self.assertEqual(self.base.stat().st_mode & 0o777,0o777)

    def test_existing_profiles_and_unrelated_approvals_are_preserved(self):
        store=install.Store();store.mkdir(self.root)
        marker=self.root/"owner-data.json"
        fd=store.open(marker,create=True)
        with os.fdopen(fd,"wb") as stream:stream.write(b"keep existing owner configuration")
        before=(marker.stat().st_ino,marker.read_bytes())
        self.installer().install()
        self.assertEqual((marker.stat().st_ino,marker.read_bytes()),before)

    @unittest.skipIf(sys.platform=="win32","Windows API path mocked on POSIX; native suite uses real APIs")
    def test_windows_store_flow_with_private_creation_and_lock_at_byte_zero(self):
        calls=[]
        class FakeWindows:
            def create_directory(self,path):calls.append(('mkdir',path));path.mkdir(mode=0o700)
            def verify_parent(self,path):calls.append(('parent',path));self.verify(path)
            def verify(self,path):
                self_info=path.lstat()
                if self_info.st_mode & 0o077:raise PermissionError('synthetic broad ACL')
            def open(self,path,*,create=False,write=False):
                calls.append(('open',path,create,write))
                flags=(os.O_RDWR|os.O_CREAT|os.O_EXCL) if create else os.O_RDWR if write else os.O_RDONLY
                return os.open(path,flags|os.O_NOFOLLOW,0o600)
        def locking(fd,mode,count):
            self.assertEqual(os.lseek(fd,0,os.SEEK_CUR),0);self.assertEqual(count,1)
        (self.package/self.engine_name).write_bytes(binary("windows"))
        files=dict(self.manifest.files);files[self.engine_name]=digest(binary("windows"))
        self.manifest=replace(self.manifest,platform="windows",files=tuple(files.items()));self.write_manifest()
        with patch.object(install.sys,"platform","win32"),patch.object(install,"host_target",return_value=("windows","x86_64")), \
             patch.object(install,"InstallerWindowsSecurity",return_value=FakeWindows()), \
             patch.dict(sys.modules,{"msvcrt":SimpleNamespace(LK_NBLCK=1,locking=locking)}):
            self.assertTrue(self.installer().install()["installed"])
        self.assertTrue(any(row[0]=='open' and row[2] for row in calls))
        self.assertTrue(any(row[0]=='mkdir' for row in calls))
        self.assertTrue(any(row[0]=='parent' for row in calls))

    def test_windows_parent_accepts_read_traverse_and_inherit_only_aces(self):
        for mask in (0x100020,0x1200a9,0x80000000,0x20000000,0x20000,0x100000):
            with self.subTest(mask=hex(mask)):
                self.assertTrue(install.parent_ace_allowed(0,0,mask,False))
        self.assertTrue(install.parent_ace_allowed(0,8,0x1f01ff,False))
        self.assertTrue(install.parent_ace_allowed(1,0,0x1f01ff,False))
        self.assertTrue(install.parent_ace_allowed(0,0,0x1f01ff,True))

    def test_windows_parent_rejects_every_effective_mutation_bit(self):
        for mask in (0x10000000,0x40000000,0x10000,0x40000,0x80000,0x40,0x2,0x4,0x10,0x100):
            with self.subTest(mask=hex(mask)):
                self.assertFalse(install.parent_ace_allowed(0,0,mask,False))
                self.assertFalse(install.parent_ace_allowed(0,3,mask|0x1200a9,False))
        self.assertFalse(install.parent_ace_allowed(9,0,0,False))

    @unittest.skipUnless(sys.platform=="win32","Native Windows default parent metadata only")
    def test_windows_native_default_parent_integrity(self):
        install.InstallerWindowsSecurity().verify_parent(Path(os.environ["LOCALAPPDATA"]))

    @unittest.skipIf(sys.platform=="win32","POSIX directory fsync failure fixture")
    def test_failure_after_deployment_publication_preserves_committed_artifact(self):
        candidate=self.installer();fsync=os.fsync
        def fail(descriptor):
            if (self.root/"deployment.json").exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("synthetic durability receipt failure")
            return fsync(descriptor)
        with patch.object(install.os,"fsync",side_effect=fail),self.assertRaises(OSError):candidate.install()
        self.assertTrue(candidate.engine.exists());self.assertTrue(candidate.approval_path.exists())
        self.assertEqual(json.loads((self.root/"deployment.json").read_bytes()),candidate.deployment)
        self.assertTrue(candidate.plan()["already_installed"])

    def test_existing_incomplete_deployment_is_never_filled_in_implicitly(self):
        candidate=self.installer();store=install.Store();store.mkdir(self.root)
        with os.fdopen(store.open(self.root/"deployment.json",create=True),"wb") as stream:
            stream.write(backend.canonical_json(candidate.deployment))
        with self.assertRaises(ValueError):candidate.install()
        self.assertFalse(candidate.version.exists());self.assertFalse(candidate.approval_path.exists())


if __name__=="__main__":unittest.main()
