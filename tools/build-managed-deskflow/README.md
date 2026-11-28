# Native managed Deskflow build

`.github/workflows/managed-deskflow.yml` builds **Windows x64** and runs headless
native tests. It does not run `deskflow-core.exe`, provision desktop sessions,
install software/services/firewall rules, publish releases or deploy to a PC.
Relevant pull requests and pushes to `main` can start this build;
`workflow_dispatch` is also available once GitHub registers the workflow.
Development branches build through their pull request, avoiding a duplicate
push/PR pair for the same commit. Concurrency preserves an active build and keeps
only the latest pending build.
Pull-request path filters inspect the entire PR diff, so later unrelated pushes
can queue another build; they must not interrupt a useful native compilation.

`inputs.json` pins the upstream commit and annotated version tag object, source patch, Python, MSVC toolset,
Windows SDK, QtBase, OpenSSL and downloaded tools. Every downloaded archive/tool
must match its committed SHA256 **before extraction or execution**. No `aqt`, pip
packages or vcpkg dependency resolution runs. CMake and Perl are portable builds
under the runner's temporary directory; the existing MSVC/SDK are selected,
never installed or updated. If that exact toolset/SDK is absent the job fails.
The configured CMake release version must also match the approved annotated tag;
a shallow untagged `1.27.0.9999` build cannot be published as release `1.27.0`.

The hosted `windows-2022` image itself is maintained by GitHub, rather than being
an immutable VM image. The recipe pins toolset `14.44.35207` and SDK
`10.0.22621.0`; provenance records the actual compiler, nmake and other tool hashes
and the runner image version. `/Brepro` is enabled for the core, but bit-identical
builds across different runner images are **not** claimed. Custom flags use CMake
`*_FLAGS_INIT`, preserving MSVC defaults; a cache guard requires exception
unwinding (`/EHsc`) and RTTI (`/GR`). `/GR` is explicitly added because modern
CMake policy CMP0117 leaves default compiler RTTI implicit. UTF-8 compilation is
explicit, including the Qt/Python interoperability cases.

OpenSSL **3.5.9 LTS** is built directly with its official Windows MSVC recipe,
`nmake`, and `no-asm`. This avoids the old upstream vcpkg baseline's OpenSSL 3.4.1
and removes NASM as a build input. Cryptographic assembly acceleration is absent;
all desktop input/clipboard features remain available. QtBase **6.10.2** contains
Core, Widgets, Network, Test and `windeployqt`; QtTools/GUI/installer builds are
unnecessary. Microsoft VC++ runtime DLLs are not redistributed: the destination
must already have an x64 VC++ 2022 runtime **14.44 or later**. No runtime installer
is launched by this workflow.

The job runs six selected upstream regression suites and runs ManagedSessionTests
again with XML output. The build rejects any skipped, missing or failed managed
case (17 Qt results), including the real Windows ACL/CreatePipe fixture.
The ACL regression verifies that inherited private Windows ACLs remain strict;
Qt's explicit POSIX permission overload creates additional metadata trustees,
so managed Windows files inherit and validate their private parent before data
is written. POSIX files still use explicit `0600` permissions.
Before compiling OpenSSL, a tiny native console probe uses the same CMake options
as the core and verifies exception-unwinding/RTTI macros, `dynamic_cast` and a
throw/catch at runtime. Invalid flags fail before the expensive dependency build. These tests use
QCoreApplication/synthetic backend facts; they do not grant portal consent, read
user clipboard contents or inject input. A green build does not replace testing
real desktop sessions on the three PCs.

The downloadable artifact contains:

- `engine/`: core, Qt/OpenSSL DLLs/plugins, license texts, Qt SBOM and build provenance.
- `artifact-manifest.json`: exact canonical Vincula `ArtifactManifest` v1; file
  names are relative to `engine/`, hashes cover all runtime/dependency/license files.
- `artifact-manifest.sha256`: manifest digest to review and approve externally.
- `sources/`: corresponding QtBase/OpenSSL source archives, upstream Deskflow
  archive, complete patch and build recipe. They are separate from runtime files
  so normal deployment checks do not reread source archives.

Every source/recipe archive hash is included in `engine/build-info.json`, which is
itself covered by the runtime manifest. Preserve `sources/` when redistributing
this engine. The upstream Deskflow and dependency license notices remain intact.
The build artifact has no signing/deployment credentials; an operator must approve
its manifest before the normal Vincula installation flow can use it.

Local runner equivalent, from a matching native MSVC x64 developer environment
with Python 3.12.10 and the required SDK/toolset already present:

```powershell
python -Werror -m unittest discover -s tools/build-managed-deskflow -p 'test_*.py' -v
python -Werror tools/build-managed-deskflow/build.py --work-root "$env:TEMP/vincula-managed-build-new"
```

The work directory must not already exist. Diagnostics are uploaded even on
failure. Native build command output is streamed and flushed to `reports/commands.log`,
so a cancelled compiler preserves its partial diagnostics.

Input provenance was checked on 2026-11-27 against primary sources:

- [Qt 6.10.2 MSVC archive and published checksums](https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/qt6_6102/qt6_6102/qt.qt6.6102.win64_msvc2022_64/).
- [QtBase corresponding source](https://download.qt.io/archive/qt/6.10/6.10.2/submodules/).
- [OpenSSL supported releases](https://openssl-library.org/source/) and
  [Windows build instructions at 3.5.9](https://github.com/openssl/openssl/blob/openssl-3.5.9/NOTES-WINDOWS.md).
- [Strawberry Perl 5.42.3.1 release and asset digest](https://github.com/StrawberryPerl/Perl-Dist-Strawberry/releases/tag/SP_54231_64bit).
- [CMake 4.2.3 checksums](https://github.com/Kitware/CMake/releases/download/v4.2.3/cmake-4.2.3-SHA-256.txt).
- [7-Zip 26.04 release and asset digest](https://github.com/ip7z/7zip/releases/tag/26.04).
- [GitHub Windows 2022 image contents](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md).
