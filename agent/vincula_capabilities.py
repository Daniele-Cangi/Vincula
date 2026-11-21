# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local registry and isolated launcher for operator-installed capabilities."""

from contextlib import closing, redirect_stdout
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from uuid import UUID, uuid4

ID = re.compile(r"[a-z][a-z0-9_-]{0,23}\.[a-z][a-z0-9_-]{0,23}")
TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,47}")
EXTENSION = re.compile(r"\.[a-z0-9]{1,12}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_FILES = 4
MAX_PATH_BYTES = 240
MAX_PARAMETER_BYTES = 512
MAX_RESULT_BYTES = 3072
MAX_MANIFEST_BYTES = 16 * 1024
MAX_ADAPTER_BYTES = 256 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_providers(
    id TEXT PRIMARY KEY, revision TEXT NOT NULL, python TEXT NOT NULL,
    definition TEXT NOT NULL, adapter BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_jobs(id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capability_plans(id TEXT PRIMARY KEY, request TEXT NOT NULL, plan TEXT NOT NULL);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)


def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Use a capability ID in namespace.name form.")
    return value


def matches(kind, value):
    if kind == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    return type(value) is {"string": str, "integer": int, "boolean": bool}[kind]


def parameter_schema(rules):
    if not isinstance(rules, dict) or len(rules) > 12:
        raise ValueError("Declare up to 12 capability parameters.")
    for name, rule in rules.items():
        if (not TOKEN.fullmatch(name) or not isinstance(rule, dict)
                or rule.keys() - {"type", "required", "enum", "description"}
                or rule.get("type") not in ("string", "integer", "boolean", "number")
                or type(rule.get("required", False)) is not bool
                or not isinstance(rule.get("description", ""), str)
                or len(rule.get("description", "")) > 160):
            raise ValueError(f"Invalid rule for capability parameter {name}.")
        if "enum" in rule and (not isinstance(rule["enum"], list) or not 1 <= len(rule["enum"]) <= 12
                               or not all(matches(rule["type"], value) for value in rule["enum"])):
            raise ValueError(f"Invalid choices for capability parameter {name}.")
    if len(encode(rules)) > 4096:
        raise ValueError("The capability parameter schema exceeds 4096 bytes.")
    return rules


def validate_parameters(parameters, rules):
    if not isinstance(parameters, dict) or parameters.keys() - rules.keys():
        raise ValueError("The request contains undeclared capability parameters.")
    for name, rule in rules.items():
        if name not in parameters:
            if rule.get("required", False):
                raise ValueError(f"Missing capability parameter {name}.")
            continue
        value = parameters[name]
        if not matches(rule["type"], value) or ("enum" in rule and value not in rule["enum"]):
            raise ValueError(f"Invalid value for capability parameter {name}.")
    if len(encode(parameters)) > MAX_PARAMETER_BYTES:
        raise ValueError("Capability parameters exceed 512 bytes.")


def suffix(path):
    value = PurePosixPath(path).suffix.lower()
    return value if EXTENSION.fullmatch(value) else ""


def validate_paths(files, inputs, contract):
    if not isinstance(inputs, list) or len(inputs) > contract["max_files"]:
        raise ValueError("Too many files for this capability part.")
    for item in inputs:
        path = item["path"]
        if not isinstance(path, str) or not path or len(path.encode("utf-8")) > MAX_PATH_BYTES:
            raise ValueError("A capability file needs a nonempty path of at most 240 UTF-8 bytes.")
        if contract["extensions"] and suffix(path) not in contract["extensions"]:
            raise ValueError("A file extension does not match the capability contract.")
        files.components(path)


def revoke(db, capability_id):
    db.execute("DELETE FROM capability_providers WHERE id=?", (valid_id(capability_id),))
    db.execute("UPDATE jobs SET state='cancelled',finished=? "
               "WHERE program=? AND state IN ('pending','queued')", (time.time(), capability_id))


def registered_contract(db, files, manifest):
    release, description = manifest["version"], manifest.get("description", "")
    if (not isinstance(release, str) or not 1 <= len(release) <= 48
            or not isinstance(description, str) or len(description) > 300):
        raise ValueError("Provide a short version and optional description.")
    inbox = manifest.get("inbox")
    if inbox is not None:
        if not isinstance(inbox, str) or not TOKEN.fullmatch(inbox):
            raise ValueError("Choose an authorized inbox alias.")
        files.root_grant(db, inbox, True)
    max_files = manifest.get("max_files", 0)
    max_bytes = manifest.get("max_input_bytes", files.FILE_LIMIT)
    extensions = manifest.get("extensions", [])
    if (type(max_files) is not int or not 0 <= max_files <= MAX_FILES
            or type(max_bytes) is not int or not 1 <= max_bytes <= files.FILE_LIMIT
            or not isinstance(extensions, list) or len(extensions) > 12
            or not all(isinstance(ext, str) and EXTENSION.fullmatch(ext) for ext in extensions)):
        raise ValueError("Invalid capability file limits or extensions.")

    return {"version": release, "description": description, "inbox": inbox,
            "contract": {"max_files": max_files, "max_input_bytes": max_bytes,
                         "extensions": sorted(set(extensions)),
                         "parameters": parameter_schema(manifest.get("parameters", {}))}}


def register(db, files, manifest):
    required = {"id", "version", "python", "adapter"}
    optional = {"inbox", "description", "max_files", "max_input_bytes", "extensions", "parameters"}
    if not isinstance(manifest, dict) or not required <= manifest.keys() <= (required | optional):
        raise ValueError("Provide a capability manifest with id, version, python and adapter.")
    capability_id = valid_id(manifest["id"])
    serialized = encode(registered_contract(db, files, manifest))
    python = Path(manifest["python"])
    adapter = Path(manifest["adapter"])
    if not python.is_absolute() or not python.is_file() or not adapter.is_absolute() or adapter.suffix != ".py":
        raise ValueError("Specify absolute Python interpreter and Python adapter file paths.")
    with adapter.open("rb") as source:
        code = source.read(MAX_ADAPTER_BYTES + 1)
    if not code or len(code) > MAX_ADAPTER_BYTES:
        raise ValueError("The adapter must contain at most 256 KiB of Python source.")
    try:
        compile(code, str(adapter), "exec")
    except SyntaxError as error:
        raise ValueError(f"Invalid Python adapter: {error.msg}") from None

    previous = db.execute("SELECT * FROM capability_providers WHERE id=?", (capability_id,)).fetchone()
    if previous and (previous["python"], previous["definition"], previous["adapter"]) == (str(python), serialized, code):
        return
    revoke(db, capability_id)
    db.execute("INSERT INTO capability_providers VALUES(?,?,?,?,?)",
               (capability_id, uuid4().hex, str(python), serialized, code))


def available(db, files, row):
    if row is None or not Path(row["python"]).is_file():
        return None
    definition = json.loads(row["definition"])
    if definition["inbox"] is not None:
        try:
            files.root_grant(db, definition["inbox"], True)
        except ValueError:
            return None
    return dict(definition, id=row["id"], revision=row["revision"])


def describe(db, files, capability_id):
    if capability_id is not None:
        row = db.execute("SELECT id,revision,python,definition FROM capability_providers WHERE id=?",
                         (valid_id(capability_id),)).fetchone()
        return available(db, files, row)
    rows = db.execute("SELECT id,revision,python,definition FROM capability_providers ORDER BY id")
    return [definition for row in rows if (definition := available(db, files, row)) is not None]


def queue(db, files, mode, job_id, capability_id, revision, inputs, parameters):
    requested = {"capability_id": valid_id(capability_id), "revision": revision,
                 "inputs": inputs, "parameters": parameters}
    existing = db.execute("SELECT manifest FROM capability_jobs WHERE id=?", (job_id,)).fetchone()
    if existing:
        saved = json.loads(existing["manifest"])
        if encode({key: saved[key] for key in requested}) != encode(requested):
            raise ValueError("Job ID already used for a different capability part.")
        return
    if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        raise ValueError("Job ID already used outside this capability.")
    definition = describe(db, files, capability_id)
    if definition is None or definition["revision"] != revision:
        raise ValueError("Capability registration changed. Submit a new run.")
    contract = definition["contract"]
    if not isinstance(inputs, list) or not all(
        isinstance(item, dict) and {"index", "root", "path"} <= item.keys() <= {"index", "root", "path", "sha256"}
        and type(item["index"]) is int and 0 <= item["index"] < 2 * MAX_FILES
        and isinstance(item["root"], str) and TOKEN.fullmatch(item["root"])
        and ("sha256" not in item or (isinstance(item["sha256"], str) and DIGEST.fullmatch(item["sha256"])))
        for item in inputs
    ):
        raise ValueError("Invalid capability file reference.")
    if len({item["index"] for item in inputs}) != len(inputs):
        raise ValueError("Duplicate capability file index.")
    validate_paths(files, inputs, contract)
    validate_parameters(parameters, contract["parameters"])
    roots = {item["root"]: str(files.root_grant(db, item["root"], "sha256" in item)) for item in inputs}
    if any("sha256" in item and item["root"] != definition["inbox"] for item in inputs):
        raise ValueError("Transferred inputs must use the capability inbox.")
    db.execute("INSERT INTO capability_jobs VALUES(?,?)", (job_id, encode(dict(requested, roots=roots))))
    db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,?,?)",
               (job_id, capability_id, "[]", "queued" if mode == "full" else "pending", time.time()))


def authorized_job(db, job_id, running=False):
    row = db.execute(
        "SELECT j.program,j.state,c.manifest,p.revision,p.python,p.definition "
        "FROM capability_jobs c JOIN jobs j ON c.id=j.id "
        "LEFT JOIN capability_providers p ON j.program=p.id WHERE c.id=?", (job_id,),
    ).fetchone()
    if row is None or (running and row["state"] != "running"):
        raise ValueError("The destination worker must start the approved capability job.")
    manifest = json.loads(row["manifest"])
    if row["revision"] != manifest["revision"] or row["program"] != manifest["capability_id"]:
        raise ValueError("Capability registration changed before launch.")
    return row, manifest


def arguments(db, job_id, home):
    row, _ = authorized_job(db, job_id, running=True)
    return [row["python"], "-I", "-B", str(home / "vincula_capabilities.py"), job_id]


def load_module(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def open_input(path, flags):
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))


def snapshot(db, files, manifest, definition, workspace):
    maximum = definition["contract"]["max_input_bytes"]
    inputs = []
    for item in manifest["inputs"]:
        root, path = item["root"], item["path"]
        if str(files.root_grant(db, root, "sha256" in item)) != manifest["roots"][root]:
            raise ValueError("An input folder was reconfigured before launch.")
        source = files.checked_path(db, root, path)
        if files.file_info(source).st_size > maximum:
            raise ValueError("A capability input exceeds the registered file size limit.")
        with open(source, "rb", opener=open_input) as stream:
            current = files.file_info(files.checked_path(db, root, path))
            if not os.path.samestat(current, os.fstat(stream.fileno())):
                raise ValueError("A capability input changed while opening it.")
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("A capability input exceeds the registered file size limit.")
        digest = sha256(raw).hexdigest()
        if item.get("sha256", digest) != digest:
            raise ValueError("The staged input differs from the verified transfer.")
        target = workspace / f"input-{item['index']}{suffix(path)}"
        target.write_bytes(raw)
        inputs.append({"index": item["index"], "path": target, "sha256": digest})
    return inputs


def execute(job_id):
    home = Path(__file__).resolve().parent
    files = load_module("vincula_files", home / "vincula_files.py")
    database = (home / "jobs.sqlite3").as_uri() + "?mode=ro"
    with TemporaryDirectory(prefix="vincula-capability-") as directory:
        workspace = Path(directory)
        with closing(sqlite3.connect(database, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            row, manifest = authorized_job(db, job_id, running=True)
            definition = json.loads(row["definition"])
            code = db.execute("SELECT adapter FROM capability_providers WHERE id=?", (row["program"],)).fetchone()[0]
            inputs = snapshot(db, files, manifest, definition, workspace)
        adapter = workspace / "adapter.py"
        adapter.write_bytes(code)
        with redirect_stdout(sys.stderr):
            module = load_module("vincula_external_adapter", adapter)
            value = module.run(inputs, manifest["parameters"], workspace)
        output = encode({"value": value})
        if len(output) > MAX_RESULT_BYTES:
            raise ValueError("Adapter result exceeds 3072 bytes of JSON.")
    return output


def main():
    try:
        print(execute(str(UUID(sys.argv[1]))))
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
