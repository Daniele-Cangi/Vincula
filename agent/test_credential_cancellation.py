# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cancellation regressions using only synthetic Python helpers and private PTYs.

Signals target a test-owned controller Popen, never this test runner. No SSH,
keys, GUI, desktop sessions or installed units are accessed by these fixtures.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

import credential_bootstrap as bootstrap


PRELUDE = r'''
import ctypes,importlib.util,json,os,signal,subprocess,sys,time
from pathlib import Path
from unittest.mock import patch
root=Path(sys.argv[2])
spec=importlib.util.spec_from_file_location('synthetic_bootstrap',sys.argv[1])
bootstrap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
real_popen=subprocess.Popen
handles=[]
def tracked_popen(*args,**kwargs):
    process=real_popen(*args,**kwargs)
    handles.append(process)
    return process

def receipt(value):
    (root/'receipt.json').write_text(json.dumps(value))

# Fixture-only parent-death protection bounds a failed regression's lifetime.
# The production module neither installs this nor receives its implementation.
helper_prefix=("import ctypes,os,signal,time; "
               "ctypes.CDLL(None).prctl(1,signal.SIGKILL,0,0,0); ")
'''

HANDLER_PROGRAM = PRELUDE + r'''
previous={}
for signum in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):
    handler=lambda _signum,_frame:None
    signal.signal(signum,handler)
    previous[signum]=handler
code=None
continued=False
try:
    with bootstrap.termination_signals():
        os.kill(os.getpid(),signal.SIGTERM)
        os.kill(os.getpid(),signal.SIGHUP)
        continued=True
except SystemExit as error:
    code=error.code
assert code==143
assert continued, 'handler unwound asynchronously'
assert all(signal.getsignal(signum) is handler for signum,handler in previous.items())
receipt({'code':code,'handler_did_not_raise':continued,'handlers_restored':True})
'''

ASKPASS_PROGRAM = PRELUDE + r'''
# Only this isolated fixture adopts the synthetic grandchild for reliable reap.
assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0
mutation=len(sys.argv)>3 and sys.argv[3]=='only-leader'
runtime=root/'runtime'
runtime.mkdir(mode=0o700)
config=root/'profile.json'
config.write_text(json.dumps({'version':1,'registry':str(root/'unused-registry.json'),
                              'agent_source':'openssh'}))
config.chmod(0o600)
environment=dict(os.environ,XDG_RUNTIME_DIR=str(runtime))
# No parent-death protection on this descendant: it must survive a faulty
# leader-only cleanup so that the negative fixture detects that regression.
grandchild_code='import time; time.sleep(15)'
helper=(helper_prefix+"import subprocess,sys; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-I','-B','-c',{grandchild_code!r}]); "
        f"Path({str(root/'grandchild.pid')!r}).write_text(str(p.pid)); "
        f"Path({str(root/'ready')!r}).touch(); time.sleep(15)")
original_stop=bootstrap.stop_helper
def slow_stop(process):
    (root/'cleanup-entered').touch()
    time.sleep(.3)
    if mutation:
        bootstrap.stop_terminal_helper(process)
    else:
        original_stop(process)

legacy_fixture={'identity':root/'unused-private','public':root/'unused-public','nodes':['fixture']}
def synthetic_prepare(_config,keys,_environment,directory,**_options):
    assert keys==[legacy_fixture], 'fixture did not reach the legacy preparation boundary'
    bootstrap.write_state(directory/'automatic-fixture.json',{'automatic':True})
    bootstrap.command([sys.executable,'-I','-B','-c',helper],environment,10)
    raise AssertionError('cancellation did not interrupt the synthetic helper')

code=None
expected=None
negative_detected=False
running_before_cleanup=False
fixture_reaped=False
try:
    with patch.dict(os.environ,environment,clear=True), \
         patch.object(sys,'argv',['synthetic-cli','--config',str(config),'--action','unlock','--automatic']), \
         patch.object(bootstrap,'credentials',return_value=[legacy_fixture]), \
         patch.object(bootstrap,'prepare_legacy',side_effect=synthetic_prepare), \
         patch.object(bootstrap,'stop_helper',side_effect=slow_stop), \
         patch.object(bootstrap.subprocess,'Popen',side_effect=tracked_popen):
        try:code=bootstrap.main()
        except SystemExit as error:code=error.code
    owned_reaped=bool(handles) and all(process.returncode is not None for process in handles)
    assert owned_reaped, 'the transport did not reap its owned helper'
    assert code==143, f'first signal was not preserved: {code!r}'
    with bootstrap.state_lock(environment) as directory:
        marker_preserved=(directory/'automatic-fixture.json').is_file()
    assert marker_preserved
    expected=int((root/'grandchild.pid').read_text())
    deadline=time.monotonic()+.5
    while True:
        pid,status=os.waitpid(expected,os.WNOHANG)
        if pid:break
        if time.monotonic()>=deadline:
            running_before_cleanup=True
            break
        time.sleep(.01)
    try:
        assert not running_before_cleanup, 'synthetic grandchild is still running'
        assert pid==expected and os.waitstatus_to_exitcode(status)==-signal.SIGKILL
    except AssertionError:
        if not mutation:raise
        negative_detected=True
    if mutation:
        assert negative_detected and running_before_cleanup, 'leader-only mutation was not detected'
    else:
        receipt({'code':code,'owned_helper_reaped':owned_reaped,'marker_preserved':marker_preserved,
                 'lock_released':True,'grandchild_reaped_by_fixture':True,'live_descendants':0})
finally:
    # All observations/assertions above precede cleanup. Only unreaped owned
    # children can be signalled; WNOHANG=0 pins the adopted child's identity.
    for process in handles:
        if process.returncode is None:process.kill()
        process.wait(timeout=3)
    if expected is None and (root/'grandchild.pid').exists():
        expected=int((root/'grandchild.pid').read_text())
    if expected is not None:
        try:pid,status=os.waitpid(expected,os.WNOHANG)
        except ChildProcessError:pass
        else:
            if pid==0:
                os.kill(expected,signal.SIGKILL)
                pid,status=os.waitpid(expected,0)
                assert pid==expected
                fixture_reaped=True
if mutation:
    assert fixture_reaped
    receipt({'code':code,'negative_detected_before_cleanup':negative_detected,
             'grandchild_running_before_cleanup':running_before_cleanup,
             'fixture_cleanup_reaped_grandchild':fixture_reaped})
'''

TERMINAL_PROGRAM = PRELUDE + r'''
import fcntl,termios
os.setsid()
fcntl.ioctl(0,termios.TIOCSCTTY,0)
sentinel=real_popen([sys.executable,'-I','-B','-c',helper_prefix+'time.sleep(15)'])
child=(helper_prefix+"import termios; from pathlib import Path; "
       "a=termios.tcgetattr(0); a[3]&=~termios.ECHO; termios.tcsetattr(0,termios.TCSANOW,a); "
       f"Path({str(root/'ready')!r}).touch(); time.sleep(15)")
original_stop=bootstrap.stop_terminal_helper
def slow_stop(process):
    (root/'cleanup-entered').touch()
    time.sleep(.3)
    original_stop(process)
code=None
try:
    with patch.object(bootstrap,'stop_terminal_helper',side_effect=slow_stop), \
         patch.object(bootstrap.subprocess,'Popen',side_effect=tracked_popen):
        try:
            with bootstrap.termination_signals():
                bootstrap.terminal_command([sys.executable,'-I','-B','-c',child],dict(os.environ),10)
        except SystemExit as error:code=error.code
    owned_reaped=bool(handles) and all(process.returncode is not None for process in handles)
    assert owned_reaped
    assert sentinel.poll() is None, 'cleanup signalled the shared terminal group'
    receipt({'code':code,'owned_helper_reaped':owned_reaped,'sentinel_survived':True})
finally:
    for process in handles+[sentinel]:
        if process.returncode is None:process.kill()
        process.wait(timeout=3)
'''

SPAWN_RACE_PROGRAM = PRELUDE + r'''
from contextlib import nullcontext
transport=getattr(bootstrap,sys.argv[3])
returned=False
created=[]
def interrupted_constructor(*args,**kwargs):
    global returned
    child=real_popen(*args,**kwargs)
    created.append(child)
    os.kill(os.getpid(),signal.SIGTERM)
    returned=True
    return child
code=None
try:
    with patch.object(bootstrap.subprocess,'Popen',side_effect=interrupted_constructor), \
         patch.object(bootstrap,'terminal_attributes',side_effect=nullcontext):
        try:
            with bootstrap.termination_signals():
                transport([sys.executable,'-I','-B','-c',helper_prefix+'time.sleep(15)'],dict(os.environ),10)
        except SystemExit as error:code=error.code
    assert returned, 'handler raised inside the Popen constructor'
    assert len(created)==1 and created[0].returncode is not None, 'created child was not reaped by transport'
    receipt({'code':code,'constructor_returned':returned,'owned_helper_reaped':True})
finally:
    for child in created:
        if child.returncode is None:child.kill()
        child.wait(timeout=3)
'''

PRESPAWN_PROGRAM = PRELUDE + r'''
transport=getattr(bootstrap,sys.argv[3])
code=None
with patch.object(bootstrap.subprocess,'Popen') as popen:
    try:
        with bootstrap.termination_signals():
            os.kill(os.getpid(),signal.SIGHUP)
            transport([sys.executable,'-I','-B','-c','pass'],dict(os.environ),2)
    except SystemExit as error:code=error.code
    assert not popen.called, 'a pending cancellation still created a helper'
receipt({'code':code,'children_created':0})
'''


@unittest.skipUnless(sys.platform == 'linux', 'Linux signals and private PTY fixtures')
class CredentialCancellationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def spawn(self, program, *, terminal=None, arguments=()):
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', program,
                                    str(Path(bootstrap.__file__).resolve()), str(self.root), *arguments],
                                   stdin=subprocess.DEVNULL if terminal is None else terminal,
                                   stdout=subprocess.DEVNULL if terminal is None else terminal,
                                   stderr=subprocess.PIPE if terminal is None else terminal)
        self.addCleanup(self.finish, process)
        return process

    @staticmethod
    def finish(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def wait_for(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            self.assertIsNone(process.poll(), 'synthetic controller exited before readiness')
            self.assertLess(time.monotonic(), deadline, 'synthetic controller did not become ready')
            time.sleep(.01)

    def receipt(self, process):
        _stdout, stderr = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, '' if stderr is None else stderr.decode(errors='replace'))
        return json.loads((self.root/'receipt.json').read_text())

    def test_handlers_latch_first_signal_without_async_raise_and_restore(self):
        value = self.receipt(self.spawn(HANDLER_PROGRAM))
        self.assertEqual(value['code'], 143)
        self.assertTrue(value['handler_did_not_raise'])
        self.assertTrue(value['handlers_restored'])

    def test_sigterm_cli_preserves_marker_releases_lock_and_stops_askpass_tree(self):
        process = self.spawn(ASKPASS_PROGRAM)
        self.wait_for(self.root/'ready', process)
        process.send_signal(signal.SIGTERM)
        self.wait_for(self.root/'cleanup-entered', process)
        process.send_signal(signal.SIGHUP)
        process.send_signal(signal.SIGINT)
        value = self.receipt(process)
        self.assertEqual(value['code'], 143)
        self.assertTrue(value['marker_preserved'])
        self.assertTrue(value['lock_released'])
        self.assertTrue(value['owned_helper_reaped'])
        self.assertTrue(value['grandchild_reaped_by_fixture'])
        self.assertEqual(value['live_descendants'], 0)

    def test_leader_only_mutation_is_detected_before_fixture_cleanup(self):
        process = self.spawn(ASKPASS_PROGRAM, arguments=('only-leader',))
        self.wait_for(self.root/'ready', process)
        process.send_signal(signal.SIGTERM)
        value = self.receipt(process)
        self.assertTrue(value['negative_detected_before_cleanup'])
        self.assertTrue(value['grandchild_running_before_cleanup'])
        self.assertTrue(value['fixture_cleanup_reaped_grandchild'])

    def terminal_case(self, signum):
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = termios.tcgetattr(slave)
        process = self.spawn(TERMINAL_PROGRAM, terminal=slave)
        self.wait_for(self.root/'ready', process)
        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO)
        process.send_signal(signum)
        self.wait_for(self.root/'cleanup-entered', process)
        process.send_signal(signal.SIGTERM)
        value = self.receipt(process)
        self.assertEqual(value['code'], 128 + signum)
        self.assertTrue(value['owned_helper_reaped'])
        self.assertTrue(value['sentinel_survived'])
        self.assertEqual(termios.tcgetattr(slave), before)

    def test_sigterm_restores_terminal_echo_and_preserves_shared_group(self):
        self.terminal_case(signal.SIGTERM)

    def test_sighup_restores_terminal_echo_and_preserves_shared_group(self):
        self.terminal_case(signal.SIGHUP)

    def test_signal_between_child_creation_and_popen_return_cannot_lose_handle(self):
        for name in ('command', 'terminal_command'):
            with self.subTest(transport=name):
                value = self.receipt(self.spawn(SPAWN_RACE_PROGRAM, arguments=(name,)))
                self.assertEqual(value['code'], 143)
                self.assertTrue(value['constructor_returned'])
                self.assertTrue(value['owned_helper_reaped'])

    def test_pending_signal_prevents_helper_creation(self):
        for name in ('command', 'terminal_command'):
            with self.subTest(transport=name):
                value = self.receipt(self.spawn(PRESPAWN_PROGRAM, arguments=(name,)))
                self.assertEqual(value['code'], 128 + signal.SIGHUP)
                self.assertEqual(value['children_created'], 0)


if __name__ == '__main__':
    unittest.main()
