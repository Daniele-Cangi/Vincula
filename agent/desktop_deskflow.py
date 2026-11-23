# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure Deskflow 1.27 profile previews; no engine or profile is opened or launched."""

from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import PurePosixPath, PureWindowsPath
import re

from desktop_backend import (GroupManifest, PreflightCheck, PreflightReport, VerifiedArtifact,
                             canonical_uuid, relative_artifact_file, valid_digest, validate_windows_components)

UPSTREAM_REPOSITORY = "https://github.com/deskflow/deskflow"
UPSTREAM_COMMIT = "081f6478e654a112168879e67a41ce7d7e4ae3af"
MANAGED_CAPABILITIES = frozenset({"managed_profile_v1", "private_attestation_v1", "mutual_tls_uuid_v1", "backend_consent_v1"})
DIRECTIONS = (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1))
SETTINGS_FILENAME = "Deskflow.conf"
LAYOUT_FILENAME = "layout.conf"


def _windows_path(value, path, parts):
    if re.fullmatch(r"[A-Za-z]:", path.drive) is None or any(char in value for char in ('"', '%', '!')):
        raise ValueError("Use a local Windows drive path without expansions or device syntax.")
    validate_windows_components(part for part in parts[1:] if part)


def _path(value, platform):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Use an absolute destination-local path without control characters.")
    path = PureWindowsPath(value) if platform == "windows" else PurePosixPath(value)
    raw_parts = value.replace("\\", "/").split("/") if platform == "windows" else value.split("/")
    if not path.is_absolute() or any(part in {".", ".."} for part in raw_parts):
        raise ValueError("Use an absolute destination-local path without traversal.")
    if platform == "windows":
        _windows_path(value, path, raw_parts)
    elif value.startswith("//"):
        raise ValueError("Use an unambiguous local POSIX root.")
    return path


@dataclass(frozen=True)
class LocalPaths:
    platform: str
    artifact_root: str
    profile_root: str
    certificate_reference: str

    def __post_init__(self):
        if not isinstance(self.platform, str) or self.platform not in {"windows", "linux"}:
            raise ValueError("Unsupported destination platform.")
        for value in (self.artifact_root, self.profile_root, self.certificate_reference):
            _path(value, self.platform)
        artifact, profile = (_path(value, self.platform) for value in (self.artifact_root, self.profile_root))
        if artifact == profile or artifact in profile.parents or profile in artifact.parents:
            raise ValueError("The session profile and immutable artifact must use separate roots.")


@dataclass(frozen=True)
class NetworkProfile:
    bind_address: str
    port: int
    primary_address: str | None = None

    def __post_init__(self):
        if not isinstance(self.bind_address, str) or self.primary_address is not None and not isinstance(self.primary_address, str):
            raise ValueError("Use literal IPv4 strings, not integer address aliases.")
        try:
            bind = IPv4Address(self.bind_address)
            primary = None if self.primary_address is None else IPv4Address(self.primary_address)
        except (ValueError, TypeError) as error:
            raise ValueError("This adapter profile requires literal IPv4 addresses.") from error
        broadcast = IPv4Address("255.255.255.255")
        if bind.is_multicast or bind == broadcast or type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Use a unicast bind and an explicit TCP port.")
        if primary is not None and (primary.is_unspecified or primary.is_multicast or primary == broadcast):
            raise ValueError("Use one concrete primary IPv4 address.")


def _profile_entries(files, role):
    if (not isinstance(files, tuple) or not 3 <= len(files) <= 4
            or any(not isinstance(row, tuple) or len(row) != 2
                   or not all(isinstance(value, str) for value in row) for row in files)):
        raise ValueError("Use immutable in-memory profile entries.")
    expected = {SETTINGS_FILENAME, "tls/trusted-clients", "tls/trusted-servers"}
    if role == "primary":
        expected.add(LAYOUT_FILENAME)
    if len(files) != len(expected) or {name for name, _ in files} != expected:
        raise ValueError("Unexpected managed profile file selection.")


@dataclass(frozen=True)
class PreparedProfile:
    node_id: str
    session_id: str
    common_manifest_sha256: str
    role: str
    argv: tuple[str, ...]
    files: tuple[tuple[str, str], ...]

    def __post_init__(self):
        canonical_uuid(self.node_id)
        canonical_uuid(self.session_id)
        valid_digest(self.common_manifest_sha256)
        if not isinstance(self.role, str) or self.role not in {"primary", "client"}:
            raise ValueError("Invalid desktop role.")
        if (not isinstance(self.argv, tuple) or len(self.argv) != 4
                or not all(isinstance(value, str) for value in self.argv)
                or self.argv[1:3] != ("server" if self.role == "primary" else "client", "--settings")):
            raise ValueError("Use the fixed Deskflow role/settings invocation.")
        _profile_entries(self.files, self.role)

    @property
    def launchable(self):
        return False

    @property
    def desktop_ready(self):
        return False


def _observed_hashes(values, platform):
    if not isinstance(values, tuple) or len(values) > 512:
        raise ValueError("Use a bounded immutable observed file map.")
    for row in values:
        if not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[0], str):
            raise ValueError("Invalid observed file hash.")
        relative_artifact_file(row[0], platform)
        valid_digest(row[1])
    if len(dict(values)) != len(values):
        raise ValueError("Duplicate observed file name.")


@dataclass(frozen=True)
class PreflightFacts:
    """Facts measured elsewhere by the authorized destination; never supplied by the artifact itself."""

    node_id: str
    artifact_root: str
    platform: str
    architecture: str
    profile_root: str
    certificate_reference: str
    graphics: str | None = None
    observed_files: tuple[tuple[str, str], ...] | None = None
    executable_usable: bool | None = None
    private_profile_available: bool | None = None
    certificate_sha256: str | None = None
    private_certificate_access: bool | None = None
    interactive_session: bool | None = None
    network_authorized: bool | None = None
    owner_input_consent: bool | None = None
    owner_clipboard_consent: bool | None = None

    def __post_init__(self):
        canonical_uuid(self.node_id)
        if (not isinstance(self.platform, str) or self.platform not in {"windows", "linux"}
                or not isinstance(self.architecture, str) or self.architecture not in {"x86_64", "aarch64"}):
            raise ValueError("Unsupported measured platform/architecture.")
        LocalPaths(self.platform, self.artifact_root, self.profile_root, self.certificate_reference)
        if self.graphics is not None and (not isinstance(self.graphics, str) or self.graphics not in {"windows", "wayland", "x11"}):
            raise ValueError("Invalid measured graphical backend.")
        if self.certificate_sha256 is not None:
            valid_digest(self.certificate_sha256)
        for value in (self.executable_usable, self.private_profile_available, self.private_certificate_access,
                      self.interactive_session, self.network_authorized, self.owner_input_consent, self.owner_clipboard_consent):
            if value is not None and type(value) is not bool:
                raise ValueError("Measured prerequisites must be booleans or unknown.")
        if self.observed_files is not None:
            _observed_hashes(self.observed_files, self.platform)


def _ini_string(value):
    # Qt6 INI uses UTF8; quote scalar strings and escape INI backslashes/quotes.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _layout(group):
    positions = {(screen.x, screen.y): screen.node_id for screen in group.screens}
    names = {screen.node_id: "vincula-" + screen.node_id for screen in group.screens}
    lines = ["section: screens", *(" " + names[screen.node_id] + ":" for screen in group.screens), "end", "section: links"]
    for screen in group.screens:
        lines.append(" " + names[screen.node_id] + ":")
        for direction, dx, dy in DIRECTIONS:
            target = positions.get((screen.x + dx, screen.y + dy))
            if target is not None:
                lines.append("  " + direction + " = " + names[target])
    return "\n".join([*lines, "end", ""])


def _known_source(manifest):
    return (manifest.backend == "deskflow" and manifest.upstream_repository == UPSTREAM_REPOSITORY
            and manifest.upstream_commit == UPSTREAM_COMMIT and manifest.version == "1.27.0")


def _compile_inputs(group, node_id, artifact, paths, network):
    canonical_uuid(node_id)
    if not isinstance(group, GroupManifest) or node_id not in dict(group.tls_pins):
        raise ValueError("Compile only a member of the common manifest.")
    if not isinstance(artifact, VerifiedArtifact) or not isinstance(paths, LocalPaths) or not isinstance(network, NetworkProfile):
        raise ValueError("Use explicit verified artifact and local profile inputs.")
    if not _known_source(artifact.manifest) or artifact.manifest.platform != paths.platform:
        raise ValueError("Artifact does not match the local Deskflow platform.")
    primary = node_id == group.primary
    if (primary and network.primary_address is not None) or (not primary and network.primary_address is None):
        raise ValueError("Only a client must specify exactly one primary address.")
    if not primary and IPv4Address(network.bind_address).is_unspecified:
        raise ValueError("A client needs a concrete authorized source address.")
    return primary


def _base_settings(node_id, paths, network):
    certificate = _path(paths.certificate_reference, paths.platform).as_posix()
    sections = ["[core]", "computerName=" + _ini_string("vincula-" + node_id),
                "interface=" + _ini_string(network.bind_address), "port=" + str(network.port),
                "enableEnterCommand=false", "enableExitCommand=false"]
    if paths.platform == "windows":
        sections.append("useHooks=true")
    return [*sections, "", "[security]", "tlsEnabled=true", "checkPeerFingerprints=true",
            "certificate=" + _ini_string(certificate), "", "[log]", "toFile=false"]


def _trust_files(group, node_id):
    pins = dict(group.tls_pins)
    primary = node_id == group.primary
    clients = "".join("v2:sha256:" + pins[screen.node_id] + "\n" for screen in group.screens if screen.node_id != node_id)
    return {"tls/trusted-servers": "" if primary else "v2:sha256:" + pins[group.primary] + "\n",
            "tls/trusted-clients": clients if primary else ""}


def compile_profile(group, node_id, artifact, paths, network):
    """Return in-memory files/argv using only verified upstream flags. This is never a launch authorization."""
    primary = _compile_inputs(group, node_id, artifact, paths, network)
    profile = _path(paths.profile_root, paths.platform)
    engine = _path(paths.artifact_root, paths.platform).joinpath(*artifact.manifest.engine_file.split("/"))
    settings = profile / SETTINGS_FILENAME
    sections, files = _base_settings(node_id, paths, network), _trust_files(group, node_id)
    if primary:
        sections += ["", "[server]", "externalConfig=true", "externalConfigFile=" + _ini_string((profile / LAYOUT_FILENAME).as_posix())]
        files[LAYOUT_FILENAME] = _layout(group)
    else:
        sections += ["", "[client]", "remoteHost=" + _ini_string(network.primary_address)]
        sections += ["", "[server]"]
    sections += ["protocol=barrier", "enableClipboard=" + str(group.clipboard).lower(),
                 "clipboardSize=" + ("3" if group.clipboard else "0")]
    files[SETTINGS_FILENAME] = "\n".join([*sections, ""])
    return PreparedProfile(node_id, group.session_id, group.digest, "primary" if primary else "client",
                           (str(engine), "server" if primary else "client", "--settings", str(settings)), tuple(sorted(files.items())))


def _preflight_context(group, node_id, paths, facts, approved_artifact_sha256):
    canonical_uuid(node_id)
    valid_digest(approved_artifact_sha256)
    if not isinstance(group, GroupManifest) or node_id not in dict(group.tls_pins):
        raise ValueError("Preflight only a manifest member.")
    if not isinstance(paths, LocalPaths) or not isinstance(facts, PreflightFacts) or facts.node_id != node_id:
        raise ValueError("Measured facts must belong to the destination node.")
    if facts.platform != paths.platform:
        raise ValueError("Measured facts refer to another destination platform.")
    fields = ("artifact_root", "profile_root", "certificate_reference")
    observed = tuple(_path(getattr(facts, name), facts.platform) for name in fields)
    requested = tuple(_path(getattr(paths, name), paths.platform) for name in fields)
    if observed != requested:
        raise ValueError("Measured facts refer to another local path context.")


def _check(code, value):
    if value is None:
        status = "missing"
    elif value:
        status = "ok"
    else:
        status = "unsupported"
    return PreflightCheck(code, status)


def _artifact_checks(artifact, facts, approved_sha256):
    checks = []
    checks.append(_check("artifact_approval", None if artifact is None else isinstance(artifact, VerifiedArtifact)
                         and artifact.manifest_sha256 == approved_sha256))
    if isinstance(artifact, VerifiedArtifact):
        manifest = artifact.manifest
        checks += [_check("deskflow_source", _known_source(manifest)),
                   _check("managed_engine", manifest.patch_sha256 is not None and MANAGED_CAPABILITIES <= set(manifest.capabilities)),
                   _check("platform", manifest.platform == facts.platform and manifest.architecture == facts.architecture),
                   _check("artifact_files", None if facts.observed_files is None else dict(facts.observed_files) == dict(manifest.files))]
    return checks


def preflight(group, node_id, artifact, paths, facts, *, approved_artifact_sha256):
    """Pure prerequisite assessment. Approval comes from an external pin, never a capability claim."""
    _preflight_context(group, node_id, paths, facts, approved_artifact_sha256)
    checks = _artifact_checks(artifact, facts, approved_artifact_sha256)
    checks += [_check("executable", facts.executable_usable), _check("private_profile", facts.private_profile_available),
               _check("tls_identity", None if facts.certificate_sha256 is None else facts.certificate_sha256 == dict(group.tls_pins)[node_id]),
               _check("tls_private_access", facts.private_certificate_access), _check("interactive_session", facts.interactive_session)]
    graphics = None if facts.graphics is None else ((facts.platform == "windows" and facts.graphics == "windows")
               or (facts.platform == "linux" and facts.graphics in {"x11", "wayland"}))
    checks.append(_check("graphical_backend", graphics))
    if node_id == group.primary and facts.graphics == "wayland" and group.clipboard:
        checks.append(_check("continuous_wayland_primary_clipboard", False))
    checks += [_check("data_route", facts.network_authorized), _check("owner_input_consent", facts.owner_input_consent)]
    if group.clipboard:
        checks.append(_check("owner_clipboard_consent", facts.owner_clipboard_consent))
    return PreflightReport(tuple(checks))
