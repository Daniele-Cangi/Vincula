# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated UI/controller protocol QA. Never executes the real desktop controller."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gio, Gtk

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'outputs/Windows Dock'))
spec = importlib.util.spec_from_file_location('managed_group_gtk_qa', root / 'outputs/Windows Dock/windows-dock.py')
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)
app = Gtk.Application(application_id='local.vincula.ManagedGroupProtocolQA', flags=Gio.ApplicationFlags.NON_UNIQUE)
app.register(None)

def pump():
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

def wait(predicate):
    until = time.monotonic() + 8
    while not predicate() and time.monotonic() < until:
        pump()
        time.sleep(.01)
    pump()
    assert predicate(), 'UI/controller checkpoint timed out'

ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333']
registry = {'local': ids[0], 'nodes': [{'id': uid, 'label': name, 'family': 'windows' if n == 1 else 'linux'}
            for n, (uid, name) in enumerate(zip(ids, ['Ubuntu', 'Windows', 'Kali']))],
            'desktop': {'primary': ids[1], 'members': [{'node_id': uid, 'x': n - 1, 'y': 0} for n, uid in enumerate(ids)], 'clipboard': True}}
owners = []
def owner():
    widget = gui.DockWindow(app)
    widget.nodes_snapshot = {'ok': True, 'revision': '0' * 64, 'registry': registry}
    owners.append(widget)
    return widget

def script(base, mode):
    base.mkdir()
    nodes = base / 'nodes.py'
    nodes.write_text('# QA runtime marker only\n')
    marker = base / 'requests.jsonl'
    text = '''import json,pathlib,sys,threading,time
assert sys.argv[1:]==['run']
marker=pathlib.Path(MARKER)
def emit(state,message,cleanup=False):
 print(json.dumps({'type':'status','state':state,'message':message,'cleanup_complete':cleanup}),flush=True)
sys.stderr.write('PRIVATE_SSH_DIAGNOSTIC_MUST_NOT_REACH_UI\\n');sys.stderr.flush()
mode=MODE
emit('starting','Avvio sintetico')
if mode=='prerequisite':
 emit('failed','Motore gestito non installato',True);raise SystemExit(2)
if mode=='crash':
 emit('waiting_ready','Preparazione sintetica');raise SystemExit(3)
if mode=='invalid':
 print('NOT JSON',flush=True)
else:
 emit('ready' if mode=='ready' else 'waiting_ready','Pronto sintetico' if mode=='ready' else 'RPC sintetica pendente')
threading.Thread(target=lambda:time.sleep(60),daemon=True).start()
for line in sys.stdin:
 request=json.loads(line)
 with marker.open('a') as f:f.write(json.dumps(request)+'\\n')
 if request=={'action':'stop'}:
  emit('ready','Risposta ready tardiva da ignorare')
  emit('stopping','Arresto sintetico')
  emit('stopped','Gruppo sintetico arrestato',True)
  if mode!='close':break
if mode=='close':
 with marker.open('a') as f:f.write(json.dumps({'eof':True})+'\\n')
 emit('stopped','Gruppo chiuso con EOF',True)
'''.replace('MARKER', repr(str(marker))).replace('MODE', repr(mode))
    (base / 'desktop_app.py').write_text(text)
    return nodes, marker

results = []
with tempfile.TemporaryDirectory(prefix='vincula-managed-protocol-qa-') as temp:
    base = Path(temp)
    with patch.object(gui.windows, 'status', side_effect=AssertionError('No network/status call permitted')), \
         patch.object(gui, 'share_input', side_effect=AssertionError('No legacy engine permitted')), \
         patch.dict(os.environ, {'VINCULA_PYTHON': sys.executable}):
        try:
            w = owner()
            w.remote_stop = gui.Event()
            with patch.object(gui.subprocess, 'Popen', side_effect=AssertionError('No implicit legacy stop/start')):
                w.toggle_group()
            assert w.group_session is None and 'Ferma prima' in w.group_status.get_text() and not w.remote_stop.is_set()
            w.remote_stop = None
            w.destroy()
            results.append('legacy sharing preserved, explicit stop requested')

            nodes, marker = script(base / 'pending', 'pending')
            w = owner()
            with patch.object(gui.node_catalog, 'runtime_script', return_value=nodes):
                w.toggle_group()
                wait(lambda: 'RPC sintetica' in w.group_status.get_text())
                assert not w.group_status.get_style_context().has_class('session-online')
                w.busy = w.nodes_busy = True
                assert w.group_header_button.get_sensitive()
                started = time.monotonic()
                w.group_header_button.emit('clicked')
                wait(lambda: w.group_session is None)
                assert time.monotonic() - started < 2
                assert not w.group_uncertain
                assert not w.group_status.get_style_context().has_class('session-online')
                assert marker.read_text().count('"stop"') == 1
                w.busy = w.nodes_busy = False
            w.destroy()
            results.append('header stop independent of transfer/catalog busy and blocked RPC; late ready ignored')

            nodes, marker = script(base / 'ready', 'ready')
            w = owner()
            with patch.object(gui.node_catalog, 'runtime_script', return_value=nodes):
                w.toggle_group()
                wait(lambda: w.group_status.get_style_context().has_class('session-online'))
                w.stop_group()
                wait(lambda: w.group_session is None)
                assert not w.group_uncertain
            w.destroy()
            results.append('ready only on explicit backend ready; confirmed cleanup resets controls')

            nodes, marker = script(base / 'prerequisite', 'prerequisite')
            w = owner()
            with patch.object(gui.node_catalog, 'runtime_script', return_value=nodes):
                w.toggle_group()
                wait(lambda: w.group_session is None)
                assert 'non installato' in w.group_status.get_text() and not w.group_uncertain
                assert w.group_button.get_sensitive() and w.input_button.get_sensitive()
                assert 'PRIVATE_SSH' not in w.group_status.get_text()
            w.destroy()
            results.append('missing prerequisites inline, no false ready, no stderr exposure')

            nodes, marker = script(base / 'crash', 'crash')
            w = owner()
            with patch.object(gui.node_catalog, 'runtime_script', return_value=nodes):
                w.toggle_group()
                wait(lambda: w.group_session is None)
                assert w.group_uncertain and not w.group_button.get_sensitive() and not w.input_button.get_sensitive()
                assert 'non confermato' in w.group_status.get_text()
            w.destroy()
            results.append('unexpected exit retains uncertainty and blocks replacement/legacy start')

            nodes, marker = script(base / 'close', 'close')
            w = owner()
            with patch.object(gui.node_catalog, 'runtime_script', return_value=nodes):
                w.toggle_group()
                wait(lambda: 'RPC sintetica' in w.group_status.get_text())
                session = w.group_session
                assert w.on_close()
                wait(lambda: w.group_session is None)
                assert session['process'].poll() == 0
                received = [json.loads(line) for line in marker.read_text().splitlines()]
                assert received == [{'action': 'stop'}, {'eof': True}]
            results.append('window close sends stop and EOF; waits for owned process exit without kill')

            nodes, marker = script(base / 'invalid', 'invalid')
            w = owner()
            with patch.object(gui.node_catalog, 'runtime_script', return_value=nodes):
                w.toggle_group()
                wait(lambda: w.group_session is None)
                assert not w.group_uncertain and 'non valida' in w.group_status.get_text()
                assert w.group_status.get_style_context().has_class('session-failed')
            w.destroy()
            results.append('invalid protocol requests stop and preserves visible failure after cleanup')
        finally:
            for w in owners:
                if w.group_session is not None:
                    w.request_group_stop(w.group_session, close_input=True)
                    wait(lambda: w.group_session is None)
                w.destroy()
print(json.dumps({'passed': True, 'synthetic_only': True, 'checks': results}, ensure_ascii=False))
