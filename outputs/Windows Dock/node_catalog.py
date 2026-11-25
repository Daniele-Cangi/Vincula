# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""JSON bridge from the desktop application to the local Vincula node runtime."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys


class CatalogError(Exception):
    pass


def runtime_script(enrollment=False, node_check=False):
    override = os.environ.get("VINCULA_RUNTIME")
    if override:
        roots = [Path(override)]
    else:
        app = Path(__file__).resolve().parent
        roots = [app, app.parent.parent, Path.home() / ".local/share/vincula/runtime"]
    for root in roots:
        script = root / "agent/nodes.py"
        if all((root / "agent" / name).is_file() for name in ("nodes.py", "node_registry.py", "desktop_layout.py")):
            if enrollment and not (root / "agent/node_enrollment.py").is_file():
                raise CatalogError("Aggiorna il runtime Vincula: gli inviti richiedono agent/node_enrollment.py. "
                                   "Il catalogo manuale resta disponibile.")
            if node_check and not (root / "agent/node_probe.py").is_file():
                raise CatalogError("Aggiorna il runtime Vincula: la verifica del nodo richiede agent/node_probe.py. "
                                   "Il catalogo manuale resta disponibile.")
            return script
    raise CatalogError("Runtime dei nodi incompleto. Installa nodes.py, node_registry.py e desktop_layout.py "
                       "nella cartella agent dell’app oppure imposta VINCULA_RUNTIME alla radice di Vincula.")


def request(payload):
    interpreter = os.environ.get("VINCULA_PYTHON") or sys.executable
    action = str(payload.get("action", ""))
    invitation_reply = action in ("enrollment_export", "enrollment_preview")
    timeout = 75 if action == "enrollment_check" else 15
    try:
        result = subprocess.run([interpreter, str(runtime_script(action.startswith("enrollment_"), action == "enrollment_check"))], input=json.dumps(payload),
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CatalogError(f"Il runtime non ha risposto entro {timeout} secondi. Aggiorna l’elenco per verificarne lo stato.") from None
    except OSError as error:
        raise CatalogError("Impossibile avviare il runtime dei nodi. " + str(error)) from None
    try:
        reply = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise CatalogError("Risposta del runtime non valida. Verifica Python e l’installazione di Vincula.") from None
    if not isinstance(reply, dict):
        raise CatalogError("Risposta del runtime non valida.")
    if reply.get("ok") is not True:
        error = reply.get("error")
        if not isinstance(error, dict):
            error = {}
        if error.get("code") == "conflict":
            if action == "enrollment_check":
                raise CatalogError("Il catalogo, la rotta o la chiave host sono cambiati. Aggiorna l’elenco e ripeti la verifica.")
            raise CatalogError("Il catalogo è cambiato. Aggiorna l’elenco e riapri il modulo prima di salvare.")
        raise CatalogError("Catalogo non aggiornato. " + str(error.get("message", "Errore del runtime.")))
    complete = isinstance(reply.get("invitation"), dict) if invitation_reply else "registry" in reply and "revision" in reply
    if result.returncode or not complete:
        raise CatalogError("Risposta del runtime incompleta.")
    return reply


def read_public_text(path, limit, *, host_key=False):
    """Read a bounded public document; identity file paths never pass through here."""
    source = Path(path)
    if host_key and source.suffix.lower() != ".pub":
        raise CatalogError("Seleziona esclusivamente la chiave host pubblica con estensione .pub.")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CatalogError("Seleziona un file pubblico regolare, senza collegamenti simbolici.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CatalogError(f"Il file pubblico supera il limite di {limit // 1024} KiB.")
    try:
        value = data.decode("utf-8")
    except UnicodeError:
        raise CatalogError("Il file pubblico deve essere UTF-8.") from None
    if host_key and not value.startswith("ssh-ed25519 "):
        raise CatalogError("Il file selezionato non contiene una chiave SSH pubblica.")
    return value.rstrip("\r\n") if host_key else value
