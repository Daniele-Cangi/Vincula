# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure desktop backend contracts. These objects do not authorize or launch software."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
from uuid import UUID

DIGEST = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
WINDOWS_RESERVED = re.compile(r"(?:CON|CONIN\$|CONOUT\$|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", re.IGNORECASE)


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Use a canonical UUID.")
    return value


def valid_digest(value):
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ValueError("Use a lowercase SHA256 digest.")
    return value


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True)
class Screen:
    node_id: str
    x: int
    y: int

    def __post_init__(self):
        canonical_uuid(self.node_id)
        if any(type(value) is not int or not -64 <= value <= 64 for value in (self.x, self.y)):
            raise ValueError("Use integer desktop grid positions between -64 and 64.")


def _member_map(values, members):
    if (not isinstance(values, tuple) or len(values) != len(members)
            or any(not isinstance(row, tuple) or len(row) != 2 for row in values)):
        raise ValueError("Use immutable UUID/digest pairs.")
    for node, digest in values:
        canonical_uuid(node)
        valid_digest(digest)
    if len(values) != len(members) or {node for node, _ in values} != members:
        raise ValueError("Supply exactly one digest per desktop member.")
    return tuple(sorted(values))


def _connected(screens, primary):
    positions = {(screen.x, screen.y): screen.node_id for screen in screens}
    by_id = {screen.node_id: screen for screen in screens}
    reached, pending = set(), [primary]
    while pending:
        node = pending.pop()
        if node in reached:
            continue
        reached.add(node)
        screen = by_id[node]
        pending.extend(positions[position] for position in
                       ((screen.x - 1, screen.y), (screen.x + 1, screen.y),
                        (screen.x, screen.y - 1), (screen.x, screen.y + 1)) if position in positions)
    return reached


@dataclass(frozen=True)
class GroupManifest:
    """Common public session identity; local paths and credentials are deliberately absent."""

    session_id: str
    primary: str
    screens: tuple[Screen, ...]
    clipboard: bool
    tls_pins: tuple[tuple[str, str], ...]
    revisions: tuple[tuple[str, str], ...]

    def __post_init__(self):
        canonical_uuid(self.session_id)
        canonical_uuid(self.primary)
        if (not isinstance(self.screens, tuple) or not 2 <= len(self.screens) <= 64
                or not all(isinstance(screen, Screen) for screen in self.screens) or type(self.clipboard) is not bool):
            raise ValueError("Supply two to 64 desktop screens and an explicit clipboard boolean.")
        members = {screen.node_id for screen in self.screens}
        positions = {(screen.x, screen.y): screen.node_id for screen in self.screens}
        if len(members) != len(self.screens) or len(positions) != len(self.screens) or self.primary not in members:
            raise ValueError("Desktop UUIDs/grid cells must be unique and include the primary.")
        if _connected(self.screens, self.primary) != members:
            raise ValueError("Desktop screens must form a connected grid.")
        pins = _member_map(self.tls_pins, members)
        if len({digest for _, digest in pins}) != len(pins):
            raise ValueError("Each node needs a distinct enrolled TLS certificate.")
        object.__setattr__(self, "screens", tuple(sorted(self.screens, key=lambda screen: screen.node_id)))
        object.__setattr__(self, "tls_pins", pins)
        object.__setattr__(self, "revisions", _member_map(self.revisions, members))

    def to_document(self):
        return {"schema_version": 1, "session_id": self.session_id, "primary": self.primary,
                "members": [{"node_id": screen.node_id, "x": screen.x, "y": screen.y} for screen in self.screens],
                "clipboard": self.clipboard, "tls_pins": dict(self.tls_pins), "revisions": dict(self.revisions)}

    @property
    def digest(self):
        return hashlib.sha256(canonical_json(self.to_document())).hexdigest()

    @classmethod
    def from_document(cls, value):
        fields = {"schema_version", "session_id", "primary", "members", "clipboard", "tls_pins", "revisions"}
        if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or not isinstance(value["members"], list)
                or not 2 <= len(value["members"]) <= 64
                or not isinstance(value["tls_pins"], dict) or not isinstance(value["revisions"], dict)
                or len(value["tls_pins"]) != len(value["members"])
                or len(value["revisions"]) != len(value["members"])):
            raise ValueError("Invalid common desktop manifest schema.")
        screens = []
        for row in value["members"]:
            if not isinstance(row, dict) or set(row) != {"node_id", "x", "y"}:
                raise ValueError("Invalid common desktop member.")
            screens.append(Screen(**row))
        return cls(value["session_id"], value["primary"], tuple(screens), value["clipboard"],
                   tuple(value["tls_pins"].items()), tuple(value["revisions"].items()))


def validate_windows_components(parts):
    """Reject Win32 aliases rather than silently normalizing them in a preview."""
    for part in parts:
        if (not part or part in {".", ".."} or part[-1] in {" ", "."}
                or any(char in part for char in '<>:"|?*')
                or WINDOWS_RESERVED.fullmatch(part.split(".", 1)[0].rstrip(" ")) is not None):
            raise ValueError("Unsupported Windows path component.")


def relative_artifact_file(value, platform):
    if (not isinstance(value, str) or not 1 <= len(value) <= 240 or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} or re.fullmatch(r"[A-Za-z0-9_.+-]+", part) is None for part in value.split("/"))):
        raise ValueError("Artifact files must be simple relative POSIX paths.")
    if platform == "windows":
        validate_windows_components(value.split("/"))
    return value


def _source_metadata(repository, commit, version, patch):
    if (not isinstance(repository, str) or not repository.startswith("https://")
            or len(repository) > 200 or any(ord(char) <= 32 or ord(char) == 127 for char in repository)
            or not isinstance(commit, str) or COMMIT.fullmatch(commit) is None):
        raise ValueError("Pin the upstream repository and full commit.")
    url = urlsplit(repository)
    if not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError("Use a public HTTPS repository URL without credentials or query data.")
    if not isinstance(version, str) or re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", version) is None:
        raise ValueError("Invalid artifact version.")
    if patch is not None:
        valid_digest(patch)


def _artifact_files(files, engine_file, platform):
    relative_artifact_file(engine_file, platform)
    if not isinstance(files, tuple) or not 1 <= len(files) <= 512:
        raise ValueError("Supply a bounded artifact file manifest.")
    for row in files:
        if not isinstance(row, tuple) or len(row) != 2:
            raise ValueError("Invalid artifact file entry.")
        relative_artifact_file(row[0], platform)
        valid_digest(row[1])
    if len(dict(files)) != len(files) or engine_file not in dict(files):
        raise ValueError("Artifact file names must be unique and include the engine.")
    names = [name.casefold() if platform == "windows" else name for name, _ in files]
    if len(set(names)) != len(names):
        raise ValueError("Artifact files must not alias on the destination platform.")
    named = set(names)
    for name in names:
        if any("/".join(name.split("/")[:count]) in named for count in range(1, len(name.split("/")))):
            raise ValueError("An artifact file cannot also be a directory.")
    return tuple(sorted(files))


def _capabilities(values):
    if (not isinstance(values, tuple) or len(values) > 32
            or any(not isinstance(name, str) or NAME.fullmatch(name) is None for name in values)
            or len(set(values)) != len(values)):
        raise ValueError("Invalid artifact capabilities.")
    return tuple(sorted(values))


@dataclass(frozen=True)
class ArtifactManifest:
    backend: str
    upstream_repository: str
    upstream_commit: str
    version: str
    patch_sha256: str | None
    platform: str
    architecture: str
    engine_file: str
    files: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.backend, str) or NAME.fullmatch(self.backend) is None:
            raise ValueError("Invalid backend identifier.")
        _source_metadata(self.upstream_repository, self.upstream_commit, self.version, self.patch_sha256)
        if (not isinstance(self.platform, str) or self.platform not in {"linux", "windows"}
                or not isinstance(self.architecture, str) or self.architecture not in {"x86_64", "aarch64"}):
            raise ValueError("Unsupported artifact platform/architecture.")
        object.__setattr__(self, "files", _artifact_files(self.files, self.engine_file, self.platform))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))

    def to_document(self):
        return {"schema_version": 1, "backend": self.backend, "upstream_repository": self.upstream_repository,
                "upstream_commit": self.upstream_commit, "version": self.version, "patch_sha256": self.patch_sha256,
                "platform": self.platform, "architecture": self.architecture, "engine_file": self.engine_file,
                "files": dict(self.files), "capabilities": list(self.capabilities)}


@dataclass(frozen=True)
class VerifiedArtifact:
    """Integrity checked against a pin supplied by the authorized control plane, not by the manifest."""

    manifest: ArtifactManifest
    manifest_sha256: str

    def __post_init__(self):
        if not isinstance(self.manifest, ArtifactManifest):
            raise ValueError("Expected an artifact manifest.")
        valid_digest(self.manifest_sha256)
        if hashlib.sha256(canonical_json(self.manifest.to_document())).hexdigest() != self.manifest_sha256:
            raise ValueError("Verified artifact fields do not match its canonical manifest digest.")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field.")
        result[name] = value
    return result


def verify_artifact_manifest(raw, *, approved_sha256):
    """No files are opened. The approval pin must come from outside the supplied bytes."""
    valid_digest(approved_sha256)
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 1024 * 1024:
        raise ValueError("Supply bounded artifact manifest bytes.")
    if hashlib.sha256(raw).hexdigest() != approved_sha256:
        raise ValueError("Artifact manifest differs from the externally approved digest.")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    expected = {"schema_version", "backend", "upstream_repository", "upstream_commit", "version", "patch_sha256",
                "platform", "architecture", "engine_file", "files", "capabilities"}
    if (not isinstance(value, dict) or set(value) != expected or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or not isinstance(value["files"], dict) or not isinstance(value["capabilities"], list)):
        raise ValueError("Invalid artifact manifest schema.")
    if raw != canonical_json(value):
        raise ValueError("Artifact v1 requires canonical JSON bytes.")
    fields = {name: value[name] for name in expected - {"schema_version", "files", "capabilities"}}
    return VerifiedArtifact(ArtifactManifest(**fields, files=tuple(value["files"].items()),
                                             capabilities=tuple(value["capabilities"])), approved_sha256)


@dataclass(frozen=True)
class PreflightCheck:
    code: str
    status: str

    def __post_init__(self):
        if not isinstance(self.code, str) or NAME.fullmatch(self.code) is None:
            raise ValueError("Invalid preflight check identifier.")
        if not isinstance(self.status, str) or self.status not in {"ok", "missing", "unsupported"}:
            raise ValueError("Invalid preflight status.")


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    def __post_init__(self):
        if (not isinstance(self.checks, tuple) or not 1 <= len(self.checks) <= 64
                or not all(isinstance(check, PreflightCheck) for check in self.checks)
                or len({check.code for check in self.checks}) != len(self.checks)):
            raise ValueError("Use a bounded immutable set of distinct preflight checks.")

    def to_document(self):
        return {"configuration_valid": self.configuration_valid, "launchable": False, "desktop_ready": False,
                "checks": [{"code": check.code, "status": check.status} for check in self.checks]}

    @property
    def configuration_valid(self):
        return all(check.status == "ok" for check in self.checks)

    @property
    def launchable(self):
        # No managed engine/guardian plumbing exists in this source-only increment.
        return False

    @property
    def desktop_ready(self):
        return False
