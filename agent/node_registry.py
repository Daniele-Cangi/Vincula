# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read the operator's node identities and SSH routes. MCP callers only supply node IDs."""

from dataclasses import dataclass
from contextlib import contextmanager
from functools import lru_cache, wraps
from ipaddress import ip_address
import json
from pathlib import Path
import re
import sys
from uuid import UUID

SELECTOR_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
MAX_NODES = 64
SELECTOR = re.compile(SELECTOR_PATTERN)
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,252}")
ACCOUNT = re.compile(r"[A-Za-z0-9_.\\@-]{1,120}")


@lru_cache(maxsize=1)
def _windows_registry_identity():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, data, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(wintypes.LPVOID))[0]
        text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


@contextmanager
def registry_guard():
    """Serialize Windows registry access across processes, threads and sessions."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CreateMutexExW.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.LPCWSTR,
                                    wintypes.DWORD, wintypes.DWORD]
    kernel.CreateMutexExW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    sid = _windows_registry_identity()
    # Same-account GUI and SSH tokens can have different integrity levels.
    sddl = f"D:P(A;;0x00100001;;;{sid})(A;;0x00100001;;;SY)S:(ML;;NW;;;ME)"
    descriptor = wintypes.LPVOID()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        mutex = kernel.CreateMutexExW(ctypes.byref(attributes), "Global\\Vincula.NodeRegistry.v1." + sid,
                                     0, 0x00100001)  # SYNCHRONIZE | MUTEX_MODIFY_STATE
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
    acquired = False
    try:
        result = kernel.WaitForSingleObject(mutex, 10000)
        if result == 0x102:
            raise TimeoutError("The node registry is busy.")
        if result not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED; publication is atomic.
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        try:
            if acquired and not kernel.ReleaseMutex(mutex):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(mutex)


def synchronized(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        with registry_guard():
            return operation(*args, **kwargs)
    return guarded


@dataclass(frozen=True)
class Route:
    destination: str
    options: tuple[str, ...]
    required_files: tuple[Path, ...] = ()
    network: str | None = None

    def configured(self):
        return all(path.is_file() for path in self.required_files)


@dataclass(frozen=True)
class Node:
    node_id: str
    label: str
    family: str
    aliases: tuple[str, ...] = ()
    route: Route | None = None


@dataclass(frozen=True)
class DesktopMember:
    node_id: str
    x: int
    y: int


@dataclass(frozen=True)
class Desktop:
    primary: str
    members: tuple[DesktopMember, ...]
    clipboard: bool


class Registry:
    def __init__(self, local_id, nodes):
        self.local_id = local_id
        self.nodes = tuple(nodes)
        self.desktop = None
        self.selectors = {}
        for node in self.nodes:
            for selector in (node.node_id, *node.aliases):
                if not SELECTOR.fullmatch(selector) or selector in self.selectors:
                    raise ValueError(f"Duplicate or invalid node selector: {selector!r}.")
                self.selectors[selector] = node
        local = self.get(local_id)
        if local.node_id != local_id or local.route is not None:
            raise ValueError("The local node must use its canonical ID and have no SSH route.")

    def get(self, selector):
        try:
            return self.selectors[selector]
        except (KeyError, TypeError):
            raise ValueError(f"Unknown registered node: {selector}.") from None


def operator_path(value):
    if not isinstance(value, str) or any(c in value for c in ('"', '\x00', '\r', '\n', '%', '$', '~')):
        raise ValueError("Specify literal absolute paths for SSH identity and known hosts.")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("SSH identity and known hosts paths must be absolute.")
    return path


def host_address(host):
    if isinstance(host, str):
        try:
            return str(ip_address(host))
        except ValueError:
            if HOST.fullmatch(host):
                return host
    raise ValueError("Specify an IP address or DNS hostname for the SSH route.")


def ssh_route(data, empty_config, node_id):
    if not isinstance(data, dict) or data.keys() - {
        "host", "user", "port", "identity_file", "known_hosts_file", "bind_address", "network",
    }:
        raise ValueError("Invalid SSH route fields.")
    host = host_address(data.get("host"))
    user, port = data.get("user"), data.get("port", 22)
    if not isinstance(user, str) or not ACCOUNT.fullmatch(user) or user.startswith("-"):
        raise ValueError("Specify a valid SSH account.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("The SSH port must be between 1 and 65535.")
    identity = operator_path(data.get("identity_file"))
    known = operator_path(data.get("known_hosts_file"))
    network = data.get("network")
    if network is not None and (not isinstance(network, str) or not 1 <= len(network) <= 72):
        raise ValueError("The route network label must contain 1 to 72 characters.")
    options = ["-F", empty_config, "-i", str(identity), "-o", f"User={user}", "-o", f"Port={port}",
               "-o", f'UserKnownHostsFile="{known}"', "-o", f"GlobalKnownHostsFile={empty_config}",
               "-o", f"HostKeyAlias=vincula-{node_id}",
               "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "UpdateHostKeys=no"]
    if data.get("bind_address") is not None:
        options += ["-o", f"BindAddress={ip_address(data['bind_address'])}"]
    return Route(host, tuple(options), (identity, known), network)


def configured_route(entry, legacy, empty_config, node_id, family):
    if "legacy_route" in entry:
        if "ssh" in entry or entry["legacy_route"] not in ("ubuntu", "windows"):
            raise ValueError("Choose exactly one SSH route for a node.")
        previous = legacy.get(entry["legacy_route"])
        if previous.route is None or previous.family != family:
            raise ValueError("The selected legacy route does not match this remote node.")
        return previous.route
    if "ssh" in entry:
        return ssh_route(entry["ssh"], empty_config, node_id)
    return None


def node_from_config(entry, legacy, empty_config):
    if not isinstance(entry, dict) or entry.keys() - {"id", "label", "family", "aliases", "ssh", "legacy_route"}:
        raise ValueError("Invalid registered node fields.")
    node_id = entry.get("id")
    if not isinstance(node_id, str) or str(UUID(node_id)) != node_id:
        raise ValueError("Use a canonical UUID as the node ID.")
    label, family = entry.get("label"), entry.get("family")
    aliases = entry.get("aliases", [])
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120 or family not in ("windows", "linux"):
        raise ValueError("Each node needs a label and a supported OS family.")
    if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
        raise ValueError("Node aliases must be strings.")
    route = configured_route(entry, legacy, empty_config, node_id, family)
    return Node(node_id, label, family, tuple(aliases), route)


def desktop_member(entry, registry):
    if not isinstance(entry, dict) or set(entry) != {"node_id", "x", "y"}:
        raise ValueError("Each desktop member needs node_id, x and y.")
    if type(entry["x"]) is not int or type(entry["y"]) is not int:
        raise ValueError("Desktop positions must use integer grid coordinates.")
    node_id = entry["node_id"]
    if registry.get(node_id).node_id != node_id:
        raise ValueError("Desktop members must use canonical node IDs.")
    return DesktopMember(node_id, entry["x"], entry["y"])


def desktop_from_config(data, registry):
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != {"primary", "members", "clipboard"}:
        raise ValueError("The desktop group needs primary, members and clipboard.")
    members = data["members"]
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_NODES:
        raise ValueError(f"Register between 1 and {MAX_NODES} desktop members.")
    if type(data["clipboard"]) is not bool:
        raise ValueError("Desktop clipboard must be a boolean.")
    layout = tuple(desktop_member(entry, registry) for entry in members)
    identities = {entry.node_id for entry in layout}
    positions = {(entry.x, entry.y) for entry in layout}
    if len(identities) != len(layout) or len(positions) != len(layout):
        raise ValueError("Desktop members and their positions must be unique.")
    if not isinstance(data["primary"], str) or data["primary"] not in identities:
        raise ValueError("The desktop primary must be one of its registered members.")
    return Desktop(data["primary"], layout, data["clipboard"])


def from_document(config, legacy, empty_config):
    if (not isinstance(config, dict) or not {"local", "nodes"} <= config.keys() <= {"local", "nodes", "desktop"}
            or not isinstance(config["nodes"], list)):
        raise ValueError("The node registry must contain local and nodes.")
    if not 1 <= len(config["nodes"]) <= MAX_NODES:
        raise ValueError(f"Register between 1 and {MAX_NODES} nodes.")
    registry = Registry(config["local"], (node_from_config(item, legacy, empty_config) for item in config["nodes"]))
    if registry.get(registry.local_id).family != legacy.get(legacy.local_id).family:
        raise ValueError("The local node's OS family must match the running server.")
    registry.desktop = desktop_from_config(config.get("desktop"), registry)
    return registry


@synchronized
def load(path, legacy, empty_config):
    if path is None:
        return legacy
    with path.open(encoding="utf-8-sig") as file:
        return from_document(json.load(file), legacy, empty_config)
