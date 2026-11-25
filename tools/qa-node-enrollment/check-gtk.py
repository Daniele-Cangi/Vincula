"""Isolated enrollment QA. Only an offscreen widget tree is mapped."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, Gtk


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "work/node-enrollment"
sys.path.insert(0, str(ROOT / "outputs/Windows Dock"))
import node_catalog


def pump():
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


def wait(owner):
    deadline = monotonic() + 15
    while owner.nodes_busy and monotonic() < deadline:
        pump()
        sleep(0.01)
    pump()
    assert not owner.nodes_busy, "GUI catalog request did not finish"


def widgets(parent):
    yield parent
    if isinstance(parent, Gtk.Container):
        for child in parent.get_children():
            yield from widgets(child)


def render(dialog, name, *, bottom=False, width=680, height=690):
    body = dialog.get_child()
    dialog.remove(body)
    offscreen = Gtk.OffscreenWindow()
    offscreen.get_style_context().add_class("dock")
    offscreen.set_size_request(width, height)
    offscreen.add(body)
    offscreen.show_all()
    try:
        for _ in range(15):
            pump()
            sleep(0.01)
        if bottom:
            scroll = next(item for item in widgets(body) if isinstance(item, Gtk.ScrolledWindow))
            adjustment = scroll.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
            pump()
        for item in widgets(body):
            if isinstance(item, Gtk.Label) and item.get_visible():
                text_width, text_height = item.get_layout().get_pixel_size()
                assert text_width <= item.get_allocated_width() + 2, (item.get_text(), "horizontal crop")
                assert text_height <= item.get_allocated_height() + 2, (item.get_text(), "vertical crop")
        pixbuf = offscreen.get_pixbuf()
        assert pixbuf is not None
        pixbuf.savev(str(OUTPUT / name), "png", [], [])
        return {"image": name, "width": pixbuf.get_width(), "height": pixbuf.get_height()}
    finally:
        offscreen.remove(body)
        dialog.add(body)
        offscreen.destroy()


class SavePublicDocument:
    """A fixture destination avoids opening a native file dialog."""

    def __init__(self, destination):
        self.destination = destination

    def set_current_name(self, _name):
        pass

    def set_do_overwrite_confirmation(self, _enabled):
        pass

    def run(self):
        return Gtk.ResponseType.ACCEPT

    def get_filename(self):
        return str(self.destination)

    def destroy(self):
        pass


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    images = []
    with TemporaryDirectory(prefix="vincula-enrollment-gtk-") as temporary:
        base = Path(temporary)
        controller = base / "controller/nodes.json"
        source = base / "peer/nodes.json"
        local_id, known_id, new_id = (str(uuid4()) for _ in range(3))
        os.environ["VINCULA_RUNTIME"] = str(ROOT)
        os.environ["VINCULA_NODE_REGISTRY"] = str(source)
        node_catalog.request({"action": "add", "local": True,
                              "node": {"id": new_id, "label": "Nuovo computer · caffè", "family": "linux"}})
        wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x01" * 32
        host_key = base / "ssh_host_ed25519_key.pub"
        host_key.write_text("ssh-ed25519 " + base64.b64encode(wire).decode("ascii") + " fixture\n")
        endpoints = [{"host": "192.0.2.40", "user": "worker", "port": 22, "network": "Ethernet"},
                     {"host": "peer.example", "user": "operator", "port": 2222, "network": "Wi-Fi"}]
        invitation = node_catalog.request({"action": "enrollment_export", "endpoints": endpoints,
                                           "public_host_key": node_catalog.read_public_text(host_key, 8192, host_key=True)})["invitation"]
        preview = node_catalog.request({"action": "enrollment_preview", "invitation_text": json.dumps(invitation)})["invitation"]
        os.environ["VINCULA_NODE_REGISTRY"] = str(controller)
        node_catalog.request({"action": "add", "local": True,
                              "node": {"id": local_id, "label": "Origine Ubuntu", "family": "linux"}})
        identity = str(base / "identity-never-read.pub")
        agent = str(base / "identity-agent-never-opened.sock")
        node_catalog.request({"action": "add", "node": {"id": known_id, "label": "Nodo già registrato", "family": "linux",
                              "ssh": {"host": "known.example", "user": "worker", "port": 22, "identity_file": identity,
                                      "identity_agent": agent, "bind_address": "192.0.2.10",
                                      "known_hosts_file": str(base / "existing-known-hosts")}}})
        spec = importlib.util.spec_from_file_location("enrollment_gui_qa", ROOT / "outputs/Windows Dock/windows-dock.py")
        gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gui)
        application = Gtk.Application(application_id="local.vincula.EnrollmentQA", flags=Gio.ApplicationFlags.NON_UNIQUE)
        application.register(None)
        owner = gui.DockWindow(application)  # Never shown: no refresh, network or input sharing.
        css = Gtk.CssProvider()
        css.load_from_data(gui.STYLE)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        owner.catalog_request({"action": "list"})
        wait(owner)
        before = controller.read_bytes()
        editor = gui.InvitationEditor(owner, preview)
        owner.invitation_editor = editor
        assert not owner.get_visible() and not editor.get_visible()
        assert editor.fields["identity_file"].get_text() == identity
        assert editor.fields["identity_agent"].get_text() == agent
        assert not editor.submit_button.get_sensitive() and not editor.verified.get_active()
        editor.submit()
        assert not owner.nodes_busy and controller.read_bytes() == before
        images.append(render(editor, "gtk-import-unverified.png"))
        editor.endpoint.set_active(1)
        editor.verified.set_active(True)
        assert editor.submit_button.get_sensitive()
        images.append(render(editor, "gtk-import-verified.png", bottom=True))
        editor.submit()
        wait(owner)
        assert owner.nodes_snapshot["state"] == "registered"
        assert set(owner.nodes_snapshot["checks"].values()) == {"unchecked"}
        route = next(node for node in owner.nodes_snapshot["registry"]["nodes"] if node["id"] == new_id)["ssh"]
        assert route["host"] == "peer.example" and route["port"] == 2222
        assert route["identity_file"] == identity and route["identity_agent"] == agent
        assert "NON verificati" in editor.message.get_text()
        assert not Path(identity).exists() and not Path(agent).exists()
        assert not editor.form.get_sensitive() and not editor.submit_button.get_sensitive()
        images.append(render(editor, "gtk-import-registered.png", bottom=True))
        editor.destroy()

        export = gui.InvitationEditor(owner)
        owner.invitation_editor = export
        for key, value in endpoints[0].items():
            export.endpoints[0][key].set_text(str(value))
        export.add_endpoint()
        for key, value in endpoints[1].items():
            export.endpoints[1][key].set_text(str(value))
        export.fields["public_host_key"].set_text(str(host_key))
        images.append(render(export, "gtk-export-top.png"))
        images.append(render(export, "gtk-export-bottom.png", bottom=True))
        destination = base / "public-invitation.json"
        with patch.object(Gtk.FileChooserNative, "new", return_value=SavePublicDocument(destination)) as chooser:
            host_key.write_text("ssh-ed25519 INVALID_BASE64\n")
            export.submit()
            wait(owner)
            assert not destination.exists() and not chooser.called, "Unvalidated invitation was saved"
            host_key.write_text("ssh-ed25519 " + base64.b64encode(wire).decode("ascii") + " fixture\n")
            export.submit()
            wait(owner)
            saved = json.loads(destination.read_text())
            assert saved["node"]["id"] == local_id and saved["endpoints"] == endpoints
            assert "Invito pubblico salvato" in export.message.get_text()
            assert chooser.call_count == 1
        images.append(render(export, "gtk-export-saved.png", bottom=True))
        assert not owner.get_visible() and not export.get_visible()
        export.destroy()
        owner.destroy()
        report = {"ok": True, "scope": "temporary registry, hidden owner, offscreen dialog body only",
                  "checks": ["fingerprint gate blocks GUI and direct submit", "canonical preview and endpoint selection",
                             "local agent identity prefill without identity file access", "registered and unchecked state",
                             "labels and buttons have complete Pango allocations", "invalid export cannot open save picker",
                             "validated export preserves local UUID and both endpoints"], "images": images}
        (OUTPUT / "gtk-qa-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    run()
