# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded protocol-2 storage. Callers own authorization, locks and metadata.

No database, transport or process is opened here. Paths must already have passed
the endpoint's grant validation. Callbacks must finish their own short database
transactions before returning; filesystem work never runs inside those scopes.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

BLOCK_SIZE = 4 * 1024 * 1024
MAX_BYTES = 1024 ** 4
STAGE_PREFIX = ".vincula-import-"
HEADER_FIELDS = {"version", "export_id", "request_sha256", "size", "sha256", "block_size", "block_count", "blocks_sha256"}
IDENTITY_FIELDS = {"dev", "ino", "size", "mtime_ns", "ctime_ns"}
DIGEST = re.compile(r"[0-9a-f]{64}")
PRIVATE_NAME = re.compile(r"(?:payload(?:\.part)?|pending-owner\.json|block-[0-9]{8}|\.(?:block-[0-9]{8}|pending-owner)-[0-9a-f]{32}\.tmp)")


class Cancelled(RuntimeError):
    """The caller requested a recoverable stop."""


def _noop(*args):
    pass


def _never():
    return False


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use a canonical endpoint UUID.")
    return value


def _digest(value):
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ValueError("Use a canonical SHA-256 digest.")
    return value


def _integer(value, maximum=MAX_BYTES):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid bounded storage integer.")
    return value


def _safe(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Links and reparse points are excluded from transfer storage.")
    return info


def _identity(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def source_identity(path):
    # Use the same metadata API as the open source's before/after checks.
    # Windows CPython 3.14 can expose creation time in lstat().st_ctime_ns
    # but change time in fstat().st_ctime_ns (CPython issue 157671). Preserve
    # exact descriptor timestamps; do not discard or round the change guard.
    with _open(path) as stream:
        return _identity(os.fstat(stream.fileno()))


def _same_inode(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _windows_account():
    # Existing Windows dependency, imported only on Windows. Match the worker's
    # private-storage principals, without depending on jobs or its database.
    import ntsecuritycon
    import win32api
    import win32security as security
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), ntsecuritycon.TOKEN_QUERY)
    try:
        account = security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()
    return security, security.ConvertSidToStringSid(account)


def _windows_attributes(*, directory=False):
    import pywintypes
    security, account = _windows_account()
    flags = "OICI" if directory else ""
    descriptor = "D:P" + "".join(f"(A;{flags};FA;;;{sid})" for sid in (account, "S-1-5-18", "S-1-5-32-544"))
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = False
    attributes.SECURITY_DESCRIPTOR = security.ConvertStringSecurityDescriptorToSecurityDescriptor(descriptor, security.SDDL_REVISION_1)
    return attributes


def _windows_security(path):
    security, account = _windows_account()
    trusted = {account, "S-1-5-18", "S-1-5-32-544"}
    descriptor = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
    if security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) not in trusted:
        raise ValueError("Transfer staging has an unexpected Windows owner.")
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise ValueError("Transfer staging has no private Windows DACL.")
    for index in range(acl.GetAceCount()):
        kind, mask, principal = acl.GetAce(index)
        if kind[0] == security.ACCESS_DENIED_ACE_TYPE:
            continue
        if kind[0] != security.ACCESS_ALLOWED_ACE_TYPE or mask and security.ConvertSidToStringSid(principal) not in trusted:
            raise ValueError("Transfer staging grants another Windows principal access.")


def _make_directory(path):
    if os.name == "nt":
        import win32file
        try:
            win32file.CreateDirectory(str(path), _windows_attributes(directory=True))
        except Exception as error:
            if getattr(error, "winerror", None) in {80, 183}:
                raise FileExistsError("Transfer directory was concurrently created.") from None
            raise
    else:
        path.mkdir(mode=0o700)


def _descriptor(path, flags, *, write, create, dir_fd=None):
    if os.name != "nt":
        return os.open(path, flags, 0o600, dir_fd=dir_fd)
    import msvcrt
    import win32con
    import win32file
    access = win32con.GENERIC_READ | (win32con.GENERIC_WRITE if write else 0)
    handle = win32file.CreateFile(str(path), access,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        _windows_attributes() if create else None, win32con.CREATE_NEW if create else win32con.OPEN_EXISTING,
        0x00200000, None)  # FILE_FLAG_OPEN_REPARSE_POINT: inspect, never follow.
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
        handle.Detach()  # CRT descriptor now owns the exact original handle.
        return descriptor
    except BaseException:
        handle.Close()
        raise


def _directory(path, *, private=False, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Storage paths must be absolute.")
    for component in reversed((path, *path.parents)):
        try:
            info = _safe(component.lstat())
        except FileNotFoundError:
            if not create:
                raise
            try:
                _make_directory(component)
            except FileExistsError:
                pass  # Reinspect the winning directory; never trust existence alone.
            info = _safe(component.lstat())
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Storage parent is not a directory.")
    if private and os.name != "nt":
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Transfer staging must be private to its account.")
    elif private:
        _windows_security(path)
    return path


@contextmanager
def _open(path, *, write=False, create=False, private=False, max_links=1, dir_fd=None):
    path = Path(path)
    def inspect():
        return _safe(os.stat(path, dir_fd=dir_fd, follow_symlinks=False))
    before = None if create else inspect()
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise ValueError("Transfer storage requires regular files.")
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = _descriptor(path, flags, write=write, create=create, dir_fd=dir_fd)
    try:
        info = _safe(os.fstat(descriptor))
        current = inspect()
        if (not stat.S_ISREG(info.st_mode) or not _same_inode(info, current)
                or before is not None and not _same_inode(info, before)):
            raise ValueError("Transfer file identity changed.")
        if os.name == "nt" and (create or private):
            _windows_security(path)
        if private and (not 1 <= info.st_nlink <= max_links or os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise ValueError("Transfer staging file is not private and singly linked.")
        with os.fdopen(descriptor, "r+b" if write else "rb", buffering=0) as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _unlink_owned(path, info, *, dir_fd=None):
    current = _safe(os.stat(path, dir_fd=dir_fd, follow_symlinks=False))
    if not _same_inode(current, info):
        raise ValueError("Refusing cleanup of a replacement file.")
    os.unlink(path, dir_fd=dir_fd)


def _sync_directory(path):
    if os.name == "nt":
        return  # Windows has no stdlib directory-fsync equivalent.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(stream, data):
    view = memoryview(data)
    while view:
        count = stream.write(view)
        if not count:
            raise OSError("Transfer write made no progress.")
        view = view[count:]


def _guard(authorize, cancelled):
    if cancelled():
        raise Cancelled("Transfer stopped; verified checkpoints are retained.")
    authorize()


def validate_header(header):
    if not isinstance(header, dict) or set(header) != HEADER_FIELDS or type(header["version"]) is not int or header["version"] != 2:
        raise ValueError("Invalid closed protocol-2 content header.")
    _uuid(header["export_id"])
    for key in ("request_sha256", "sha256", "blocks_sha256"):
        _digest(header[key])
    size = _integer(header["size"])
    if type(header["block_size"]) is not int or header["block_size"] != BLOCK_SIZE:
        raise ValueError("Unsupported transfer block size.")
    if _integer(header["block_count"], MAX_BYTES // BLOCK_SIZE) != (size + BLOCK_SIZE - 1) // BLOCK_SIZE:
        raise ValueError("Content block count differs from its size.")
    return dict(header)


def _record(record, index, size):
    if (not isinstance(record, dict) or set(record) != {"index", "size", "sha256"}
            or type(record["index"]) is not int or record["index"] != index
            or type(record["size"]) is not int or record["size"] != size):
        raise ValueError("Block metadata differs from its index or expected size.")
    _digest(record["sha256"])
    return dict(record)


def block_line(record):
    _record(record, _integer(record["index"], MAX_BYTES // BLOCK_SIZE - 1), _integer(record["size"], BLOCK_SIZE))
    return f"{record['index']}:{record['size']}:{record['sha256']}\n".encode("ascii")


def _block_size(header, index):
    _integer(index, header["block_count"])
    if index >= header["block_count"]:
        raise ValueError("Block index outside content.")
    return min(BLOCK_SIZE, header["size"] - index * BLOCK_SIZE)


class PrivateFiles:
    """Only named files inside one operator-selected private endpoint namespace."""
    def __init__(self, base, kind, endpoint_id):
        if kind not in {"exports", "imports"}:
            raise ValueError("Unknown private transfer namespace.")
        self.base = _directory(base, private=True, create=True)
        self.parent = _directory(self.base / kind, private=True, create=True)
        self.directory = _directory(self.parent / _uuid(endpoint_id), private=True, create=True)

    def path(self, name):
        if PRIVATE_NAME.fullmatch(name) is None:
            raise ValueError("Unknown private transfer filename.")
        _directory(self.directory, private=True)
        return self.directory / name


def _header(export_id, request_sha256, size, whole, blocks):
    return validate_header({"version": 2, "export_id": export_id, "request_sha256": request_sha256,
        "size": size, "sha256": whole.hexdigest(), "block_size": BLOCK_SIZE,
        "block_count": (size + BLOCK_SIZE - 1) // BLOCK_SIZE, "blocks_sha256": blocks.hexdigest()})


def prepare_export(source, private_base, export_id, request_sha256, identity, *, get_block,
                   checkpoint, cancelled=_never, authorize=_noop, max_bytes=MAX_BYTES):
    """Resume only hash-verified, checkpointed prefixes; leave tails recoverable."""
    _uuid(export_id); _digest(request_sha256); _integer(max_bytes)
    if (not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS
            or any(type(identity[key]) is not int or not 0 <= identity[key] <= 2**128 - 1 for key in ("dev", "ino"))
            or type(identity["size"]) is not int or not 0 <= identity["size"] <= MAX_BYTES
            or any(type(identity[key]) is not int or not -(2**63) <= identity[key] < 2**63 for key in ("mtime_ns", "ctime_ns"))):
        raise ValueError("Invalid source identity metadata.")
    if identity["size"] > max_bytes:
        raise ValueError("Source exceeds its authorized byte budget.")
    _guard(authorize, cancelled)
    files = PrivateFiles(private_base, "exports", export_id)
    complete, partial = files.path("payload"), files.path("payload.part")
    sealed = os.path.lexists(complete)
    path = complete if sealed else partial
    create = not os.path.lexists(path)
    whole, blocks, copied = sha256(), sha256(), 0
    count = (identity["size"] + BLOCK_SIZE - 1) // BLOCK_SIZE
    doubled = sealed and os.path.lexists(partial)
    with _open(source) as input_stream, _open(path, write=not sealed, create=create, private=True,
                                           max_links=2 if doubled else 1) as output:
        if doubled and not _same_inode(_safe(partial.lstat()), os.fstat(output.fileno())):
            raise ValueError("Export publication has a different partial inode.")
        if _identity(os.fstat(input_stream.fileno())) != identity or source_identity(source) != identity:
            raise ValueError("Source identity changed before preparing its immutable export.")
        missing = False
        for index in range(count):
            metadata = get_block(index)
            if metadata is None:
                missing = True
                if sealed:
                    raise ValueError("Sealed export has missing block metadata.")
                continue
            if missing:
                raise ValueError("Export checkpoints are not a contiguous verified prefix.")
            size = min(BLOCK_SIZE, identity["size"] - index * BLOCK_SIZE)
            metadata = _record(metadata, index, size)
            _guard(authorize, cancelled)
            data = output.read(size)
            if len(data) != size or sha256(data).hexdigest() != metadata["sha256"]:
                raise ValueError("Checkpointed export prefix is corrupt.")
            whole.update(data); blocks.update(block_line(metadata)); copied += size
            del data
        if get_block(count) is not None:
            raise ValueError("Export metadata extends beyond source content.")
        if sealed and output.read(1):
            raise ValueError("Sealed export contains an unexpected suffix.")
        if not sealed:
            _guard(authorize, cancelled)
            output.truncate(copied); output.seek(copied); input_stream.seek(copied)
            while copied < identity["size"]:
                _guard(authorize, cancelled)
                data = input_stream.read(min(BLOCK_SIZE, identity["size"] - copied))
                if not data:
                    raise ValueError("Source changed during export preparation.")
                index = copied // BLOCK_SIZE
                metadata = {"index": index, "size": len(data), "sha256": sha256(data).hexdigest()}
                if len(data) != min(BLOCK_SIZE, identity["size"] - copied):
                    raise ValueError("Source changed during export preparation.")
                _write_all(output, data); os.fsync(output.fileno())
                checkpoint(metadata)
                whole.update(data); blocks.update(block_line(metadata)); copied += len(data)
                del data
        if _identity(os.fstat(input_stream.fileno())) != identity or source_identity(source) != identity:
            raise ValueError("Source identity changed during export preparation.")
        header = _header(export_id, request_sha256, copied, whole, blocks)
        if not sealed:
            _guard(authorize, cancelled)
            os.fsync(output.fileno())
            os.link(partial, complete)
            _unlink_owned(partial, os.fstat(output.fileno()))
            _sync_directory(files.directory)
        elif doubled:
            _unlink_owned(partial, os.fstat(output.fileno()))
            _sync_directory(files.directory)
    return header


def read_export_block(private_base, header, index, get_block, *, authorize=_noop, cancelled=_never):
    header = validate_header(header)
    expected = _record(get_block(index), index, _block_size(header, index))
    _guard(authorize, cancelled)
    path = PrivateFiles(private_base, "exports", header["export_id"]).path("payload")
    with _open(path, private=True) as stream:
        if os.fstat(stream.fileno()).st_size != header["size"]:
            raise ValueError("Export spool size changed.")
        stream.seek(index * BLOCK_SIZE)
        data = stream.read(expected["size"])
    if len(data) != expected["size"] or sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("Export block checksum changed.")
    return data


def _block_path(files, index):
    return files.path(f"block-{index:08d}")


def verify_stored_block(private_base, import_id, header, index, expected, *, authorize=_noop, cancelled=_never):
    header = validate_header(header)
    expected = _record(expected, index, _block_size(header, index))
    _guard(authorize, cancelled)
    files = PrivateFiles(private_base, "imports", import_id)
    try:
        with _open(_block_path(files, index), private=True) as stream:
            digest, size = sha256(), 0
            while size < expected["size"]:
                data = stream.read(min(64 * 1024, expected["size"] - size))
                if not data:
                    break
                size += len(data); digest.update(data)
            extra = stream.read(1)
    except FileNotFoundError:
        return False
    return not extra and size == expected["size"] and digest.hexdigest() == expected["sha256"]


def store_block(private_base, import_id, header, index, data, expected, *, checkpoint,
                cancelled=_never, authorize=_noop):
    header = validate_header(header)
    expected = _record(expected, index, _block_size(header, index))
    _guard(authorize, cancelled)
    if hasattr(data, "read"):
        stream = data
        data = stream.read(expected["size"])
        if stream.read(1):
            raise ValueError("Incoming block exceeds its expected size.")
    if not isinstance(data, bytes) or len(data) != expected["size"] or sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("Incoming block checksum or size differs from the manifest.")
    files = PrivateFiles(private_base, "imports", import_id)
    target = _block_path(files, index)
    if verify_stored_block(private_base, import_id, header, index, expected, authorize=authorize, cancelled=cancelled):
        with _open(target, write=True, private=True) as stream:
            os.fsync(stream.fileno())
        checkpoint(expected)
        return expected
    scratch = files.path(f".block-{index:08d}-{uuid4().hex}.tmp")
    with _open(scratch, write=True, create=True, private=True) as stream:
        owned = os.fstat(stream.fileno())
        try:
            _guard(authorize, cancelled)
            _write_all(stream, data); os.fsync(stream.fileno())
            _guard(authorize, cancelled)
            # Replacing a corrupt, already private block is deliberate; a link
            # or reparse replacement is rejected before publishing the repair.
            if os.path.lexists(target):
                with _open(target, private=True):
                    pass
            os.replace(scratch, target)
            _sync_directory(files.directory)
            checkpoint(expected)
        finally:
            if os.path.lexists(scratch):
                _unlink_owned(scratch, owned)
    return expected


def _check_content(stream, header, get_block, *, authorize, cancelled, output=None, checkpoint=None):
    whole, root, size = sha256(), sha256(), 0
    for index in range(header["block_count"]):
        _guard(authorize, cancelled)
        expected = _record(get_block(index), index, _block_size(header, index))
        data = stream(index, expected) if callable(stream) else stream.read(expected["size"])
        if len(data) != expected["size"] or sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Stored content block is missing or corrupt.")
        if output is not None:
            _write_all(output, data)
        whole.update(data); root.update(block_line(expected)); size += len(data)
        if checkpoint is not None:
            if output is not None:
                os.fsync(output.fileno())
            checkpoint(expected)
        del data
    if not callable(stream) and stream.read(1):
        raise ValueError("Stored content has an unexpected suffix.")
    if get_block(header["block_count"]) is not None:
        raise ValueError("Block metadata extends beyond the content header.")
    if size != header["size"] or whole.hexdigest() != header["sha256"] or root.hexdigest() != header["blocks_sha256"]:
        raise ValueError("Content checksum, size or block-root differs from the header.")


def _pending_target(import_id, target):
    target = Path(target)
    _directory(target.parent)
    if not target.name or target.name.casefold().startswith(STAGE_PREFIX):
        raise ValueError("Invalid authorized final filename.")
    return target, target.parent / (STAGE_PREFIX + ".large-" + _uuid(import_id))


def _target_digest(target):
    return sha256(os.fsencode(os.path.normcase(str(target)))).hexdigest()


class _PublicationDirectory:
    """Anchor links to the checked directory; Windows also prevents ancestor renames."""
    def __init__(self, directory):
        self.path = _directory(directory)
        self.info, self.fd, self.handles = self.path.lstat(), None, []

    def __enter__(self):
        try:
            if os.name == "nt":
                import win32con
                import win32file
                for directory in reversed((self.path, *self.path.parents)):
                    before = _safe(directory.lstat())
                    handle = win32file.CreateFile(str(directory), 0x80,
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
                        win32con.OPEN_EXISTING, 0x02200000, None)
                    self.handles.append(handle)  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                    if not _same_inode(before, _safe(directory.lstat())):
                        raise ValueError("Publication ancestor changed while acquiring its guard.")
            else:
                self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if not _same_inode(self.info, os.fstat(self.fd)):
                    raise ValueError("Publication directory changed while acquiring its guard.")
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd); self.fd = None
        for handle in reversed(self.handles):
            handle.Close()
        self.handles.clear()

    def check(self):
        _directory(self.path)
        if not _same_inode(self.info, _safe(self.path.lstat())):
            raise ValueError("Publication directory was replaced.")

    def name(self, path):
        return Path(path).name if self.fd is not None else path

    def exists(self, path):
        try:
            self.inspect(path)
            return True
        except FileNotFoundError:
            return False

    def inspect(self, path):
        return _safe(os.stat(self.name(path), dir_fd=self.fd, follow_symlinks=False))

    def open(self, path, **kwargs):
        return _open(self.name(path), dir_fd=self.fd, **kwargs)

    def link(self, source, target):
        self.check()
        if self.fd is not None:
            os.link(self.name(source), self.name(target), src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
        else:
            os.link(source, target)

    def unlink(self, path, info):
        self.check()
        _unlink_owned(self.name(path), info, dir_fd=self.fd)

    def sync(self):
        if self.fd is not None:
            os.fsync(self.fd)


def _owner(files, target):
    with _open(files.path("pending-owner.json"), private=True, max_links=2) as stream:
        info = os.fstat(stream.fileno())
        if info.st_nlink == 2:
            match = None
            with os.scandir(files.directory) as entries:
                for entry in entries:
                    if re.fullmatch(r"\.pending-owner-[0-9a-f]{32}\.tmp", entry.name) is None:
                        continue
                    # Windows DirEntry.stat caches zero file identifiers.
                    # Fetch actual no-follow metadata before the held-FD check.
                    current = _safe(os.stat(entry.path, follow_symlinks=False))
                    if _same_inode(info, current):
                        if match is not None:
                            raise ValueError("Ambiguous interrupted owner publication.")
                        match = files.path(entry.name)
            if match is None:
                raise ValueError("Pending owner has an unknown hard link.")
            with _open(match, private=True, max_links=2) as linked:
                if not _same_inode(info, os.fstat(linked.fileno())):
                    raise ValueError("Interrupted owner publication was replaced.")
                _unlink_owned(match, info)
            _sync_directory(files.directory)
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Oversized pending ownership receipt.")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate pending ownership field.")
            result[key] = value
        return result
    owner = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(owner, dict) or set(owner) != {"version", "dev", "ino", "target_sha256"}
            or type(owner["version"]) is not int or owner["version"] != 1
            or any(type(owner[key]) is not int or owner[key] < 0 for key in ("dev", "ino"))
            or owner["target_sha256"] != _target_digest(target)):
        raise ValueError("Pending ownership receipt differs from this destination.")
    return owner


def _publish_owner(files, target, info):
    value = {"version": 1, "dev": info.st_dev, "ino": info.st_ino, "target_sha256": _target_digest(target)}
    scratch = files.path(".pending-owner-" + uuid4().hex + ".tmp")
    with _open(scratch, write=True, create=True, private=True) as stream:
        owned = os.fstat(stream.fileno())
        try:
            _write_all(stream, json.dumps(value, sort_keys=True).encode("ascii")); os.fsync(stream.fileno())
            os.link(scratch, files.path("pending-owner.json"))
            _sync_directory(files.directory)
        finally:
            if os.path.lexists(scratch):
                _unlink_owned(scratch, owned)


def _owned_pending(files, target, info):
    owner = _owner(files, target)
    if (info.st_dev, info.st_ino) != (owner["dev"], owner["ino"]):
        raise ValueError("Refusing a replaced pending publication file.")


def finalize_import(private_base, import_id, header, target, *, get_block, authorize,
                    commit_point, cancelled=_never, checkpoint=None):
    """Publish without overwrite. The owner sidecar precedes all pending data I/O."""
    header = validate_header(header)
    _guard(authorize, cancelled)
    target, pending = _pending_target(import_id, target)
    files = PrivateFiles(private_base, "imports", import_id)
    with _PublicationDirectory(target.parent) as directory:
        return _finalize(files, import_id, header, target, pending, directory, get_block=get_block,
            authorize=authorize, commit_point=commit_point, cancelled=cancelled, checkpoint=checkpoint)


def _finalize(files, import_id, header, target, pending, directory, *, get_block, authorize, commit_point, cancelled, checkpoint):
    exists, published = directory.exists(pending), directory.exists(target)
    if published and not exists:
        raise FileExistsError("The destination already exists.")
    with directory.open(pending, write=not published, create=not exists) as output:
        info = os.fstat(output.fileno())
        if not exists:
            _publish_owner(files, target, info)
        _owned_pending(files, target, info)
        if published:
            with directory.open(target) as final:
                if not _same_inode(info, os.fstat(final.fileno())):
                    raise FileExistsError("The destination already contains another file.")
            _check_content(output, header, get_block, authorize=authorize, cancelled=cancelled)
            _guard(authorize, cancelled); commit_point()
            directory.check()
        else:
            _guard(authorize, cancelled)
            output.truncate(0)
            def read(index, expected):
                with _open(_block_path(files, index), private=True) as source:
                    value = source.read(expected["size"])
                    if source.read(1):
                        raise ValueError("Stored block has an unexpected suffix.")
                return value
            _check_content(read, header, get_block, authorize=authorize, cancelled=cancelled,
                           output=output, checkpoint=checkpoint)
            os.fsync(output.fileno())
            _guard(authorize, cancelled); commit_point()
            directory.check()
            _owned_pending(files, target, os.fstat(output.fileno()))
            if not _same_inode(directory.inspect(pending), info):
                raise ValueError("Pending publication file was replaced.")
            directory.link(pending, target)
            directory.sync()
        return dict(header, import_id=import_id, published=True,
                    target_identity={"dev": info.st_dev, "ino": info.st_ino})


def cleanup_pending(private_base, import_id, target):
    """Delete only the attested reserved pending link, never the final target."""
    target, pending = _pending_target(import_id, target)
    files = PrivateFiles(private_base, "imports", import_id)
    with _PublicationDirectory(target.parent) as directory:
        if directory.exists(pending):
            with directory.open(pending) as stream:
                info = os.fstat(stream.fileno())
                _owned_pending(files, target, info)
                directory.unlink(pending, info)
        elif os.path.lexists(files.path("pending-owner.json")):
            _owner(files, target)  # Recover a crash after unlink(pending), before unlink(owner).
        else:
            return True
        directory.sync()
    with _open(files.path("pending-owner.json"), private=True) as owner:
        _unlink_owned(files.path("pending-owner.json"), os.fstat(owner.fileno()))
    _sync_directory(files.directory)
    return True


def cleanup_private(private_base, kind, endpoint_id):
    """Remove only guarded staging files. Final and pending publication links are excluded."""
    files = PrivateFiles(private_base, kind, endpoint_id)
    if os.path.lexists(files.path("pending-owner.json")):
        raise ValueError("Pending publication cleanup must be confirmed first.")
    with os.scandir(files.directory) as entries:
        for entry in entries:
            with _open(files.path(entry.name), private=True):
                pass
    with os.scandir(files.directory) as entries:
        for entry in entries:
            path = files.path(entry.name)
            with _open(path, private=True) as stream:
                _unlink_owned(path, os.fstat(stream.fileno()))
    files.directory.rmdir()
    _sync_directory(files.parent)
