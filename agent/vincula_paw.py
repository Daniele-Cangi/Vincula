# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Node-local adapter for offline PAW analysis."""

import base64
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

CAPABILITY = "paw.analyze_offline"
MAX_FILES = 4
MAX_INPUT_BYTES = 4 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS paw_provider(
    id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL,
    python TEXT NOT NULL, version TEXT NOT NULL, inbox TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paw_jobs(id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paw_plans(id TEXT PRIMARY KEY, request TEXT NOT NULL, plan TEXT NOT NULL);
"""


def register(db, files, executable, inbox):
    path = Path(executable)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Choose the absolute path of the Python interpreter with PAW installed.")
    files.root_grant(db, inbox, True)
    result = subprocess.run(
        [path, "-I", "-B", "-c", "from importlib.metadata import version; print(version('paw'))"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
    )
    release = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode or not release:
        raise ValueError("PAW is not installed in that Python environment.")
    revoke(db)
    db.execute("INSERT INTO paw_provider VALUES(1,?,?,?,?)", (uuid4().hex, str(path), release, inbox))


def revoke(db):
    db.execute("DELETE FROM paw_provider")
    db.execute("UPDATE jobs SET state='cancelled',finished=? "
               "WHERE program=? AND state IN ('pending','queued')", (time.time(), CAPABILITY))


def provider(db, files):
    row = db.execute("SELECT revision,python,version,inbox FROM paw_provider WHERE id=1").fetchone()
    if row is None or not Path(row["python"]).is_file():
        return None
    try:
        files.root_grant(db, row["inbox"], True)
    except ValueError:
        return None
    return row


def describe(db, files):
    row = provider(db, files)
    if row is None:
        return None
    return {"id": CAPABILITY, "revision": row["revision"], "paw_version": row["version"],
            "inbox": row["inbox"], "input_extension": ".eml", "max_files": MAX_FILES,
            "max_input_bytes": MAX_INPUT_BYTES}


def queue(db, files, mode, job_id, revision, inputs):
    requested = {"revision": revision, "inputs": inputs}
    existing = db.execute("SELECT manifest FROM paw_jobs WHERE id=?", (job_id,)).fetchone()
    if existing:
        saved = json.loads(existing["manifest"])
        if any(saved[key] != value for key, value in requested.items()):
            raise ValueError("Job ID already used for a different PAW part.")
        return
    if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        raise ValueError("Job ID already used outside PAW.")
    row = provider(db, files)
    if row is None or row["revision"] != revision:
        raise ValueError("PAW registration changed. Prepare a new plan.")
    if not 1 <= len(inputs) <= MAX_FILES:
        raise ValueError("Invalid number of PAW inputs.")
    roots = {}
    for item in inputs:
        if Path(item["path"]).suffix.lower() != ".eml":
            raise ValueError("PAW accepts EML files for this capability.")
        files.components(item["path"])
        roots[item["root"]] = str(files.root_grant(db, item["root"], False))
    manifest = json.dumps(dict(requested, roots=roots), sort_keys=True)
    db.execute("INSERT INTO paw_jobs VALUES(?,?)", (job_id, manifest))
    db.execute("INSERT INTO jobs(id,program,argv,state,created) VALUES(?,?,?,?,?)",
               (job_id, CAPABILITY, "[]", "queued" if mode == "full" else "pending", time.time()))


def arguments(db, files, job_id, home):
    row = provider(db, files)
    manifest = json.loads(db.execute("SELECT manifest FROM paw_jobs WHERE id=?", (job_id,)).fetchone()["manifest"])
    if row is None or row["revision"] != manifest["revision"]:
        raise ValueError("PAW provider changed before launch.")
    assigned = []
    for item in manifest["inputs"]:
        if str(files.root_grant(db, item["root"], False)) != manifest["roots"][item["root"]]:
            raise ValueError("An input folder was reconfigured before launch.")
        path = files.checked_path(db, item["root"], item["path"])
        if files.file_info(path).st_size > MAX_INPUT_BYTES:
            raise ValueError("A PAW input exceeds 4 MiB.")
        assigned.append({"index": item["index"], "file": str(path), "sha256": item.get("sha256")})
    manifest = {"paw_version": row["version"], "inputs": assigned}
    payload = base64.b64encode(json.dumps(manifest).encode()).decode("ascii")
    return [row["python"], "-I", "-B", str(home / "vincula_paw.py"), payload]


def run_sample(index, raw):
    with TemporaryDirectory(prefix="vincula-paw-") as directory:
        root = Path(directory)
        source = root / "message.eml"
        source.write_bytes(raw)

        def run(*args):
            result = subprocess.run([sys.executable, "-I", "-B", "-m", "paw", *args], cwd=root,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f"PAW {args[0]} failed with exit code {result.returncode}.")

        run("full", str(source), "--no-egress")
        case, = (root / "cases").glob("case-*")
        run("verify", "--case", str(case))
        execution = json.loads((case / "execution.json").read_bytes())
        score = json.loads((case / "report" / "score.json").read_bytes())
        if (execution["status"] != "completed" or execution["no_egress"] is not True
                or (case / "input.eml").read_bytes() != raw):
            raise RuntimeError("PAW did not complete the verified offline analysis.")
        return {"index": index, "sha256": sha256(raw).hexdigest(),
                "score": score["score"], "decision": score["decision"], "seal_verified": True}


def execute(manifest):
    if version("paw") != manifest["paw_version"]:
        raise ValueError("Installed PAW version differs from the registered version.")
    report = []
    for item in manifest["inputs"]:
        with Path(item["file"]).open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("A PAW input exceeds 4 MiB.")
        expected = item.get("sha256")
        if expected and sha256(raw).hexdigest() != expected:
            raise ValueError("The staged input differs from the verified transfer.")
        report.append(run_sample(item["index"], raw))
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(execute(json.loads(base64.b64decode(sys.argv[1], validate=True)))))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
