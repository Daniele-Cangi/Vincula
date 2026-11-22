# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""The local Windows status probe must also work with a filtered MCP environment."""

import base64
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs" / "Windows Dock"))
import windows_ssh
from server import WINDOWS_NVIDIA_LOCATOR, WINDOWS_RESOURCES


class WindowsStatusEnvironmentTests(unittest.TestCase):
    def probe(self, environment):
        result = subprocess.CompletedProcess([], 0, stdout=b'{"ci":"ok"}', stderr=b'')
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(subprocess, "run", return_value=result) as run:
            before = dict(os.environ)
            self.assertEqual(windows_ssh.local_status(timeout=12), {"ci": "ok"})
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(run.call_args.kwargs["timeout"], 12)
        return run.call_args

    def test_filtered_environment_gets_only_system_modules(self):
        call = self.probe({"SystemRoot": "C:/Windows", "USERPROFILE": "private-profile"})
        powershell = call.args[0][0]
        self.assertEqual({name.casefold(): value for name, value in call.kwargs["env"].items()}, {
            "systemroot": "C:/Windows", "userprofile": "private-profile",
            "psmodulepath": str(powershell.parent / "Modules"),
        })
        script = self.script(call)
        self.assertLess(script.index("$env:PSModulePath = "), script.index("Get-CimInstance"))
        prelude = script.split("$env:PSModulePath = ", 1)[1].split("\n", 1)[0]
        self.assertNotIn("Join-Path", prelude)
        self.assertIn("[Text.Encoding]::UTF8.GetString", prelude)

    @staticmethod
    def script(call):
        arguments = call.args[0]
        payload = arguments[arguments.index("-EncodedCommand") + 1]
        return base64.b64decode(payload).decode("utf-16le")

    def test_explicit_module_path_is_preserved_case_insensitively(self):
        for name, value in (("PSModulePath", "custom-modules"), ("psmodulepath", "")):
            with self.subTest(name=name, value=value):
                # Windows normalizes environment names to uppercase.
                call = self.probe({name: value})
                actual = call.kwargs["env"]
                matches = [key for key in actual if key.casefold() == "psmodulepath"]
                self.assertEqual(len(matches), 1)
                self.assertEqual(actual[matches[0]], value)
                self.assertNotIn("$env:PSModulePath = ", self.script(call))


class NvidiaLookupTests(unittest.TestCase):
    def test_resource_query_uses_no_powershell_command_discovery(self):
        self.assertNotIn("Get-Command", WINDOWS_RESOURCES)

    @unittest.skipUnless(sys.platform == "win32", "Real Windows PowerShell locator")
    def test_locator_skips_relative_paths_and_handles_quoted_absolute_paths(self):
        with TemporaryDirectory(prefix="vincula-nvidia-locator-") as temporary:
            root = Path(temporary)
            first, second = root / "quoted tools", root / "other tools"
            for directory in (first, second):
                directory.mkdir()
                (directory / "nvidia-smi.exe").touch()
            # These files are only located; the test never executes them.
            absent = root / "absent"
            cases = (
                (f';{first.relative_to(first.anchor)};{str(first)[2:]}', ""),
                (str(absent), ""),
                (f';relative;"{first}";{second}', str(first / "nvidia-smi.exe")),
            )
            for search_path, expected in cases:
                with self.subTest(search_path=search_path):
                    command = "$env:PATH = " + windows_ssh._ps_string(search_path) + "\n"
                    command += WINDOWS_NVIDIA_LOCATOR + r'''
[Console]::WriteLine('{"found":"' +
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$nvidia)) + '"}')
'''
                    result = windows_ssh.local_status(timeout=12, command=command)
                    found = base64.b64decode(result["found"]).decode("utf-8")
                    if expected:
                        self.assertTrue(Path(found).samefile(expected))
                    else:
                        self.assertEqual(found, "")


if __name__ == "__main__":
    unittest.main()
