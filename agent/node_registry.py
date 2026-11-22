# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read the operator's node identities and SSH routes. MCP callers only supply node IDs."""

from dataclasses import dataclass
from ipaddress import ip_address
import json
from pathlib import Path
import re
from uuid import UUID

SELECTOR_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
MAX_NODES = 64
SELECTOR = re.compile(SELECTOR_PATTERN)
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,252}")
ACCOUNT = re.compile(r"[A-Za-z0-9_.\\@-]{1,120}")


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


class Registry:
    def __init__(self, local_id, nodes):
        self.local_id = local_id
        self.nodes = tuple(nodes)
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


def load(path, legacy, empty_config):
    if path is None:
        return legacy
    with path.open(encoding="utf-8-sig") as file:
        config = json.load(file)
    if not isinstance(config, dict) or set(config) != {"local", "nodes"} or not isinstance(config["nodes"], list):
        raise ValueError("The node registry must contain local and nodes.")
    if not 1 <= len(config["nodes"]) <= MAX_NODES:
        raise ValueError(f"Register between 1 and {MAX_NODES} nodes.")
    registry = Registry(config["local"], (node_from_config(item, legacy, empty_config) for item in config["nodes"]))
    if registry.get(registry.local_id).family != legacy.get(legacy.local_id).family:
        raise ValueError("The local node's OS family must match the running server.")
    return registry
