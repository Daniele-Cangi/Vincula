# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Support installation fixtures; never register a task or start a live service."""
from contextlib import ExitStack
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import desktop_session as session


def task_fixture_script():
    """Only in-memory XML validation; never calls a Task Scheduler command."""
    config = {"sid": "S-1-5-21-1-2-3-1001", "python": "C:\\approved\\python.exe",
              "arguments": "-IB approved.py serve", "root": "C:\\approved"}
    xml = '''<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>S-1-5-21-1-2-3-1001</UserId></LogonTrigger></Triggers>
<Principals><Principal><UserId>S-1-5-21-1-2-3-1001</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><Enabled>true</Enabled><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>
<Actions><Exec><Command>C:\\approved\\python.exe</Command><Arguments>-IB approved.py serve</Arguments><WorkingDirectory>C:\\approved</WorkingDirectory></Exec></Actions>
</Task>'''
    cases = [(xml, True), (xml.replace("<Enabled>true</Enabled>", ""), True),
             (xml.replace("</Actions>", "<ComHandler><ClassId>unexpected</ClassId></ComHandler></Actions>"), False),
             (xml.replace("</Triggers>", "<TimeTrigger/></Triggers>"), False),
             (xml.replace("<Settings><Enabled>true", "<Settings><Enabled>false"), False),
             (xml.replace("<LogonTrigger><Enabled>true", "<LogonTrigger><Enabled>false"), False),
             (xml.replace("LeastPrivilege", "HighestAvailable"), False),
             (xml.replace("<StopIfGoingOnBatteries>false", "<StopIfGoingOnBatteries>true"), False)]
    data = base64.b64encode(json.dumps({"config": config, "cases": [{"xml": text, "accept": accepted}
                                          for text, accepted in cases]}).encode()).decode()
    return session.TASK_ASSERT + "\n$ErrorActionPreference='Stop'\n" + \
        "$data=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "')) | ConvertFrom-Json\n" + \
        "$count=0;foreach($case in $data.cases){$accepted=$true;try{Assert-VinculaDesktopTask ([xml]$case.xml) $data.config}catch{$accepted=$false};" + \
        "if($accepted -ne $case.accept){throw 'Unexpected task acceptance'};$count++};" + \
        "@{passed=$true;cases=$count;registered_tasks=0}|ConvertTo-Json -Compress\n"


@unittest.skipUnless(os.name == "nt", "Native PowerShell XML fixture runs on Windows.")
class NativeTaskDefinitionTests(unittest.TestCase):
    def test_only_the_expected_enabled_task_definition_is_accepted(self):
        bootstrap = "& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                 base64.b64encode(bootstrap.encode("utf-16le")).decode()],
                                input=task_fixture_script().encode(), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8-sig")), {"passed": True, "cases": 8, "registered_tasks": 0})


class BundleTests(unittest.TestCase):
    def test_fixed_allowlist_excludes_credentials_and_rejects_missing_source(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary)
            for name in session.SUPPORT_FILES:
                (source / name).write_bytes(b"synthetic code, not executed\n")
            (source / "private-key").write_bytes(b"private sentinel never included")
            digest, manifest, raw = session.bundle(source)
            self.assertEqual(set(manifest["files"]), set(session.SUPPORT_FILES))
            self.assertEqual(set(raw), set(session.SUPPORT_FILES))
            self.assertEqual(len(digest), 64)
            (source / session.SUPPORT_FILES[-1]).unlink()
            with self.assertRaises(OSError):
                session.bundle(source)

    def test_autostart_quotes_paths_without_shell_or_terminal(self):
        text = session.autostart_content('/usr/bin/python3', '/private/è % " $/desktop_session.py')
        self.assertIn('%%', text)
        self.assertIn('Terminal=false', text)
        self.assertIn('"serve"', text)
        self.assertNotIn('/bin/sh', text)
        with self.assertRaises(ValueError):
            session.autostart_content('/usr/bin/python3', '/private/line\nbreak')


@unittest.skipUnless(os.name != "nt", "Filesystem/autostart fixtures are POSIX; native task setup has its own gate.")
class SupportInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="vincula-support-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source, self.root, self.xdg = (self.base / name for name in ("source", "installed", "xdg"))
        self.source.mkdir()
        for name in session.SUPPORT_FILES:
            (self.source / name).write_bytes(("# synthetic " + name + "\n").encode())
        self.digest, _, _ = session.bundle(self.source)
        self.registry = self.base / "nodes.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(session.desktop_node, "home", return_value=self.root))
        self.stack.enter_context(patch.object(session.desktop_node, "deployment", return_value=({}, None, None)))
        self.stack.enter_context(patch.object(session.nodes, "snapshot", return_value={"registry": {"local": "synthetic"}}))
        self.stack.enter_context(patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}))

    def install(self, pin=None):
        return session.install(self.source, pin or self.digest, self.registry, sys.executable)

    def test_wrong_approval_pin_has_no_effects(self):
        with self.assertRaises(ValueError):
            self.install("0" * 64)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.xdg.exists())

    def test_install_is_idle_and_identical_retry_preserves_files(self):
        result = self.install()
        self.assertFalse(result["engine_started"])
        self.assertFalse(result["supervisor_started"])
        self.assertEqual(session.verify_installed()["python"], str(Path(sys.executable).resolve()))
        before = {p: p.stat().st_mtime_ns for p in self.root.iterdir()}
        self.install()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.root.iterdir()})
        self.assertEqual(json.loads((self.root / "catalog.json").read_text()), {"registry": str(self.registry)})
        self.assertTrue((self.xdg / "autostart/vincula-desktop-runtime.desktop").is_file())

    def test_conflicting_support_is_not_repaired_or_partially_published(self):
        self.root.mkdir(mode=0o700)
        conflict = self.root / "desktop_ssh.py"
        conflict.write_bytes(b"previous release")
        conflict.chmod(0o600)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(self.root.iterdir()), [conflict])
        self.assertEqual(conflict.read_bytes(), b"previous release")

    def test_conflicting_autostart_is_found_before_support_publication(self):
        autostart = self.xdg / "autostart"
        autostart.mkdir(parents=True)
        entry = autostart / "vincula-desktop-runtime.desktop"
        entry.write_bytes(b"unrelated existing entry")
        entry.chmod(0o600)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(entry.read_bytes(), b"unrelated existing entry")

    def test_changed_code_cannot_start_an_installed_supervisor(self):
        self.install()
        (self.root / "desktop_process.py").write_bytes(b"changed code")
        with self.assertRaises(ValueError):
            session.verify_installed()

    def test_start_without_graphical_manager_does_not_invent_a_display(self):
        with patch.object(session, "verify_installed", return_value={"python": sys.executable}), \
             patch.object(session.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"XDG_RUNTIME_DIR=/run/user/1000\n")) as run:
            with self.assertRaises(ValueError):
                session.start()
            run.assert_called_once()

    def test_serve_from_headless_context_never_opens_supervisor(self):
        with patch.object(session, "verify_installed"), patch.dict(os.environ, {}, clear=True), \
             patch.object(session.desktop_supervisor, "serve") as serve:
            with self.assertRaises(ValueError):
                session.serve()
            serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
