# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import credential_bootstrap as bootstrap


@unittest.skipUnless(sys.platform == 'linux', 'Linux desktop credential bootstrap')
class CredentialBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / 'run'
        self.runtime.mkdir(mode=0o700)
        self.agent = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.agent.close)
        self.agent.bind(str(self.runtime / 'openssh_agent'))
        self.local, self.peer, self.other = (str(uuid4()) for _ in range(3))
        self.identity = self.root / 'identity with space'
        self.identity.write_text('PRIVATE SYNTHETIC SENTINEL: must never be read by bootstrap')
        self.identity.chmod(0o600)
        kind = b'ssh-ed25519'
        blob = len(kind).to_bytes(4, 'big') + kind + (32).to_bytes(4, 'big') + b'k' * 32
        self.public = Path(str(self.identity) + '.pub')
        self.public.write_text('ssh-ed25519 ' + base64.b64encode(blob).decode() + ' fixture\n')
        self.public.chmod(0o644)
        self.registry = self.root / 'nodes.json'
        self.document = {'local': self.local, 'nodes': [
            {'id': self.local, 'family': 'linux'},
            {'id': self.peer, 'family': 'windows', 'ssh': {'identity_file': str(self.identity)}}]}
        self.registry.write_text(json.dumps(self.document))
        self.profile = {'version': 1, 'registry': str(self.registry), 'agent_source': 'openssh'}
        self.config = self.root / 'credentials.json'
        self.save_config()
        self.environment = {'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_SESSION_ID': 'login-7',
                            'DISPLAY': ':0', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/fixture/user-bus'}
        self.askpass = self.root / 'fixture-askpass'
        self.askpass.write_text('synthetic executable, never executed')
        self.askpass.chmod(0o700)
        self.loaded = False
        self.return_after_load = 0
        self.load_calls = []
        self.calls = []
        self.session_type = 'wayland'
        self.session_uid = str(os.getuid())
        self.session_valid = True
        self.manager_response = {'type': 'as', 'data': [
            'OTHER_SECRET=never publish', 'SSH_AUTH_SOCK=' + str(self.runtime / 'openssh_agent')]}

    def save_config(self):
        self.config.write_text(json.dumps(self.profile))
        self.config.chmod(0o600)

    def helper(self, argv, environment, timeout):
        self.calls.append((list(argv), dict(environment), timeout))
        if argv == [bootstrap.SSH_ADD, '-l']:
            return (0 if self.loaded else 1), b''
        if argv[:2] == [bootstrap.SSH_ADD, '-T']:
            return (0 if self.loaded else 1), b''
        if argv[:2] == [bootstrap.LOGINCTL, 'show-session']:
            session = argv[2] if argv[2] != 'auto' else 'auto-local-7'
            if not self.session_valid:
                return 1, b''
            return 0, (f'Id={session}\nUser={self.session_uid}\nType={self.session_type}\nRemote=no\nState=active\n').encode()
        if argv == bootstrap.MANAGER_ENVIRONMENT:
            return 0, json.dumps(self.manager_response).encode()
        self.assertEqual(argv[0], bootstrap.SSH_ADD)
        self.load_calls.append((list(argv), dict(environment)))
        if self.return_after_load == 0:
            self.loaded = True
        return self.return_after_load, b'raw ssh-add diagnostics must not be published'

    def run_bootstrap(self, **options):
        with patch.object(bootstrap, 'command', side_effect=self.helper):
            return bootstrap.run(self.config, self.environment, **options)

    def enable_askpass(self):
        self.profile['askpass'] = str(self.askpass)
        self.save_config()

    def test_status_uses_configured_agent_and_requires_a_signature_without_prompt(self):
        self.environment['SSH_AUTH_SOCK'] = '/wrong/inherited/socket'
        value = self.run_bootstrap()
        self.assertEqual(value['code'], 'key_locked')
        self.assertFalse(value['credentials_ready'])
        self.assertFalse(self.load_calls)
        self.assertTrue(all(call[1]['SSH_AUTH_SOCK'] == str(self.runtime / 'openssh_agent') for call in self.calls))
        self.assertIn([bootstrap.SSH_ADD, '-T', str(self.public)], [call[0] for call in self.calls])

    def test_already_signing_does_not_require_gui_or_askpass(self):
        self.loaded = True
        self.environment.pop('DISPLAY')
        self.environment.pop('DBUS_SESSION_BUS_ADDRESS')
        value = self.run_bootstrap(unlock=True, automatic=True)
        self.assertTrue(value['credentials_ready'])
        self.assertEqual(value['code'], 'ready')
        self.assertFalse(self.load_calls)
        self.assertFalse(list((self.runtime / 'vincula-credentials').glob('automatic-*')))

    def test_unlock_uses_standard_askpass_and_verifies_the_signature_after_loading(self):
        self.enable_askpass()
        value = self.run_bootstrap(unlock=True, automatic=True)
        self.assertTrue(value['credentials_ready'])
        argv, environment = self.load_calls[0]
        self.assertEqual(argv, [bootstrap.SSH_ADD, str(self.identity)])
        self.assertEqual(environment['SSH_ASKPASS'], str(self.askpass))
        self.assertEqual(environment['SSH_ASKPASS_REQUIRE'], 'force')
        self.assertEqual(sum(call[0][1:2] == ['-T'] for call in self.calls), 2)
        directory = self.runtime / 'vincula-credentials'
        self.assertEqual(stat.S_IMODE((directory / 'status.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(next(directory.glob('automatic-*')).stat().st_mode), 0o600)
        self.assertNotIn('raw ssh-add', json.dumps(value))
        self.assertNotIn('PRIVATE SYNTHETIC', (directory / 'status.json').read_text())

    def test_successful_ssh_add_without_matching_signature_is_not_ready(self):
        self.enable_askpass()
        with patch.object(bootstrap, 'signs', return_value=False):
            value = self.run_bootstrap(unlock=True)
        self.assertEqual(value['code'], 'unlock_incomplete')
        self.assertFalse(value['credentials_ready'])
        self.assertEqual(len(self.load_calls), 1)

    def test_cancelled_automatic_popup_is_not_repeated_but_explicit_retry_is_allowed(self):
        self.enable_askpass()
        self.return_after_load = 1
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'unlock_incomplete')
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'automatic_attempt_already_made')
        self.assertEqual(len(self.load_calls), 1)
        self.return_after_load = 0
        self.assertTrue(self.run_bootstrap(unlock=True)['credentials_ready'])
        self.assertEqual(len(self.load_calls), 2)

    def test_different_desktop_session_allows_one_new_automatic_attempt(self):
        self.enable_askpass()
        self.return_after_load = 1
        self.run_bootstrap(unlock=True, automatic=True)
        self.environment['XDG_SESSION_ID'] = 'login-8'
        self.run_bootstrap(unlock=True, automatic=True)
        self.assertEqual(len(self.load_calls), 2)

    def test_timeout_keeps_keys_unready_and_does_not_repeat_popup(self):
        self.enable_askpass()
        self.return_after_load = None
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'unlock_timeout')
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'automatic_attempt_already_made')
        self.assertFalse(self.loaded)

    def test_user_manager_socket_is_selected_without_publishing_environment(self):
        self.profile['agent_source'] = 'user-manager'
        self.save_config()
        self.loaded = True
        value = self.run_bootstrap()
        self.assertTrue(value['credentials_ready'])
        self.assertNotIn('OTHER_SECRET', json.dumps(value))
        self.assertEqual(self.calls[0][0], ['/usr/bin/busctl', '--user', '--json=short', 'get-property',
                                         'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
                                         'org.freedesktop.systemd1.Manager', 'Environment'])

    def test_manager_socket_path_preserves_whitespace_unicode_and_shell_characters(self):
        target = self.runtime / "è '$`\\ = agent"
        agent = socket.socket(socket.AF_UNIX)
        self.addCleanup(agent.close)
        agent.bind(str(target))
        self.profile['agent_source'] = 'user-manager'
        self.save_config()
        self.manager_response['data'][1] = 'SSH_AUTH_SOCK=' + str(target)
        self.loaded = True
        value = self.run_bootstrap()
        self.assertTrue(value['credentials_ready'])
        helpers = [call for call in self.calls if call[0][0] == bootstrap.SSH_ADD]
        self.assertTrue(helpers)
        self.assertTrue(all(call[1]['SSH_AUTH_SOCK'] == str(target) for call in helpers))
        self.assertNotIn('OTHER_SECRET', json.dumps(value))

    def test_manager_environment_rejects_invalid_shape_or_ambiguous_socket(self):
        self.profile['agent_source'] = 'user-manager'
        self.save_config()
        invalid = [{'type': 's', 'data': []}, {'type': 'as', 'data': [3]},
                   {'type': 'as', 'data': []}, {'type': 'as', 'data': ['OTHER_SECRET=value']},
                   {'type': 'as', 'data': ['SSH_AUTH_SOCK=/one', 'SSH_AUTH_SOCK=/two']}]
        for document in invalid:
            with self.subTest(document=document):
                self.manager_response = document
                value = self.run_bootstrap()
                self.assertEqual(value['code'], 'agent_missing')
                self.assertFalse(value['credentials_ready'])
        self.assertFalse(self.load_calls)

    def test_manager_environment_rejects_malformed_json_without_exposing_data(self):
        with patch.object(bootstrap, 'command', return_value=(0, b'PRIVATE malformed {')):
            with self.assertRaises(bootstrap.BootstrapError) as caught:
                bootstrap.manager_agent_socket(self.environment)
        self.assertEqual(caught.exception.code, 'agent_missing')
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_kali_without_askpass_reports_unsupported_without_terminal_or_key_load(self):
        self.profile['agent_source'] = 'user-manager'
        self.save_config()
        value = self.run_bootstrap(unlock=True, automatic=True)
        self.assertEqual(value['code'], 'unsupported')
        self.assertFalse(value['credentials_ready'])
        self.assertFalse(self.load_calls)
        self.assertFalse(list((self.runtime / 'vincula-credentials').glob('automatic-*')))

    def test_missing_graphical_session_never_prompts(self):
        self.enable_askpass()
        self.environment.pop('DISPLAY')
        value = self.run_bootstrap(unlock=True, automatic=True)
        self.assertEqual(value['code'], 'graphical_session_missing')
        self.assertFalse(self.load_calls)

    def test_automatic_without_graphical_login_never_prompts_but_explicit_is_available(self):
        self.enable_askpass()
        self.environment.pop('XDG_SESSION_ID')
        self.session_valid = False
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'session_missing')
        self.assertFalse(self.load_calls)
        self.assertTrue(self.run_bootstrap(unlock=True)['credentials_ready'])

    def test_automatic_can_use_verified_loginctl_auto_when_session_variable_is_missing(self):
        self.enable_askpass()
        self.environment.pop('XDG_SESSION_ID')
        self.assertTrue(self.run_bootstrap(unlock=True, automatic=True)['credentials_ready'])
        queries = [call[0] for call in self.calls if call[0][:2] == [bootstrap.LOGINCTL, 'show-session']]
        self.assertEqual(queries[0][2], 'auto')

    def test_tty_or_other_users_session_cannot_trigger_automatic_popup(self):
        self.enable_askpass()
        self.session_type = 'tty'
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'session_missing')
        self.session_type = 'wayland'
        self.session_uid = str(os.getuid() + 1)
        self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'session_missing')
        self.assertFalse(self.load_calls)

    def test_unlock_activates_only_existing_openssh_socket_when_missing(self):
        self.agent.close()
        target = self.runtime / 'openssh_agent'
        target.unlink()
        expected = [bootstrap.SYSTEMCTL, '--user', 'start', 'ssh-agent.socket']
        def helper(argv, environment, timeout):
            self.assertEqual(argv, expected)
            replacement = socket.socket(socket.AF_UNIX)
            self.addCleanup(replacement.close)
            replacement.bind(str(target))
            return 0, b''
        with patch.object(bootstrap, 'command', side_effect=helper) as command:
            self.assertEqual(bootstrap.agent_socket(self.profile, self.environment, activate=True), target)
        command.assert_called_once()

    def test_socket_activation_failure_does_not_create_an_ad_hoc_agent(self):
        self.agent.close()
        (self.runtime / 'openssh_agent').unlink()
        with patch.object(bootstrap, 'command', return_value=(1, b'')) as command:
            with self.assertRaises(bootstrap.BootstrapError) as error:
                bootstrap.agent_socket(self.profile, self.environment, activate=True)
        self.assertEqual(error.exception.code, 'agent_missing')
        self.assertEqual(command.call_args.args[0], [bootstrap.SYSTEMCTL, '--user', 'start', 'ssh-agent.socket'])

    def test_user_manager_mode_never_activates_another_agent(self):
        self.agent.close()
        (self.runtime / 'openssh_agent').unlink()
        self.profile['agent_source'] = 'user-manager'
        self.save_config()
        value = self.run_bootstrap(unlock=True)
        self.assertEqual(value['code'], 'agent_missing')
        self.assertEqual([call[0] for call in self.calls], [bootstrap.MANAGER_ENVIRONMENT])

    def test_absent_socket_and_non_socket_are_distinct_from_locked_key(self):
        self.agent.close()
        (self.runtime / 'openssh_agent').unlink()
        self.assertEqual(self.run_bootstrap()['code'], 'agent_missing')
        (self.runtime / 'openssh_agent').write_text('not a socket')
        self.assertEqual(self.run_bootstrap()['code'], 'agent_invalid')
        self.assertFalse(self.calls)

    def test_socket_owned_by_another_uid_is_rejected(self):
        original = Path.lstat
        target = self.runtime / 'openssh_agent'
        def metadata(path):
            item = original(path)
            if path == target:
                values = list(item)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return item
        with patch.object(Path, 'lstat', metadata):
            self.assertEqual(self.run_bootstrap()['code'], 'agent_invalid')
        self.assertFalse(self.calls)

    def test_socket_not_answering_is_agent_missing_not_key_locked(self):
        def helper(argv, environment, timeout):
            return 2, b'private failure details'
        with patch.object(bootstrap, 'command', side_effect=helper):
            value = bootstrap.run(self.config, self.environment)
        self.assertEqual(value['code'], 'agent_missing')
        self.assertNotIn('private failure', json.dumps(value))

    def test_runtime_lock_prevents_concurrent_prompts(self):
        with bootstrap.state_lock(self.environment):
            self.assertEqual(self.run_bootstrap(unlock=True)['code'], 'busy')
        self.assertFalse(self.calls)

    def test_runtime_directory_with_shared_permissions_is_rejected(self):
        self.runtime.chmod(0o755)
        self.assertEqual(self.run_bootstrap()['code'], 'unsafe_runtime')
        self.assertFalse(self.calls)

    def test_legacy_profile_supplies_identity_without_machine_specific_uuid(self):
        self.document['nodes'][1].pop('ssh')
        self.document['nodes'][1]['legacy_route'] = 'a-local-profile'
        self.document['nodes'].append({'id': self.other, 'ssh': {'identity_file': str(self.identity)}})
        self.registry.write_text(json.dumps(self.document))
        self.assertEqual(self.run_bootstrap()['code'], 'legacy_profile_missing')
        self.profile['legacy_routes'] = {'a-local-profile': {'identity_file': str(self.identity)}}
        self.save_config()
        self.loaded = True
        value = self.run_bootstrap()
        self.assertEqual(len(value['keys']), 1)
        self.assertEqual(value['keys'][0]['node_ids'], [self.peer, self.other])

    def test_private_file_is_never_opened_by_bootstrap(self):
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == self.identity:
                self.fail('bootstrap attempted to read a private key')
            return original(path, *args, **kwargs)
        self.enable_askpass()
        with patch.object(Path, 'open', guarded):
            self.assertTrue(self.run_bootstrap(unlock=True)['credentials_ready'])

    def test_permissive_private_key_and_invalid_public_key_do_not_prompt(self):
        self.enable_askpass()
        self.identity.chmod(0o644)
        self.assertEqual(self.run_bootstrap(unlock=True)['code'], 'key_invalid')
        self.identity.chmod(0o600)
        self.public.write_text('ssh-ed25519 not-base64\n')
        self.assertEqual(self.run_bootstrap(unlock=True)['code'], 'public_key_invalid')
        self.assertFalse(self.load_calls)

    def test_renderer_and_plan_are_read_only_and_quote_exec_arguments(self):
        path = self.root / 'config $value `cmd` 10%.json'
        path.write_text(json.dumps(self.profile))
        path.chmod(0o600)
        with patch.object(Path, 'home', return_value=self.root):
            plan = bootstrap.install_plan(path, Path('/usr/bin/python3'))
        self.assertFalse(plan['applied'])
        self.assertNotIn('/versions/6ac', plan['module_destination'])
        self.assertIn('credential-bootstrap/', plan['module_destination'])
        self.assertIn('10%%.json', plan['autostart_content'])
        self.assertIn(('\\' * 2) + '$value', plan['autostart_content'])
        self.assertIn(('\\' * 2) + '`cmd' + ('\\' * 2) + '`', plan['autostart_content'])
        self.assertIn('--automatic', plan['autostart_content'])
        self.assertFalse((self.root / '.config/autostart').exists())
        self.assertFalse((self.root / '.local/share/vincula').exists())

    def test_missing_public_key_replaces_previous_ready_status(self):
        self.loaded = True
        self.assertTrue(self.run_bootstrap()['credentials_ready'])
        self.public.unlink()
        value = self.run_bootstrap()
        self.assertFalse(value['credentials_ready'])
        status = json.loads((self.runtime / 'vincula-credentials/status.json').read_text())
        self.assertFalse(status['credentials_ready'])
        self.assertEqual(status['code'], 'local_input_error')

    def test_timeout_kills_owned_prompt_group_before_reaping_and_hides_output(self):
        with patch.object(bootstrap.subprocess, 'Popen') as popen, patch.object(bootstrap.os, 'killpg') as kill:
            process = popen.return_value
            process.pid = 43210
            process.returncode = None
            process.wait.side_effect = [subprocess.TimeoutExpired(['fixture'], 2), 0]
            observed = []
            kill.side_effect = lambda *args: observed.append('kill')
            def wait(*args, **kwargs):
                observed.append('wait')
                if len(observed) == 1:
                    raise subprocess.TimeoutExpired(['fixture'], 2)
                return 0
            process.wait.side_effect = wait
            code, output = bootstrap.command([bootstrap.SSH_ADD, 'fixture'], self.environment, 2)
        self.assertIsNone(code)
        self.assertEqual(output, b'')
        self.assertEqual(observed, ['wait', 'kill', 'wait'])
        kill.assert_called_once_with(43210, bootstrap.signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertNotIn('shell', popen.call_args.kwargs)

    def test_keyboard_interrupt_after_real_child_reap_never_signals_released_group(self):
        real_popen = subprocess.Popen
        children = []
        class InterruptAfterReap:
            def __init__(self, *args, **kwargs):
                self.child = real_popen(*args, **kwargs)
                self.interrupted = False
                children.append(self.child)

            @property
            def pid(self):
                return self.child.pid

            @property
            def returncode(self):
                return self.child.returncode

            def wait(self, *args, **kwargs):
                code = self.child.wait(*args, **kwargs)
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt
                return code
        try:
            with patch.object(bootstrap.subprocess, 'Popen', side_effect=InterruptAfterReap), \
                    patch.object(bootstrap.os, 'killpg') as signals, \
                    patch.object(bootstrap, 'terminal_attributes', side_effect=nullcontext):
                for transport in (bootstrap.command, bootstrap.terminal_command):
                    with self.assertRaises(KeyboardInterrupt):
                        transport([sys.executable, '-I', '-B', '-c', 'pass'], dict(os.environ), 5)
            self.assertEqual(len(children), 2)
            self.assertTrue(all(child.returncode == 0 for child in children))
            signals.assert_not_called()
        finally:
            for child in children:
                child.wait(timeout=5)

    def test_keyboard_interrupt_before_reap_cleans_owned_group_and_propagates(self):
        with patch.object(bootstrap.subprocess, 'Popen') as popen, patch.object(bootstrap.os, 'killpg') as signals:
            process = popen.return_value
            process.pid = 43210
            process.returncode = None
            process.wait.side_effect = [KeyboardInterrupt, -9]
            with self.assertRaises(KeyboardInterrupt):
                bootstrap.command([bootstrap.SSH_ADD, 'fixture'], self.environment, 2)
        signals.assert_called_once_with(43210, bootstrap.signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)

    def test_terminal_profile_requires_local_tty_only_when_unlock_is_needed(self):
        self.profile['prompt_mode'] = 'terminal'
        self.save_config()
        with patch.object(bootstrap.os, 'isatty', return_value=False), \
                patch.object(bootstrap, 'terminal_command') as terminal:
            self.assertEqual(self.run_bootstrap()['code'], 'key_locked')
            self.assertEqual(self.run_bootstrap(unlock=True)['code'], 'terminal_missing')
            self.loaded = True
            self.assertTrue(self.run_bootstrap(unlock=True)['credentials_ready'])
        terminal.assert_not_called()

    def test_terminal_unlock_disables_askpass_and_marks_session_before_local_prompt(self):
        self.profile['prompt_mode'] = 'terminal'
        self.save_config()
        self.environment['SSH_ASKPASS'] = '/must/not/be/invoked'
        self.environment['SSH_ASKPASS_REQUIRE'] = 'force'
        def load(argv, environment, timeout):
            self.assertEqual(argv, [bootstrap.SSH_ADD, str(self.identity)])
            self.assertEqual(environment['SSH_ASKPASS_REQUIRE'], 'never')
            self.assertNotIn('SSH_ASKPASS', environment)
            self.assertEqual(timeout, 180)
            self.assertTrue(list((self.runtime / 'vincula-credentials').glob('automatic-*')))
            self.loaded = True
            return 0
        with patch.object(bootstrap, 'require_local_terminal') as local, \
                patch.object(bootstrap, 'terminal_command', side_effect=load) as terminal:
            value = self.run_bootstrap(unlock=True, automatic=True)
        self.assertTrue(value['credentials_ready'])
        self.assertEqual(local.call_count, 1)
        self.assertEqual(terminal.call_count, 1)
        self.assertFalse(self.load_calls)

    def test_terminal_prompt_automatic_retry_is_suppressed_but_explicit_can_retry(self):
        self.profile['prompt_mode'] = 'terminal'
        self.save_config()
        with patch.object(bootstrap, 'require_local_terminal'), \
                patch.object(bootstrap, 'terminal_command', return_value=1) as terminal:
            self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'unlock_incomplete')
            self.assertEqual(self.run_bootstrap(unlock=True, automatic=True)['code'], 'automatic_attempt_already_made')
            self.assertEqual(self.run_bootstrap(unlock=True)['code'], 'unlock_incomplete')
        self.assertEqual(terminal.call_count, 2)

    def test_ssh_terminal_is_rejected_even_when_all_streams_are_tty(self):
        with patch.object(bootstrap.os, 'isatty', return_value=True), patch.object(bootstrap.os, 'open') as opened:
            with self.assertRaises(bootstrap.BootstrapError) as error:
                bootstrap.require_local_terminal(self.environment | {'SSH_CONNECTION': 'synthetic remote'})
        self.assertEqual(error.exception.code, 'terminal_missing')
        opened.assert_not_called()

    def test_terminal_helper_timeout_or_interrupt_kills_only_owned_child(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), \
                    patch.object(bootstrap.subprocess, 'Popen') as popen, \
                    patch.object(bootstrap.os, 'killpg') as group, \
                    patch.object(bootstrap, 'terminal_attributes', side_effect=nullcontext):
                process = popen.return_value
                process.returncode = None
                error = KeyboardInterrupt if interrupted else subprocess.TimeoutExpired(['fixture'], 2)
                process.wait.side_effect = [error, -9]
                if interrupted:
                    with self.assertRaises(KeyboardInterrupt):
                        bootstrap.terminal_command([bootstrap.SSH_ADD, 'fixture'], self.environment, 2)
                else:
                    self.assertIsNone(bootstrap.terminal_command([bootstrap.SSH_ADD, 'fixture'], self.environment, 2))
                process.kill.assert_called_once_with()
                self.assertEqual(process.wait.call_count, 2)
                group.assert_not_called()
                self.assertFalse(popen.call_args.kwargs['start_new_session'])
                self.assertTrue(all(popen.call_args.kwargs[name] is None for name in ('stdin', 'stdout', 'stderr')))

    def test_terminal_renderer_uses_explicit_profile_and_default_remains_askpass(self):
        with patch.object(Path, 'home', return_value=self.root):
            default = bootstrap.install_plan(self.config, Path('/usr/bin/python3'))
            self.profile['prompt_mode'] = 'terminal'
            self.save_config()
            terminal = bootstrap.install_plan(self.config, Path('/usr/bin/python3'))
        self.assertIn('Terminal=false', default['autostart_content'])
        self.assertEqual(default['prompt_mode'], 'askpass')
        self.assertIn('Terminal=true', terminal['autostart_content'])
        self.assertEqual(terminal['prompt_mode'], 'terminal')
        self.assertNotIn('qterminal', terminal['autostart_content'].lower())
        self.assertFalse((self.root / '.config/autostart').exists())

    def test_terminal_without_controlling_tty_is_rejected(self):
        with patch.object(bootstrap.os, 'isatty', return_value=True), \
                patch.object(bootstrap.os, 'open', side_effect=OSError('no controlling tty')):
            with self.assertRaises(bootstrap.BootstrapError) as error:
                bootstrap.require_local_terminal(self.environment)
        self.assertEqual(error.exception.code, 'terminal_missing')

    def test_unknown_prompt_mode_is_rejected_without_helper_calls(self):
        self.profile['prompt_mode'] = 'spawn-a-terminal'
        self.save_config()
        self.assertEqual(self.run_bootstrap(unlock=True)['code'], 'invalid_config')
        self.assertFalse(self.calls)

    def assert_pty_attributes_restored(self, interrupted):
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = termios.tcgetattr(slave)
        marker = self.root / 'synthetic-child-echo-disabled'
        program = r"""
import fcntl,importlib.util,os,sys,termios,time
from pathlib import Path
from unittest.mock import patch
os.setsid()
fcntl.ioctl(0,termios.TIOCSCTTY,0)
spec=importlib.util.spec_from_file_location('fixture_bootstrap',sys.argv[1])
bootstrap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
marker=Path(sys.argv[2])
child=('import termios,time; from pathlib import Path; '
       'attributes=termios.tcgetattr(0); attributes[3]&=~termios.ECHO; '
       'termios.tcsetattr(0,termios.TCSANOW,attributes); '
       f'Path({str(marker)!r}).touch(); time.sleep(5)')
arguments=[sys.executable,'-I','-B','-c',child]
if sys.argv[3]=='interrupt':
    real_popen=bootstrap.subprocess.Popen
    children=[]
    class InterruptWhileReading:
        def __init__(self,*args,**kwargs):
            self.child=real_popen(*args,**kwargs)
            self.interrupted=False
            children.append(self.child)
        @property
        def returncode(self):return self.child.returncode
        def kill(self):return self.child.kill()
        def wait(self,*args,**kwargs):
            if not self.interrupted:
                deadline=time.monotonic()+3
                while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
                assert marker.exists(),'synthetic child did not disable ECHO'
                self.interrupted=True
                raise KeyboardInterrupt
            return self.child.wait(*args,**kwargs)
    try:
        with patch.object(bootstrap.subprocess,'Popen',side_effect=InterruptWhileReading):
            try:bootstrap.terminal_command(arguments,dict(os.environ),3)
            except KeyboardInterrupt:pass
            else:raise AssertionError('interruption was swallowed')
    finally:
        for process in children:process.wait(timeout=2)
else:
    assert bootstrap.terminal_command(arguments,dict(os.environ),.4) is None
assert marker.exists(),'synthetic child did not disable ECHO'
"""
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program, bootstrap.__file__,
                                 str(marker), 'interrupt' if interrupted else 'timeout'],
                                stdin=slave, stdout=slave, stderr=slave, timeout=8, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertTrue(before[3] & termios.ECHO)
        self.assertEqual(termios.tcgetattr(slave), before)

    def test_terminal_timeout_restores_real_pty_attributes(self):
        self.assert_pty_attributes_restored(interrupted=False)

    def test_terminal_interrupt_restores_real_pty_attributes(self):
        self.assert_pty_attributes_restored(interrupted=True)

    def test_duplicate_fields_and_duplicate_node_ids_fail_closed(self):
        self.config.write_text('{"version":1,"version":1}')
        self.assertEqual(self.run_bootstrap()['code'], 'invalid_config')
        self.save_config()
        self.document['nodes'].append(self.document['nodes'][1])
        self.registry.write_text(json.dumps(self.document))
        self.assertEqual(self.run_bootstrap()['code'], 'invalid_registry')
        self.assertFalse(self.calls)


class UnsupportedPlatformTests(unittest.TestCase):
    def test_non_linux_does_not_read_config_or_change_platform_services(self):
        with patch.object(bootstrap.sys, 'platform', 'win32'), patch.object(bootstrap, 'read_config') as reader:
            result = bootstrap.run(Path('/does/not/exist'), {})
        self.assertEqual(result['code'], 'unsupported')
        self.assertFalse(result['credentials_ready'])
        reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
