# Deskflow configuration parser probe

This headless QA program calls the real Deskflow `Settings::knownComputers()`
and `server::Config` parser. It reports computer names and directional links as
JSON. Deskflow 1.27 loads names from `[computer_<name>]` groups with `name=...`
in the general INI file; its old layout `section: screens` is ignored.

The C++ source is the exact probe used to reproduce the first live configuration
failure and verify its correction. SHA256:
`e2787e787dab40f409e192a0cf931e6411245130ab13824820a209014e1f811b`.
It uses `QCoreApplication`, with no server/client, graphical application,
computer backend, listener, portal, input hook or clipboard provider constructed.
Linking platform libraries does not activate their backends.

## Build from existing libraries

Use the patched Deskflow source at commit
`081f6478e654a112168879e67a41ce7d7e4ae3af`, built with `BUILD_GUI=OFF`,
`BUILD_TESTS=ON`, `SKIP_BUILD_TESTS=ON` and single-configuration Ninja. Build the
core and required library dependencies first, as documented in
[the managed patch instructions](../../third_party/deskflow/README.md).
The `ServerConfigTests` target must be configured; its executable need not
already exist. Reuse the same compiler, Qt/SDK and dependency runtime paths.

From the Vincula checkout:

```sh
python3 -I -B tools/qa-desktop-config/build_probe.py \
  --build /absolute/deskflow/build-managed \
  --output /absolute/private-qa/config-probe-build
```

The output directory must not exist; its parent must exist. The helper reads
the existing CMake cache and asks Ninja for the `ServerConfigTests` commands.
It preserves that target's compiler flags and linked libraries while replacing
only the test source/objects and output paths. Dependency-file outputs pointing
into the upstream build are removed. It runs no shell, rebuilds no upstream
target and writes only the fresh output directory. Commands, hashes and logs
are recorded there. No system packages or settings are changed, and the probe
is not automatically run. The helper currently supports Linux/Ninja, not MSVC.

If an isolated SDK needs loader paths, supply its approved `LD_LIBRARY_PATH`
only to these commands, just as for the existing Deskflow unit tests. The helper
does not alter the compiler's normal dependency selection.

## Run the regression

From the Vincula checkout:

```sh
VINCULA_DESKFLOW_CONFIG_PROBE=/absolute/private-qa/config-probe-build/config-probe \
  python3 -B -W error -m unittest discover -s agent \
  -p test_desktop_deskflow.py -v
```

The selected integration test generates a four-node profile, checks all names
and reciprocal neighbors through the actual parser, then verifies that the old
screens-only declaration is rejected. It removes display, Wayland and session
bus variables and isolates **config, data, cache and state** paths in its private
temporary directory. The optional integration test is explicitly skipped when
the probe environment variable is absent.

Direct invocation accepts `config-probe SETTINGS_INI LAYOUT_CONF`. For direct
runs, first set `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and
`XDG_STATE_HOME` to private disposable directories: `Settings` can normalize and
write settings/state even though this is a parser-only test. Use synthetic QA
files, not active deployment profiles. No display or clipboard is required.

This checks configuration syntax and semantics. It does not establish TLS peer
identity, desktop consent, shared input or clipboard readiness.
