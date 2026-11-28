# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual private pipes/process lifecycle with synthetic facts, no input or clipboard."""
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

import desktop_managed as managed
import desktop_security
import desktop_supervisor as supervisor
from test_desktop_managed import configuration


@unittest.skipUnless(sys.platform == "linux", "Synthetic executable fixture uses a POSIX shebang; wire tests run everywhere.")
class ManagedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="vincula-managed-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = desktop_security.private_directory(self.root / "approvals")
        self.environment = patch.dict(os.environ, {"VINCULA_DESKTOP_ARTIFACT_ROOT": str(self.artifacts),
                                                   "PYTHONDONTWRITEBYTECODE": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.daemon = supervisor.Supervisor(self.root / "control", lease_seconds=20, guardian_lease=1)
        self.addCleanup(self.daemon.close)

    def fixture(self, mode="ready", approve=True):
        binary = self.root / "synthetic-core"
        module_path = str(Path(managed.__file__).parent)
        code = '''#!/usr/bin/python3
import json,os,sys,time
sys.dont_write_bytecode=True
sys.path.insert(0,'''+repr(module_path)+''')
from desktop_managed import read_frame,write_frame
incoming=int(sys.argv[sys.argv.index('--vincula-managed-in')+1])
outgoing=int(sys.argv[sys.argv.index('--vincula-managed-out')+1])
settings=sys.argv[sys.argv.index('--settings')+1]
mode=json.load(open(settings))['mode']
print('SYNTHETIC LOG CLAIMS READY; THIS IS NOT ATTESTATION',flush=True)
if mode=='never-read':time.sleep(60)
bootstrap=read_frame(incoming)
sequence=0
while True:
 try:request=read_frame(incoming)
 except EOFError:break
 sequence+=1
 if mode=='block-after-ready' and sequence>1:time.sleep(60)
 node=bootstrap['node_id'];primary=bootstrap['primary']
 peers=set(bootstrap['members'])-{node} if node==primary else {primary}
 reply={k:bootstrap[k] for k in ('session_id','node_id','engine_nonce','manifest_sha256')}
 reply.update(type='snapshot',protocol=1,challenge=request['challenge'],sequence=sequence,
  revoked=mode=='revoke' and sequence>1,reason='',role='primary' if node==primary else 'client',
  input_ready=True,clipboard_ready=mode!='no-clipboard',
  peers=[dict(node_id=p,pin_sha256=bootstrap['tls_pins'][p],handshake=True) for p in sorted(peers)],
  local_pin_sha256=bootstrap['tls_pins'][node])
 write_frame(outgoing,reply)
'''
        binary.write_text(code)
        binary.chmod(0o700)
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        if approve:
            approval = self.artifacts / (digest + ".json")
            approval.write_text(json.dumps({"protocol": 1, "binary_sha256": digest, "executable": str(binary),
                                           "artifact_manifest_sha256": "a" * 64}))
            approval.chmod(0o600)
        config = configuration()
        profile = desktop_security.private_directory(self.root / "profile")
        settings = profile / "settings.conf"
        settings.write_text(json.dumps({"mode": mode}))
        settings.chmod(0o600)
        config.update(settings_file=str(settings), state_file=str(profile / "state.conf"), binary_sha256=digest)
        group = config["group"]
        node = config["node_id"]
        self.plan = {"session_id": group["session_id"], "revision": group["revisions"][node], "local_id": node,
                     "primary": group["primary"], "members": sorted(group["tls_pins"]),
                     "commands": [{"name": "engine", "argv": [str(binary), "server", "--settings", str(settings)],
                                   "managed": config}]}
        self.daemon.prepare(self.plan)
        self.daemon.commit(self.plan["session_id"])

    def wait(self, predicate, timeout=7):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            state = self.daemon.status()
            if predicate(state):
                return state
            if state["state"] in {"prepared", "starting", "running"}:
                self.daemon.heartbeat(self.plan["session_id"])
            sleep(0.02)
        self.fail("Synthetic process condition did not occur: " + repr(self.daemon.status()))

    def test_fresh_bound_snapshot_and_scoped_stop(self):
        self.fixture()
        self.wait(lambda state: state["desktop_ready"])
        before = monotonic()
        self.daemon.stop(self.plan["session_id"])
        self.assertLess(monotonic() - before, 0.3)
        self.assertFalse(self.daemon.status()["desktop_ready"])
        self.wait(lambda state: state["state"] == "stopped")

    def test_a_ready_log_without_requested_clipboard_never_becomes_ready(self):
        self.fixture("no-clipboard")
        self.wait(lambda state: state["state"] == "running")
        sleep(0.7)
        self.assertFalse(self.daemon.status()["desktop_ready"])
        self.assertIsNotNone(self.daemon.jobs[0].get("engine_report"))
        self.daemon.stop(self.plan["session_id"])
        self.wait(lambda state: state["state"] == "stopped")

    def test_revocation_and_blocked_snapshot_are_terminal(self):
        self.fixture("block-after-ready")
        self.wait(lambda state: state["desktop_ready"])
        final = self.wait(lambda state: state["state"] == "failed")
        self.assertFalse(final["desktop_ready"])
        self.assertIsNotNone(final["components"][0]["exit_code"])

    def test_stop_does_not_wait_for_a_child_that_ignores_private_bootstrap(self):
        self.fixture("never-read")
        self.wait(lambda state: state["state"] == "running")
        self.assertFalse(self.daemon.status()["desktop_ready"])
        before = monotonic()
        self.daemon.stop(self.plan["session_id"])
        self.assertLess(monotonic() - before, 0.3)
        self.wait(lambda state: state["state"] == "stopped")

    def test_self_selected_binary_hash_without_local_approval_is_not_executed(self):
        self.fixture(approve=False)
        state = self.wait(lambda state: state["state"] == "failed")
        self.assertFalse(state["desktop_ready"])
        self.assertIsNone(state["components"][0]["pid"])


if __name__ == "__main__":
    unittest.main()
