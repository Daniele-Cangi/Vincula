# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Windows build and headless tests. Never runs a desktop core or installer."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

RECIPE = Path(__file__).resolve().parent
REPOSITORY = RECIPE.parent.parent
sys.path.insert(0, str(REPOSITORY / "agent"))
from desktop_backend import ArtifactManifest, canonical_json, verify_artifact_manifest
from desktop_deskflow import MANAGED_CAPABILITIES

TESTS = ("ManagedSessionTests", "SettingsTests", "ServerTests", "ClientProxyTests", "ServerProxyTests", "SecureUtilsTests")
MANAGED_TESTS = {
    "initTestCase", "cleanupTestCase", "canonicalPythonInterop", "rejectsAmbiguousJson", "validatesExactBootstrap",
    "bindsCertificateToExactUuid", "preservesStarTopology", "latchesBackendRevocation", "ignoresStrangerDisconnection",
    "neverClaimsDisabledClipboardReady", "requiresActualWaylandFacts", "provisionIsExclusiveAndSessionSpecific",
    "exclusiveFileCreationKeepsPrivateAcl",
    "privateControlBindsChallengeAndStops", "privateControlRejectsAmbiguousRequest",
    "privateBootstrapRejectsAnotherSessionCertificate", "privateControlRequiresLiveBackendLoop",
}



def run_command(command, reports, environment, *, cwd=None):
    command = [str(value) for value in command]
    rendered = subprocess.list2cmdline(command)
    print("Running:", rendered, flush=True)
    output = []
    with (reports / "commands.log").open("a", encoding="utf-8", newline="\n") as log:
        log.write(rendered + "\n")
        log.flush()
        with subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as process:
            for line in process.stdout:
                output.append(line)
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            process.wait()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, output="".join(output))
    return "".join(output).strip()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(item, directory):
    path = directory / item["filename"]
    if path.name != item["filename"] or not re.fullmatch(r"[a-zA-Z0-9_.+-]+", path.name):
        raise ValueError("Invalid locked download filename")
    if not item["url"].startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
        raise ValueError("Every build input needs HTTPS and an exact SHA256")
    partial = path.with_name(path.name + ".partial")
    try:
        with urllib.request.urlopen(item["url"], timeout=60) as source, partial.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if sha256(partial) != item["sha256"]:
            raise ValueError("Downloaded input differs from locked SHA256: " + path.name)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def unpack_zip(path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = item.filename.replace("\\", "/")
            if name.startswith("/") or ":" in name or ".." in name.split("/"):
                raise ValueError("Archive entry escaped the build root")
        archive.extractall(destination)


def verify_managed_tests(path):
    root = ET.parse(path).getroot()
    functions = root.findall("TestFunction")
    if {item.get("name") for item in functions} != MANAGED_TESTS or len(functions) != len(MANAGED_TESTS):
        raise ValueError("Run every native managed test, including provisioning and private pipes")
    for function in functions:
        incidents = function.findall("Incident")
        if (not incidents or any(item.get("type") != "pass" for item in incidents)
                or any(item.get("type") == "skip" for item in function.findall("Message"))):
            raise ValueError("A native managed test failed or was skipped: " + str(function.get("name")))
    return len(functions)



def verify_configured_version(cache, version):
    values = re.findall(r"^CMAKE_PROJECT_VERSION:STATIC=(.+)$", cache.read_text(encoding="utf-8"), re.MULTILINE)
    if values != [version + ".0"]:
        raise ValueError("Compiled project version differs from the approved release tag")
    return values[0]



def verify_cpp_flags(cache):
    values = re.findall(r"^CMAKE_CXX_FLAGS:STRING=(.*)$", cache.read_text(encoding="utf-8"), re.MULTILINE)
    if len(values) != 1 or not {"/EHsc", "/GR", "/utf-8", "/Brepro"} <= set(values[0].split()):
        raise ValueError("Preserve MSVC exception unwinding/RTTI defaults and the approved UTF-8/repro flags")
    return values[0]


def write_artifact_manifest(engine, output, inputs):
    files = []
    for path in sorted(engine.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Artifact contains a link or reparse point")
        if stat.S_ISREG(info.st_mode):
            files.append((path.relative_to(engine).as_posix(), sha256(path)))
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("Artifact contains a non-regular file")
    manifest = ArtifactManifest(
        backend="deskflow", upstream_repository=inputs["upstream_repository"],
        upstream_commit=inputs["upstream_commit"], version=inputs["version"], patch_sha256=inputs["patch_sha256"],
        platform="windows", architecture="x86_64", engine_file="deskflow-core.exe",
        files=tuple(files), capabilities=tuple(MANAGED_CAPABILITIES),
    )
    raw = canonical_json(manifest.to_document())
    digest = hashlib.sha256(raw).hexdigest()
    verify_artifact_manifest(raw, approved_sha256=digest)
    output.write_bytes(raw)
    output.with_suffix(".sha256").write_text(digest + "\n", encoding="ascii", newline="\n")
    return digest


def qt_licenses(source_archive, destination):
    destination.mkdir(parents=True)
    count = 0
    with tarfile.open(source_archive) as archive:
        for member in archive:
            parts = member.name.split("/")
            if len(parts) == 3 and parts[1] == "LICENSES" and member.isfile():
                if not re.fullmatch(r"[a-zA-Z0-9_.+-]+", parts[2]):
                    raise ValueError("Invalid upstream license basename")
                with archive.extractfile(member) as source, (destination / parts[2]).open("xb") as target:
                    shutil.copyfileobj(source, target)
                count += 1
    if count < 3 or not (destination / "LGPL-3.0-only.txt").is_file():
        raise ValueError("Qt source license texts are missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This recipe requires native Windows/MSVC; it cannot certify a cross build")
    inputs = json.loads((RECIPE / "inputs.json").read_text(encoding="utf-8"))
    if sys.version.split()[0] != inputs["python_version"]:
        raise ValueError("Select the exact locked Python version")
    if os.environ.get("VCToolsVersion", "").rstrip("\\/") != inputs["msvc_toolset"]:
        raise ValueError("The exact locked MSVC toolset is not active; no toolchain installation is attempted")
    if os.environ.get("WindowsSDKVersion", "").rstrip("\\/") != inputs["windows_sdk"]:
        raise ValueError("The locked Windows SDK is not active")
    patch = REPOSITORY / inputs["patch_file"]
    if sha256(patch) != inputs["patch_sha256"]:
        raise ValueError("The managed source patch differs from the locked input")
    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=False)  # No resume/adoption of a previous build directory.
    reports = work / "reports"
    reports.mkdir()
    environment = dict(os.environ)

    def run(command, *, cwd=None):
        return run_command(command, reports, environment, cwd=cwd)

    downloads = work / "downloads"
    downloads.mkdir()
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = dict(zip(inputs["downloads"], pool.map(lambda item: download(item, downloads), inputs["downloads"].values())))
    cmake_root = work / "cmake"
    unpack_zip(paths["cmake"], cmake_root)
    cmake = cmake_root / "cmake-4.2.3-windows-x86_64/bin/cmake.exe"
    ctest = cmake.with_name("ctest.exe")
    perl_root = work / "perl"
    unpack_zip(paths["perl"], perl_root)
    perl = perl_root / "perl/bin/perl.exe"
    qt = work / "qt"
    qt.mkdir()
    run([paths["sevenzip"], "x", "-y", "-o" + str(qt), paths["qtbase"]])
    (qt / "bin/qt.conf").write_text("[Paths]\nPrefix=..\n", encoding="ascii", newline="\n")
    source = work / "deskflow-src"
    run(["git", "-c", "core.autocrlf=false", "init", source])
    run(["git", "-C", source, "config", "core.autocrlf", "false"])
    run(["git", "-C", source, "remote", "add", "origin", inputs["upstream_repository"] + ".git"])
    tag_ref = "refs/tags/" + inputs["upstream_tag"]
    run(["git", "-C", source, "fetch", "--depth=1", "origin", tag_ref + ":" + tag_ref])
    if run(["git", "-C", source, "rev-parse", tag_ref]) != inputs["upstream_tag_object"]:
        raise ValueError("Upstream version tag differs from its approved tag object")
    run(["git", "-C", source, "checkout", "--detach", inputs["upstream_commit"]])
    if run(["git", "-C", source, "rev-parse", "HEAD"]) != inputs["upstream_commit"]:
        raise ValueError("Unexpected Deskflow source commit")
    run(["git", "-C", source, "apply", "--check", patch])
    run(["git", "-C", source, "apply", patch])
    ssl_source = work / "openssl-src"
    ssl_source.mkdir()
    with tarfile.open(paths["openssl_source"]) as archive:
        archive.extractall(ssl_source, filter="data")
    ssl_source = ssl_source / ("openssl-" + inputs["openssl_version"])
    ssl = work / "openssl"
    environment["PATH"] = str(perl.parent) + os.pathsep + str(qt / "bin") + os.pathsep + environment["PATH"]
    environment.pop("OPENSSL_CONF", None)
    environment.pop("OPENSSL_MODULES", None)
    environment.pop("OPENSSL_ENGINES", None)
    compiler = shutil.which("cl.exe", path=environment["PATH"])
    nmake = shutil.which("nmake.exe", path=environment["PATH"])
    if not compiler or not nmake:
        raise ValueError("MSVC cl.exe and nmake.exe must be provided by the selected developer environment")
    run([perl, "Configure", "VC-WIN64A", "shared", "no-asm", "no-tests", "no-docs", "no-apps", "no-makedepend",
         "--prefix=" + str(ssl), "--libdir=lib", "--openssldir=" + str(ssl / "ssl")], cwd=ssl_source)
    run([nmake, "/NOLOGO"], cwd=ssl_source)
    run([nmake, "/NOLOGO", "install_sw"], cwd=ssl_source)
    environment["PATH"] = str(ssl / "bin") + os.pathsep + environment["PATH"]
    build = work / "build"
    run([cmake, "-S", source, "-B", build, "-G", "Visual Studio 17 2022", "-A", "x64",
         "-T", "v143,version=" + inputs["msvc_toolset"], "-DCMAKE_SYSTEM_VERSION=" + inputs["windows_sdk"],
         "-DQt6_DIR=" + str(qt / "lib/cmake/Qt6"), "-DOPENSSL_ROOT_DIR=" + str(ssl),
         "-DOPENSSL_INCLUDE_DIR=" + str(ssl / "include"), "-DOPENSSL_CRYPTO_LIBRARY=" + str(ssl / "lib/libcrypto.lib"),
         "-DOPENSSL_SSL_LIBRARY=" + str(ssl / "lib/libssl.lib"), "-DOPENSSL_USE_STATIC_LIBS=OFF",
         "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CXX_FLAGS_INIT=/Brepro /utf-8", "-DCMAKE_EXE_LINKER_FLAGS_INIT=/Brepro",
         "-DBUILD_GUI=OFF", "-DBUILD_INSTALLER=OFF", "-DBUILD_TESTS=ON", "-DSKIP_BUILD_TESTS=ON"])
    compiled_version = verify_configured_version(build / "CMakeCache.txt", inputs["version"])
    cpp_flags = verify_cpp_flags(build / "CMakeCache.txt")
    run([cmake, "--build", build, "--config", "Release", "--parallel", "4", "--target", "deskflow-core", *TESTS])
    environment["QT_QPA_PLATFORM"] = "minimal"
    # Capture the native contract's own logs even if a later CTest run fails.
    run([build / "src/unittests/common/Release/ManagedSessionTests.exe",
         "-o", str(reports / "managed-tests.xml") + ",xml", "-o", str(reports / "managed-tests.txt") + ",txt"])
    managed_count = verify_managed_tests(reports / "managed-tests.xml")
    run([ctest, "--test-dir", build / "src/unittests", "-C", "Release", "--no-tests=error",
         "-R", "^(" + "|".join(TESTS) + ")$", "--output-on-failure", "--output-junit", reports / "ctest.xml"])
    dist = work / "dist"
    engine, sources = dist / "engine", dist / "sources"
    engine.mkdir(parents=True)
    sources.mkdir()
    shutil.copy2(build / "bin/Release/deskflow-core.exe", engine / "deskflow-core.exe")
    # This only scans/copies dependencies. No invocation of the desktop engine itself.
    run([qt / "bin/windeployqt.exe", "--release", "--no-translations", "--no-opengl-sw",
         "--no-system-d3d-compiler", "--no-compiler-runtime", engine / "deskflow-core.exe"])
    ssl_dlls = list((ssl / "bin").glob("*.dll"))
    if not any(path.name.startswith("libcrypto-3") for path in ssl_dlls) or not any(path.name.startswith("libssl-3") for path in ssl_dlls):
        raise ValueError("The native OpenSSL runtime DLLs are missing")
    for path in ssl_dlls:
        shutil.copy2(path, engine / path.name)
    for dirname in ("ossl-modules", "engines-3"):
        if (ssl / "lib" / dirname).is_dir():
            shutil.copytree(ssl / "lib" / dirname, engine / dirname)
    licenses = engine / "licenses"
    shutil.copytree(source / "LICENSES", licenses / "Deskflow")
    shutil.copy2(source / "LICENSE", licenses / "Deskflow/LICENSE")
    qt_licenses(paths["qtbase_source"], licenses / "QtBase")
    (licenses / "OpenSSL").mkdir()
    shutil.copy2(ssl_source / "LICENSE.txt", licenses / "OpenSSL/LICENSE.txt")
    if (ssl_source / "NOTICE.txt").is_file():
        shutil.copy2(ssl_source / "NOTICE.txt", licenses / "OpenSSL/NOTICE.txt")
    if (qt / "sbom").is_dir():
        shutil.copytree(qt / "sbom", engine / "sbom")
    for name in ("qtbase_source", "openssl_source"):
        shutil.copy2(paths[name], sources / paths[name].name)
    shutil.copy2(patch, sources / "vincula-managed-v1.patch")
    run(["git", "-C", source, "archive", "--format=tar", "--prefix=deskflow-upstream/", "-o", sources / "deskflow-upstream.tar", "HEAD"])
    for path in RECIPE.iterdir():
        if path.is_file() and path.suffix in {".py", ".json", ".md"}:
            shutil.copy2(path, sources / path.name)
    (engine / "RUNTIME-DEPENDENCIES.txt").write_text(
        "Windows x64. Requires an already installed Microsoft Visual C++ 2022 x64 runtime >=14.44.\n"
        "No Microsoft runtime installer, service or firewall rule is included or run.\n"
        "QtBase is dynamically linked and replaceable under its included license terms.\n"
        "Corresponding source archives and build recipe accompany this engine in ../sources/.\n"
        "OpenSSL 3.5.9 was built with no-asm; no desktop engine has been run by this recipe.\n",
        encoding="utf-8", newline="\n")
    compiler_metadata = next((build / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake")).read_text(encoding="utf-8")
    match = re.search(r'set\(CMAKE_CXX_COMPILER_VERSION "([^\"]+)"\)', compiler_metadata)
    if not match:
        raise ValueError("Missing native compiler version metadata")
    provenance = {
        "schema_version": 1, "inputs": inputs, "compiled_version": compiled_version, "cpp_flags": cpp_flags,
        "compiler_version": match.group(1), "compiler_sha256": sha256(compiler),
        "nmake_sha256": sha256(nmake), "cmake_sha256": sha256(cmake), "perl_sha256": sha256(perl),
        "python_sha256": sha256(sys.executable), "git_sha256": sha256(shutil.which("git.exe")),
        "runner_image": os.environ.get("ImageVersion", "unknown"), "openssl_no_asm": True,
        "native_managed_qt_results": managed_count, "native_ctest_suites": list(TESTS),
        "sources": {path.name: sha256(path) for path in sorted(sources.iterdir())},
        "recipe_sha256": sha256(Path(__file__)),
    }
    (engine / "build-info.json").write_bytes(canonical_json(provenance))
    digest = write_artifact_manifest(engine, dist / "artifact-manifest.json", inputs)
    print("Native Windows build/tests complete. Artifact manifest SHA256:", digest, flush=True)


if __name__ == "__main__":
    main()
