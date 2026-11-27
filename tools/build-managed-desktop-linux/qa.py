#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless artifact integrity, ELF closure and fresh-certificate provisioning QA.

Never starts a desktop server/client, loads a display, or reads any clipboard.
Uses the glibc loader's trace mode for the engine and every Qt plugin/library.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

EXPECTED_PATCH = "b7c51d981fda6aee2e4e0bc7f05a4fb7de9281e62fa0d98093ad72d1261dd7dc"


def qualify_source(manifest, contract):
    """Use the same known-source predicate as compile_profile/preflight."""
    if (not contract._known_source(manifest) or manifest.patch_sha256 != EXPECTED_PATCH
            or not contract.MANAGED_CAPABILITIES <= set(manifest.capabilities)
            or manifest.platform != "linux" or manifest.architecture != "x86_64"
            or manifest.engine_file != "bin/deskflow-core"):
        raise ValueError("Artifact does not satisfy the managed Linux runtime source contract")


def contract_regressions(manifest, contract):
    """Metadata changes must fail before any ELF/provision execution."""
    incompatible = (
        replace(manifest, upstream_repository=contract.UPSTREAM_REPOSITORY + ".git"),
        replace(manifest, upstream_commit="0" * 40), replace(manifest, version="0.0.0"),
        replace(manifest, patch_sha256="0" * 64), replace(manifest, capabilities=()),
    )
    for bad in incompatible:
        try:
            qualify_source(bad, contract)
        except ValueError:
            continue
        raise AssertionError("An incompatible managed source variant was accepted")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--manifest-pin", required=True)
    parser.add_argument("--agent-source", required=True, type=Path)
    parser.add_argument("--profile-parent", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.agent_source.resolve()))
    from desktop_backend import GroupManifest, Screen, verify_artifact_manifest
    import desktop_deskflow as contract
    root = args.artifact.resolve(strict=True)
    verified = verify_artifact_manifest((root / "artifact-manifest.json").read_bytes(), approved_sha256=args.manifest_pin)
    qualify_source(verified.manifest, contract)
    contract_regressions(verified.manifest, contract)
    # Exercise the actual pure profile compiler as an integration gate. These are
    # synthetic identities and documentation-only IPs; no files/sockets open.
    primary, secondary = str(uuid.uuid4()), str(uuid.uuid4())
    group = GroupManifest(str(uuid.uuid4()), primary,
                          (Screen(primary, 0, 0), Screen(secondary, 1, 0)), False,
                          ((primary, "1" * 64), (secondary, "2" * 64)),
                          ((primary, "3" * 64), (secondary, "4" * 64)))
    preview = args.profile_parent.resolve() / "source-contract-preview-not-created"
    paths = contract.LocalPaths("linux", str(root), str(preview), str(preview / "tls/identity.pem"))
    prepared = contract.compile_profile(group, secondary, verified, paths,
                                        contract.NetworkProfile("192.0.2.2", 24800, "192.0.2.1"))
    if prepared.role != "client" or prepared.argv[0] != str(root / verified.manifest.engine_file):
        raise ValueError("The real runtime compiler selected an unexpected engine/profile")
    expected = dict(verified.manifest.files)
    observed = set()
    for path in root.rglob("*"):
        info = path.lstat()
        if path.is_symlink() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("Artifact must contain only real regular files/directories")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise ValueError("Artifact hardlink rejected")
            name = path.relative_to(root).as_posix()
            observed.add(name)
            if name != "artifact-manifest.json" and expected.get(name) != digest(path):
                raise ValueError("Artifact hash mismatch: " + name)
    if observed != set(expected) | {"artifact-manifest.json"}:
        raise ValueError("Artifact contains missing or unlisted files")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    elf_files, dependencies = [], {}
    host_allowed = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux-x86-64.so.2"}
    for name in expected:
        path = root / name
        with path.open("rb") as stream:
            is_elf = stream.read(4) == b"\x7fELF"
        if not is_elf:
            continue
        result = subprocess.run(["ldd", "-r", str(path)], env=env, capture_output=True, text=True, timeout=30)
        if result.returncode or re.search(r"not found|undefined symbol|version [`'].*not found", result.stdout + result.stderr):
            raise ValueError("ELF dependency validation failed for " + name + ": " + result.stdout + result.stderr)
        for soname, target in re.findall(r"^\s*(\S+) => (/[^(\n]+) \(", result.stdout, re.M):
            target = Path(target.strip()).resolve(strict=True)
            if not target.is_relative_to(root) and soname not in host_allowed:
                raise ValueError("Unexpected host dependency: " + soname + " => " + str(target))
            dependencies[soname] = "bundle" if target.is_relative_to(root) else "host-glibc"
        dynamic = subprocess.run(["readelf", "-d", str(path)], env=env, capture_output=True, text=True, timeout=10)
        rpaths = re.findall(r"\((?:RPATH|RUNPATH)\).*?\[(.*?)\]", dynamic.stdout)
        if not rpaths or any(not row.startswith("$ORIGIN") or ":" in row for row in rpaths):
            raise ValueError("Nonlocal or absent ELF search path: " + name)
        elf_files.append(name)
    args.profile_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="headless-provision-", dir=args.profile_parent) as temporary:
        profile = Path(temporary)
        profile.chmod(0o700)
        session, node = str(uuid.uuid4()), str(uuid.uuid4())
        argv = [str(root / verified.manifest.engine_file), "--vincula-provision", str(profile),
                "--vincula-session", session, "--vincula-node", node]
        provision = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        if provision.returncode:
            raise ValueError("Headless certificate provisioning failed: " + provision.stderr)
        public = json.loads(provision.stdout)
        if public.get("protocol") != 1 or public.get("session_id") != session or public.get("node_id") != node:
            raise ValueError("Unexpected provisioning public metadata")
        identity = profile / "tls/identity.pem"
        before = digest(identity)
        if stat.S_IMODE(identity.stat().st_mode) != 0o600:
            raise ValueError("Provisioned identity is not private")
        repeated = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        if repeated.returncode == 0 or digest(identity) != before:
            raise ValueError("Exclusive provisioning did not preserve the first identity")
        # No identity bytes or PEM path are emitted by this QA report.
    print(json.dumps({"passed": True, "manifest_sha256": args.manifest_pin, "verified_files": len(expected),
                      "runtime_known_source": True, "source_contract_regressions": 5,
                      "runtime_profile_compilation": "in-memory client profile accepted",
                      "managed_patch_sha256": verified.manifest.patch_sha256,
                      "elf_files": len(elf_files), "resolved_sonames": len(dependencies),
                      "host_dependencies": sorted(name for name, where in dependencies.items() if where == "host-glibc"),
                      "provision": "fresh private identity; exclusive retry refused; first identity unchanged",
                      "desktop_client_or_server_started": False, "display_environment_used": False}, sort_keys=True))


if __name__ == "__main__":
    main()
