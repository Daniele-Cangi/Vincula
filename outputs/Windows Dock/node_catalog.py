# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""JSON bridge from the desktop application to the local Vincula node runtime."""

import json
import os
from pathlib import Path
import subprocess
import sys


class CatalogError(Exception):
    pass


def runtime_script():
    override = os.environ.get("VINCULA_RUNTIME")
    if override:
        roots = [Path(override)]
    else:
        app = Path(__file__).resolve().parent
        roots = [app, app.parent.parent, Path.home() / ".local/share/vincula/runtime"]
    for root in roots:
        script = root / "agent/nodes.py"
        if all((root / "agent" / name).is_file() for name in ("nodes.py", "node_registry.py", "desktop_layout.py")):
            return script
    raise CatalogError("Runtime dei nodi incompleto. Installa nodes.py, node_registry.py e desktop_layout.py "
                       "nella cartella agent dell’app oppure imposta VINCULA_RUNTIME alla radice di Vincula.")


def request(payload):
    interpreter = os.environ.get("VINCULA_PYTHON") or sys.executable
    try:
        result = subprocess.run([interpreter, str(runtime_script())], input=json.dumps(payload),
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except subprocess.TimeoutExpired:
        raise CatalogError("Il catalogo non ha risposto entro 15 secondi. Aggiorna l’elenco per verificarne lo stato.") from None
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
            raise CatalogError("Il catalogo è cambiato. Aggiorna l’elenco e riapri il modulo prima di salvare.")
        raise CatalogError("Catalogo non aggiornato. " + str(error.get("message", "Errore del runtime.")))
    if result.returncode or "registry" not in reply or "revision" not in reply:
        raise CatalogError("Risposta del runtime incompleta.")
    return reply
