#!/usr/bin/python3 -B
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""GTK desktop console for the Windows Dock Ethernet link."""

from datetime import datetime
import json
from pathlib import Path, PureWindowsPath
import sys
from threading import Event, Thread
from uuid import uuid4

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango

import node_catalog
import windows_ssh as windows
from remote_desktop import share_input, show_windows


STYLE = b"""
window.dock, .dock viewport, .dock scrolledwindow { background-color: #0a111a; color: #e8f2f8; }
.dock label { color: #e8f2f8; }
.dock button {
    min-height: 29px;
    padding: 3px 12px;
    border-radius: 6px;
    border: 1px solid #2b4051;
    background-image: none;
    background-color: #152333;
    color: #e8f2f8;
    text-shadow: none;
    box-shadow: none;
}
.dock button:hover { background-color: #203446; border-color: #476376; }
.dock button.suggested-action { background-color: #56e0df; border-color: #56e0df; color: #07222a; }
.dock button.suggested-action:hover { background-color: #8ceceb; border-color: #8ceceb; }
.dock button.stop-action { background-color: #48232c; border-color: #bb6674; color: #ffc3c8; }
.dock button.stop-action:hover { background-color: #612e39; }
.dock button:disabled { background-color: #111a25; border-color: #223141; color: #8193a5; }
.dock button label { color: inherit; }
.dock button:focus, .dock entry:focus {
    outline: 2px solid #56e0df;
    outline-offset: -3px;
}
.dock entry {
    min-height: 28px;
    padding: 3px 9px;
    border-radius: 5px;
    border: 1px solid #2b4051;
    background: #0b1520;
    color: #e8f2f8;
    caret-color: #56e0df;
    box-shadow: none;
}
.dock entry:focus { border-color: #56e0df; }
.dock entry placeholder { color: #9aafc2; }
.dock .hero {
    background: #0e1926;
    padding: 18px 24px;
    border-bottom: 1px solid #26394a;
    box-shadow: inset 3px 0 #56e0df;
}
.dock .hero-title { font-size: 27px; font-weight: bold; }
.dock .hero-meta { color: #9eb5c7; font-family: monospace; font-size: 11px; }
.dock .connection {
    color: #f6d295; background: #30271c; border: 1px solid #5c4931;
    border-radius: 6px; padding: 6px 9px;
    font-family: monospace; font-size: 11px; font-weight: bold;
}
.dock .connection.online { color: #82edb4; background: #173329; border-color: #306450; }
.dock .navigation button {
    border-radius: 0;
    border-color: transparent;
    border-bottom: 2px solid transparent;
    background-color: #0a111a;
    color: #a2b7c9;
}
.dock .navigation button:hover { background-color: #142333; color: #e8f2f8; }
.dock .navigation button:checked {
    border-bottom-color: #56e0df;
    background-color: #101e2b;
    color: #56e0df;
    font-weight: bold;
}
.dock .section-title { font-size: 20px; font-weight: bold; }
.dock .card {
    padding: 14px 16px;
    border-radius: 8px;
    background: #132131;
    border: 1px solid #2b4051;
}
.dock .card-title { color: #c0d3e4; font-weight: bold; }
.dock .metric { font-size: 21px; font-weight: bold; }
.dock .muted { color: #a2b7c9; }
.dock .session-online { color: #82edb4; }
.dock .session-waiting { color: #f6d295; }
.dock .footer {
    background-color: #0e1926; color: #a2b7c9; padding: 9px 24px;
    border-top: 1px solid #26394a;
}
.dock .footer.failed { color: #ffc3c8; }
.dock treeview { background-color: #0b1520; color: #e8f2f8; }
.dock treeview:selected { background-color: #203446; color: #e8f2f8; }
.dock .console text { background: #0b1520; color: #e8f2f8; }
.dock .console text selection, .dock entry selection { background: #56e0df; color: #07222a; }
.dock progressbar trough { background: #243648; border-color: #243648; }
.dock progressbar progress { background: #56e0df; border-color: #56e0df; }
"""


def label(text="", style=None):
    widget = Gtk.Label(label=text, xalign=0)
    if style:
        widget.get_style_context().add_class(style)
    return widget


def vertical(spacing=8):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def add(box, widget, expand=False):
    box.pack_start(widget, expand, expand, 0)


def card(title):
    panel = vertical(9)
    panel.get_style_context().add_class("card")
    add(panel, label(title, "card-title"))
    return panel


def action_button(text, handler, suggested=False):
    button = Gtk.Button(label=text)
    button.connect("clicked", handler)
    if suggested:
        button.get_style_context().add_class("suggested-action")
    return button


def gib(value):
    return f"{value / (1024 ** 3):.1f}".replace(".", ",") + " GiB"


def uptime(seconds):
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return (f"{days} g " if days else "") + f"{hours} h {minutes} min"


class NodeEditor(Gtk.Window):
    def __init__(self, owner, node=None):
        super().__init__(title="Modifica nodo" if node else "Aggiungi nodo")
        self.owner = owner
        self.node = node
        snapshot = owner.nodes_snapshot
        registry = snapshot["registry"]
        self.local = registry is None or bool(node and node["id"] == registry["local"])
        self.revision = snapshot["revision"]
        self.saving = False
        self.fields = {}
        self.get_style_context().add_class("dock")
        self.set_transient_for(owner)
        self.set_destroy_with_parent(True)
        self.set_default_size(590, 650)
        self.connect("delete-event", lambda *_args: self.saving)
        self.connect("destroy", lambda *_args: setattr(owner, "node_editor", None))
        body = vertical(12)
        body.set_border_width(18)
        self.add(body)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.form = Gtk.Grid(column_spacing=14, row_spacing=9)
        scroll.add(self.form)
        add(body, scroll, True)

        def field(key, title, value="", editable=True):
            row = len(self.fields)
            self.form.attach(label(title), 0, row, 1, 1)
            entry = Gtk.Entry(text=str(value))
            entry.set_hexpand(True)
            entry.set_editable(editable)
            self.form.attach(entry, 1, row, 1, 1)
            self.fields[key] = entry
            return entry

        current = node or {}
        suggested_id = str(uuid4()) if self.local else ""
        field("id", "UUID del computer", current.get("id", suggested_id), node is None)
        field("label", "Nome", current.get("label", ""))
        row = len(self.fields)
        self.form.attach(label("Sistema"), 0, row, 1, 1)
        self.family = Gtk.ComboBoxText()
        self.family.append("linux", "Linux")
        self.family.append("windows", "Windows")
        self.family.set_active_id(current.get("family", "linux"))
        self.family.set_sensitive(not self.local)
        self.form.attach(self.family, 1, row, 1, 1)
        # Keep subsequent form rows after the two selector rows.
        self.fields["family"] = self.family
        row += 1
        self.form.attach(label("Collegamento"), 0, row, 1, 1)
        self.route = Gtk.ComboBoxText()
        if self.local:
            self.route.append("local", "Questo computer")
            mode = "local"
        else:
            self.route.append("none", "Da configurare")
            self.route.append("ssh", "SSH esplicito")
            if current.get("legacy_route"):
                self.route.append("legacy", "Profilo SSH esistente")
            mode = "none"
            if current.get("legacy_route"):
                mode = "legacy"
            if "ssh" in current:
                mode = "ssh"
        self.route.set_active_id(mode)
        self.form.attach(self.route, 1, row, 1, 1)
        self.fields["route"] = self.route
        ssh = current.get("ssh", {})
        field("host", "Indirizzo SSH", ssh.get("host", ""))
        field("port", "Porta SSH", ssh.get("port", 22))
        field("user", "Utente SSH", ssh.get("user", ""))
        field("identity_file", "Chiave SSH su questo PC", ssh.get("identity_file", ""))
        field("identity_agent", "Socket agent SSH locale (facoltativo)", ssh.get("identity_agent", ""))
        field("bind_address", "Indirizzo locale di partenza (facoltativo)", ssh.get("bind_address", ""))
        field("known_hosts_file", "known_hosts su questo PC", ssh.get("known_hosts_file", ""))
        field("network", "Rete", ssh.get("network", ""))
        self.fields["id"].set_tooltip_text("Per un computer remoto usa il suo UUID Vincula verificato.")
        if not self.local and node is None:
            self.fields["id"].set_placeholder_text("UUID verificato del computer remoto")
        self.fields["identity_file"].set_tooltip_text("Percorso assoluto dell’identità su questo PC; con un socket agent usa il selettore pubblico .pub.")
        self.fields["known_hosts_file"].set_tooltip_text("Percorso assoluto del file con la chiave host verificata.")
        self.route.connect("changed", lambda *_args: self.update_route())
        self.update_route()
        self.status = label("Salva identità e percorso SSH; accesso e capacità restano da verificare.", "muted")
        self.status.set_line_wrap(True)
        add(body, self.status)
        actions = Gtk.Box(spacing=9)
        add(actions, action_button("Chiudi", lambda *_args: self.close()))
        self.save_button = action_button("Salva nodo", self.save, True)
        add(actions, self.save_button)
        add(body, actions)

    def update_route(self):
        explicit = self.route.get_active_id() == "ssh"
        for key in ("host", "port", "user", "identity_file", "identity_agent", "bind_address", "known_hosts_file", "network"):
            self.fields[key].set_sensitive(explicit)

    def ssh_fields(self):
        ssh = dict(self.node.get("ssh", {})) if self.node else {}
        ssh.update({key: self.fields[key].get_text().strip()
                    for key in ("host", "user", "identity_file", "known_hosts_file")})
        ssh["port"] = int(self.fields["port"].get_text())
        for key in ("network", "identity_agent", "bind_address"):
            value = self.fields[key].get_text().strip()
            if value:
                ssh[key] = value
            else:
                ssh.pop(key, None)
        return ssh

    def request_payload(self):
        changes = {"label": self.fields["label"].get_text().strip(), "family": self.family.get_active_id()}
        mode = self.route.get_active_id()
        if mode == "ssh":
            changes.update(ssh=self.ssh_fields(), legacy_route=None)
        elif mode == "none":
            changes.update(ssh=None, legacy_route=None)
        if self.node:
            payload = {"action": "update", "node_id": self.node["id"], "changes": changes}
        else:
            legacy = self.local and windows.IDENTITY.is_file() and windows.HOST_KEY.is_file()
            node = {"id": self.fields["id"].get_text().strip(), "aliases": ["ubuntu"] if legacy else [], **changes}
            node = {key: value for key, value in node.items() if value is not None}
            payload = {"action": "add", "node": node, "local": self.local}
        payload["expected_revision"] = self.revision
        return payload

    def save(self, *_args):
        if self.owner.nodes_busy:
            self.status.set_text("Attendi il completamento dell’operazione sul catalogo.")
            return
        try:
            payload = self.request_payload()
        except ValueError:
            self.status.set_text("La porta SSH deve essere un numero intero.")
            return
        self.saving = True
        self.form.set_sensitive(False)
        self.save_button.set_sensitive(False)
        self.status.set_text("Salvataggio del catalogo…")
        self.owner.catalog_request(payload, self.saved, self.failed)

    def saved(self, _reply):
        self.saving = False
        self.close()

    def failed(self, message):
        self.saving = False
        self.form.set_sensitive(True)
        self.save_button.set_sensitive(True)
        self.status.set_text(message)


class NodeCheckEditor(Gtk.Window):
    CHECKS = (("ssh", "SSH autenticato"), ("identity", "UUID persistente"), ("helper", "Helper su disco"),
              ("policy", "Permessi osservati"), ("worker", "Worker autonomo"), ("desktop", "Desktop"))

    def __init__(self, owner, node):
        super().__init__(title="Verifica nodo")
        self.owner, self.node_id = owner, node["id"]
        self.alive, self.running = True, False
        self.request_revision = None
        self.origin_id = None
        self.values = {}
        self.get_style_context().add_class("dock")
        self.set_transient_for(owner)
        self.set_destroy_with_parent(True)
        self.set_default_size(680, 610)
        self.connect("destroy", self.closed)
        body = vertical(12)
        body.set_border_width(18)
        self.add(body)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        content = vertical(12)
        scroll.add(content)
        add(body, scroll, True)
        identity = label(f'{node["label"]}\nUUID {self.node_id}', "muted")
        identity.set_line_wrap(True)
        identity.set_selectable(True)
        add(content, identity)
        for key, caption in self.CHECKS:
            row = card(caption)
            value = label("Non verificato")
            value.set_line_wrap(True)
            value.set_selectable(True)
            add(row, value)
            add(content, row)
            self.values[key] = value
        self.metadata = label("Nessuna verifica eseguita.", "muted")
        self.metadata.set_line_wrap(True)
        self.metadata.set_selectable(True)
        add(content, self.metadata)
        self.message = label("La presenza dell’helper non prova l’avvio del servizio.\n"
                             "Worker senza sessione grafica e desktop richiedono collaudi separati.", "muted")
        self.message.set_line_wrap(True)
        add(body, self.message)
        actions = Gtk.Box(spacing=8)
        add(actions, action_button("Chiudi", lambda *_: self.close()))
        self.check_button = action_button("Verifica nodo", self.submit, True)
        add(actions, self.check_button)
        add(body, actions)

    def closed(self, *_):
        self.alive = False
        if self.owner.node_check_editor is self:
            self.owner.node_check_editor = None

    @staticmethod
    def description(name, check):
        state = check.get("state")
        if check.get("reason") == "termination_unconfirmed":
            return "Arresto della verifica non confermato"
        descriptions = {
            ("ssh", "authenticated"): "Autenticazione SSH riuscita",
            ("ssh", "failed"): "Autenticazione o collegamento SSH non riusciti",
            ("identity", "match"): "UUID persistente corrispondente",
            ("identity", "mismatch"): "UUID diverso: identità del nodo non confermata",
            ("identity", "ambiguous"): "Identità ambigua: verifica la configurazione del destinatario",
            ("identity", "unavailable"): "UUID persistente non disponibile",
            ("helper", "compatible_on_disk"): "Helper compatibile su disco; avvio del servizio non provato",
            ("helper", "missing"): "Helper non presente",
            ("helper", "unverified"): "Compatibilità dell’helper non verificata",
            ("worker", "unverified"): "Servizio autonomo non verificato",
            ("desktop", "unchecked"): "Consensi e sessione desktop non verificati",
        }
        if name == "ssh" and check.get("reason") == "local_node":
            return "Nodo locale: SSH non necessario"
        if name == "ssh" and check.get("reason") == "legacy_route_requires_explicit":
            return "Profilo precedente: configura una rotta SSH esplicita"
        if name == "policy" and state == "observed":
            mode = {"full": "Accesso completo", "ask": "Richiede approvazione"}.get(check.get("mode"), "Profilo non riconosciuto")
            native = check.get("native_enabled")
            execution = "abilitata" if native is True else "disabilitata" if native is False else "non verificata"
            return f"{mode}; esecuzione generale {execution}"
        return descriptions.get((name, state), "Non verificato")

    def submit(self, *_):
        if not self.alive or self.running:
            return
        if self.owner.nodes_busy:
            self.message.set_text("Attendi il completamento dell’operazione sul catalogo.")
            return
        snapshot = self.owner.nodes_snapshot
        registry = snapshot.get("registry") if snapshot else None
        if not registry or not any(node["id"] == self.node_id for node in registry["nodes"]):
            self.message.set_text("Il nodo non è più nel catalogo. Aggiorna l’elenco.")
            return
        self.request_revision, self.origin_id = snapshot["revision"], registry["local"]
        self.running = True
        self.check_button.set_sensitive(False)
        for value in self.values.values():
            value.set_text("In attesa della verifica")
        self.metadata.set_text(f"Revisione richiesta {self.request_revision[:12]}")
        self.message.set_text("Verifica in corso. Puoi chiudere questa finestra; lo stop della condivisione resta disponibile.")
        self.owner.catalog_request({"action": "enrollment_check", "node_id": self.node_id,
                                    "expected_revision": self.request_revision}, self.finished, self.failed)

    def failed(self, error):
        if not self.alive:
            return
        self.running = False
        self.check_button.set_sensitive(True)
        for value in self.values.values():
            value.set_text("Verifica non conclusa")
        if isinstance(error, str) and "75 secondi" in error:
            self.message.set_text("La verifica non ha risposto entro 75 secondi. Riprova; i permessi restano invariati.")
        elif isinstance(error, str) and ("node_probe.py" in error or "node_enrollment.py" in error):
            self.message.set_text("Aggiorna il runtime Vincula per abilitare la verifica del nodo. Il catalogo manuale resta disponibile.")
        else:
            self.message.set_text("Verifica non conclusa. Il catalogo può essere cambiato oppure il runtime richiede un aggiornamento. "
                                  "Aggiorna l’elenco e riprova; i permessi restano invariati.")

    def finished(self, reply):
        if not self.alive:
            return
        result = reply.get("node_check", {})
        if (not isinstance(result, dict) or result.get("node_id") != self.node_id or result.get("origin_id") != self.origin_id
                or result.get("revision") != self.request_revision or reply.get("revision") != self.request_revision):
            self.failed(None)
            return
        self.running = False
        self.check_button.set_sensitive(True)
        checks = result.get("checks", {})
        if not isinstance(checks, dict) or any(not isinstance(checks.get(key), dict) for key, _ in self.CHECKS):
            self.failed(None)
            return
        for key, _caption in self.CHECKS:
            self.values[key].set_text(self.description(key, checks.get(key, {})))
        try:
            checked_at = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M:%S")
        except (KeyError, TypeError, ValueError, AttributeError):
            checked_at = "Data non disponibile"
        digest = result.get("route_digest", "")
        route = digest[:12] if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else "non disponibile"
        self.metadata.set_text(f"Revisione {self.request_revision[:12]} · Rotta {route}\nVerificato il {checked_at}")
        self.message.set_text("Resoconto di questa identità e rotta. Permessi invariati.\n"
                              "Helper su disco, servizio autonomo e desktop sono verifiche distinte.")


class InvitationEditor(Gtk.Window):
    """Public enrollment documents and credentials belonging to this computer."""

    def __init__(self, owner, invitation=None):
        super().__init__(title="Importa invito" if invitation else "Esporta invito")
        self.owner, self.invitation = owner, invitation
        self.revision = owner.nodes_snapshot["revision"]
        self.saving = False
        self.fields, self.endpoints = {}, []
        self.get_style_context().add_class("dock")
        self.set_transient_for(owner)
        self.set_destroy_with_parent(True)
        self.set_default_size(680, 690)
        self.connect("delete-event", lambda *_: self.saving)
        self.connect("destroy", lambda *_: setattr(owner, "invitation_editor", None))
        body = vertical(12)
        body.set_border_width(18)
        self.add(body)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.form = vertical(10)
        scroll.add(self.form)
        add(body, scroll, True)
        registry = owner.nodes_snapshot["registry"]
        if invitation:
            node, key = invitation["node"], invitation["ssh_host_key"]
            self.note(f'{node["label"]} · {node["family"]}\nUUID {node["id"]}\n'
                      f'Impronta host SSH\n{key["fingerprint"]}')
            self.endpoint = Gtk.ComboBoxText()
            for index, endpoint in enumerate(invitation["endpoints"]):
                self.endpoint.append(str(index), f'{endpoint["user"]}@{endpoint["host"]}:{endpoint["port"]}'
                                     f' · {endpoint.get("network", "")}')
            self.endpoint.set_active(0)
            self.endpoint.get_cells()[0].set_property("ellipsize", Pango.EllipsizeMode.END)
            self.endpoint.get_cells()[0].set_property("max-width-chars", 60)
            add(self.form, label("Indirizzo da usare da questo PC"))
            add(self.form, self.endpoint)
            identities = {tuple(node.get("ssh", {}).get(key, "") for key in
                                ("identity_file", "identity_agent", "bind_address"))
                          for node in registry["nodes"] if node.get("ssh", {}).get("identity_file")}
            identity = next(iter(identities)) if len(identities) == 1 else ("", "", "")
            self.field("identity_file", "Identità SSH locale (percorso, contenuto mai letto)", identity[0], browse=True)
            self.field("identity_agent", "Socket agent locale (facoltativo)", identity[1])
            self.field("bind_address", "Indirizzo locale di partenza (facoltativo)", identity[2])
            self.note("Con un socket agent usa il selettore pubblico .pub della tua identità. "
                      "La chiave pubblica di questa origine deve essere autorizzata sul nuovo PC.")
            self.verified = Gtk.CheckButton(label="Ho confrontato l’impronta sul nuovo PC")
            self.verified.get_child().set_line_wrap(True)
            self.verified.get_child().set_max_width_chars(55)
            self.verified.connect("toggled", lambda *_: self.submit_button.set_sensitive(self.verified.get_active()))
            add(self.form, self.verified)
        else:
            local = next(node for node in registry["nodes"] if node["id"] == registry["local"])
            self.note(f'Invito pubblico di {local["label"]}\nUUID persistente {local["id"]}\n'
                      "Indica gli indirizzi raggiungibili e la chiave host pubblica del listener SSH effettivo.")
            self.endpoint_box = vertical(12)
            add(self.form, self.endpoint_box)
            self.add_endpoint()
            add(self.form, action_button("Aggiungi indirizzo", lambda *_: self.add_endpoint()))
            self.field("public_host_key", "Chiave host SSH pubblica (.pub)", browse=True, public=True)
            self.note("Solo dati pubblici. Non esporta credenziali, non configura SSH, worker o desktop.")
        self.message = label("La registrazione lascia SSH, runtime e desktop da verificare.", "muted")
        self.message.set_line_wrap(True)
        add(body, self.message)
        actions = Gtk.Box(spacing=9)
        add(actions, action_button("Chiudi", lambda *_: self.close()))
        self.submit_button = action_button("Registra nodo" if invitation else "Genera e salva invito", self.submit, True)
        self.submit_button.set_sensitive(invitation is None)
        add(actions, self.submit_button)
        if invitation:
            self.node_check_button = action_button("Verifica nodo", lambda *_: owner.edit_node_check(invitation["node"]["id"]))
            self.node_check_button.set_sensitive(False)
            add(actions, self.node_check_button)
        add(body, actions)

    def note(self, text):
        widget = label(text, "muted")
        widget.set_line_wrap(True)
        widget.set_selectable(True)
        add(self.form, widget)

    def field(self, key, caption, value="", *, browse=False, public=False):
        add(self.form, label(caption))
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(text=value)
        add(row, entry, True)
        if browse:
            def choose(*_):
                dialog = Gtk.FileChooserNative.new(caption, self, Gtk.FileChooserAction.OPEN, "Seleziona", "Annulla")
                if public:
                    file_filter = Gtk.FileFilter()
                    file_filter.set_name("Chiavi pubbliche SSH (*.pub)")
                    file_filter.add_pattern("*.pub")
                    dialog.add_filter(file_filter)
                if dialog.run() == Gtk.ResponseType.ACCEPT:
                    entry.set_text(dialog.get_filename())
                dialog.destroy()
            add(row, action_button("Sfoglia…", choose))
        add(self.form, row)
        self.fields[key] = entry

    def add_endpoint(self):
        group = card(f"Indirizzo {len(self.endpoints) + 1}")
        grid = Gtk.Grid(column_spacing=10, row_spacing=7)
        fields = {}
        for index, (key, caption, default) in enumerate((("host", "Host / IP", ""), ("user", "Utente SSH", ""),
                                                       ("port", "Porta", "22"), ("network", "Rete", ""))):
            grid.attach(label(caption), 0, index, 1, 1)
            entry = Gtk.Entry(text=default)
            entry.set_hexpand(True)
            grid.attach(entry, 1, index, 1, 1)
            fields[key] = entry
        add(group, grid)
        add(self.endpoint_box, group)
        self.endpoints.append(fields)
        group.show_all()

    def submit(self, *_):
        if self.saving or self.owner.nodes_busy:
            return
        try:
            if self.invitation:
                if not self.verified.get_active():
                    return
                connection = {key: entry.get_text().strip() for key, entry in self.fields.items() if entry.get_text().strip()}
                if not connection.get("identity_file"):
                    raise ValueError("Indica il percorso dell’identità SSH locale.")
                payload = {"action": "enrollment_import", "invitation": self.invitation,
                           "expected_revision": self.revision, "expected_fingerprint": self.invitation["ssh_host_key"]["fingerprint"],
                           "endpoint_index": self.endpoint.get_active(), "local_connection": connection}
            else:
                endpoints = []
                for fields in self.endpoints:
                    endpoint = {key: entry.get_text().strip() for key, entry in fields.items()}
                    endpoint["port"] = int(endpoint["port"])
                    endpoints.append(endpoint)
                public_key = node_catalog.read_public_text(self.fields["public_host_key"].get_text(), 8192, host_key=True)
                payload = {"action": "enrollment_export", "endpoints": endpoints, "public_host_key": public_key}
        except (OSError, ValueError, node_catalog.CatalogError) as error:
            self.message.set_text(str(error))
            return
        self.saving = True
        self.form.set_sensitive(False)
        self.submit_button.set_sensitive(False)
        self.message.set_text("Validazione dell’invito…")
        self.owner.catalog_request(payload, self.finished, self.failed)

    def failed(self, error):
        self.saving = False
        self.form.set_sensitive(True)
        self.submit_button.set_sensitive(not self.invitation or self.verified.get_active())
        self.message.set_text(error)

    def finished(self, reply):
        self.saving = False
        if self.invitation:
            steps = reply.get("next_steps", [])
            details = "\n".join(str(step) for step in steps) if isinstance(steps, list) else str(steps)
            self.message.set_text("Nodo registrato. SSH, runtime e desktop NON verificati.\n" + details)
            self.owner.nodes_status.set_text("Nodo registrato; collegamento, runtime e desktop da verificare.")
            self.node_check_button.set_sensitive(True)
            return
        dialog = Gtk.FileChooserNative.new("Salva invito pubblico", self, Gtk.FileChooserAction.SAVE, "Salva", "Annulla")
        dialog.set_current_name("vincula-invito.json")
        dialog.set_do_overwrite_confirmation(True)
        try:
            if dialog.run() == Gtk.ResponseType.ACCEPT:
                Path(dialog.get_filename()).write_text(json.dumps(reply["invitation"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.message.set_text("Invito pubblico salvato. Confronta l’impronta sul PC che lo importerà.")
            else:
                self.message.set_text("Invito validato; salvataggio annullato.")
        except OSError as error:
            self.message.set_text("Impossibile salvare l’invito: " + str(error))
        finally:
            dialog.destroy()
            self.form.set_sensitive(True)
            self.submit_button.set_sensitive(True)


class DesktopEditor(Gtk.Window):
    def __init__(self, owner):
        super().__init__(title="Layout desktop — anteprima")
        self.owner = owner
        registry = owner.nodes_snapshot["registry"]
        self.revision = owner.nodes_snapshot["revision"]
        self.saving = False
        self.members = []
        self.get_style_context().add_class("dock")
        self.set_transient_for(owner)
        self.set_destroy_with_parent(True)
        self.set_default_size(760, 680)
        self.connect("delete-event", lambda *_args: self.saving)
        self.connect("destroy", lambda *_args: setattr(owner, "desktop_editor", None))
        body = vertical(10)
        body.set_border_width(18)
        self.add(body)
        guide = label("Scegli il PC con mouse e tastiera. Celle adiacenti: X verso destra, Y verso il basso.\n"
                      "Ogni PC rappresenta il suo desktop logico. Il salvataggio non avvia la condivisione.", "muted")
        guide.set_line_wrap(True)
        add(body, guide)
        profile = registry.get("desktop") or {"primary": registry["local"], "clipboard": True,
                                             "members": [{"node_id": registry["local"], "x": 0, "y": 0}]}
        positions = {member["node_id"]: member for member in profile["members"]}
        self.primary = Gtk.ComboBoxText()
        for node in registry["nodes"]:
            self.primary.append(node["id"], node["label"] + " · " + node["id"][:8])
        self.primary.set_active_id(profile["primary"])
        add(body, label("Principale (mouse e tastiera collegati qui)"))
        add(body, self.primary)
        self.clipboard = Gtk.CheckButton(label="Condividi gli appunti tra i membri")
        self.clipboard.set_active(profile["clipboard"])
        add(body, self.clipboard)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        for column, caption in enumerate(("Membro", "X", "Y")):
            grid.attach(label(caption, "muted"), column, 0, 1, 1)
        for row, node in enumerate(registry["nodes"], 1):
            position = positions.get(node["id"])
            selected = Gtk.CheckButton(label=node["label"] + " · " + node["id"][:8])
            selected.set_active(position is not None)
            grid.attach(selected, 0, row, 1, 1)
            coordinates = []
            for column, axis in enumerate(("x", "y"), 1):
                entry = Gtk.Entry(text=str(position[axis] if position else 0), width_chars=6)
                entry.set_hexpand(False)
                grid.attach(entry, column, row, 1, 1)
                coordinates.append(entry)
            self.members.append((node["id"], selected, *coordinates))
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(130)
        scroll.add(grid)
        add(body, scroll, True)
        self.preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        result_scroll = Gtk.ScrolledWindow()
        result_scroll.set_min_content_height(180)
        result_scroll.add(self.preview)
        add(body, result_scroll, True)
        self.message = label("Anteprima e controlli locali; accesso remoto e TLS restano da verificare.", "muted")
        self.message.set_line_wrap(True)
        add(body, self.message)
        actions = Gtk.Box(spacing=8)
        self.buttons = [action_button("Anteprima", lambda *_: self.submit("desktop_preview"), True),
                        action_button("Salva layout", lambda *_: self.submit("desktop")),
                        action_button("Rimuovi layout", lambda *_: self.submit("desktop", clear=True)),
                        action_button("Chiudi", lambda *_: self.destroy())]
        for button in self.buttons:
            add(actions, button)
        add(body, actions)

    def profile(self):
        members = []
        for node_id, selected, x, y in self.members:
            if selected.get_active():
                try:
                    members.append({"node_id": node_id, "x": int(x.get_text()), "y": int(y.get_text())})
                except ValueError:
                    raise ValueError("Le coordinate X e Y devono essere numeri interi.") from None
        return {"primary": self.primary.get_active_id(), "members": members, "clipboard": self.clipboard.get_active()}

    def finished(self, reply=None, error=None):
        self.saving = False
        for button in self.buttons:
            button.set_sensitive(True)
        if error:
            self.message.set_text(error)
        elif reply and "desktop_preview" in reply:
            result = reply["desktop_preview"]
            lines = [result["preflight"]["summary"], ""]
            nodes = {node["id"]: node["label"] for node in reply["registry"]["nodes"]}
            for check in result["preflight"]["checks"]:
                lines.append(f'[{check["status"]}] {nodes.get(check["node_id"], "Vincula")}: {check["message"]}')
            lines.extend(("", "LAYOUT DESKFLOW", result["layout"], "OPZIONI PREVISTE (non avviabili)",
                          json.dumps(result["server_options"], ensure_ascii=False, indent=2)))
            self.preview.get_buffer().set_text("\n".join(lines))
            self.message.set_text("Layout valido. Condivisione multinodo non attiva: consulta i prerequisiti sopra.")
        else:
            self.destroy()

    def submit(self, action, clear=False):
        if self.saving or self.owner.nodes_busy:
            return
        try:
            profile = None if clear else self.profile()
        except ValueError as error:
            self.message.set_text(str(error))
            return
        self.saving = True
        for button in self.buttons:
            button.set_sensitive(False)
        self.owner.catalog_request({"action": action, "desktop": profile, "expected_revision": self.revision},
                                   success=lambda reply: self.finished(reply=reply), failure=lambda error: self.finished(error=error))


class DockWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Windows Dock")
        self.get_style_context().add_class("dock")
        self.set_default_size(860, 730)
        self.set_size_request(660, 510)
        self.connect("delete-event", self.on_close)
        self.busy = False
        self.close_pending = False
        self.remote_stop = None
        self.remote_mode = None
        self.nodes_busy = False
        self.nodes_loaded = False
        self.nodes_snapshot = None
        self.node_editor = None
        self.desktop_editor = None
        self.invitation_editor = None
        self.node_check_editor = None
        try:
            self.set_icon_from_file(str(Path(__file__).with_name("windows-dock-icon.png")))
        except GLib.Error:
            self.set_icon_name("computer")
        self.profile = PureWindowsPath(r"C:\Users\Admin")
        self.suggested_remote = ""

        root = vertical(0)
        self.add(root)
        add(root, self.build_header())

        self.content = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        tabs = Gtk.StackSwitcher(stack=self.content, halign=Gtk.Align.START)
        tabs.get_style_context().add_class("navigation")
        tabs.set_margin_start(24)
        tabs.set_margin_end(24)
        tabs.set_margin_top(14)
        add(root, tabs)

        self.content.add_titled(self.status_page(), "status", "Stato")
        self.content.add_titled(self.transfer_page(), "files", "Trasferimenti")
        self.content.add_titled(self.script_page(), "scripts", "PowerShell")
        self.content.add_titled(self.input_page(), "input", "Mouse e tastiera")
        self.content.add_titled(self.nodes_page(), "nodes", "Nodi")
        self.content.connect("notify::visible-child-name", self.catalog_tab_changed)
        add(root, self.content, True)

        self.footer = label("Pronto", "footer")
        self.footer.set_selectable(True)
        self.footer.set_line_wrap(True)
        add(root, self.footer)

    def nodes_page(self):
        page = self.page("Catalogo dei computer")
        self.nodes_status = label("Aggiorna il catalogo per leggere i computer registrati.", "muted")
        self.nodes_status.set_line_wrap(True)
        add(page, self.nodes_status)
        self.node_store = Gtk.ListStore(str, str, str, str, object)
        self.node_list = Gtk.TreeView(model=self.node_store)
        self.node_list.set_headers_visible(True)
        self.node_list.set_tooltip_column(2)
        for index, title in enumerate(("Computer", "Sistema", "UUID", "Collegamento")):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            column.set_fixed_width((170, 80, 180, 145)[index])
            column.set_min_width((100, 70, 120, 135)[index])
            column.set_expand(index in (0, 2))
            self.node_list.append_column(column)
        self.node_list.get_selection().connect("changed", lambda *_args: self.catalog_buttons())
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(170)
        scroll.add(self.node_list)
        add(page, scroll, True)
        actions = Gtk.Box(spacing=8)
        self.node_add = action_button("Aggiungi", lambda *_args: self.edit_node(), True)
        self.node_edit = action_button("Modifica", lambda *_args: self.edit_node(self.selected_node()))
        self.node_remove = action_button("Rimuovi", self.remove_node)
        self.node_refresh = action_button("Aggiorna", lambda *_args: self.catalog_request({"action": "list"}))
        for button in (self.node_add, self.node_edit, self.node_remove, self.node_refresh):
            add(actions, button)
        add(page, actions)
        invitations = Gtk.Box(spacing=8)
        self.node_import = action_button("Importa invito", self.import_invitation)
        self.node_export = action_button("Esporta invito", lambda *_: self.edit_invitation())
        self.node_check = action_button("Verifica nodo", lambda *_: self.edit_node_check())
        add(invitations, self.node_import)
        add(invitations, self.node_export)
        add(invitations, self.node_check)
        add(page, invitations)
        self.node_desktop = action_button("Layout desktop e prerequisiti", lambda *_: self.edit_desktop())
        add(page, self.node_desktop)
        guide = label("Catalogo e layout: le altre schede usano ancora il profilo a due PC.", "muted")
        guide.set_line_wrap(True)
        add(page, guide)
        self.catalog_buttons()
        return page

    def catalog_tab_changed(self, *_args):
        if self.content.get_visible_child_name() == "nodes" and not self.nodes_loaded and not self.nodes_busy:
            self.catalog_request({"action": "list"})

    def selected_node(self):
        model, iterator = self.node_list.get_selection().get_selected()
        return model[iterator][4] if iterator is not None else None

    def catalog_buttons(self):
        selected = self.selected_node()
        available = self.nodes_snapshot is not None and not self.nodes_busy
        self.node_add.set_sensitive(available)
        self.node_edit.set_sensitive(available and selected is not None)
        registry = self.nodes_snapshot["registry"] if self.nodes_snapshot else None
        self.node_remove.set_sensitive(available and selected is not None and registry is not None
                                       and selected["id"] != registry["local"])
        self.node_refresh.set_sensitive(not self.nodes_busy)
        self.node_list.set_sensitive(not self.nodes_busy)
        self.node_desktop.set_sensitive(available and registry is not None)
        self.node_import.set_sensitive(available and registry is not None)
        self.node_export.set_sensitive(available and registry is not None)
        self.node_check.set_sensitive(available and registry is not None and selected is not None)
        self.node_add.set_label("Registra questo PC" if available and registry is None else "Aggiungi")

    def show_catalog(self, reply):
        previous = self.selected_node()
        self.nodes_snapshot = reply
        self.nodes_loaded = True
        self.node_store.clear()
        registry = reply["registry"]
        if registry is None:
            self.nodes_status.set_text("Profilo a due PC. Registra questo computer per creare il catalogo.")
            return
        for node in registry["nodes"]:
            local = node["id"] == registry["local"]
            network = "Locale" if local else node.get("ssh", {}).get("network", "")
            if not network:
                network = "Profilo SSH esistente" if node.get("legacy_route") else "SSH" if node.get("ssh") else "Da configurare"
            iterator = self.node_store.append([node["label"],
                                               "Windows" if node["family"] == "windows" else "Linux",
                                               node["id"], network, node])
            if previous and previous["id"] == node["id"]:
                self.node_list.get_selection().select_iter(iterator)
        self.nodes_status.set_text(f"{len(registry['nodes'])} computer nel catalogo.")

    def catalog_request(self, payload, success=None, failure=None):
        if self.nodes_busy:
            return
        self.nodes_busy = True
        self.nodes_status.set_text("Aggiornamento del catalogo…")
        self.catalog_buttons()

        def work():
            reply, error = None, None
            try:
                reply = node_catalog.request(payload)
            except Exception as problem:
                error = str(problem)
            GLib.idle_add(self.catalog_finished, reply, error, success, failure)

        Thread(target=work).start()

    def catalog_finished(self, reply, error, success, failure):
        self.nodes_busy = False
        if error:
            self.nodes_status.set_text(error)
            if failure:
                failure(error)
        else:
            if "registry" in reply:
                self.show_catalog(reply)
            else:
                self.nodes_status.set_text("Invito pubblico validato.")
            if success:
                success(reply)
        self.catalog_buttons()
        self.finish_close()
        return False

    def edit_node(self, node=None):
        if self.nodes_snapshot is None or self.nodes_busy:
            return
        if self.node_editor is not None:
            self.node_editor.present()
            return
        self.node_editor = NodeEditor(self, node)
        self.node_editor.show_all()

    def import_invitation(self, *_):
        if self.nodes_busy or not self.nodes_snapshot or not self.nodes_snapshot["registry"]:
            return
        if self.invitation_editor:
            self.invitation_editor.present()
            return
        dialog = Gtk.FileChooserNative.new("Apri invito pubblico Vincula", self, Gtk.FileChooserAction.OPEN, "Apri", "Annulla")
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Inviti JSON (*.json)")
        file_filter.add_pattern("*.json")
        dialog.add_filter(file_filter)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if path:
            try:
                raw = node_catalog.read_public_text(path, 32768)
            except (OSError, node_catalog.CatalogError) as error:
                self.nodes_status.set_text(str(error))
                return
            self.catalog_request({"action": "enrollment_preview", "invitation_text": raw},
                                 lambda reply: self.edit_invitation(reply["invitation"]))

    def edit_invitation(self, invitation=None):
        if self.nodes_busy or not self.nodes_snapshot or not self.nodes_snapshot["registry"]:
            return
        if self.invitation_editor:
            self.invitation_editor.present()
            return
        self.invitation_editor = InvitationEditor(self, invitation)
        self.invitation_editor.show_all()

    def edit_node_check(self, node_id=None):
        if self.nodes_busy or not self.nodes_snapshot or not self.nodes_snapshot["registry"]:
            return
        registry = self.nodes_snapshot["registry"]
        node = next((item for item in registry["nodes"] if item["id"] == node_id), None) if node_id else self.selected_node()
        if node is None:
            self.nodes_status.set_text("Seleziona un computer con UUID persistente nel catalogo.")
            return
        if self.node_check_editor:
            if self.node_check_editor.node_id == node["id"]:
                self.node_check_editor.present()
                self.node_check_editor.submit()
                return
            self.node_check_editor.destroy()
        self.node_check_editor = NodeCheckEditor(self, node)
        self.node_check_editor.show_all()
        self.node_check_editor.submit()

    def edit_desktop(self):
        if self.nodes_busy or self.nodes_snapshot is None or self.nodes_snapshot["registry"] is None:
            return
        if self.desktop_editor is not None:
            self.desktop_editor.present()
            return
        self.desktop_editor = DesktopEditor(self)
        self.desktop_editor.show_all()

    def remove_node(self, *_args):
        node = self.selected_node()
        if node is not None and self.nodes_snapshot is not None:
            self.catalog_request({"action": "remove", "node_id": node["id"],
                                  "expected_revision": self.nodes_snapshot["revision"]})


    def build_header(self):
        header = Gtk.Box(spacing=14)
        header.get_style_context().add_class("hero")
        try:
            path = Path(__file__).with_name("windows-dock-icon.png")
            logo = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 48, 48, True))
        except GLib.Error:
            logo = Gtk.Image.new_from_icon_name("computer", Gtk.IconSize.DIALOG)
        logo.set_valign(Gtk.Align.CENTER)
        add(header, logo)
        titles = vertical(3)
        add(titles, label("Vincula", "hero-title"))
        self.host_line = label(f"Windows Dock  ·  {windows.HOST}", "hero-meta")
        self.host_line.set_ellipsize(Pango.EllipsizeMode.END)
        add(titles, self.host_line)
        add(header, titles, True)
        self.connection = label("SSH da verificare", "connection")
        self.connection.set_valign(Gtk.Align.CENTER)
        add(header, self.connection)
        self.desktop_button = action_button("Desktop remoto", self.toggle_desktop)
        self.desktop_button.set_valign(Gtk.Align.CENTER)
        add(header, self.desktop_button)
        return header

    def page(self, title, button=None):
        outer = vertical(14)
        outer.set_border_width(24)
        heading = Gtk.Box(spacing=10)
        add(heading, label(title, "section-title"), True)
        if button is not None:
            add(heading, button)
        add(outer, heading)
        return outer

    def status_page(self):
        refresh = action_button("Aggiorna", lambda _button: self.refresh(), True)
        page = self.page("Stato di Windows", refresh)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.status_body = vertical(12)
        scroll.add(self.status_body)
        add(page, scroll, True)
        add(self.status_body, label("In attesa dei dati del PC Windows.", "muted"))
        return page

    def refresh(self):
        self.start("Lettura dello stato di Windows…", windows.status, self.show_status,
                   failure=self.show_status_failure)

    def show_status_failure(self):
        self.connection.set_label("SSH non verificato")
        self.connection.get_style_context().remove_class("online")

    def show_status(self, report):
        self.profile = PureWindowsPath(report["profile"])
        self.host_line.set_text(f"Windows Dock  ·  {windows.HOST}")
        self.host_line.set_tooltip_text(f"{report['host'].lower()} · {report['user']}")
        self.connection.set_label("SSH attivo")
        self.connection.get_style_context().add_class("online")
        for child in self.status_body.get_children():
            child.destroy()

        overview = card("Sistema")
        add(overview, label(report["os"], "metric"))
        add(overview, label(f"Build {report['build']}  ·  Acceso da {uptime(report['uptime'])}", "muted"))
        add(self.status_body, overview)

        row = Gtk.Box(spacing=12, homogeneous=True)
        cpu = card("Processore")
        add(cpu, label(f"{report['cpu']:.0f} %" if report["cpu"] is not None else "—", "metric"))
        add(row, cpu, True)

        memory = card("Memoria")
        used = report["ram_total"] - report["ram_free"]
        add(memory, label(f"{gib(used)} / {gib(report['ram_total'])}", "metric"))
        add(memory, self.meter(used, report["ram_total"]))
        add(row, memory, True)
        add(self.status_body, row)

        disks = card("Unità Windows")
        for disk in report["disks"]:
            total, free = disk["total"], disk["free"]
            if not total or free is None:
                add(disks, label(f"{disk['drive']}  ·  Spazio non disponibile", "muted"))
                continue
            add(disks, label(f"{disk['drive']}  ·  {gib(free)} liberi di {gib(total)}", "muted"))
            add(disks, self.meter(total - free, total))
        if not report["disks"]:
            add(disks, label("Nessuna unità locale rilevata.", "muted"))
        add(self.status_body, disks)
        self.status_body.show_all()
        return f"Aggiornato alle {datetime.now():%H:%M:%S}"

    @staticmethod
    def meter(used, total):
        meter = Gtk.ProgressBar()
        meter.set_fraction(min(1, max(0, used / total)) if total else 0)
        return meter

    def field(self, panel, title, placeholder, browse=None):
        caption = label(title)
        add(panel, caption)
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(placeholder_text=placeholder)
        caption.set_mnemonic_widget(entry)
        add(row, entry, True)
        if browse:
            add(row, action_button("Sfoglia…", lambda _button: browse(entry)))
        add(panel, row)
        return entry

    def transfer_page(self):
        page = self.page("Trasferisci file")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(spacing=14, homogeneous=True, valign=Gtk.Align.START)
        scroll.add(body)
        add(page, scroll, True)

        send = card("Linux → Windows")
        self.upload_local = self.field(send, "File su Linux", "/home/utente/documento.txt",
                                       self.choose_upload)
        self.upload_remote = self.field(send, "Destinazione su Windows", r"C:\Users\Admin\documento.txt")
        send_action = action_button("Invia file", lambda _button: self.transfer(True), True)
        send_action.set_halign(Gtk.Align.END)
        add(send, send_action)
        add(body, send, True)

        receive = card("Windows → Linux")
        self.download_remote = self.field(receive, "File su Windows", r"C:\Users\Admin\documento.txt")
        self.download_local = self.field(receive, "Destinazione su Linux", "/home/utente/documento.txt",
                                         self.choose_download)
        receive_action = action_button("Scarica file", lambda _button: self.transfer(False), True)
        receive_action.set_halign(Gtk.Align.END)
        add(receive, receive_action)
        add(body, receive, True)
        return page

    def choose_file(self, entry, *, save=False, name="", powershell=False):
        mode = Gtk.FileChooserAction.SAVE if save else Gtk.FileChooserAction.OPEN
        title = "Salva su Linux" if save else "Seleziona un file su Linux"
        dialog = Gtk.FileChooserNative.new(title, self, mode, "Seleziona", "Annulla")
        if name and save:
            dialog.set_current_name(name)
        if powershell:
            file_filter = Gtk.FileFilter()
            file_filter.set_name("Script PowerShell (.ps1)")
            file_filter.add_pattern("*.[pP][sS]1")
            dialog.add_filter(file_filter)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if path:
            entry.set_text(path)
        return path

    def choose_upload(self, entry):
        path = self.choose_file(entry)
        current = self.upload_remote.get_text()
        if path and (not current or current == self.suggested_remote):
            self.suggested_remote = str(self.profile / Path(path).name)
            self.upload_remote.set_text(self.suggested_remote)

    def choose_download(self, entry):
        filename = PureWindowsPath(self.download_remote.get_text()).name
        self.choose_file(entry, save=True, name=filename)

    def transfer(self, upload):
        if upload:
            local = self.upload_local.get_text()
            remote = self.upload_remote.get_text()
            job, description = windows.upload, "Invio verso Windows…"
            args, direction = (local, remote), "Inviato su Windows"
            destination = remote
        else:
            local = self.download_local.get_text()
            remote = self.download_remote.get_text()
            job, description = windows.download, "Download verso Linux…"
            args, direction = (remote, local), "Salvato su Linux"
            destination = local
        if not local.strip() or not remote.strip():
            self.message("Indica il file e il percorso di destinazione.", failed=True)
            return
        self.start(description, lambda: job(*args), lambda _: f"{direction} · {destination}")

    def script_page(self):
        page = self.page("Esegui script PowerShell")
        panel = card(f"Account di esecuzione su Windows · {windows.USER}")
        self.script_file = self.field(panel, "Script .ps1 su Linux", "/home/utente/script.ps1",
                                      lambda entry: self.choose_file(entry, powershell=True))
        execute = action_button("Esegui su Windows", lambda _button: self.execute(), True)
        execute.set_halign(Gtk.Align.END)
        add(panel, execute)
        add(page, panel)

        heading = Gtk.Box(spacing=8)
        add(heading, label("Risultato", "card-title"), True)
        add(heading, action_button("Copia output", self.copy_output))
        add(page, heading)
        self.output = Gtk.TextView(editable=False, monospace=True,
                                   wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.output.set_left_margin(12)
        self.output.set_right_margin(12)
        self.output.set_top_margin(10)
        self.output.get_style_context().add_class("console")
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.output)
        add(page, scroll, True)
        return page

    def copy_output(self, _button):
        buffer = self.output.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        if text:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def execute(self):
        path = self.script_file.get_text()
        if not path.strip():
            self.message("Seleziona uno script .ps1.", failed=True)
            return
        self.output.get_buffer().set_text("")
        self.start("Script in esecuzione su Windows…", lambda: windows.run_script(path),
                   self.show_script)

    def show_script(self, result):
        code, stdout, stderr = result
        parts = [f"Codice di uscita: {code}"]
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"Errori e diagnostica\n{stderr}")
        self.output.get_buffer().set_text("\n\n".join(parts))
        return f"Script terminato · codice {code}"

    def message(self, text, failed=False):
        self.footer.set_text(text)
        style = self.footer.get_style_context()
        if failed:
            style.add_class("failed")
        else:
            style.remove_class("failed")

    def start(self, description, job, success, failure=None):
        if self.busy:
            return
        self.busy = True
        self.set_job_pages_sensitive(False)
        self.message(description)

        def worker():
            try:
                value = job()
            except (OSError, windows.LinkError) as error:
                GLib.idle_add(self.finish, success, None, str(error), failure)
            else:
                GLib.idle_add(self.finish, success, value, None, failure)

        Thread(target=worker, daemon=True).start()

    def finish(self, success, value, error, failure):
        self.busy = False
        self.set_job_pages_sensitive(True)
        if error:
            if failure:
                failure()
            self.message(error, failed=True)
        else:
            self.message(success(value))
        self.finish_close()
        return False

    def set_job_pages_sensitive(self, sensitive):
        for name in ("status", "files", "scripts"):
            self.content.get_child_by_name(name).set_sensitive(sensitive)

    def input_page(self):
        page = self.page("Condividi mouse e tastiera")
        panel = card("Posizione dello schermo Windows")
        self.input_edge = Gtk.ComboBoxText()
        for direction, title in (("east", "A destra di Linux"), ("west", "A sinistra di Linux"),
                                 ("north", "Sopra Linux"), ("south", "Sotto Linux")):
            self.input_edge.append(direction, title)
        self.input_edge.set_active_id("east")
        self.input_edge.set_tooltip_text("Scegli il bordo da cui passare allo schermo Windows.")
        add(panel, self.input_edge)

        guide = label("Porta il puntatore oltre il bordo scelto. Premi Ctrl+F12 per cambiare PC.", "muted")
        guide.set_line_wrap(True)
        add(panel, guide)

        row = Gtk.Box(spacing=10)
        self.input_status = label("Disattivata", "muted")
        self.input_status.set_line_wrap(True)
        add(row, self.input_status, True)
        self.input_button = action_button("Attiva condivisione", self.toggle_input, True)
        add(row, self.input_button)
        add(panel, row)
        add(page, panel)
        return page

    def toggle_desktop(self, _button):
        if self.remote_stop is not None:
            self.stop_remote()
        else:
            self.toggle_remote("desktop", show_windows)

    def toggle_input(self, _button):
        edge = self.input_edge.get_active_id()

        def run(stop):
            share_input(stop, edge, lambda: GLib.idle_add(self.input_connected))

        self.toggle_remote("input", run)

    def input_connected(self):
        if self.remote_mode == "input" and not self.remote_stop.is_set():
            self.input_status.set_label("Condivisione attiva")
            style = self.input_status.get_style_context()
            style.remove_class("session-waiting")
            style.add_class("session-online")
        return False

    def toggle_remote(self, mode, job):
        if self.remote_stop is not None:
            if mode == self.remote_mode:
                self.stop_remote()
            return

        stop = Event()
        self.remote_stop = stop
        self.remote_mode = mode
        self.input_button.set_sensitive(mode == "input")
        self.input_edge.set_sensitive(False)
        self.desktop_button.get_style_context().add_class("stop-action")
        if mode == "desktop":
            self.desktop_button.set_label("Chiudi desktop remoto")
            self.input_status.set_label("Chiudi il desktop remoto per usare questa modalità.")
        else:
            self.input_status.set_label("Connessione via Ethernet…")
            self.input_status.get_style_context().add_class("session-waiting")
            self.input_button.get_style_context().add_class("stop-action")
            self.input_button.set_label("Disattiva condivisione")
            self.desktop_button.set_label("Disattiva condivisione")

        def worker():
            error_text = ""
            try:
                job(stop)
            except (OSError, windows.LinkError) as error:
                error_text = str(error)
            GLib.idle_add(self.finish_remote, mode, error_text)

        Thread(target=worker).start()

    def stop_remote(self):
        self.remote_stop.set()
        self.desktop_button.set_sensitive(False)
        self.input_button.set_sensitive(False)
        if self.remote_mode == "input":
            self.input_status.set_label("Chiusura…")
            style = self.input_status.get_style_context()
            style.remove_class("session-online")
            style.add_class("session-waiting")

    def finish_remote(self, mode, error):
        self.remote_stop = None
        self.remote_mode = None
        self.desktop_button.set_label("Desktop remoto")
        self.desktop_button.get_style_context().remove_class("stop-action")
        self.desktop_button.set_sensitive(True)
        self.input_button.set_label("Attiva condivisione")
        self.input_button.get_style_context().remove_class("stop-action")
        self.input_button.set_sensitive(True)
        self.input_edge.set_sensitive(True)
        self.input_status.set_label("Disattivata")
        style = self.input_status.get_style_context()
        style.remove_class("session-online")
        style.remove_class("session-waiting")
        if error and not self.close_pending:
            title = "Condivisione di mouse e tastiera" if mode == "input" else "Collegamento desktop remoto"
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                       message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE,
                                       text=title)
            dialog.format_secondary_text(error)
            dialog.run()
            dialog.destroy()
        self.finish_close()
        return False

    def on_close(self, *_args):
        if self.remote_stop is not None:
            self.stop_remote()
        if self.busy or self.nodes_busy or self.remote_stop is not None:
            self.close_pending = True
        if self.busy:
            self.message("Attendi il termine dell’operazione prima di chiudere.")
        return self.close_pending

    def finish_close(self):
        if self.close_pending and not self.busy and not self.nodes_busy and self.remote_stop is None:
            self.close_pending = False
            self.close()

class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.windowsdock.Console",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            css = Gtk.CssProvider()
            css.load_from_data(STYLE)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            window = DockWindow(self)
            window.show_all()
            window.refresh()
        window.deiconify()
        surface = window.get_window()
        if isinstance(surface, GdkX11.X11Window):
            surface.set_events(surface.get_events() | Gdk.EventMask.PROPERTY_CHANGE_MASK)
            window.present_with_time(GdkX11.x11_get_server_time(surface))
        else:
            window.present()


if __name__ == "__main__":
    sys.exit(Application().run(sys.argv))
