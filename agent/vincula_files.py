# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Authorized file operations on one node. The transport only sees private staging paths."""

from codecs import getincrementaldecoder
from contextlib import suppress
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import stat
from tempfile import NamedTemporaryFile
import time
from uuid import UUID

FILE_LIMIT = 64 * 1024 * 1024
READ_SIZE = 8 * 1024
PAGE_SIZE = 40
STAGE_PREFIX = ".vincula-import-"
LABELS = {"browse": "Sfoglia", "read": "Leggi", "export": "Invia", "import": "Ricevi"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS file_roots(alias TEXT PRIMARY KEY, directory TEXT NOT NULL, writable INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS file_requests(
    id TEXT PRIMARY KEY, operation TEXT NOT NULL, root TEXT NOT NULL, relative TEXT NOT NULL,
    offset INTEGER NOT NULL, state TEXT NOT NULL, auto INTEGER NOT NULL,
    created REAL NOT NULL, result TEXT
);
CREATE TABLE IF NOT EXISTS file_transfers(id TEXT PRIMARY KEY, plan TEXT NOT NULL);
"""


def components(relative):
    if not relative:
        return []
    parts = relative.split("/")
    for part in parts:
        if (part in {"", ".", ".."} or part.endswith((" ", ".")) or part.casefold().startswith(STAGE_PREFIX)
                or any(char in '\\:<>|?*"' or ord(char) < 32 for char in part)
                or (os.name == "nt" and PureWindowsPath(part).is_reserved())):
            raise ValueError("Use a relative file path inside an authorized folder.")
    return parts


def regular_info(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Symbolic links and reparse points are excluded from file access.")
    return info


def root_grant(db, root, write):
    grant = db.execute("SELECT directory,writable FROM file_roots WHERE alias=?", (root,)).fetchone()
    if grant is None or (write and not grant["writable"]):
        raise ValueError("Folder is not authorized for this operation.")
    return Path(grant["directory"])


def checked_path(db, root, relative, write=False):
    parts = components(relative)
    base = root_grant(db, root, write)
    if base.resolve(strict=True) != base or not stat.S_ISDIR(regular_info(base).st_mode):
        raise ValueError("The authorized folder has changed.")
    path = base
    for pos, part in enumerate(parts):
        path /= part
        if pos == len(parts) - 1 and write and not os.path.lexists(path):
            break
        info = regular_info(path)
        if pos < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError("An intermediate path component is not a directory.")
    return path


def file_info(path):
    info = regular_info(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Select a regular file.")
    return info


def decide(db, key, allowed):
    result = db.execute("UPDATE file_requests SET state=?,auto=0 WHERE id=? AND state='pending'",
                        ("approved" if allowed else "denied", str(UUID(key))))
    if not result.rowcount:
        raise ValueError("File request is not awaiting approval.")


def permission_items(db):
    return db.execute("SELECT id,operation,root,relative,offset FROM file_requests WHERE state='pending' "
                      "ORDER BY created").fetchall()


def grant(db, alias, directory, writable):
    if directory is not None:
        path = Path(directory).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Specify an existing absolute directory path.")
        db.execute("INSERT OR REPLACE INTO file_roots VALUES(?,?,?)", (alias, str(path.resolve()), int(writable)))
    else:
        db.execute("DELETE FROM file_roots WHERE alias=?", (alias,))
    db.execute("UPDATE file_requests SET state='denied',result=NULL WHERE root=? "
               "AND (operation!='import' OR state!='completed')", (alias,))


def save_plan(db, key, plan):
    canonical = json.dumps(plan, sort_keys=True)
    db.execute("INSERT OR IGNORE INTO file_transfers VALUES(?,?)", (key, canonical))
    saved = db.execute("SELECT plan FROM file_transfers WHERE id=?", (key,)).fetchone()[0]
    if saved != canonical:
        raise ValueError("Transfer ID already used with different endpoints.")


def browse(path, offset):
    names = []
    for item in sorted(path.iterdir(), key=lambda child: child.name):
        if item.name.startswith(STAGE_PREFIX):
            continue
        with suppress(OSError, ValueError):
            components(item.name)
            info = regular_info(item)
            if stat.S_ISDIR(info.st_mode):
                names.append({"name": item.name, "kind": "directory", "size": None})
            elif stat.S_ISREG(info.st_mode):
                names.append({"name": item.name, "kind": "file", "size": info.st_size})
    end = offset + PAGE_SIZE
    return {"entries": names[offset:end], "next_offset": end if end < len(names) else None}


def read_text(path, offset):
    file_info(path)
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(READ_SIZE + 1)
    more = len(data) > READ_SIZE
    decoder = getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data[:READ_SIZE], final=not more)
    pending, _ = decoder.getstate()
    return {"text": text, "next_offset": offset + READ_SIZE - len(pending) if more else None}


def request(db, key, mode, operation_kind, root, relative, offset=0):
    operation = operation_kind
    if operation not in LABELS or (operation != "browse" and not relative):
        raise ValueError("Invalid file operation.")
    spec = (operation, root, relative, offset)
    row = db.execute("SELECT * FROM file_requests WHERE id=?", (key,)).fetchone()
    if row is None:
        components(relative)
        root_grant(db, root, write=operation == "import")
        db.execute("INSERT INTO file_requests VALUES(?,?,?,?,?,?,?,?,NULL)",
                   (key, *spec, "approved" if mode == "full" else "pending", int(mode == "full"), time.time()))
    elif tuple(row[field] for field in ("operation", "root", "relative", "offset")) != spec:
        raise ValueError("File request ID already used for another operation.")
    return status(db, key)


def get_request(db, key):
    row = db.execute("SELECT * FROM file_requests WHERE id=?", (key,)).fetchone()
    if row is None:
        raise ValueError("Unknown file request.")
    return row


def status(db, key):
    row = get_request(db, key)
    result = json.loads(row["result"]) if row["result"] else None
    state = row["state"]
    if state == "approved" and row["operation"] in {"browse", "read"}:
        path = checked_path(db, row["root"], row["relative"])
        reader = browse if row["operation"] == "browse" else read_text
        result = reader(path, row["offset"])
        state = "completed"
        db.execute("UPDATE file_requests SET state=?,result=? WHERE id=?", (state, json.dumps(result), key))
    # The public request excludes absolute paths and transfer staging metadata.
    return {"id": key, "state": state, "operation": row["operation"],
            "root": row["root"], "path": row["relative"],
            "result": result if row["operation"] in {"browse", "read"} or state == "completed" else None}


def stage_path(home, key):
    base = home / "file-stage"
    directory = base / key
    for component in (base, directory):
        component.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(regular_info(component).st_mode):
            raise ValueError("Unsafe transfer staging directory.")
    return directory / "payload"


def copy_limited(source, output):
    file_info(source)
    digest, size = sha256(), 0
    with source.open("rb") as stream:
        while block := stream.read(128 * 1024):
            size += len(block)
            if size > FILE_LIMIT:
                raise ValueError("File is larger than the 64 MiB transfer limit.")
            digest.update(block)
            if output is not None:
                output.write(block)
    return {"sha256": digest.hexdigest(), "size": size}


def stage(db, home, key):
    row = get_request(db, key)
    if row["state"] != "approved" or row["operation"] not in {"export", "import"}:
        raise ValueError("This transfer endpoint still needs authorization.")
    path = checked_path(db, row["root"], row["relative"], write=row["operation"] == "import")
    payload = stage_path(home, key)
    if row["operation"] == "import":
        if os.path.lexists(payload):
            file_info(payload)
        return {"path": str(payload)}
    if row["result"] and payload.is_file():
        return dict(json.loads(row["result"]), path=str(payload))
    with NamedTemporaryFile(dir=payload.parent, delete=False) as output:
        scratch = Path(output.name)
        try:
            receipt = copy_limited(path, output)
        except BaseException:
            output.close()
            scratch.unlink(missing_ok=True)
            raise
    try:
        scratch.replace(payload)
    finally:
        scratch.unlink(missing_ok=True)
    db.execute("UPDATE file_requests SET result=? WHERE id=?", (json.dumps(receipt), key))
    return dict(receipt, path=str(payload))


def commit(db, home, key, expected):
    row = get_request(db, key)
    if row["operation"] != "import" or row["state"] not in {"approved", "completed"}:
        raise ValueError("The destination is not authorized.")
    if row["state"] == "completed":
        return json.loads(row["result"])
    target = checked_path(db, row["root"], row["relative"], write=True)
    pending_link = target.parent / (STAGE_PREFIX + key)
    if os.path.lexists(target):
        # A lost reply or interruption after publication can leave the original hard link.
        if not pending_link.is_file() or not os.path.samefile(pending_link, target):
            raise ValueError("The target already exists.")
        file_info(pending_link)
        if copy_limited(target, None) != expected:
            raise ValueError("The published file no longer matches the expected checksum.")
    else:
        payload = stage_path(home, key)
        with NamedTemporaryFile(dir=target.parent, prefix=STAGE_PREFIX, delete=False) as output:
            scratch = Path(output.name)
            try:
                actual = copy_limited(payload, output)
                if actual != expected:
                    raise ValueError("Transfer checksum or size does not match.")
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                output.close()
                scratch.unlink(missing_ok=True)
                raise
        try:
            try:
                os.link(scratch, pending_link)
            except FileExistsError:
                if copy_limited(pending_link, None) != expected:
                    raise ValueError("The existing staging file does not match the checksum.")
            os.link(pending_link, target)
        finally:
            scratch.unlink(missing_ok=True)
    db.execute("UPDATE file_requests SET state='completed',result=? WHERE id=?", (json.dumps(expected), key))
    return expected


def cleanup(db, home, key):
    row = get_request(db, key)
    if row["operation"] == "import" and row["state"] == "completed":
        with suppress(OSError, ValueError):
            path = checked_path(db, row["root"], row["relative"], write=True)
            pending = path.parent / (STAGE_PREFIX + key)
            if os.path.samefile(pending, path):
                pending.unlink()
    staged = home / "file-stage" / key
    with suppress(OSError):
        (staged / "payload").unlink(missing_ok=True)
        staged.rmdir()
    return True


def dispatch(db, home, mode, operation, request_id=None, **fields):
    if operation == "roots":
        return [dict(row) for row in db.execute("SELECT alias,writable FROM file_roots ORDER BY alias")]
    key = str(UUID(request_id))
    if operation == "request":
        return request(db, key, mode, **fields)
    if operation == "status":
        return status(db, key)
    if operation == "stage":
        return stage(db, home, key)
    if operation == "commit":
        return commit(db, home, key, **fields)
    if operation == "cleanup":
        return cleanup(db, home, key)
    raise ValueError("Unknown file operation.")
