# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Prepare local SSH-agent credentials; never connects to another computer.

This standalone Linux CLI is intentionally independent from MCP and job startup.
It reads only public key bytes and private key metadata. OpenSSH owns key loading
and the selected local passphrase prompt. No enrollment or readiness grants are made.
"""

import argparse
import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryFile
import time
from uuid import UUID

LIMIT = 64 * 1024
SSH_ADD = '/usr/bin/ssh-add'
SYSTEMCTL = '/usr/bin/systemctl'
LOGINCTL = '/usr/bin/loginctl'


class BootstrapError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def literal_path(value):
    if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in '\x00\r\n'):
        raise BootstrapError('invalid_config', 'An absolute local path is required.')
    return Path(value)


def private_directory(path):
    try:
        item = path.lstat()
    except FileNotFoundError:
        raise BootstrapError('unsafe_runtime', 'The desktop runtime directory is absent.') from None
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid() or stat.S_IMODE(item.st_mode) & 0o077:
        raise BootstrapError('unsafe_runtime', 'The runtime directory must be owned by this user with mode 0700.')


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BootstrapError('invalid_config', 'Duplicate JSON fields are not supported.')
            result[key] = value
        return result

    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise BootstrapError('invalid_config', 'The local JSON input exceeds its size limit.')
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)


def read_config(path):
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o022:
        raise BootstrapError('invalid_config', 'The local profile must be a user-owned regular file without shared write access.')
    config = load_json(path)
    if not isinstance(config, dict) or set(config) - {'version', 'registry', 'agent_source', 'askpass', 'legacy_routes', 'prompt_mode'}:
        raise BootstrapError('invalid_config', 'Unsupported credential profile fields.')
    if config.get('version') != 1 or type(config['version']) is not int:
        raise BootstrapError('invalid_config', 'Credential profile version 1 is required.')
    literal_path(config.get('registry'))
    if config.get('agent_source') not in ('openssh', 'user-manager'):
        raise BootstrapError('invalid_config', 'Select openssh or user-manager as the agent source.')
    if config.get('prompt_mode', 'askpass') not in ('askpass', 'terminal'):
        raise BootstrapError('invalid_config', 'Select askpass or terminal as the local prompt mode.')
    if config.get('askpass') is not None:
        literal_path(config['askpass'])
    if not isinstance(config.get('legacy_routes', {}), dict):
        raise BootstrapError('invalid_config', 'legacy_routes must map local route names to identity_file profiles.')
    return config


def credentials(config):
    document = load_json(literal_path(config['registry']))
    if not isinstance(document, dict) or not isinstance(document.get('nodes'), list) or not 1 <= len(document['nodes']) <= 64:
        raise BootstrapError('invalid_registry', 'The local node registry is missing or invalid.')
    local = document.get('local')
    selected, identifiers = {}, set()
    for entry in document['nodes']:
        if not isinstance(entry, dict):
            raise BootstrapError('invalid_registry', 'A registry entry is invalid.')
        node_id = entry.get('id')
        try:
            valid_id = isinstance(node_id, str) and str(UUID(node_id)) == node_id
        except ValueError:
            valid_id = False
        if not valid_id or node_id in identifiers:
            raise BootstrapError('invalid_registry', 'Node IDs must be unique canonical UUIDs.')
        identifiers.add(node_id)
        if node_id == local:
            continue
        if 'ssh' in entry and 'legacy_route' in entry:
            raise BootstrapError('invalid_registry', 'A node must use only one SSH route.')
        profile = entry.get('ssh')
        if 'legacy_route' in entry:
            profile = config.get('legacy_routes', {}).get(entry['legacy_route'])
            if profile is None:
                raise BootstrapError('legacy_profile_missing', 'An identity profile for a legacy route is missing.')
        if profile is None:
            continue
        if not isinstance(profile, dict):
            raise BootstrapError('invalid_registry', 'An SSH identity profile is invalid.')
        identity = literal_path(profile.get('identity_file'))
        key = str(identity)
        selected.setdefault(key, {'identity': identity, 'public': Path(key + '.pub'), 'nodes': []})['nodes'].append(node_id)
    if local not in identifiers:
        raise BootstrapError('invalid_registry', 'The local UUID is not registered.')
    return list(selected.values())


def stop_helper(process):
    # Popen.wait may reap the leader while handling KeyboardInterrupt. Once
    # returncode is set, the numeric PID/PGID no longer proves ownership.
    # Do not poll here: an exited but unreaped child still reserves its PID.
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def command(argv, environment, timeout):
    # Fixed executables, argument arrays, no shell, no terminal input or raw logs.
    with TemporaryFile() as output:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.DEVNULL, env=environment,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # The timeout path keeps the leader unreaped until group cleanup.
            stop_helper(process)
            return None, b''
        except BaseException:
            # In particular, KeyboardInterrupt may arrive after wait reaped it.
            stop_helper(process)
            raise
        output.seek(0)
        raw = output.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise BootstrapError('output_limit', 'A local helper exceeded its output limit.')
        return code, raw


def require_local_terminal(environment):
    if any(environment.get(name) for name in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY')):
        raise BootstrapError('terminal_missing', 'Unlock needs a terminal on the local desktop, not an SSH terminal.')
    if not all(os.isatty(descriptor) for descriptor in (0, 1, 2)):
        raise BootstrapError('terminal_missing', 'Unlock needs local terminal input, output and error streams.')
    try:
        descriptor = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    except OSError:
        raise BootstrapError('terminal_missing', 'This process has no controlling local terminal.') from None
    try:
        if not os.isatty(descriptor):
            raise BootstrapError('terminal_missing', 'The controlling terminal is invalid.')
    finally:
        os.close(descriptor)


def stop_terminal_helper(process):
    # There is no owned process group here: ssh-add shares the local terminal.
    # Popen.kill targets only this child and skips an already collected child.
    if process.returncode is None:
        process.kill()
    process.wait()


@contextmanager
def terminal_attributes():
    import termios
    descriptor = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    try:
        saved = termios.tcgetattr(descriptor)
        try:
            yield
        finally:
            # ssh-add normally restores ECHO itself, but cannot do so after
            # forced termination. Restore all attributes without reading input.
            termios.tcsetattr(descriptor, termios.TCSANOW, saved)
    finally:
        os.close(descriptor)


def terminal_command(argv, environment, timeout):
    with terminal_attributes():
        process = subprocess.Popen(argv, stdin=None, stdout=None, stderr=None, env=environment,
                                   start_new_session=False)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_terminal_helper(process)
            return None
        except BaseException:
            stop_terminal_helper(process)
            raise


def agent_socket(config, environment, *, activate=False):
    runtime = literal_path(environment.get('XDG_RUNTIME_DIR'))
    private_directory(runtime)
    candidate = runtime / 'openssh_agent'
    if config['agent_source'] == 'user-manager':
        code, raw = command([SYSTEMCTL, '--user', 'show-environment'], environment, 5)
        if code != 0:
            raise BootstrapError('agent_missing', 'The desktop user manager is unavailable.')
        values = [line.partition('=')[2] for line in raw.decode('utf-8').splitlines()
                  if line.startswith('SSH_AUTH_SOCK=')]
        if len(values) != 1:
            raise BootstrapError('agent_missing', 'The user manager does not select one SSH-agent socket.')
        candidate = literal_path(values[0])
    elif activate and not candidate.exists():
        # Socket activation starts only the existing packaged user unit. Never
        # spawn a second ad-hoc agent or change the desktop manager selection.
        code, _ = command([SYSTEMCTL, '--user', 'start', 'ssh-agent.socket'], environment, 5)
        if code != 0:
            raise BootstrapError('agent_missing', 'The existing OpenSSH user socket could not be activated.')
        deadline = time.monotonic() + 2
        while not candidate.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    try:
        item = candidate.lstat()
    except FileNotFoundError:
        raise BootstrapError('agent_missing', 'The selected SSH-agent socket is absent.') from None
    if not stat.S_ISSOCK(item.st_mode) or item.st_uid != os.getuid():
        raise BootstrapError('agent_invalid', 'The selected socket must belong to this user and be a real socket.')
    return candidate


def key_metadata(key):
    # Do not open/read the private file. Only OpenSSH receives its filename.
    for name in ('identity', 'public'):
        item = key[name].lstat()
        if not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid():
            raise BootstrapError('key_invalid', 'SSH key files must be regular files owned by this user.')
        if name == 'identity' and stat.S_IMODE(item.st_mode) & 0o077:
            raise BootstrapError('key_invalid', 'Private key permissions allow access by other users.')
        if name == 'public' and item.st_mode & 0o022:
            raise BootstrapError('key_invalid', 'The public key can be modified by other users.')
    # The public file is deliberately the only key material read by this module.
    with key['public'].open('rb') as stream:
        raw = stream.read(8193)
    fields = raw.split()
    if len(raw) > 8192 or len(raw.splitlines()) != 1 or len(fields) < 2:
        raise BootstrapError('public_key_invalid', 'A single OpenSSH public key is required.')
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (ValueError, binascii.Error):
        blob = b''
    length = int.from_bytes(blob[:4], 'big')
    if not length or blob[4:4 + length] != fields[0] or len(blob) <= 4 + length:
        raise BootstrapError('public_key_invalid', 'The SSH public key wire type does not match its declaration.')
    return hashlib.sha256(raw).hexdigest()


def signs(key, environment):
    code, _ = command([SSH_ADD, '-T', str(key['public'])], environment, 5)
    return code == 0


def graphical_askpass(config, environment):
    if not environment.get('DISPLAY') and not environment.get('WAYLAND_DISPLAY'):
        raise BootstrapError('graphical_session_missing', 'Unlock requires the local graphical desktop session.')
    if not environment.get('DBUS_SESSION_BUS_ADDRESS'):
        raise BootstrapError('graphical_session_missing', 'The desktop session bus is not available.')
    value = config.get('askpass')
    if not value:
        raise BootstrapError('unsupported', 'No local graphical askpass is configured; terminal fallback is not implemented.')
    askpass = literal_path(value)
    try:
        item = askpass.stat()
    except OSError:
        raise BootstrapError('unsupported', 'The configured graphical askpass is unavailable.') from None
    if not stat.S_ISREG(item.st_mode) or item.st_uid not in (0, os.getuid()) or item.st_mode & 0o022 or not os.access(askpass, os.X_OK):
        raise BootstrapError('unsupported', 'The graphical askpass is not a trusted local executable.')
    return askpass


@contextmanager
def state_lock(environment):
    import fcntl
    runtime = literal_path(environment.get('XDG_RUNTIME_DIR'))
    private_directory(runtime)
    directory = runtime / 'vincula-credentials'
    directory.mkdir(mode=0o700, exist_ok=True)
    private_directory(directory)
    descriptor = os.open(directory / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+b') as stream:
        item = os.fstat(stream.fileno())
        if not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
            raise BootstrapError('unsafe_runtime', 'The credential lock permissions are invalid.')
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BootstrapError('busy', 'Another credential request is still running.') from None
        try:
            yield directory
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_state(path, value):
    stage = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.credentials-', delete=False) as stream:
            stage = Path(stream.name)
            json.dump(value, stream, ensure_ascii=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def session_marker(environment):
    hint = environment.get('XDG_SESSION_ID', '')
    selector = hint if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', hint) else 'auto'
    code, raw = command([LOGINCTL, 'show-session', selector, '--no-pager',
                         '--property=Id', '--property=User', '--property=Type',
                         '--property=Remote', '--property=State'], environment, 5)
    observed = {}
    for line in raw.decode('utf-8').splitlines():
        key, separator, value = line.partition('=')
        if separator:
            if key in observed:
                raise BootstrapError('session_missing', 'The desktop login identity is ambiguous.')
            observed[key] = value
    session = observed.get('Id', '')
    if (code != 0 or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session)
            or observed.get('User') != str(os.getuid())
            or observed.get('Type') not in ('x11', 'wayland')
            or observed.get('Remote') != 'no' or observed.get('State') not in ('active', 'online')
            or (selector != 'auto' and session != selector)):
        raise BootstrapError('session_missing', 'Automatic unlock needs a verified local graphical login session.')
    return 'automatic-' + hashlib.sha256(session.encode('ascii')).hexdigest() + '.json'


def result(code, *, ready=False, keys=(), message=None):
    value = {'version': 1, 'code': code, 'credentials_ready': ready, 'keys': list(keys),
             'checked_at': int(time.time())}
    if message:
        value['message'] = message
    return value


def prepare(config, environment, directory, *, unlock=False, automatic=False):
    keys = credentials(config)
    if not keys:
        return result('no_credentials_configured')
    socket = agent_socket(config, environment, activate=unlock)
    selected_env = environment | {'SSH_AUTH_SOCK': str(socket), 'SSH_ASKPASS_REQUIRE': 'never'}
    agent_code, _ = command([SSH_ADD, '-l'], selected_env, 5)
    if agent_code not in (0, 1):
        raise BootstrapError('agent_missing', 'The selected SSH-agent socket does not answer.')
    states, missing = [], []
    for key in keys:
        metadata = key_metadata(key)
        signed = signs(key, selected_env)
        states.append({'node_ids': key['nodes'], 'public_file_sha256': metadata,
                       'state': 'ready' if signed else 'key_locked'})
        if not signed:
            missing.append(key)
    if not missing:
        return result('ready', ready=True, keys=states)
    if not unlock:
        return result('key_locked', keys=states,
                      message='A configured key cannot sign through this agent; encryption is not inferred.')
    terminal = config.get('prompt_mode', 'askpass') == 'terminal'
    if terminal:
        require_local_terminal(selected_env)
    else:
        askpass = graphical_askpass(config, selected_env)
    marker = directory / session_marker(environment) if automatic else None
    if marker is not None:
        if marker.exists():
            return result('automatic_attempt_already_made', keys=states,
                          message='Use an explicit local unlock request to retry.')
        # Record before the popup so cancellation/crash cannot trigger another automatic popup.
        write_state(marker, {'attempted_at': int(time.time()), 'automatic': True})
    arguments = [SSH_ADD, *(str(key['identity']) for key in missing)]
    if terminal:
        selected_env.pop('SSH_ASKPASS', None)
        selected_env['SSH_ASKPASS_REQUIRE'] = 'never'
        code = terminal_command(arguments, selected_env, 180)
    else:
        selected_env |= {'SSH_ASKPASS': str(askpass), 'SSH_ASKPASS_REQUIRE': 'force'}
        code, _ = command(arguments, selected_env, 180)
    for key, state in zip(keys, states):
        state['state'] = 'ready' if signs(key, selected_env) else 'key_locked'
    ready = all(state['state'] == 'ready' for state in states)
    return result('ready' if ready else ('unlock_timeout' if code is None else 'unlock_incomplete'),
                  ready=ready, keys=states)


def run(config_path, environment, *, unlock=False, automatic=False):
    if sys.platform != 'linux':
        return result('unsupported', message='This bootstrap supports Linux desktop sessions only.')
    try:
        config = read_config(config_path)
        with state_lock(environment) as directory:
            try:
                value = prepare(config, environment, directory, unlock=unlock, automatic=automatic)
            except BootstrapError as error:
                value = result(error.code, message=str(error))
            except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                value = result('local_input_error', message='A local key, registry or desktop input could not be read.')
            write_state(directory / 'status.json', value)
            return value
    except BootstrapError as error:
        return result(error.code, message=str(error))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return result('local_input_error', message='A local profile, key metadata or runtime input could not be read.')


def desktop_quote(value):
    value = str(value)
    if any(c in value for c in '\r\n\x00'):
        raise BootstrapError('invalid_config', 'Desktop command paths contain an invalid character.')
    value = value.replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        value = value.replace(character, '\\' + character)
    return '"' + value + '"'


def render_autostart(python, module, config, *, terminal=False):
    arguments = [literal_path(str(python)), '-I', '-B', literal_path(str(module)),
                 '--config', literal_path(str(config)), '--action', 'unlock', '--automatic']
    # Desktop Entry value unescaping runs before Exec argument unquoting.
    execution = ' '.join(desktop_quote(argument) for argument in arguments).replace('\\', '\\\\')
    return ('[Desktop Entry]\nType=Application\nName=Vincula SSH credentials\n'
            'Comment=Prepare the local SSH-agent credentials once per desktop session\n'
            'Exec=' + execution + '\nTerminal=' + ('true' if terminal else 'false')
            + '\nX-GNOME-Autostart-enabled=true\n')


def install_plan(config, python):
    source = Path(__file__).absolute()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = Path.home() / '.local/share/vincula/credential-bootstrap' / digest
    module = root / source.name
    desktop = Path.home() / '.config/autostart/vincula-credentials.desktop'
    profile = read_config(config)
    prompt_mode = profile.get('prompt_mode', 'askpass')
    rendered = render_autostart(python, module, config, terminal=prompt_mode == 'terminal')
    return {'version': 1, 'applied': False, 'source': str(source), 'source_sha256': digest,
            'module_destination': str(module), 'autostart_destination': str(desktop),
            'autostart_content': rendered, 'config': str(config), 'prompt_mode': prompt_mode,
            'install_steps': ['Freeze and review the source digest and local profile first.',
                              'Copy the standalone module into the new private version directory; verify its digest.',
                              'Back up an existing autostart file; publish the reviewed desktop file atomically.',
                              'Run status, then one explicit unlock from the local desktop using the selected prompt mode.'],
            'rollback': ['Restore the exact saved autostart file, or remove only the newly created entry.',
                         'Remove only this version directory after confirming no process uses it.',
                         'Do not unload keys, stop the shared agent, alter MCP or change the installed runtime.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--action', choices=('status', 'unlock', 'render-autostart', 'install-plan'), default='status')
    parser.add_argument('--automatic', action='store_true')
    options = parser.parse_args()
    if options.automatic and options.action != 'unlock':
        parser.error('--automatic applies only to unlock')
    try:
        if options.action == 'render-autostart':
            plan = install_plan(options.config.absolute(), Path(sys.executable).absolute())
            print(plan['autostart_content'], end='')
            return 0
        if options.action == 'install-plan':
            value = install_plan(options.config.absolute(), Path(sys.executable).absolute())
        else:
            value = run(options.config.absolute(), dict(os.environ), unlock=options.action == 'unlock', automatic=options.automatic)
    except (BootstrapError, OSError, ValueError):
        value = result('local_input_error', message='Could not render the local installation plan.')
    print(json.dumps(value, ensure_ascii=True))
    return 0 if value.get('credentials_ready') or value.get('applied') is False else 1


if __name__ == '__main__':
    raise SystemExit(main())
