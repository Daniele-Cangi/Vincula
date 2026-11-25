# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated setup tests; optional native unit parsing, never service start/root/network."""
from contextlib import closing
from dataclasses import replace
import ast
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core_service as core
if sys.platform.startswith("linux"):
    import fcntl


class FakeCommands:
    def __init__(self, case):
        self.case, self.calls = case, []
        self.old_active, self.old_enabled, self.system_active = True, True, False
        self.system_enabled = "disabled"
        self.fail_verify = self.fail_start = self.lose_policy_ack = self.replace_payload = False
        self.profile_permissions = ""
        self.mutate_grants = False
        self.no_user_manager = False

    def __call__(self, argv, **options):
        self.calls.append((list(argv), options))
        user = options.get("user", False)
        if argv[0].endswith("systemctl"):
            args = [s for s in argv[1:] if s not in ("--user", "--no-reload")]
            if args[0] == "show":
                old = args[1] == core.OLD_UNIT
                if old and self.no_user_manager:
                    return ""
                active = self.old_active if old else self.system_active
                enabled = ("enabled" if self.old_enabled else "disabled") if old else self.system_enabled
                return "\n".join(("LoadState=loaded", "ActiveState=" + ("active" if active else "inactive"),
                                  "UnitFileState=" + enabled,
                                  "MainPID=" + ("42" if old else "84"),
                                  "FragmentPath=" + str(self.case.installer.old_file if old else self.case.installer.unit_file)))
            if args[0] == "is-enabled":
                return "enabled" if self.old_enabled else "disabled"
            if args[0] in {"disable", "enable"}:
                if user:
                    self.old_enabled = args[0] == "enable"
                else:
                    self.system_active = args[0] == "enable"
                    self.system_enabled = "enabled" if args[0] == "enable" else "disabled"
                    if self.system_active and self.fail_start:
                        if self.replace_payload:
                            path = self.case.installer.unit_file
                            original = (path.stat().st_dev, path.stat().st_ino)
                            replacement = path.with_name("replacement.service")
                            replacement.write_bytes(path.read_bytes())
                            changed = (replacement.stat().st_dev, replacement.stat().st_ino)
                            self.case.assertNotEqual(original, changed, "Replacement must exist before the old inode is unlinked")
                            os.replace(replacement, path)
                            self.case.assertEqual((path.stat().st_dev, path.stat().st_ino), changed)
                        raise RuntimeError("Simulated start failure")
            if args[0] in {"stop", "start"} and user:
                self.old_active = args[0] == "start"
            return ""
        if argv[0].endswith("systemd-analyze"):
            if self.fail_verify:
                raise RuntimeError("Simulated native verification failure")
            return ""
        if argv[0].endswith("nmcli"):
            return self.case.settings.network_profile + "\n" + self.profile_permissions
        if argv[-1] == "shutdown":
            self.old_active = self.system_active = False
            return ""
        if "-c" in argv:
            # The generated owner command runs only against a test-created fixture.
            code = argv[-1]
            if "core_owner_setup" in code and self.mutate_grants:
                code = code.replace(" tables=table_digests(db)\n", " db.execute('DELETE FROM grants')\n tables=table_digests(db)\n")
            exec(compile(code, "<isolated-owner-command>", "exec"), {})
            if "core_owner_setup" in code and self.lose_policy_ack:
                raise RuntimeError("Simulated lost commit acknowledgment")
            return ""
        raise AssertionError("Unexpected command: " + repr(argv))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux Core installer")
class CoreServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o700)
        self.home, self.release = self.base / "receiver-home", self.base / "release with space"
        self.state = self.home / ".local/share/vincula-jobs"
        self.state.mkdir(parents=True)
        (self.release / "agent").mkdir(parents=True)
        self.database = self.state / "jobs.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('ask');
                CREATE TABLE direct_execution_policy(id INTEGER PRIMARY KEY, enabled INTEGER); INSERT INTO direct_execution_policy VALUES(1,0);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT); CREATE TABLE programs(name TEXT,argv TEXT);
                INSERT INTO programs VALUES('preserved','["example"]');
                CREATE TABLE grants(id TEXT); INSERT INTO grants VALUES('owner-grant');
                CREATE TABLE worker_environment(id INTEGER PRIMARY KEY,path TEXT,pathext TEXT);
                INSERT INTO worker_environment VALUES(1,'/usr/local/custom tools:/usr/bin','');
            """)
        fixture = """from pathlib import Path
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace
ROOT=Path(HOME_STATE)
DATABASE=ROOT/'jobs.sqlite3'
@contextmanager
def transaction():
 with closing(sqlite3.connect(DATABASE)) as db, db:
  yield db
def mode(db):return db.execute('SELECT mode FROM policy').fetchone()[0]
def set_mode(db,value):db.execute('UPDATE policy SET mode=?',(value,))
def support(db):return {'enabled':bool(db.execute('SELECT enabled FROM direct_execution_policy').fetchone()[0])}
def set_enabled(db,value):db.execute('UPDATE direct_execution_policy SET enabled=?',(int(value),))
direct_execution=SimpleNamespace(support=support,set_enabled=set_enabled)
""".replace("HOME_STATE", repr(str(self.state)))
        source = Path(__file__).with_name("jobs.py").read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "transaction")
        transaction = "".join(source.splitlines(keepends=True)[node.decorator_list[0].lineno - 1:node.end_lineno])
        start, end = fixture.index("@contextmanager\ndef transaction():"), fixture.index("\ndef mode")
        fixture = fixture[:start] + transaction + "\n" + fixture[end:]
        files = {}
        for name in core.HELPERS:
            raw = fixture.encode() if name == "jobs.py" else b"# isolated helper\n"
            (self.release / "agent" / name).write_bytes(raw)
            (self.state / name).write_bytes(raw)
            files["agent/" + name] = {"sha256": core.sha(raw), "bytes": len(raw)}
        self.manifest = {"source_commit": "a" * 40, "files": files}
        (self.release / "_release-manifest.json").write_text(json.dumps(self.manifest, indent=2))
        self.settings = core.Settings("receiver", self.release, core.sha(json.dumps(self.manifest, indent=2).encode()), "full", "enable", Path("/usr/bin/python3"))
        self.account = SimpleNamespace(pw_name="receiver", pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home))
        self.assertNotEqual(self.account.pw_uid, 0, "Tests must run as an unprivileged user")
        self.paths = core.Paths(self.base / "system", self.base / "maintenance", self.base / "libexec", self.base / "proc", self.base, os.getuid())
        self.commands = FakeCommands(self)
        self.make_installer()
        self.installer.old_file.parent.mkdir(parents=True)
        self.installer.old_file.write_text("[Service]\nExecStart=/old/worker\n")

    def make_installer(self, **changes):
        self.settings = replace(self.settings, **changes)
        self.installer = core.Installer(self.settings, paths=self.paths, runner=self.commands, account=self.account)
        # Fake only external OS observations; database/hash/publication remain real.
        self.installer.ready = lambda: self.commands.system_active
        self.installer.lock_owners = lambda: [42] if self.commands.old_active else []
        return self.installer

    def apply(self):
        with patch.object(core.os, "geteuid", return_value=0):
            return self.installer.install()

    def observe_payload_fds(self):
        self.assertTrue(self.installer.created_fds)
        for path, descriptor in self.installer.created_fds.items():
            info = os.fstat(descriptor)
            self.assertEqual((info.st_dev, info.st_ino), self.installer.created[path][0])
        return list(self.installer.created_fds.values())

    def assert_payload_fds_closed(self, descriptors):
        self.assertTrue(descriptors)
        self.assertEqual(self.installer.created_fds, {})
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_compiler_is_generic_and_has_no_desktop_or_outgoing_identity(self):
        plan = self.installer.plan()
        unit = plan["payloads"][self.installer.unit_file].decode()
        self.assertIn("User=receiver", unit)
        self.assertIn(str(self.release), unit)
        self.assertIn('Environment="PATH=/usr/local/custom tools:/usr/bin"', unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("UnsetEnvironment=DISPLAY", unit)
        self.assertNotIn("NoNewPrivileges", unit)
        self.assertNotIn("ProtectHome", unit)
        self.assertNotIn("PrivateTmp", unit)
        self.assertNotIn("IdentityFile", unit)
        self.assertFalse(any("native" in str(call) for call in self.commands.calls))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "Native systemd unit parser unavailable")
    def test_native_systemd_verify_accepts_scalar_home_spaces_and_percent(self):
        home = self.base / "receiver home %q"
        (home / ".local/share/vincula-jobs").mkdir(parents=True)
        release = self.base / "release with spaces %q"; release.mkdir()
        owner = core.pwd.getpwuid(os.getuid())
        account = SimpleNamespace(pw_name=owner.pw_name, pw_uid=owner.pw_uid, pw_dir=str(home))
        settings = replace(self.settings, owner=owner.pw_name, release=release)
        installer = core.Installer(settings, paths=self.paths, account=account, runner=self.commands)
        unit = self.base / core.UNIT
        raw = installer.payloads("/usr/bin")[installer.unit_file]
        unit.write_bytes(raw)
        self.assertIn("WorkingDirectory=" + str(installer.state).replace("%", "%%") + "\n", raw.decode())
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "SYSTEMD_LOG_LEVEL": "warning", "SYSTEMD_OFFLINE": "1"}
        result = subprocess.run([shutil.which("systemd-analyze"), "--man=no", "--generators=no", "verify", str(unit)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_explicit_choices_and_explicit_root_owner_protected_paths(self):
        for changes in ({"access": None}, {"native_execution": None}, {"access": "default"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.settings, **changes)
        root = SimpleNamespace(pw_name="root", pw_uid=0, pw_dir=str(self.home))
        installer = core.Installer(replace(self.settings, owner="root"), account=root)
        with self.assertRaisesRegex(ValueError, "OS-protected"):
            installer.plan()

    def test_root_path_readonly_validation_allows_os_python_symlink(self):
        core.root_owned(Path("/usr/bin/python3"), interpreter=True)
        with self.assertRaisesRegex(ValueError, "OS-protected"):
            core.root_owned(self.release)

    def test_windows_import_is_safe_and_entrypoint_refuses_platform(self):
        import runpy
        with patch.object(sys, "platform", "win32"):
            namespace = runpy.run_path(str(Path(core.__file__)), run_name="core_windows_import_probe")
            self.assertIsNone(namespace["fcntl"])
            self.assertIsNone(namespace["pwd"])
            with self.assertRaisesRegex(ValueError, "requires Linux"):
                namespace["main"]()

    def test_manifest_pin_and_existing_helper_must_match_before_install(self):
        (self.state / "wire_framing.py").write_text("changed")
        with self.assertRaisesRegex(ValueError, "helper mismatch"):
            self.installer.plan()
        self.assertFalse(self.paths.system.exists())
        self.assertFalse(self.paths.maintenance.exists())

    def test_manifest_duplicate_key_is_rejected(self):
        path = self.release / "_release-manifest.json"
        path.write_text('{"source_commit":"' + "a" * 40 + '","source_commit":"' + "a" * 40 + '"}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.installer.plan()

    def test_unprivileged_install_fails_before_any_os_write(self):
        with self.assertRaisesRegex(ValueError, "root-only"):
            self.installer.install()
        self.assertFalse(self.paths.maintenance.exists())

    def test_success_preserves_database_and_helpers_and_replay_does_not_restart(self):
        before = self.installer.state_snapshot()
        helpers = {n: (self.state / n).read_bytes() for n in core.HELPERS}
        result = self.apply()
        self.assertTrue(result["applied"], result)
        self.assertEqual(self.installer.state_snapshot()["policy"], ["full", 1])
        self.assertEqual(self.installer.state_snapshot()["tables"], before["tables"])
        self.assertEqual(helpers, {n: (self.state / n).read_bytes() for n in core.HELPERS})
        backup = Path(result["backup"])
        with closing(sqlite3.connect(backup / "jobs.sqlite3.backup")) as db:
            self.assertEqual(db.execute("SELECT mode FROM policy").fetchone()[0], "ask")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in backup.iterdir()))
        mutations = len([c for c, _ in self.commands.calls if "enable" in c])
        second = self.apply()
        self.assertTrue(second["already_configured"])
        self.assertEqual(len([c for c, _ in self.commands.calls if "enable" in c]), mutations)
        self.assertFalse(result["reboot_verified"])
        self.assertFalse(result["desktop_ready"])

    def test_native_verification_failure_rolls_back_before_old_worker_stop(self):
        self.commands.fail_verify = True
        result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assertFalse(self.installer.unit_file.exists())
        self.assertTrue(self.commands.old_active)
        self.assertTrue(self.commands.old_enabled)
        self.assertFalse(any("shutdown" in c for c, _ in self.commands.calls))
        self.assertEqual(self.installer.state_snapshot()["policy"], ["ask", 0])

    def test_failed_start_restores_active_but_previously_disabled_user_worker(self):
        self.commands.old_enabled = False
        self.commands.fail_start = True
        result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assertTrue(self.commands.old_active)
        self.assertFalse(self.commands.old_enabled)
        self.assertFalse(self.installer.unit_file.exists())

    def test_rollback_refuses_replaced_inode_even_with_same_payload(self):
        self.commands.fail_start = self.commands.replace_payload = True
        result = self.apply()
        self.assertFalse(result["rollback"], result)
        self.assertTrue(result["manual_review_required"])
        self.assertTrue(self.installer.unit_file.exists())
        self.assertFalse(any("disable" in c and "--now" in c and core.UNIT in c for c, _ in self.commands.calls))
        self.assertEqual(len([c for c, _ in self.commands.calls if "shutdown" in c]), 1)

    def test_payload_fds_remain_pinned_through_cutover_and_close_after_success(self):
        self.make_installer(network_profile="9197b042-28a2-426d-b768-54dce4ea01e5", expected_ipv4="192.0.2.20")
        verify, policy = self.installer._native_verify, self.installer._policy
        observed = []
        def verify_with_pins():
            observed.extend(self.observe_payload_fds())
            verify()
        def policy_with_pins(before):
            self.assertEqual(set(self.observe_payload_fds()), set(observed))
            policy(before)
        self.installer._native_verify, self.installer._policy = verify_with_pins, policy_with_pins
        result = self.apply()
        self.assertTrue(result["applied"], result)
        self.assertEqual(len(observed), 3)
        self.assert_payload_fds_closed(observed)

    def test_payload_fds_stay_open_after_rollback_unlink_and_close_at_exit(self):
        self.commands.fail_verify = True
        rollback = self.installer._rollback
        observed = []
        def rollback_with_pins(plan):
            observed.extend(self.observe_payload_fds())
            rollback(plan)
            self.assertTrue(all(os.fstat(fd).st_nlink == 0 for fd in observed))
        self.installer._rollback = rollback_with_pins
        result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assert_payload_fds_closed(observed)

    def test_unlink_recreate_cannot_reuse_pinned_original_inode(self):
        self.commands.fail_start = True
        runner, observed = self.installer.runner, []
        def replace_at_start(command, **options):
            if "enable" in command and "--now" in command and core.UNIT in command:
                observed.extend(self.observe_payload_fds())
                path = self.installer.unit_file
                raw = path.read_bytes(); path.unlink()
                self.assertEqual(os.fstat(observed[0]).st_nlink, 0)
                path.write_bytes(raw)
                self.assertNotEqual((path.stat().st_dev, path.stat().st_ino), self.installer.created[path][0])
            return runner(command, **options)
        self.installer.runner = replace_at_start
        result = self.apply()
        self.assertFalse(result["rollback"], result)
        self.assertTrue(result["manual_review_required"])
        self.assertTrue(self.installer.unit_file.exists())
        self.assertFalse(any("disable" in c and "--now" in c and core.UNIT in c for c, _ in self.commands.calls))
        self.assert_payload_fds_closed(observed)

    def test_publication_failure_closes_descriptor_even_before_ledger_entry(self):
        open_file, observed = os.open, []
        def capture_open(path, flags, *args, **kwargs):
            descriptor = open_file(path, flags, *args, **kwargs)
            if Path(path).name.startswith("." + core.UNIT + "."):
                observed.append(descriptor)
            return descriptor
        with patch.object(core.os, "open", side_effect=capture_open), patch.object(core.os, "link", side_effect=OSError("Simulated publication failure")):
            result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assertFalse(self.installer.unit_file.exists())
        self.assert_payload_fds_closed(observed)

    def test_lost_policy_ack_preserves_new_worker_and_committed_owner_choices(self):
        self.commands.lose_policy_ack = True
        result = self.apply()
        self.assertTrue(result["policy_committed"], result)
        self.assertTrue(result["manual_review_required"])
        self.assertNotIn("rollback", result)
        self.assertTrue(self.commands.system_active)
        self.assertEqual(self.installer.state_snapshot()["policy"], ["full", 1])

    def test_active_work_blocks_plan(self):
        for state in core.ACTIVE:
            with self.subTest(state=state):
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute("DELETE FROM jobs"); db.execute("INSERT INTO jobs VALUES('id',?)", (state,))
                with self.assertRaisesRegex(ValueError, "work prevents"):
                    self.installer.plan()

    def test_historical_uncertainty_is_preserved_with_current_worker_lock(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO jobs VALUES('old-powerloss','unconfirmed')")
        self.assertEqual(self.installer.plan()["before"]["active"], 0)
        before = self.installer.state_snapshot()["tables"]
        result = self.apply()
        self.assertTrue(result["applied"], result)
        self.assertEqual(self.installer.state_snapshot()["tables"], before)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id='old-powerloss'").fetchone()[0], "unconfirmed")

    def test_unowned_lock_blocks_setup_even_with_only_historical_rows(self):
        self.commands.old_active = False
        self.installer.lock_owners = lambda: [99]
        with self.assertRaisesRegex(ValueError, "Unowned worker lock"):
            self.installer.plan()
        self.commands.old_active = True
        with self.assertRaisesRegex(ValueError, "worker lock not confirmed"):
            self.installer.plan()

    def test_actual_transaction_uses_row_factory_and_shared_value_digest(self):
        import runpy
        fixture = runpy.run_path(str(self.installer.job))
        with fixture["transaction"]() as db:
            self.assertIs(db.row_factory, sqlite3.Row)
            self.assertEqual(core.table_digests(db), self.installer.state_snapshot()["tables"])
        result = self.apply()
        self.assertTrue(result["applied"], result)

    def test_root_role_rejects_untrusted_existing_bytecode_before_runtime_load(self):
        cache = self.release / "agent/__pycache__"; cache.mkdir()
        bytecode = cache / "direct_execution.cpython-test.pyc"; bytecode.write_bytes(b"untrusted existing cache")
        original = Path.lstat
        def os_metadata(path):
            info = original(path)
            return SimpleNamespace(st_uid=os.getuid() if path == cache else 0, st_mode=info.st_mode & ~0o022)
        root = SimpleNamespace(pw_name="root", pw_uid=0, pw_dir=str(self.home))
        installer = core.Installer(replace(self.settings, owner="root"), account=root, runner=self.commands)
        with patch.object(Path, "lstat", os_metadata), self.assertRaisesRegex(ValueError, "OS-protected"):
            installer.plan()
        self.assertFalse(any("-c" in command for command, _ in self.commands.calls))
        self.assertEqual(bytecode.read_bytes(), b"untrusted existing cache")

    def test_readonly_root_cache_validation_checks_entry_not_just_directory(self):
        cache = self.release / "agent/__pycache__"; cache.mkdir()
        bytecode = cache / "direct_execution.cpython-test.pyc"; bytecode.write_bytes(b"cache")
        original = Path.lstat
        def os_metadata(path):
            info = original(path)
            return SimpleNamespace(st_uid=os.getuid() if path == bytecode else 0, st_mode=info.st_mode & ~0o022)
        with patch.object(Path, "lstat", os_metadata), self.assertRaisesRegex(ValueError, "OS-protected"):
            core.root_caches(cache.parent)

    def test_policy_side_effect_on_existing_grants_rolls_back_transaction(self):
        before = self.installer.state_snapshot()
        self.commands.mutate_grants = True
        result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assertEqual(self.installer.state_snapshot(), before)
        self.assertTrue(self.commands.old_active)

    def test_worker_path_is_preserved_or_explicitly_bootstrapped(self):
        original = self.installer.plan()["payloads"][self.installer.unit_file]
        self.assertIn(b"PATH=/usr/local/custom tools:/usr/bin", original)
        self.make_installer(worker_path="/different")
        self.assertIn(b"PATH=/different", self.installer.plan()["payloads"][self.installer.unit_file])
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM worker_environment")
        self.make_installer(worker_path=None)
        with self.assertRaisesRegex(ValueError, "Worker PATH"):
            self.installer.plan()
        self.make_installer(worker_path="/opt/receiver/bin:/usr/bin")
        payload = self.installer.plan()["payloads"][self.installer.unit_file]
        self.assertIn(b"PATH=/opt/receiver/bin:/usr/bin", payload)

    def test_worker_path_rejects_empty_relative_or_control_entries(self):
        for value in ("", "/usr/bin::/bin", "relative:/bin", "/bin:\n/usr/bin", "/bin:\x00"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Worker PATH"):
                replace(self.settings, worker_path=value)

    def test_real_readiness_checks_process_owner_argv_and_exclusive_lock(self):
        self.commands.old_active, self.commands.system_active = False, True
        self.paths.proc.mkdir()
        root = self.paths.proc / "84"; root.mkdir()
        (root / "cmdline").write_bytes(b"\0".join(os.fsencode(s) for s in self.installer.worker_argv()) + b"\0")
        self.installer.lock_owners = lambda: [84]
        self.assertTrue(core.Installer.ready(self.installer))
        (root / "cmdline").write_bytes(b"python3\0unrelated.py\0worker\0" + str(self.installer.job).encode() + b"\0")
        self.assertFalse(core.Installer.ready(self.installer))
        (root / "cmdline").write_bytes(b"/usr/bin/python3\0-I\0-B\0/unrelated.py\0" + str(self.installer.job).encode() + b"\0worker\0")
        self.assertFalse(core.Installer.ready(self.installer))
        (root / "cmdline").unlink()
        self.assertFalse(core.Installer.ready(self.installer))

    def test_actual_lock_parser_uses_device_inode_and_kernel_flock_owner(self):
        lock = self.state / "worker.lock"; lock.touch()
        info = lock.stat()
        self.paths.proc.mkdir()
        (self.paths.proc / "locks").write_text(f"1: FLOCK ADVISORY WRITE 84 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n")
        self.assertEqual(core.Installer.lock_owners(self.installer), [84])

    def test_root_payload_parent_symlink_is_rejected_without_target_write(self):
        destination = self.base / "unrelated"; destination.mkdir()
        self.paths.system.symlink_to(destination, target_is_directory=True)
        result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assertFalse(list(destination.iterdir()))

    def test_network_profile_is_explicit_public_metadata_and_atomic_payloads(self):
        profile = "9197b042-28a2-426d-b768-54dce4ea01e5"
        self.make_installer(network_profile=profile, expected_ipv4="192.0.2.20")
        result = self.apply()
        self.assertTrue(result["applied"], result)
        helper = self.paths.libexec / "core-network.py"
        self.assertEqual(helper.read_bytes(), core.NETWORK_HELPER)
        dropin = self.paths.system / "ssh.service.d/95-vincula-core-network.conf"
        self.assertIn(profile, dropin.read_text())
        self.assertIn("192.0.2.20", dropin.read_text())
        self.assertFalse(any("secret" in c or "reload" in c and "ssh.service" in c for c, _ in self.commands.calls))
        self.assertEqual(helper.stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(helper.parent.glob(".*")))

    def test_user_only_network_profile_rejected_before_cutover(self):
        self.make_installer(network_profile="9197b042-28a2-426d-b768-54dce4ea01e5", expected_ipv4="192.0.2.20")
        self.commands.profile_permissions = "user:receiver:"
        result = self.apply()
        self.assertTrue(result["rollback"], result)
        self.assertTrue(self.commands.old_active)
        self.assertFalse(self.installer.unit_file.exists())

    def test_installer_lock_prevents_concurrent_cutover(self):
        self.installer._root_parents(self.paths.maintenance)
        lock = self.paths.maintenance / "setup.lock"
        with lock.open("w") as stream:
            lock.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(core.os, "geteuid", return_value=0), self.assertRaises(BlockingIOError):
                self.installer.install()
        self.assertFalse(self.installer.unit_file.exists())

    def test_root_payload_symlink_and_conflicting_configuration_rejected(self):
        self.paths.system.mkdir()
        self.installer.unit_file.symlink_to(self.database)
        with self.assertRaises(OSError):
            self.installer.plan()
        self.installer.unit_file.unlink()
        self.installer.unit_file.write_text("operator-owned different setup")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.installer.plan()

    def test_existing_system_worker_is_not_repaired_or_reconfigured_implicitly(self):
        self.paths.system.mkdir()
        self.installer.unit_file.write_bytes(self.installer.payloads("/usr/local/custom tools:/usr/bin")[self.installer.unit_file])
        self.installer.unit_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "separate repair"):
            self.apply()
        self.assertTrue(self.commands.old_active)

    def test_preexisting_active_compatible_worker_lifecycle_is_never_owned(self):
        self.paths.system.mkdir()
        self.installer.unit_file.write_bytes(self.installer.payloads("/usr/local/custom tools:/usr/bin")[self.installer.unit_file])
        self.installer.unit_file.chmod(0o644)
        self.commands.old_active, self.commands.system_active = False, True
        with self.assertRaisesRegex(ValueError, "separate repair"):
            self.apply()
        self.assertTrue(self.commands.system_active)
        self.assertFalse(any("shutdown" in c or "disable" in c for c, _ in self.commands.calls))

    def test_active_but_not_persistently_enabled_is_not_already_configured(self):
        self.paths.system.mkdir()
        self.installer.unit_file.write_bytes(self.installer.payloads("/usr/local/custom tools:/usr/bin")[self.installer.unit_file])
        self.installer.unit_file.chmod(0o644)
        self.commands.old_active, self.commands.system_active = False, True
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE policy SET mode='full'")
            db.execute("UPDATE direct_execution_policy SET enabled=1")
        for enabled in ("disabled", "enabled-runtime"):
            self.commands.system_enabled = enabled
            with self.subTest(enabled=enabled), self.assertRaisesRegex(ValueError, "separate repair"):
                self.apply()
            self.assertEqual(self.commands.system_enabled, enabled)
        self.assertFalse(any("enable" in c or "disable" in c or "shutdown" in c for c, _ in self.commands.calls))

    def test_no_user_manager_uses_offline_enablement_and_retires_legacy(self):
        self.commands.no_user_manager, self.commands.old_active = True, False
        result = self.apply()
        self.assertTrue(result["applied"], result)
        self.assertFalse(self.commands.old_enabled)
        self.assertTrue(any("is-enabled" in c and core.OLD_UNIT in c for c, _ in self.commands.calls))
        self.assertTrue(any("disable" in c and "--user" in c and "--no-reload" in c and core.OLD_UNIT in c for c, _ in self.commands.calls))

    def test_unknown_offline_enablement_fails_without_assuming_disabled(self):
        self.commands.no_user_manager, self.commands.old_active = True, False
        original = self.installer.runner
        self.installer.runner = lambda command, **options: "" if "is-enabled" in command else original(command, **options)
        with self.assertRaisesRegex(ValueError, "enablement is unknown"):
            self.apply()
        self.assertTrue(self.commands.old_enabled)
        self.assertFalse(self.installer.unit_file.exists())

    def test_matching_but_writable_os_payload_is_rejected(self):
        self.paths.system.mkdir()
        self.installer.unit_file.write_bytes(self.installer.payloads("/usr/local/custom tools:/usr/bin")[self.installer.unit_file])
        self.installer.unit_file.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "Writable OS payload"):
            self.installer.plan()


if __name__ == "__main__":
    unittest.main()
