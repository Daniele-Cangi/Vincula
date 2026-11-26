# Managed Deskflow source patch

This directory contains a source patch, not a prebuilt engine or an installed
desktop service. Apply it once to the official Deskflow repository at
`081f6478e654a112168879e67a41ce7d7e4ae3af` (1.27.0).

`vincula-managed-v1.patch` SHA256:
`b7c51d981fda6aee2e4e0bc7f05a4fb7de9281e62fa0d98093ad72d1261dd7dc`.
It includes Vincula's earlier GNOME clipboard patch; do not apply that separately.

The upstream copyright and license notices remain in the patch. New managed C++
files use `GPL-2.0-only WITH LicenseRef-OpenSSL-Exception`; modified build scripts
retain their MIT notices. The corresponding upstream license texts are included
in `LICENSE` and `LICENSES/`. Vincula's original Python and C# code keeps its
separate repository license.

## Reproduce

Fetch the exact upstream commit, verify the patch digest, and run
`git apply --check` before `git apply`. Never apply against a moving branch.
Dependencies and original build instructions remain in upstream
`docs/dev/build.md` at the pinned commit.

Linux requires Qt >= 6.7, OpenSSL >= 3, libei and libportal with clipboard APIs;
enable X11 support for Kali as well as Wayland for Ubuntu. Build in a new directory:

```sh
cmake -S . -B build-managed -DBUILD_GUI=OFF -DBUILD_INSTALLER=OFF \
  -DBUILD_TESTS=ON -DSKIP_BUILD_TESTS=ON -DBUILD_X11_SUPPORT=ON
cmake --build build-managed --target deskflow-core ManagedSessionTests \
  SettingsTests ServerTests ClientProxyTests ServerProxyTests SecureUtilsTests
ctest --test-dir build-managed/src/unittests \
  -R '^(ManagedSessionTests|SettingsTests|ServerTests|ClientProxyTests|ServerProxyTests|SecureUtilsTests)$' \
  --output-on-failure
```

The local Ubuntu build used Qt 6.10.2, GCC 15.2, OpenSSL 3.5.5, libei 1.5 and
libportal 0.10. The incomplete local SDK required disabling optional RandR and
Xinerama headers (`X11_EXTENSIONS_XRANDR_H=0`,
`HAVE_X11_EXTENSIONS_XINERAMA_H=0`). These flags are not needed on a complete SDK.
All 16 managed Qt results (14 tests plus init/cleanup) and the six selected CTest suites passed. The resulting
ELF is dynamically linked; copying it alone is insufficient for another PC.

`windows-managed-workflow.proposed.yml` records the proposed native MSVC/Qt/vcpkg
build. It is deliberately outside `.github/workflows`, has not run, and is not a
Windows success claim. Select its patch path and verify package availability
before activating it. Preserve dependency licenses and hashes in the resulting
distribution. Native Windows compilation and real desktop acceptance are still
required before release.

## Managed boundary

- Provision a fresh private root and call `--vincula-provision ROOT
  --vincula-session UUID --vincula-node UUID`. Only public metadata is returned.
- The guardian assigns two private inherited pipes. Managed requests are bounded
  canonical JSON frames; stdout logs do not establish readiness.
- Fresh certificates bind node and session identities. A consumed profile cannot
  restart; loss of an adopted peer, backend consent or live event loop is terminal.
- The first supported topology has a Windows primary and Linux clients using
  X11 or Wayland. Wayland still requires the local portal consent.
- The external guardian must retain process-tree kill and independent leases:
  graceful pipe shutdown alone is insufficient when a peer stops reading.

See [the Vincula implementation status](../../docs/desktop-multinode-implementation.md)
for integration and remaining deployment gates.
