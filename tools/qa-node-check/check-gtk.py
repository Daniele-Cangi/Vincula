"""Isolated modeless node-check QA; default mode uses no network or real runtime."""

import argparse
from contextlib import closing
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, Gtk

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "outputs/Windows Dock"))


def pump():
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


def wait(owner):
    deadline = monotonic() + 20
    while owner.nodes_busy and monotonic() < deadline:
        pump()
        sleep(0.01)
    pump()
    assert not owner.nodes_busy, "Node-check callback did not finish"


def load_gui():
    spec = importlib.util.spec_from_file_location("node_check_gtk_qa", ROOT / "outputs/Windows Dock/windows-dock.py")
    gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gui)
    app = Gtk.Application(application_id="local.vincula.NodeCheckQA", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    owner = gui.DockWindow(app)  # Never show: no real status, sharing or desktop starts.
    css = Gtk.CssProvider()
    css.load_from_data(gui.STYLE)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return gui, owner


def render(editor, destination, *, bottom=False):
    body = editor.get_child()
    editor.remove(body)
    offscreen = Gtk.OffscreenWindow()
    offscreen.get_style_context().add_class("dock")
    offscreen.set_size_request(680, 610)
    offscreen.add(body)
    offscreen.show_all()
    try:
        for _ in range(15):
            pump()
            sleep(0.01)
        if bottom:
            scroll = body.get_children()[0]
            adjustment = scroll.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
            pump()
        for item in (editor.message, editor.metadata, *editor.values.values()):
            width, height = item.get_layout().get_pixel_size()
            assert width <= item.get_allocated_width() + 2, "Label width cropped"
            assert height <= item.get_allocated_height() + 2, "Label height cropped"
        assert editor.check_button.get_allocated_height() >= editor.check_button.get_preferred_height()[0]
        offscreen.get_pixbuf().savev(str(destination), "png", [], [])
    finally:
        offscreen.remove(body)
        editor.add(body)
        offscreen.destroy()


def snapshot(local, peer, revision):
    return {"ok": True, "revision": revision, "registry": {"local": local, "nodes": [
        {"id": local, "label": "Origine fixture", "family": "linux"},
        {"id": peer, "label": "Nodo fixture · caffè", "family": "linux", "ssh": {
            "host": "not-contacted.example", "user": "worker", "port": 22,
            "identity_file": "/fixture/never-read.pub", "identity_agent": "/fixture/never-opened.sock",
            "known_hosts_file": "/fixture/pin"}}]}}


def run_mock(output):
    gui, owner = load_gui()
    local, peer, revision = str(uuid4()), str(uuid4()), "a" * 64
    initial = snapshot(local, peer, revision)
    report = {**deepcopy(initial), "node_check": {"node_id": peer, "origin_id": local, "revision": revision,
              "route_digest": "b" * 64, "checked_at": "2026-11-25T12:00:00+00:00", "elapsed_seconds": 0.1,
              "checks": {"ssh": {"state": "authenticated"}, "identity": {"state": "match"},
                         "helper": {"state": "compatible_on_disk"},
                         "policy": {"state": "observed", "mode": "full", "native_enabled": True},
                         "worker": {"state": "unverified"}, "desktop": {"state": "unchecked"}}}}
    owner.show_catalog(initial)
    editor = gui.NodeCheckEditor(owner, initial["registry"]["nodes"][1])
    owner.node_check_editor = editor
    release, calls = Event(), []

    def response(payload):
        calls.append(payload)
        assert release.wait(5), "Fixture gate was not released"
        return deepcopy(report)

    try:
        with patch.object(gui.node_catalog, "request", side_effect=response):
            owner.busy = True  # A transfer is independently in progress.
            editor.submit()
            assert editor.running and owner.nodes_busy and not editor.check_button.get_sensitive()
            render(editor, output / "gtk-check-pending.png")
            owner.remote_stop, owner.remote_mode = Event(), "input"
            owner.stop_remote()
            assert owner.remote_stop.is_set(), "Stop was blocked by node check or transfer"
            owner.remote_stop, owner.remote_mode, owner.busy = None, None, False
            release.set()
            wait(owner)
        assert calls == [{"action": "enrollment_check", "node_id": peer, "expected_revision": revision}]
        assert editor.values["ssh"].get_text() == "Autenticazione SSH riuscita"
        assert "corrispondente" in editor.values["identity"].get_text()
        assert "su disco" in editor.values["helper"].get_text()
        assert "Accesso completo" in editor.values["policy"].get_text()
        assert editor.values["worker"].get_text() == "Servizio autonomo non verificato"
        assert "non verificati" in editor.values["desktop"].get_text()
        assert "aaaaaaaaaaaa" in editor.metadata.get_text() and "bbbbbbbbbbbb" in editor.metadata.get_text()
        for name in ("ssh", "identity", "helper", "policy"):
            assert gui.NodeCheckEditor.description(name, {"state": "unchecked", "reason": "termination_unconfirmed"}) == "Arresto della verifica non confermato"
        render(editor, output / "gtk-check-result-top.png")
        render(editor, output / "gtk-check-result-bottom.png", bottom=True)
        for key, value in (("node_id", str(uuid4())), ("origin_id", str(uuid4())), ("revision", "c" * 64)):
            wrong = deepcopy(report)
            wrong["node_check"][key] = value
            editor.finished(wrong)
            assert all(label.get_text() == "Verifica non conclusa" for label in editor.values.values())
        editor.failed("Il runtime non ha risposto entro 75 secondi")
        assert "75 secondi" in editor.message.get_text()
        editor.failed("Aggiorna agent/node_probe.py")
        assert "Aggiorna il runtime" in editor.message.get_text()
        editor.failed("/private/diagnostic-must-not-display")
        assert "/private/" not in editor.message.get_text()
        owner.show_catalog(initial)
        release.clear()
        with patch.object(gui.node_catalog, "request", side_effect=response):
            editor.submit()
            editor.destroy()
            assert not editor.alive
            release.set()
            wait(owner)
        assert owner.node_check_editor is None
        invitation = {"node": {"id": peer, "label": "Nodo fixture", "family": "linux"},
                      "ssh_host_key": {"fingerprint": "SHA256:fixture"},
                      "endpoints": [{"host": "not-contacted.example", "user": "worker", "port": 22, "network": "fixture"}]}
        imported = gui.InvitationEditor(owner, invitation)
        assert not imported.node_check_button.get_sensitive()
        imported.finished({"next_steps": []})
        selected = []
        with patch.object(owner, "edit_node_check", side_effect=lambda node_id: selected.append(node_id)):
            imported.node_check_button.clicked()
        assert selected == [peer]
        assert not owner.get_visible() and not imported.get_visible()
        imported.destroy()
        return {"ok": True, "mode": "mock", "network": "none", "checks": [
            "typed six-state report and policy full remain observations", "UUID/origin/revision binding rejects stale replies",
            "postimport button binds imported UUID", "timeout and runtime update messages", "private diagnostics omitted",
            "asynchronous check plus transfer flag cannot block Stop", "late callback after dialog destruction ignored",
            "offscreen labels, metadata and disabled button allocations complete"]}
    finally:
        release.set()
        owner.remote_stop, owner.remote_mode, owner.busy = None, None, False
        if editor.alive:
            editor.destroy()
        owner.destroy()


def run_real_local(output):
    """Explicit mode for the coordinator, after the backend freeze. No SSH route exists."""
    with TemporaryDirectory(prefix="vincula-check-local-") as temporary:
        home = Path(temporary)
        environment = {"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home / "AppData/Local"),
                       "XDG_CONFIG_HOME": str(home / ".config"), "VINCULA_RUNTIME": str(ROOT),
                       "VINCULA_NODE_REGISTRY": str(home / ".config/vincula/core/nodes.json")}
        with patch.dict(os.environ, environment):
            import node_catalog
            local = str(uuid4())
            initial = node_catalog.request({"action": "add", "local": True,
                                            "node": {"id": local, "label": "Origine locale isolata", "family": "linux"}})
            helper_root = home / ".local/share/vincula-jobs"
            helper_root.mkdir(parents=True)
            for name in ("jobs.py", "vincula_files.py", "vincula_capabilities.py", "plan_journal.py", "wire_framing.py", "direct_execution.py",
                         "resource_policy.py", "worker_resources.py", "execution_plans.py", "transfer_state.py", "transfer_storage.py",
                         "transfer_wire.py", "large_transfer_plans.py", "large_files.py"):
                (helper_root / name).write_bytes((ROOT / "agent" / name).read_bytes())
            with closing(sqlite3.connect(helper_root / "jobs.sqlite3")) as database:
                database.executescript("CREATE TABLE policy(mode TEXT); INSERT INTO policy VALUES('full');"
                                       "CREATE TABLE direct_execution_policy(id INTEGER, enabled INTEGER);"
                                       "INSERT INTO direct_execution_policy VALUES(1,1);")
                database.commit()
            registry = Path(environment["VINCULA_NODE_REGISTRY"])
            before = {path: path.read_bytes() for path in [registry, *helper_root.iterdir()]}
            gui, owner = load_gui()
            owner.show_catalog(initial)
            editor = gui.NodeCheckEditor(owner, initial["registry"]["nodes"][0])
            owner.node_check_editor = editor
            try:
                editor.submit()
                wait(owner)
                observed = owner.nodes_snapshot["node_check"]
                checks = observed["checks"]
                assert observed["node_id"] == local and observed["origin_id"] == local
                assert checks["ssh"] == {"state": "unchecked", "reason": "local_node"}
                assert checks["identity"]["state"] == "match"
                assert checks["helper"]["state"] == "compatible_on_disk"
                assert checks["policy"] == {"state": "observed", "mode": "full", "native_enabled": True}
                assert checks["worker"]["state"] == "unverified" and checks["desktop"]["state"] == "unchecked"
                assert all(path.read_bytes() == content for path, content in before.items())
                assert not owner.get_visible() and not editor.get_visible()
                render(editor, output / "gtk-check-real-local-top.png")
                render(editor, output / "gtk-check-real-local-bottom.png", bottom=True)
                return {"ok": True, "mode": "real_local", "network": "none; local UUID only, no SSH route",
                        "checks": checks, "fixture_policy_and_files_unchanged": True}
            finally:
                editor.destroy()
                owner.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-local", action="store_true", help="Run the frozen backend locally against a temporary HOME")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    result = run_real_local(arguments.output) if arguments.real_local else run_mock(arguments.output)
    (arguments.output / ("gtk-real-local-result.json" if arguments.real_local else "gtk-mock-result.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
