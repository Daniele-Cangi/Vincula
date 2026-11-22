# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local JSON API for editing the shared node and desktop registry."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from time import monotonic, sleep

import node_registry


class ConflictError(ValueError):
    """The configuration changed after the caller read it."""


def registry_path():
    override = os.environ.get("VINCULA_NODE_REGISTRY")
    if override:
        return Path(override).resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Vincula"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vincula"
    return base / "nodes.json"


def validation_legacy():
    # Compatibility entries validate legacy_route fields. This API never executes a route.
    local = "windows" if sys.platform == "win32" else "ubuntu"
    family = {"ubuntu": "linux", "windows": "windows"}
    nodes = (node_registry.Node(name, name, family[name],
                               route=None if name == local else node_registry.Route("legacy", ()))
             for name in ("ubuntu", "windows"))
    return node_registry.Registry(local, nodes)


def validate(document):
    return node_registry.from_document(document, validation_legacy(), "NUL" if sys.platform == "win32" else "/dev/null")


def snapshot(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"ok": True, "revision": None, "registry": None}
    document = json.loads(raw.decode("utf-8-sig"))
    validate(document)
    return {"ok": True, "revision": sha256(raw).hexdigest(), "registry": document}


@contextmanager
def edit_lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"\0")
            lock.flush()
        deadline = monotonic() + 10
        while True:
            try:
                lock.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if monotonic() >= deadline:
                    raise TimeoutError("The node registry is busy.") from None
                sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def save(path, document):
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    stage = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".nodes-", suffix=".tmp", delete=False) as file:
            stage = Path(file.name)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        stage.replace(path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
    return {"ok": True, "revision": sha256(raw).hexdigest(), "registry": document}


def request_fields(request, allowed, required):
    if request.keys() - (allowed | {"action", "expected_revision"}) or not required <= request.keys():
        raise ValueError("Invalid fields for the node operation.")


def add_node(document, request):
    request_fields(request, {"node", "local"}, {"node"})
    if "local" in request and type(request["local"]) is not bool:
        raise ValueError("The local flag must be a boolean.")
    node = deepcopy(request["node"])
    if not isinstance(node, dict):
        raise ValueError("Specify a node object.")
    if document is None:
        if request.get("local") is not True:
            raise ValueError("Register this computer with local=true before adding peers.")
        return {"local": node.get("id"), "nodes": [node]}
    if request.get("local"):
        raise ValueError("This registry already has a local node.")
    if any(entry["id"] == node.get("id") for entry in document["nodes"]):
        raise ValueError("The node is already registered.")
    document["nodes"].append(node)
    return document


def selected_node(document, node_id):
    for entry in document["nodes"]:
        if entry["id"] == node_id:
            return entry
    raise ValueError("Select a registered canonical node ID.")


def update_node(document, request):
    request_fields(request, {"node_id", "changes"}, {"node_id", "changes"})
    changes = request["changes"]
    if not isinstance(changes, dict) or "id" in changes:
        raise ValueError("Update node fields while preserving its ID.")
    entry = selected_node(document, request["node_id"])
    for name, value in changes.items():
        if name in {"ssh", "legacy_route"} and value is None:
            entry.pop(name, None)
        else:
            entry[name] = deepcopy(value)


def remove_node(document, request):
    request_fields(request, {"node_id"}, {"node_id"})
    node_id = request["node_id"]
    if node_id == document["local"]:
        raise ValueError("The local node cannot be removed.")
    desktop = document.get("desktop")
    if desktop and any(member["node_id"] == node_id for member in desktop["members"]):
        raise ValueError("Remove the node from the desktop group before removing its identity.")
    entry = selected_node(document, node_id)
    document["nodes"].remove(entry)


def edit_document(document, request):
    action = request["action"]
    if action == "add":
        return add_node(document, request)
    if document is None:
        raise ValueError("Register this computer before editing the registry.")
    if action == "update":
        update_node(document, request)
    elif action == "remove":
        remove_node(document, request)
    elif action == "desktop":
        request_fields(request, {"desktop"}, {"desktop"})
        if request["desktop"] is None:
            document.pop("desktop", None)
        else:
            document["desktop"] = deepcopy(request["desktop"])
    else:
        raise ValueError("Unknown node operation.")
    return document


def execute(request, path):
    if not isinstance(request, dict) or not isinstance(request.get("action"), str):
        raise ValueError("Specify a JSON object with an action.")
    path = Path(path)
    if request["action"] == "list":
        request_fields(request, set(), set())
        return snapshot(path)
    if request["action"] not in {"add", "update", "remove", "desktop"}:
        raise ValueError("Unknown node operation.")
    with edit_lock(path):
        before = snapshot(path)
        if "expected_revision" in request and request["expected_revision"] != before["revision"]:
            raise ConflictError("The node registry changed. Reload it before applying this edit.")
        document = edit_document(before["registry"], request)
        validate(document)
        return save(path, document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=None)
    args = parser.parse_args()
    try:
        result = execute(json.load(sys.stdin), args.registry or registry_path())
    except ConflictError as error:
        result = {"ok": False, "error": {"code": "conflict", "message": str(error)}}
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        result = {"ok": False, "error": {"code": "invalid_request", "message": str(error)}}
    except OSError as error:
        result = {"ok": False, "error": {"code": "io_error", "message": str(error)}}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
