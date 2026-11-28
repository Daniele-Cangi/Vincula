#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Link the headless Config probe against an existing Linux/Ninja Deskflow build.

Only the new output directory is written. No upstream targets are built and
the resulting probe is not run by this script.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def cache_values(path):
    return {line.split(":", 1)[0]: line.split("=", 1)[1]
            for line in path.read_text().splitlines()
            if ":" in line and "=" in line and not line.startswith(("#", "//"))}


def compile_arguments(command, source, output):
    args, result, index = shlex.split(command), [], 0
    if "-c" not in args or "-o" not in args:
        raise ValueError("Expected the native C++ compilation command.")
    while index < len(args):
        value = args[index]
        if value in {"-MT", "-MF"}:
            index += 2
            continue
        if value in {"-MD", "-MMD"}:
            index += 1
            continue
        if value in {"-o", "-c"}:
            result.extend([value, str(output if value == "-o" else source)])
            index += 2
            continue
        result.append(value)
        index += 1
    return result


def link_arguments(command, obj, output):
    args = shlex.split(command)
    if args[:2] == [":", "&&"] and args[-2:] == ["&&", ":"]:
        args = args[2:-2]
    if any(value in {"&&", "|", ";", ">", "<"} for value in args):
        raise ValueError("Unsupported link wrapper; no shell is executed.")
    result = [value for value in args if not value.endswith(".o")
              and not value.startswith("-Wl,--dependency-file=")]
    position = result.index("-o")
    result[position + 1] = str(output)
    result.insert(position, str(obj))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or not args.build.is_absolute() or not args.output.is_absolute():
        raise ValueError("Select absolute Linux build and fresh output directories.")
    build = args.build.resolve(strict=True)
    values = cache_values(build / "CMakeCache.txt")
    if values.get("CMAKE_GENERATOR") != "Ninja":
        raise ValueError("This reproducer requires the existing single-configuration Ninja build.")
    commands = run([values["CMAKE_MAKE_PROGRAM"], "-C", str(build), "-t", "commands",
                    "src/unittests/server/ServerConfigTests"], capture_output=True,
                   text=True, timeout=30).stdout.splitlines()
    compiler = next(command for command in commands
                    if " -c " in command and command.endswith("/src/unittests/server/ServerConfigTests.cpp"))
    args.output.mkdir(mode=0o700)  # Never reuse or repair an existing directory.
    output = args.output.resolve(strict=True)
    source = Path(__file__).with_name("config-probe.cpp").resolve(strict=True)
    obj, binary = output / "config-probe.o", output / "config-probe"
    compile_argv = compile_arguments(compiler, source, obj)
    link_argv = link_arguments(commands[-1], obj, binary)
    with (output / "build.log").open("xb") as log:
        run(compile_argv, cwd=build, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        run(link_argv, cwd=build, stdout=log, stderr=subprocess.STDOUT, timeout=120)
    metadata = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "upstream_source": values["CMAKE_HOME_DIRECTORY"], "build": str(build),
                "compile": compile_argv, "link": link_argv, "probe_run": False}
    (output / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({"probe": str(binary), "source_sha256": metadata["source_sha256"], "probe_run": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
