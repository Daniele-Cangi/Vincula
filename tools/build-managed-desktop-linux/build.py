#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Freeze then reproduce a private Linux managed-Deskflow QA artifact.

This tool never installs, starts a desktop client/server, or accesses a display.
The freeze step resolves dependencies from explicitly selected, trusted inputs.
The package step uses only the frozen file/hash map, never a fresh ldd search.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

UPSTREAM = "081f6478e654a112168879e67a41ce7d7e4ae3af"
PATCH = "70afdffdba804e5bc95644f7ac27b24f8815857426d6737577d3d3e47d18ea5a"
BINARY = "9a351ea90ab5fe1afb5a1cea1591a4e450351d754631a6ec5e1e7bad673f2785"
EPOCH = 1795737600  # 2026-11-27T00:00:00Z, deterministic archive metadata.
BASE_LIBS = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux-x86-64.so.2"}
PLUGINS = (
    "platforms/libqxcb.so", "platforms/libqwayland.so", "tls/libqopensslbackend.so",
    "platforminputcontexts/libcomposeplatforminputcontextplugin.so",
    "wayland-shell-integration/libxdg-shell.so", "xcbglintegrations/libqxcb-glx-integration.so",
    "xcbglintegrations/libqxcb-egl-integration.so",
    "wayland-graphics-integration-client/libqt-plugin-wayland-egl.so",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=True, **kwargs).stdout


def check_pin(path, expected):
    if sha(path) != expected:
        raise ValueError("Input hash mismatch: " + str(path))


def source_ref(roots, path):
    resolved = Path(path).resolve(strict=True)
    for name in ("source", "sdk", "portal", "host"):
        if resolved.is_relative_to(roots[name]):
            return {"root": name, "path": resolved.relative_to(roots[name]).as_posix(), "sha256": sha(resolved)}
    raise ValueError("Unmapped input: " + str(path))


def deb_index(directory):
    index = {}
    for deb in sorted(Path(directory).glob("*.deb")):
        fields = run(["dpkg-deb", "--show", "--showformat=${Package}\t${Version}\t${Source}", deb]).decode().split("\t")
        package, version, source = fields
        metadata = {"package": package, "version": version, "source_package": source or package,
                    "archive": deb.name, "archive_sha256": sha(deb)}
        listing = run(["dpkg-deb", "--contents", deb]).decode()
        for line in listing.splitlines():
            match = re.search(r"\s\./(usr/(?:lib|share/doc)/[^ ]+)", line)
            if match:
                index[match[1]] = metadata
    return index


def freeze(args, roots):
    source, sdk, portal = roots["source"], roots["sdk"], roots["portal"]
    binary = source / "build-vincula-x11/bin/deskflow-core"
    check_pin(binary, BINARY)
    patch = args.patch or source / "vincula-managed-v1.patch"
    check_pin(patch, PATCH)
    if run(["git", "-C", source, "rev-parse", "HEAD"]).decode().strip() != UPSTREAM:
        raise ValueError("The source checkout must retain the pinned upstream HEAD.")
    plugin_root = sdk / "usr/lib/x86_64-linux-gnu/qt6/plugins"
    selected = {"bin/deskflow-core": binary, **{"plugins/" + name: plugin_root / name for name in PLUGINS}}
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("LD_", "QT_"))}
    environment["LD_LIBRARY_PATH"] = str(portal / "lib") + ":" + str(sdk / "usr/lib/x86_64-linux-gnu")
    external = {}
    for path in tuple(selected.values()):
        output = run(["ldd", path], env=environment).decode()
        if "not found" in output:
            raise ValueError("Unresolved dependency: " + output)
        for name, resolved in re.findall(r"^\s*(\S+) => (/[^(\n]+) \(", output, re.M):
            resolved = Path(resolved.strip()).resolve()
            if name in BASE_LIBS:
                external[name] = {"soname": name, "reason": "host glibc ABI (not redistributed)"}
                continue
            key = "lib/" + name
            if key in selected and sha(selected[key]) != sha(resolved):
                raise ValueError("Conflicting resolved SONAME: " + name)
            selected[key] = resolved
    # Explicitly pin dynamic OpenSSL loads and the libei/portal build inputs too.
    build = json.loads((source / "vincula-managed-build-manifest.json").read_text())
    for row in build["linked_libraries"].values():
        path = Path(row["path"])
        if path.name.startswith(("libssl.so.", "libcrypto.so.", "libportal.so.", "libei.so.")):
            check_pin(path, row["sha256"])
            soname = run([args.patchelf, "--print-soname", path]).decode().strip()
            selected["lib/" + soname] = path

    sdk_index = deb_index(sdk.parent)
    portal_sdk = Path(args.portal_sdk).resolve()
    portal_index = deb_index(Path(args.portal_debs))
    packages, licenses = {}, {}
    for destination, path in tuple(selected.items()):
        if destination.startswith("bin/"):
            continue
        path = Path(path).resolve()
        if path.is_relative_to(sdk):
            metadata = sdk_index.get(path.relative_to(sdk).as_posix())
            doc_root = sdk
        elif path.is_relative_to(portal):
            mirror = portal_sdk / "usr/lib/x86_64-linux-gnu" / path.name
            if not mirror.is_file() or sha(mirror) != sha(path):
                raise ValueError("The portal binary does not match its archived package.")
            metadata = portal_index.get(mirror.relative_to(portal_sdk).as_posix())
            doc_root = portal_sdk
        else:
            ownership = run(["dpkg-query", "-S", path]).decode().splitlines()[0].rsplit(": ", 1)[0]
            fields = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}", ownership]).decode().split("\t")
            metadata = {"package": fields[0].split(":")[0], "version": fields[1], "source_package": fields[2], "source_version": fields[3]}
            doc_root = Path("/")
        if not metadata:
            raise ValueError("No package metadata for " + str(path))
        package = metadata["package"]
        copyright_path = doc_root / "usr/share/doc" / package / "copyright"
        if not copyright_path.is_file():
            raise ValueError("Missing copyright notice: " + str(copyright_path))
        # Metadata outside the SDK is copied into our frozen local input directory.
        # The archived package hash and original path are retained as provenance.
        if doc_root == portal_sdk:
            notice_name = "portal-copyright"
            copied = args.lock.parent / notice_name
            copied.write_bytes(copyright_path.read_bytes())
            copyright_path = copied
            roots["inputs"] = args.lock.parent.resolve()
            notice_ref = {"root": "inputs", "path": notice_name, "sha256": sha(copied)}
        else:
            notice_ref = source_ref(roots, copyright_path)
        licenses["licenses/packages/" + package + ".copyright"] = notice_ref
        packages[package] = metadata | {"copyright_file": "licenses/packages/" + package + ".copyright"}
    for name in ("GPL-2", "GPL-3", "LGPL-2", "LGPL-2.1", "LGPL-3", "Apache-2.0", "MPL-2.0", "BSD", "CC0-1.0"):
        licenses["licenses/common/" + name] = source_ref(roots, Path("/usr/share/common-licenses") / name)
    for path in [source / "LICENSE", *sorted((source / "LICENSES").glob("*.txt"))]:
        licenses["licenses/deskflow/" + path.name] = source_ref(roots, path)
    files = {name: source_ref(roots, path) for name, path in selected.items()}
    files.update(licenses)
    files["source/VINCULA-MANAGED-BUILD.txt"] = source_ref(roots, source / "VINCULA-MANAGED-BUILD.txt")
    files["source/vincula-managed-v1.patch"] = source_ref(roots, patch)
    lock = {"schema_version": 1, "package_version": "1.27.0-vincula-managed-v1-linux-x86_64-20261127-acl1",
            "upstream_commit": UPSTREAM, "patch_sha256": PATCH, "input_binary_sha256": BINARY,
            "patchelf_sha256": sha(args.patchelf), "patchelf_version": run([args.patchelf, "--version"]).decode().strip(),
            "epoch": EPOCH, "files": files, "packages": packages,
            "host_libraries": sorted(external.values(), key=lambda row: row["soname"]),
            "baseline": {"architecture": "x86_64", "glibc": "2.43", "qt": "6.10.2", "portal": "0.10.0", "libei": "1.5.0"}}
    args.lock.write_bytes(canonical(lock))
    print(json.dumps({"frozen_files": len(files), "packages": len(packages), "lock_sha256": sha(args.lock)}))


def deterministic_tar(root, target, epoch):
    with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                info = archive.gettarinfo(path, arcname=root.name + "/" + path.relative_to(root).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = epoch
                with path.open("rb") as stream:
                    archive.addfile(info, stream)


def package(args, roots):
    lock = json.loads(args.lock.read_bytes())
    if args.lock.read_bytes() != canonical(lock) or lock["schema_version"] != 1:
        raise ValueError("The input lock must be canonical v1 JSON.")
    if (lock["upstream_commit"] != UPSTREAM or lock["patch_sha256"] != PATCH
            or lock["input_binary_sha256"] != BINARY):
        raise ValueError("The input lock differs from the supported managed source build.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
    from desktop_backend import ArtifactManifest, verify_artifact_manifest
    import desktop_deskflow as contract
    # Repository URLs are an exact runtime contract, not interchangeable Git URLs.
    candidate = ArtifactManifest("deskflow", contract.UPSTREAM_REPOSITORY, UPSTREAM, "1.27.0", PATCH,
                                 "linux", "x86_64", "bin/deskflow-core",
                                 (("bin/deskflow-core", BINARY),), tuple(contract.MANAGED_CAPABILITIES))
    if not contract._known_source(candidate) or contract.UPSTREAM_COMMIT != UPSTREAM:
        raise ValueError("The package source does not satisfy the installed runtime contract.")
    check_pin(args.patchelf, lock["patchelf_sha256"])
    if args.output.exists():
        raise ValueError("Use a new output path; existing packages are never overwritten.")
    args.output.mkdir(parents=True, mode=0o700)
    roots["inputs"] = args.lock.parent.resolve()
    for destination, row in sorted(lock["files"].items()):
        if not re.fullmatch(r"[A-Za-z0-9_.+/-]+", destination) or any(x in ("", ".", "..") for x in destination.split("/")):
            raise ValueError("Unsafe package member")
        origin = (roots[row["root"]] / row["path"]).resolve(strict=True)
        if not origin.is_relative_to(roots[row["root"]]):
            raise ValueError("Input escaped selected root")
        check_pin(origin, row["sha256"])
        target = args.output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)  # No symlinks in an approved runtime artifact.
        executable = destination.startswith(("bin/", "lib/", "plugins/"))
        target.chmod(0o700 if executable else 0o600)
        if executable and target.read_bytes()[:4] == b"\x7fELF":
            relative = os.path.relpath(args.output / "lib", target.parent)
            rpath = "$ORIGIN" + ("/" + relative if relative != "." else "")
            run([args.patchelf, "--force-rpath", "--set-rpath", rpath, target])
    (args.output / "bin/qt.conf").write_text("[Paths]\nPrefix=..\nLibraries=lib\nPlugins=plugins\n")
    (args.output / "source/dependencies.json").write_bytes(canonical({"packages": lock["packages"], "host_libraries": lock["host_libraries"],
        "source_instructions": "Retrieve exact distribution source package/version from the Ubuntu resolute or Kali/Debian archives and the upstream Source URL in each copyright notice. This is an internal QA artifact; validate complete corresponding source before public redistribution."}))
    source_tar = run(["git", "-C", roots["source"], "archive", "--format=tar", lock["upstream_commit"]])
    (args.output / "source/deskflow-upstream.tar.gz").write_bytes(gzip.compress(source_tar, mtime=0))
    provenance = {key: lock[key] for key in ("package_version", "upstream_commit", "patch_sha256", "input_binary_sha256", "patchelf_sha256", "patchelf_version", "baseline")}
    provenance.update({"lock_sha256": sha(args.lock), "build_tool_sha256": sha(Path(__file__)), "packaging": "ELF RPATH is relative to bundled lib; qt.conf selects bundled Qt 6.10 plugins. No wrapper or global LD_LIBRARY_PATH.",
                       "source_notes": "VINCULA-MANAGED-BUILD.txt records the original SDK/build procedure. The patch and input ELF pins in this provenance supersede its historical validation state.",
                       "scope": "Linux x86_64 private QA, Ubuntu Wayland / Kali X11 candidate; no desktop-session acceptance claim."})
    (args.output / "provenance.json").write_bytes(canonical(provenance))
    (args.output / "source/inputs.lock.json").write_bytes(args.lock.read_bytes())
    files = tuple((path.relative_to(args.output).as_posix(), sha(path)) for path in sorted(args.output.rglob("*")) if path.is_file())
    manifest = ArtifactManifest("deskflow", contract.UPSTREAM_REPOSITORY, UPSTREAM, "1.27.0", PATCH, "linux", "x86_64", "bin/deskflow-core", files, tuple(contract.MANAGED_CAPABILITIES))
    if not contract._known_source(manifest):
        raise ValueError("The final manifest does not satisfy the runtime source contract.")
    raw = canonical(manifest.to_document())
    pin = hashlib.sha256(raw).hexdigest()
    verify_artifact_manifest(raw, approved_sha256=pin)
    (args.output / "artifact-manifest.json").write_bytes(raw)
    for path in args.output.rglob("*"):
        if path.is_file():
            path.chmod(0o700 if path.relative_to(args.output).parts[0] in {"bin", "lib", "plugins"} else 0o600)
        else:
            path.chmod(0o700)
    archive = Path(str(args.output) + ".tar.gz")
    deterministic_tar(args.output, archive, lock["epoch"])
    report = {"artifact_manifest_sha256": pin, "engine_sha256": sha(args.output / manifest.engine_file), "archive_sha256": sha(archive), "archive": str(archive), "files": len(files)}
    Path(str(args.output) + ".pins.json").write_bytes(canonical(report))
    print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "package"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--patch", type=Path, help="Exact pinned source patch (freeze only; defaults to source/vincula-managed-v1.patch)")
    parser.add_argument("--sdk", required=True, type=Path)
    parser.add_argument("--portal", required=True, type=Path)
    parser.add_argument("--portal-sdk", type=Path)
    parser.add_argument("--portal-debs", type=Path)
    parser.add_argument("--patchelf", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    roots = {key: getattr(args, key).resolve(strict=True) for key in ("source", "sdk", "portal")}
    roots["host"] = Path("/")
    args.patchelf = args.patchelf.resolve(strict=True)
    args.lock = args.lock.resolve()
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    if args.action == "freeze":
        if args.lock.exists() or not args.portal_sdk or not args.portal_debs:
            parser.error("freeze needs a new lock file, --portal-sdk and --portal-debs")
        freeze(args, roots)
    else:
        if args.output is None:
            parser.error("package needs --output")
        args.output = args.output.resolve()
        package(args, roots)


if __name__ == "__main__":
    main()
