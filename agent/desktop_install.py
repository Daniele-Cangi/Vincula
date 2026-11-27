# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Owner-local approved desktop artifact installation. Never launches an engine/service."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop_backend as backend
import desktop_deskflow as deskflow
import desktop_security as security

PATCH_SHA256 = "70afdffdba804e5bc95644f7ac27b24f8815857426d6737577d3d3e47d18ea5a"
DEFAULT_PORT = 24807
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MANIFEST_NAME = "artifact-manifest.json"
# Directory mutation, deletion and security control; read/traverse/synchronize are harmless here.
PARENT_MUTATION_RIGHTS = (0x10000000 | 0x40000000 | 0x10000 | 0x40000 | 0x80000
                          | 0x40 | 0x2 | 0x4 | 0x10 | 0x100)


def require(value, message):
    if not value:
        raise ValueError(message)


def default_root():
    return (Path(os.environ["LOCALAPPDATA"]) / "Vincula-Desktop" if sys.platform == "win32"
            else Path.home() / ".local/share/vincula-desktop")


def host_target():
    family = "windows" if sys.platform == "win32" else "linux"
    require(sys.platform == "win32" or sys.platform.startswith("linux"), "Unsupported desktop platform.")
    architecture = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    require(architecture is not None, "Unsupported desktop architecture.")
    return family, architecture


def plain(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "Links and reparse points are not installation inputs.")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            "Use non-linked regular files and real directories.")
    return info.st_dev, info.st_ino


def absolute(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts
            and not any(ord(c) < 32 or ord(c) == 127 for c in str(path)), "Use literal absolute installation paths.")
    for parent in reversed(path.parents):
        plain(parent, directory=True)
    return path


def windows_sddl(sid):
    return (f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;S-1-5-18)"
            "(A;OICI;FA;;;S-1-5-32-544)S:(ML;OICI;NW;;;S-1-16-8192)")


def parent_ace_allowed(kind, flags, mask, trusted):
    if flags & 8 or kind == 1:  # INHERIT_ONLY does not apply to this parent; deny grants no rights.
        return True
    return kind == 0 and (trusted or not mask & PARENT_MUTATION_RIGHTS)


def parent_sid_trusted(sid, owner, trusted_sids):
    # OWNER RIGHTS denotes this object's current owner, not an additional principal.
    return owner in trusted_sids and (sid in trusted_sids or sid == "S-1-3-4")


class InstallerWindowsSecurity(security.WindowsSecurity):
    """Create new Medium objects; never relabel or repair existing storage."""
    def descriptor(self):
        result = ctypes.c_void_p()
        if not self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW(windows_sddl(self.user_sid), 1, ctypes.byref(result), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def create_directory(self, path):
        descriptor = self.descriptor()
        try:
            attributes = self.Attributes(ctypes.sizeof(self.Attributes), descriptor, False)
            if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.LocalFree(descriptor)

    def verify_parent(self, path):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        code = self.api.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                              ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            owner_sid = self.sid_string(owner)
            require(owner_sid in self.trusted and bool(dacl), "Unsafe installation parent owner or DACL.")
            count = ctypes.c_ushort.from_address(dacl.value + 4).value
            for index in range(count):
                ace = ctypes.c_void_p()
                if not self.api.GetAce(dacl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                kind = ctypes.c_ubyte.from_address(ace.value).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                if flags & 8 or kind == 1:
                    continue
                require(kind == 0, "Unsupported effective installation parent ACE.")
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                require(parent_ace_allowed(kind, flags, mask,
                                          parent_sid_trusted(self.sid_string(ace.value + 8), owner_sid, self.trusted)),
                        "The installation parent permits another SID to mutate children or security.")
        finally:
            self.kernel.LocalFree(descriptor)

    def verify(self, path):
        super().verify(path)
        sacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
        code = self.api.GetNamedSecurityInfoW(str(path), 1, 0x10, None, None, None, ctypes.byref(sacl), ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            # No label is effectively Medium. Explicit High/Low is not this storage contract.
            if sacl:
                count = ctypes.c_ushort.from_address(sacl.value + 4).value
                for index in range(count):
                    ace = ctypes.c_void_p()
                    if not self.api.GetAce(sacl, index, ctypes.byref(ace)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if ctypes.c_ubyte.from_address(ace.value).value == 17:
                        require(self.sid_string(ace.value + 8) == "S-1-16-8192"
                                and ctypes.c_uint32.from_address(ace.value + 4).value & 1,
                                "Desktop storage must be usable at Medium integrity with no-write-up.")
        finally:
            self.kernel.LocalFree(descriptor)

    def open(self, path, *, create=False, write=False):
        from ctypes import wintypes
        import msvcrt
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(self.Attributes), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        descriptor = self.descriptor() if create else None
        attributes = self.Attributes(ctypes.sizeof(self.Attributes), descriptor, False) if create else None
        try:
            handle = self.kernel.CreateFileW(str(path), 0xC0000000 if create or write else 0x80000000, 7,
                ctypes.byref(attributes) if attributes is not None else None, 1 if create else 3, 0x200080, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return msvcrt.open_osfhandle(handle, (os.O_RDWR if create or write else os.O_RDONLY) | os.O_BINARY)
            except BaseException:
                self.kernel.CloseHandle(handle)
                raise
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)


class Store:
    def __init__(self):
        self.windows = InstallerWindowsSecurity() if sys.platform == "win32" else None

    def verify(self, path, *, directory=False):
        identity = plain(path, directory=directory)
        if self.windows:
            self.windows.verify(path)
        else:
            info = path.lstat()
            require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "Existing desktop storage must be owner-private; permissions were not changed.")
        return identity

    def parent(self, path):
        identity = plain(path, directory=True)
        if self.windows:
            self.windows.verify_parent(path)
        else:
            info = path.lstat()
            require(info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                    "The installation parent must not be writable by another account.")
        return identity

    def mkdir(self, path):
        if self.windows:
            # Parent must already exist: WindowsSecurity must not create ancestors implicitly.
            plain(path.parent, directory=True)
            self.windows.create_directory(path)
        else:
            path.mkdir(mode=0o700)
        return self.verify(path, directory=True)

    def open(self, path, *, create=False, write=False):
        if self.windows:
            descriptor = self.windows.open(path, create=create, write=write)
        else:
            flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL) if create else os.O_RDWR if write else os.O_RDONLY
            descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and not getattr(info, "st_file_attributes", 0) & 0x400
                    and (info.st_dev, info.st_ino) == plain(path), "An installation file changed while opening.")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise


def input_descriptor(path):
    if sys.platform == "win32":
        return Store().open(path)
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


def read_file(path, limit=1024 * 1024):
    before = plain(path)
    descriptor = input_descriptor(path)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (info.st_dev, info.st_ino) == before,
                "A package file changed before reading.")
        raw = stream.read(limit + 1)
    require(len(raw) <= limit and plain(path) == before, "Oversized or replaced installation file.")
    return raw


def inventory(root, filenames):
    allowed = set(filenames)
    directories = {"/".join(name.split("/")[:i]) for name in allowed for i in range(1, len(name.split("/")))}
    found, spellings = set(), {}
    def walk(path, prefix=""):
        plain(path, directory=True)
        with os.scandir(path) as entries:
            for entry in entries:
                name = prefix + entry.name
                key = name.casefold()
                require(key not in spellings or spellings[key] == name, "Case aliases are not supported in an artifact.")
                spellings[key] = name
                if entry.is_dir(follow_symlinks=False):
                    require(name in directories, "Unlisted package directory.")
                    walk(Path(entry.path), name + "/")
                else:
                    plain(Path(entry.path))
                    require(name in allowed, "Unlisted package file.")
                    found.add(name)
    walk(root)
    require(found == allowed, "The package is missing manifest files.")


def file_hash(path, *, sink=None):
    before = plain(path)
    digest, total = hashlib.sha256(), 0
    descriptor = input_descriptor(path)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (info.st_dev, info.st_ino) == before, "Package input changed.")
        for block in iter(lambda: source.read(65536), b""):
            total += len(block)
            require(total <= MAX_FILE_BYTES, "Artifact file exceeds its size limit.")
            digest.update(block)
            if sink is not None:
                sink.write(block)
    require(plain(path) == before, "Package input was replaced.")
    return digest.hexdigest(), total


def engine_header(path, family, architecture):
    raw = read_file_header(path)
    if family == "linux":
        valid = len(raw) >= 20 and raw[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", raw, 18)[0] == {"x86_64": 62, "aarch64": 183}[architecture]
    else:
        offset = struct.unpack_from("<I", raw, 60)[0] if len(raw) >= 64 else len(raw)
        valid = raw[:2] == b"MZ" and 64 <= offset <= len(raw) - 6 and raw[offset:offset+4] == b"PE\0\0" and struct.unpack_from("<H", raw, offset + 4)[0] == {"x86_64": 0x8664, "aarch64": 0xAA64}[architecture]
    require(valid, "Engine header does not match the approved platform/architecture.")


def read_file_header(path):
    # PE header is bounded independently from the (possibly large) executable.
    before = plain(path)
    with os.fdopen(input_descriptor(path), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (info.st_dev, info.st_ino) == before,
                "Engine input changed before reading.")
        raw = stream.read(1024 * 1024)
    require(plain(path) == before, "Engine input changed.")
    return raw


def existing_json(path, expected, store):
    if not os.path.lexists(path):
        return False
    store.verify(path)
    raw = read_file(path)
    require(json.loads(raw, object_pairs_hook=backend._unique_object) == expected,
            "Existing desktop metadata differs; it was not overwritten.")
    return True


class Installer:
    def __init__(self, package, manifest_file, approved_sha256, *, enabled, port=DEFAULT_PORT):
        require(enabled is True, "The owner must explicitly enable the optional desktop deployment.")
        require(type(port) is int and 1024 <= port <= 65535, "Select an unprivileged TCP port.")
        self.package, self.manifest_file = absolute(package), absolute(manifest_file)
        plain(self.package, directory=True)
        self.raw = read_file(self.manifest_file)
        self.artifact = backend.verify_artifact_manifest(self.raw, approved_sha256=approved_sha256)
        manifest = self.artifact.manifest
        require((manifest.platform, manifest.architecture) == host_target(), "The artifact targets another platform/architecture.")
        require((manifest.backend, manifest.upstream_repository, manifest.upstream_commit, manifest.version, manifest.patch_sha256)
                == ("deskflow", deskflow.UPSTREAM_REPOSITORY, deskflow.UPSTREAM_COMMIT, "1.27.0", PATCH_SHA256)
                and deskflow.MANAGED_CAPABILITIES <= set(manifest.capabilities), "Install the pinned managed Deskflow source and required capabilities.")
        self.root = absolute(default_root())
        self.version = self.root / "engines" / approved_sha256
        self.files_root = self.version / "files"
        self.manifest_target = self.version / MANIFEST_NAME
        self.engine = self.files_root / manifest.engine_file
        self.approval_path = self.root / "engines/approved" / (dict(manifest.files)[manifest.engine_file] + ".json")
        self.approval = {"protocol": 1, "binary_sha256": dict(manifest.files)[manifest.engine_file], "executable": str(self.engine), "artifact_manifest_sha256": approved_sha256}
        self.deployment = {"protocol": 1, "artifact_root": str(self.files_root), "artifact_manifest_file": str(self.manifest_target), "artifact_manifest_sha256": approved_sha256, "port": port, "enabled": True}
        configured = os.environ.get("VINCULA_DESKTOP_ARTIFACT_ROOT")
        require(not configured or Path(configured) == self.approval_path.parent, "The configured approval store differs from the installer store.")
        self.store = Store()

    def package_check(self):
        names = {name for name, _ in self.artifact.manifest.files}
        if self.manifest_file.is_relative_to(self.package):
            names.add(self.manifest_file.relative_to(self.package).as_posix())
        inventory(self.package, names)
        total = 0
        for name, expected in self.artifact.manifest.files:
            digest, size = file_hash(self.package / name)
            require(digest == expected, "Artifact executable/dependency digest mismatch.")
            total += size
            require(total <= MAX_TOTAL_BYTES, "Artifact package exceeds its total size limit.")
        engine_header(self.package / self.artifact.manifest.engine_file, *host_target())
        return total

    def installed_check(self):
        self.store.verify(self.version, directory=True)
        self.store.verify(self.files_root, directory=True)
        self.store.verify(self.manifest_target)
        require(read_file(self.manifest_target) == self.raw, "Installed manifest differs.")
        inventory(self.version, {MANIFEST_NAME, *("files/" + name for name, _ in self.artifact.manifest.files)})
        for name, expected in self.artifact.manifest.files:
            path = self.files_root / name
            for parent in path.parents:
                if parent.is_relative_to(self.version):
                    self.store.verify(parent, directory=True)
            self.store.verify(path)
            require(file_hash(path)[0] == expected, "Installed artifact file changed.")
            if sys.platform != "win32" and name == self.artifact.manifest.engine_file:
                require(path.stat().st_mode & 0o100, "Installed engine is not owner-executable.")

    def plan(self):
        total = self.package_check()
        self.store.parent(self.root.parent)
        changes = []
        if os.path.lexists(self.root):
            self.store.verify(self.root, directory=True)
        for path in (self.root / "engines", self.approval_path.parent):
            if os.path.lexists(path):
                self.store.verify(path, directory=True)
        if os.path.lexists(self.version):
            self.installed_check()
        else:
            changes.append(str(self.version))
        for path, value in ((self.approval_path, self.approval), (self.root / "deployment.json", self.deployment)):
            if not existing_json(path, value, self.store):
                changes.append(str(path))
        if (self.root / "deployment.json").exists():
            require(self.version.exists() and self.approval_path.exists(),
                    "An existing deployment is incomplete; inspect it instead of repairing it implicitly.")
        return {"ok": True, "artifact_manifest_sha256": self.artifact.manifest_sha256, "binary_sha256": self.approval["binary_sha256"],
                "root": str(self.root), "changes": changes, "total_file_bytes": total, "already_installed": not changes,
                "deployment": self.deployment, "desktop_ready": False, "engine_started": False, "service_installed": False}

    @contextmanager
    def lock(self):
        path = self.root / ".install.lock"
        try:
            descriptor = self.store.open(path, create=True)
        except FileExistsError:
            self.store.verify(path)
            descriptor = self.store.open(path, write=True)
        with os.fdopen(descriptor, "r+b") as stream:
            self.store.verify(path)
            if not path.stat().st_size:
                stream.write(b"\0");stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def install(self):
        self.plan()  # Validate the complete package before any mutation.
        if not self.root.exists():
            plain(self.root.parent, directory=True)
            self.store.mkdir(self.root)
        with self.lock(), ExitStack() as pins:
            self.plan()
            created, directories = {}, []
            committed = False
            parent_identity = self.store.parent(self.root.parent)
            directory_ids = {self.root: plain(self.root, directory=True)}
            def guard():
                require(self.store.parent(self.root.parent) == parent_identity, "Installation parent changed.")
                for parent, identity in directory_ids.items():
                    require(self.store.verify(parent, directory=True) == identity, "Installation parent changed.")
            def mkdir(path):
                if path.exists():
                    self.store.verify(path, directory=True)
                else:
                    directories.append((path, self.store.mkdir(path)))
                directory_ids[path] = plain(path, directory=True)
            def publish(path, raw=None, source=None, executable=False):
                nonlocal committed
                guard()
                stage = path.with_name(".install-" + uuid4().hex)
                descriptor = self.store.open(stage, create=True)
                pins.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                identity = info.st_dev, info.st_ino
                created[stage] = (identity, descriptor, None)
                with os.fdopen(os.dup(descriptor), "wb") as target:
                    if source is None:
                        target.write(raw)
                        expected = hashlib.sha256(raw).hexdigest()
                    else:
                        expected = dict(self.artifact.manifest.files)[source.relative_to(self.package).as_posix()]
                        require(file_hash(source, sink=target)[0] == expected, "Package changed during copying.")
                    target.flush();os.fsync(target.fileno())
                if executable and sys.platform != "win32":
                    os.fchmod(descriptor, 0o700)
                created[stage] = (identity, descriptor, expected)
                require(file_hash(stage)[0] == expected, "Staged artifact changed.")
                guard()
                os.link(stage, path)  # Atomic no-clobber publication on this same filesystem.
                created[path] = (identity, descriptor, expected)
                stage.unlink()
                del created[stage]
                if path == self.root / "deployment.json":
                    committed = True  # Published deployment may now be consumed; never roll it back blindly.
                self.store.verify(path)
                if sys.platform != "win32":
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            try:
                mkdir(self.root / "engines");mkdir(self.approval_path.parent)
                if not self.version.exists():
                    mkdir(self.version);mkdir(self.files_root)
                    for name, _ in self.artifact.manifest.files:
                        path = self.files_root / name
                        for parent in reversed(path.parents):
                            if parent.is_relative_to(self.files_root):
                                mkdir(parent)
                        publish(path, source=self.package / name, executable=name == self.artifact.manifest.engine_file)
                    publish(self.manifest_target, raw=self.raw)
                self.installed_check()
                if not existing_json(self.approval_path, self.approval, self.store):
                    publish(self.approval_path, raw=backend.canonical_json(self.approval))
                result = self.plan()  # Complete all fallible package/integrity checks before activation.
                if not existing_json(self.root / "deployment.json", self.deployment, self.store):
                    publish(self.root / "deployment.json", raw=backend.canonical_json(self.deployment))
                result["installed"] = True
                result["already_installed"], result["changes"] = True, []
                return result
            except BaseException:
                if committed:
                    raise  # Local inspection reconciles a commit with a missing final durability receipt.
                # Held descriptors pin original inodes. Never recursively remove foreign additions.
                for path, (identity, descriptor, digest) in reversed(tuple(created.items())):
                    try:
                        guard()
                        require(plain(path) == identity and (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) == identity, "Cleanup identity changed.")
                        require(digest is not None and file_hash(path)[0] == digest, "Cleanup content changed.")
                        self.store.verify(path)
                        path.unlink()
                    except (OSError, ValueError):
                        pass
                for path, identity in reversed(directories):
                    try:
                        require(plain(path, directory=True) == identity, "Cleanup directory changed.")
                        path.rmdir()
                    except (OSError, ValueError):
                        pass
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "install"))
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--enable", action="store_true", required=True)
    args = parser.parse_args()
    try:
        installer = Installer(args.package, args.manifest, args.approved_sha256, enabled=args.enable, port=args.port)
        result = installer.plan() if args.action == "plan" else installer.install()
    except (OSError, ValueError) as error:
        result = {"ok": False, "error_type": type(error).__name__, "message": str(error), "desktop_ready": False}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
