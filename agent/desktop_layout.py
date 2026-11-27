# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compile the UUID desktop grid and report prerequisites without opening sessions."""

import os
from pathlib import Path
import shutil
import sys


DIRECTIONS = (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1))


def compile_layout(registry):
    desktop = registry.desktop
    if desktop is None:
        raise ValueError("Configure a desktop group before previewing its layout.")
    positions = {(member.x, member.y): member.node_id for member in desktop.members}
    if len(positions) != len(desktop.members):
        raise ValueError("Ambiguous desktop layout: each grid cell must contain one node.")
    members = sorted(desktop.members, key=lambda member: member.node_id)
    links = {}
    for member in members:
        links[member.node_id] = {direction: positions[(member.x + dx, member.y + dy)]
                                for direction, dx, dy in DIRECTIONS
                                if (member.x + dx, member.y + dy) in positions}
    reached, pending = set(), [desktop.primary]
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(links[current].values())
    if len(reached) != len(members):
        missing = ", ".join(sorted(member.node_id for member in members if member.node_id not in reached))
        raise ValueError("Unreachable desktop screens: " + missing
                         + ". Use adjacent grid cells; diagonals and gaps are not links.")
    names = {member.node_id: "vincula-" + member.node_id for member in members}
    lines = ["section: links"]
    for member in members:
        lines.append(" " + names[member.node_id] + ":")
        for direction, target in links[member.node_id].items():
            lines.append("  " + direction + " = " + names[target])
    lines.extend(("end", "section: options", " keystroke(control+F12) = switchToNextScreen", "end", ""))
    return {"layout": "\n".join(lines), "names": names, "links": links,
            "primary": desktop.primary, "clipboard": desktop.clipboard,
            "server_options": {
                "core": {"computerName": names[desktop.primary], "interface": "127.0.0.1", "port": 24800},
                "server": {"externalConfig": True, "protocol": "barrier", "enableClipboard": desktop.clipboard,
                           "clipboardSize": 3 if desktop.clipboard else 0},
                "security": {"tlsEnabled": True, "checkPeerFingerprints": True},
                **{"computer_" + name: {"name": name} for name in names.values()}},
            "launchable": False}


def local_engine():
    executable = shutil.which("deskflow-core")
    candidates = [Path(executable)] if executable else []
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        candidates += sorted((base / "Linux Dock/input-engine").glob("deskflow*/deskflow-core.exe"))
        candidates += [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Deskflow/deskflow-core.exe"]
    else:
        candidates += [Path.home() / ".local/share/linux-dock/vnc/deskflow-wayland/bin/deskflow-core"]
    return next((str(path) for path in candidates if path.is_file()), None)


def preflight(registry):
    """A local inventory is not proof of remote access, TLS trust or desktop consent."""
    checks = []

    def check(node_id, code, status, message):
        checks.append({"node_id": node_id, "code": code, "status": status, "message": message})

    for member in sorted(registry.desktop.members, key=lambda value: value.node_id):
        node = registry.get(member.node_id)
        local = node.node_id == registry.local_id
        if local:
            engine = local_engine()
            check(node.node_id, "deskflow_binary", "present" if engine else "missing",
                  "Deskflow locale: " + engine if engine else "Installare Deskflow su questo computer.")
            check(node.node_id, "deskflow_compatibility", "unverified",
                  "Verificare versione e opzioni di Deskflow prima dell’avvio.")
            if sys.platform != "win32":
                session = os.environ.get("XDG_SESSION_TYPE", "").lower()
                if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
                    check(node.node_id, "desktop_consent", "unverified",
                          "Wayland: verificare InputCapture/RemoteDesktop e consenso locale; Clipboard se richiesto.")
                else:
                    check(node.node_id, "desktop_consent", "unverified",
                          "X11: verificare DISPLAY, autorizzazione X e sessione grafica interattiva.")
            else:
                check(node.node_id, "desktop_consent", "unverified",
                      "Verificare sessione Windows interattiva e accesso all’input.")
        else:
            check(node.node_id, "deskflow_binary", "unverified", "Verificare/installare Deskflow sul nodo remoto.")
            if node.route is None:
                check(node.node_id, "ssh_route", "missing", "Configurare il percorso SSH verso questo nodo.")
            elif node.route.destination == "legacy" and not node.route.required_files:
                check(node.node_id, "ssh_route", "unverified", "Profilo SSH legacy: verificare disponibilità e autenticazione nell’app.")
            else:
                configured = node.route.configured()
                check(node.node_id, "ssh_route_files", "present" if configured else "missing",
                      "File SSH presenti; autenticazione non collaudata." if configured else "Mancano file della chiave SSH o known_hosts.")
            check(node.node_id, "desktop_transport", "unverified",
                  "Provisionare chiavi desktop dedicate e forwarding limitato; SSH MCP non abilita il tunnel desktop.")
            check(node.node_id, "desktop_consent", "unverified",
                  "Verificare sessione grafica e consenso desktop sul nodo remoto.")
        check(node.node_id, "tls_identity", "unverified", "Verificare il certificato TLS Deskflow del nodo.")
        check(node.node_id, "tls_peer_trust", "unverified",
              "Verificare reciprocamente le impronte TLS tra principale e client; sono distinte dalle chiavi host SSH.")
    check(None, "session_supervisor", "missing",
          "Supervisor multinodo, lease e stop locale indipendente non ancora disponibili.")
    return {"ready": False, "active": False, "checks": checks,
            "summary": "Anteprima soltanto: nessuna sessione, connessione o modifica degli appunti è stata avviata."}


def preview(registry):
    compiled = compile_layout(registry)
    return {**compiled, "preflight": preflight(registry)}
