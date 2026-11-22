# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Check that the hashed CI environment satisfies its current source requirements."""

import argparse
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ("agent/requirements.txt", ".github/requirements-ci.in")
LOCK = ROOT / ".github/requirements-ci.txt"
PREFIX = "# CI input SHA-256 "


def fingerprints():
    return {name: sha256((ROOT / name).read_text(encoding="utf-8").encode()).hexdigest() for name in INPUTS}


def record_inputs():
    content = LOCK.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if not line.startswith(PREFIX)]
    header = [f"{PREFIX}{name} {digest}" for name, digest in fingerprints().items()]
    LOCK.write_text("\n".join([*header, *lines]) + "\n", encoding="utf-8")


def declarations(name):
    from packaging.requirements import Requirement

    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        declaration = line.strip()
        if declaration and not declaration.startswith(("#", "-r ")):
            yield Requirement(declaration)


def check_requirement(requirement):
    if requirement.marker is not None and not requirement.marker.evaluate():
        return
    installed = version(requirement.name)
    if installed not in requirement.specifier:
        raise RuntimeError(f"Installed {requirement.name} {installed} does not satisfy {requirement}.")


def check():
    recorded = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith(PREFIX):
            name, digest = line[len(PREFIX):].split()
            recorded[name] = digest
    if recorded != fingerprints():
        raise RuntimeError("CI inputs changed. Regenerate the lock and record its source fingerprints.")
    for name in INPUTS:
        for requirement in declarations(name):
            check_requirement(requirement)
    print("CI lock matches source inputs and installed requirements.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-inputs", action="store_true")
    args = parser.parse_args()
    if args.record_inputs:
        record_inputs()
    else:
        check()


if __name__ == "__main__":
    main()
