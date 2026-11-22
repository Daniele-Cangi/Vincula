# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import node_registry
import nodes


class NodeEditingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / 'vincula/nodes.json'
        self.local = str(uuid4())
        self.peer = str(uuid4())
        family = 'windows' if sys.platform == 'win32' else 'linux'
        self.local_node = {'id': self.local, 'label': 'This PC', 'family': family, 'aliases': ['local']}
        self.peer_node = {'id': self.peer, 'label': 'Peer', 'family': 'linux', 'aliases': ['peer']}
        nodes.execute({'action': 'add', 'node': self.local_node, 'local': True}, self.path)

    def desktop(self):
        return {'primary': self.local, 'clipboard': True,
                'members': [{'node_id': self.local, 'x': 0, 'y': 0},
                            {'node_id': self.peer, 'x': 1, 'y': 0}]}

    def cli_environment(self):
        private = str(self.path.parent.parent / 'profile')
        environment = os.environ | dict.fromkeys(('HOME', 'USERPROFILE', 'LOCALAPPDATA', 'XDG_CONFIG_HOME'), private)
        environment['VINCULA_NODE_REGISTRY'] = str(self.path)
        return environment

    def cli(self, request, *arguments, environment=None):
        return subprocess.run([sys.executable, '-S', str(Path(nodes.__file__).resolve()), *arguments],
                              input=json.dumps(request), capture_output=True, text=True,
                              env=self.cli_environment() if environment is None else environment, timeout=20)

    def test_nested_registry_guard_preserves_reads_and_mutations(self):
        attempting, entered = threading.Event(), threading.Event()
        results = []

        def read_from_another_thread():
            attempting.set()
            with node_registry.registry_guard():
                entered.set()
                loaded = node_registry.load(self.path, nodes.validation_legacy(),
                                            'NUL' if sys.platform == 'win32' else '/dev/null')
                results.append(loaded.get(self.local).label)

        reader = threading.Thread(target=read_from_another_thread)
        try:
            with node_registry.registry_guard(), node_registry.registry_guard():
                nodes.execute({'action': 'update', 'node_id': self.local, 'changes': {'label': 'Nested'}}, self.path)
                reader.start()
                self.assertTrue(attempting.wait(5))
                if sys.platform == 'win32':
                    self.assertFalse(entered.wait(.1))
        finally:
            if reader.ident is not None:
                reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(results, ['Nested'])

    def test_registry_override_bootstraps_an_absent_nested_path(self):
        missing = self.path.parent.parent / 'new registry Ω' / 'nested' / 'nodes.json'
        environment = dict(self.cli_environment(), VINCULA_NODE_REGISTRY=str(missing))
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(nodes.registry_path(), missing.resolve())
        result = self.cli({'action': 'add', 'node': self.local_node, 'local': True}, environment=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(nodes.snapshot(missing)['registry']['local'], self.local)

    def test_registry_override_preserves_a_file_symlink(self):
        alias = self.path.parent / 'registry-alias.json'
        try:
            alias.symlink_to(self.path)
        except OSError as error:
            if sys.platform == 'win32' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('The Windows account cannot create symlinks.')
            raise
        environment = dict(self.cli_environment(), VINCULA_NODE_REGISTRY=str(alias))
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(nodes.registry_path(), self.path)
        result = self.cli({'action': 'update', 'node_id': self.local, 'changes': {'label': 'Via alias'}},
                          environment=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(nodes.snapshot(self.path)['registry']['nodes'][0]['label'], 'Via alias')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows short-name aliases')
    def test_windows_registry_override_resolves_short_name_alias(self):
        import ctypes

        function = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        if not function(str(self.path), buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        environment = dict(self.cli_environment(), VINCULA_NODE_REGISTRY=buffer.value)
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(nodes.registry_path(), self.path)
        result = self.cli({'action': 'list'}, environment=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), nodes.snapshot(self.path))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows abandoned mutex ownership')
    def test_windows_registry_guard_recovers_an_abandoned_owner(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        code = ('import os,sys\n'
                f'sys.path.insert(0, {str(Path(nodes.__file__).resolve().parent)!r})\n'
                'import node_registry\n'
                'with node_registry.registry_guard():\n'
                '    print("LOCKED", flush=True)\n'
                '    sys.stdin.buffer.read(1)\n'
                '    os._exit(0)\n')
        child = subprocess.Popen([sys.executable, '-S', '-u', '-c', code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.cli_environment())
        keep_alive = None
        try:
            self.assertEqual(child.stdout.readline().rstrip(b'\r\n'), b'LOCKED')
            name = 'Global\\Vincula.NodeRegistry.v1.' + node_registry._windows_registry_identity()
            keep_alive = kernel.OpenMutexW(0x00100000, False, name)
            if not keep_alive:
                raise ctypes.WinError(ctypes.get_last_error())
            _, errors = child.communicate(input=b'x', timeout=5)
            self.assertEqual(child.returncode, 0, errors)
            with node_registry.registry_guard():
                self.assertEqual(nodes.snapshot(self.path)['registry']['local'], self.local)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
            if keep_alive:
                kernel.CloseHandle(keep_alive)

    def test_crud_keeps_stable_identity_and_guards_desktop_references(self):
        initial = nodes.execute({'action': 'list'}, self.path)
        self.assertNotIn('desktop', initial['registry'])
        added = nodes.execute({'action': 'add', 'node': self.peer_node,
                               'expected_revision': initial['revision']}, self.path)
        group = self.desktop()
        nodes.execute({'action': 'desktop', 'desktop': group}, self.path)
        topology = node_registry.load(self.path, nodes.validation_legacy(), '/dev/null')
        self.assertEqual(topology.desktop.primary, self.local)
        self.assertEqual(topology.desktop.members[1].node_id, self.peer)
        with self.assertRaisesRegex(ValueError, 'desktop group'):
            nodes.execute({'action': 'remove', 'node_id': self.peer}, self.path)
        with self.assertRaisesRegex(ValueError, 'local node'):
            nodes.execute({'action': 'remove', 'node_id': self.local}, self.path)
        with self.assertRaises(nodes.ConflictError):
            nodes.execute({'action': 'update', 'node_id': self.peer,
                           'changes': {'label': 'Stale'}, 'expected_revision': added['revision']}, self.path)
        nodes.execute({'action': 'update', 'node_id': self.peer,
                       'changes': {'label': 'Renamed'}}, self.path)
        self.assertEqual(nodes.snapshot(self.path)['registry']['nodes'][1]['id'], self.peer)
        nodes.execute({'action': 'desktop', 'desktop': None}, self.path)
        result = nodes.execute({'action': 'remove', 'node_id': self.peer}, self.path)
        self.assertEqual([node['id'] for node in result['registry']['nodes']], [self.local])

    def test_invalid_edits_and_replace_failure_preserve_previous_document(self):
        nodes.execute({'action': 'add', 'node': self.peer_node}, self.path)
        before = self.path.read_bytes()
        overlapping = self.desktop()
        overlapping['members'][1]['x'] = 0
        for request in [
            {'action': 'desktop', 'desktop': overlapping},
            {'action': 'update', 'node_id': self.peer, 'changes': {'id': str(uuid4())}},
            {'action': 'update', 'node_id': self.peer, 'changes': {'aliases': ['local']}},
            {'action': 'add', 'node': self.peer_node},
            {'action': 'remove', 'node_id': 'peer'},
        ]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                nodes.execute(request, self.path)
            self.assertEqual(self.path.read_bytes(), before)
        with patch.object(Path, 'replace', side_effect=OSError('test replace failure')):
            with self.assertRaisesRegex(OSError, 'test replace failure'):
                nodes.execute({'action': 'update', 'node_id': self.peer,
                               'changes': {'label': 'Not committed'}}, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.nodes-*.tmp')), [])

    def test_desktop_rejects_unknown_nodes_aliases_duplicates_and_boolean_positions(self):
        nodes.execute({'action': 'add', 'node': self.peer_node}, self.path)
        cases = []
        unknown = self.desktop()
        unknown['members'][1]['node_id'] = str(uuid4())
        cases.append(unknown)
        alias = self.desktop()
        alias['members'][1]['node_id'] = 'peer'
        cases.append(alias)
        repeated = self.desktop()
        repeated['members'][1]['node_id'] = self.local
        cases.append(repeated)
        boolean = self.desktop()
        boolean['members'][0]['x'] = False
        cases.append(boolean)
        invalid_primary = self.desktop()
        invalid_primary['primary'] = str(uuid4())
        cases.append(invalid_primary)
        for group in cases:
            with self.subTest(group=group), self.assertRaises(ValueError):
                nodes.execute({'action': 'desktop', 'desktop': group}, self.path)

    def test_concurrent_cli_additions_keep_all_nodes_without_mcp_dependencies(self):
        requests = [dict(action='add', node={'id': str(uuid4()), 'label': f'Peer {i}',
                                            'family': 'linux', 'aliases': [f'peer{i}']}) for i in range(5)]
        command = [sys.executable, '-S', str(Path(nodes.__file__).resolve())]
        running = []
        results = []
        try:
            for request in requests:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, env=self.cli_environment())
                running.append(process)
                process.stdin.write(json.dumps(request))
                process.stdin.close()
                process.stdin = None
            for process in running:
                output, errors = process.communicate(timeout=20)
                results.append({'returncode': process.returncode, 'stdout': output, 'stderr': errors})
        finally:
            for process in running:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        for result in results:
            self.assertEqual(result['returncode'], 0, results)
            self.assertTrue(json.loads(result['stdout'])['ok'], results)
        saved = nodes.snapshot(self.path)['registry']['nodes']
        self.assertEqual({node['id'] for node in saved}, {self.local, *(r['node']['id'] for r in requests)})

    def test_cli_uses_only_the_operator_configured_registry(self):
        result = self.cli({'action': 'list'})
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved, nodes.snapshot(self.path))
        self.assertEqual(saved['registry']['local'], self.local)

    def test_cli_rejects_registry_flag_without_reading_or_mutating_other_files(self):
        victim = self.path.parent.parent / 'unrelated.json'
        original = self.path.read_bytes()
        victim.write_bytes(original)
        traversal = self.path.parent / '..' / victim.name
        for request in ({'action': 'list'},
                        {'action': 'update', 'node_id': self.local, 'changes': {'label': 'Unexpected'}}):
            with self.subTest(action=request['action']):
                result = self.cli(request, '--registry', str(traversal))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('unrecognized arguments: --registry', result.stderr)
                self.assertEqual(victim.read_bytes(), original)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertFalse(Path(str(victim) + '.lock').exists())

    def test_cli_rejects_registry_path_in_json_payload(self):
        victim = self.path.parent.parent / 'unrelated.json'
        original = self.path.read_bytes()
        victim.write_bytes(original)
        result = self.cli({'action': 'update', 'node_id': self.local, 'changes': {'label': 'Unexpected'},
                           'registry': str(victim)})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'invalid_request')
        self.assertEqual(victim.read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(Path(str(victim) + '.lock').exists())

    def test_route_switch_and_legacy_validation_preserve_compatibility(self):
        if sys.platform == 'win32':
            name, family = 'ubuntu', 'linux'
        else:
            name, family = 'windows', 'windows'
        peer = dict(self.peer_node, family=family, legacy_route=name)
        nodes.execute({'action': 'add', 'node': peer}, self.path)
        route = {'host': '192.0.2.1', 'user': 'test', 'port': 2222,
                 'identity_file': str(self.path.parent / 'identity'),
                 'known_hosts_file': str(self.path.parent / 'known_hosts')}
        updated = nodes.execute({'action': 'update', 'node_id': self.peer,
                                 'changes': {'legacy_route': None, 'ssh': route}}, self.path)
        self.assertNotIn('legacy_route', updated['registry']['nodes'][1])
        self.assertEqual(updated['registry']['nodes'][1]['ssh'], route)
        with self.assertRaisesRegex(ValueError, 'Choose exactly one'):
            nodes.execute({'action': 'update', 'node_id': self.peer,
                           'changes': {'legacy_route': name}}, self.path)


if __name__ == '__main__':
    unittest.main()
