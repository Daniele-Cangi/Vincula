# Linux managed desktop artifact QA

`build.py` packages the pinned, already tested managed Deskflow ELF and its
dependency closure. It does not install or launch a desktop session. `qa.py`
checks the canonical Vincula `ArtifactManifest` v1, all regular-file hashes,
every ELF import with the loader's trace mode, relative RPATHs, and fresh
headless certificate provisioning. Neither script reads a clipboard or sends input.

The package is versioned as
`1.27.0-vincula-managed-v1-linux-x86_64-20261127`. Its supported ABI candidate is
Linux x86_64 with glibc 2.43. Ubuntu 26.04 and Kali 2026.3 both provide this ABI.
It bundles Qt **6.10.2** libraries and matching XCB/Wayland/TLS plugins; system Qt
plugins are not a substitute. Every packaged ELF has a local `$ORIGIN` RPATH,
and `bin/qt.conf` selects the bundled plugin tree. There is no launch wrapper or
global `LD_LIBRARY_PATH` setting. The ELF hash changes when RPATH is rewritten;
the original binary hash remains pinned separately in the input lock/provenance.

## Freeze and reproduce

Use Python 3.11+, `ldd`, `readelf`, `dpkg-query`, `dpkg-deb`, Git, and patchelf
0.18.0. Select trusted local inputs explicitly:

```sh
python3 -B tools/build-managed-desktop-linux/build.py freeze \
  --source /absolute/deskflow-upstream \
  --sdk /absolute/resolute-dev/sysroot \
  --portal /absolute/portal \
  --portal-sdk /absolute/stonking-apt/sysroot \
  --portal-debs /absolute/stonking-apt/archives \
  --patchelf /absolute/private-tools/patchelf \
  --lock /absolute/frozen-inputs/inputs.lock.json

python3 -B tools/build-managed-desktop-linux/build.py package \
  --source /absolute/deskflow-upstream \
  --sdk /absolute/resolute-dev/sysroot \
  --portal /absolute/portal \
  --patchelf /absolute/private-tools/patchelf \
  --lock /absolute/frozen-inputs/inputs.lock.json \
  --output /absolute/repro-a/deskflow-managed-linux-x86_64-1.27.0-v1
```

`freeze` resolves only the explicitly chosen trusted SDK and system libraries,
records their exact hashes and package versions, and refuses existing locks.
Review and preserve this lock outside the artifact trust boundary. `package`
does not resolve dependencies again: it verifies every frozen input and the
patchelf executable before copying. Its new output path must not exist. Repeat
the package command with the same output basename under `repro-b`; the canonical
manifest and deterministic archive must have identical SHA256 hashes. This
certifies reproducible **packaging from the pinned ELF/SDK inputs**, not a claim
that the original C++ compilation is independently bit reproducible.

The output contains `artifact-manifest.json`, while its externally approved pin
is written beside the package as `<output>.pins.json`. The manifest does not
include itself. All remaining files, including runtime dependencies, licenses,
source archive, source patch, input lock and provenance, are covered by it.
No symlinks are required. Transfer and unpack only in a fresh owner-private QA
directory; verify the archive pin before extracting and the manifest pin before
executing anything. Keep the pin in the control plane, not derived from untrusted
received bytes.

Run the bounded headless gate with that already-approved pin:

```sh
python3 -I -B tools/build-managed-desktop-linux/qa.py \
  --artifact /absolute/deskflow-managed-linux-x86_64-1.27.0-v1 \
  --manifest-pin APPROVED_SHA256 \
  --agent-source /absolute/vincula/agent \
  --profile-parent /absolute/private-qa/profiles
```

The QA profile is new, mode 0700, and temporary. Provisioning must create a mode
0600 identity, refuse reuse, and preserve the first identity. The report never
prints key bytes. No server/client, GUI, portal request, clipboard provider or
input hook is started by this gate.

## Boundaries

- The host supplies glibc/loader, its running X11 or Wayland session, session
  D-Bus, portal service and permissions, XKB/font data, and any vendor GPU drivers. The bundle
  does not replace them. ELF closure/provisioning do not attest a live desktop.
- A runtime launching this artifact should clear inherited `LD_PRELOAD`,
  `LD_LIBRARY_PATH`, `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH` and other
  operator-external Qt overrides. No environment changes are made by these
  packaging scripts outside their children.
- Upstream Deskflow source at the pinned commit and the exact managed patch are
  included, together with its licenses and local build instructions. Dependency
  notices, package versions/archive hashes where cached, and upstream source
  references are recorded. Before public redistribution, preserve and verify the
  complete corresponding dependency sources/relinking obligations; this private
  QA bundle is not represented as a public release.
- Frozen dependencies must be rebuilt/reviewed when security updates are needed.
  A new dependency lock creates a new artifact approval pin.
- The certified managed source currently supports Windows primary and Linux
  secondary roles. A passing Linux package gate does not certify Windows build,
  three-node readiness, clipboard sync, or stop during a real file transfer.
