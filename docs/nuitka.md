# {octicon}`file-binary` Nuitka compilation

The release engine compiles every selected `[project.scripts]` entry point into a self-contained, one-file executable with [Nuitka](https://github.com/Nuitka/Nuitka), for each supported platform and architecture. This page is the canonical home for how those builds work and how fast they are; the surfaces it drives are documented on their own pages:

| Page                                                                         | Covers                                                                                           |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| [Installation](install.md)                                                   | Download URLs and release attestation checks.                                                    |
| [Binaries](binaries.md)                                                      | The per-release executable catalog, VirusTotal scans, minimum OS floors, and development builds. |
| [Configuration](configuration.md)                                            | The `[tool.repomatic]` `nuitka.*` options (`dev-targets`, `entry-points`, `extras`, …).          |
| [Tool runner](tool-runner.md#nuitka)                                         | The pinned Nuitka version and the `[tool.nuitka]` → CLI-flags bridge.                            |
| [Reusable workflows](workflows.md#github-workflows-release-engine-yaml-jobs) | The `compile-binaries` and `test-binaries` job wiring inside the release engine.                 |

## Build targets

One compile job per target, each on one of the six runners the test matrix already covers, so a published binary is built on an image the suite is validated against (see the [runner inventory](test-matrix.md#github-hosted-runner-inventory)):

```{python:render}
from repomatic.release.binary import NUITKA_BUILD_TARGETS

print("| Target | Runner | Architecture | Extension |")
print("| ------ | ------ | ------------ | --------- |")
for target_id, target in NUITKA_BUILD_TARGETS.items():
    print(
        f"| `{target_id}` | `{target.runner}` | `{target.arch.id}` "
        f"| `.{target.extension}` |"
    )
```

Linux targets compile inside digest-pinned `manylinux_2_28` containers and macOS targets pin `MACOSX_DEPLOYMENT_TARGET`, so every binary carries a stable, measured OS floor instead of inheriting whatever the runner image ships. The floors, and what they open execution to, are documented in [](binaries.md#minimum-os-requirements); {py:func}`repomatic.release.binary.verify_binary_floor` fails the build when a linked floor exceeds the declared one.

## Build cadence

Compiling the full fleet on every push is the expensive habit this design retired: a release push triggers ~96 jobs against an account-wide cap of 40 concurrent Linux runners and 5 macOS ones, and queue time, not compile time, dominated release latency. Three cadences share the work:

- **Ordinary pushes** compile only the `[tool.repomatic] nuitka.dev-targets` canary subset (default `["linux-arm64"]`, the fastest and cheapest builder; set `[]` to disable dev builds entirely). The binary self-tests in place inside the compile job and refreshes the rolling dev pre-release. With a warm cache this is a single ~3-minute job.
- **The weekly schedule** (Monday 00:43 UTC, batched with the other weekly maintenance jobs) rebuilds every target: it refreshes the dev pre-release with a complete binary set, keeps each target's compile cache warm (GitHub evicts cache entries unused for 7 days), and surfaces platform-specific breakage before release day.
- **Release commits and manual `workflow_dispatch` runs** build the full fleet. Release commits additionally run the standalone `test-binaries` jobs, which re-validate each artifact on a pristine VM through the same upload/download round-trip a user's download takes.

Scheduled and dispatched runs own a run-scoped concurrency group, so a later push can neither cancel them nor be cancelled by them. Targets listed in `nuitka.unstable-targets` build with `continue-on-error`, and every compile job carries a 45-minute execution timeout so a hung build frees its runner slot instead of squatting it for GitHub's 360-minute default.

## Compile caching

Most of what Nuitka compiles is the standard library and dependencies, which barely change between builds, so the C-object caches are persisted across runs with `actions/cache` on canary and weekly builds. Release commits build cold, on purpose: see the [supply-chain posture](#supply-chain-posture) below. `NUITKA_CACHE_DIR` relocates every Nuitka cache (C objects, downloads, demoted-module bytecode) under the workspace, one cache entry per target, keyed per commit with a prefix `restore-keys` fallback: every caching run restores the latest previous snapshot and saves a fresh one, and the compiler caches hash compiler, flags and sources themselves, so a stale snapshot degrades to misses, never to wrong objects. A guard step resets any cache past 2 GiB, because Nuitka's clcache never prunes itself.

Each platform caches through a different mechanism:

| Target      | Object cache              | Provisioning                                                            |
| ----------- | ------------------------- | ----------------------------------------------------------------------- |
| `windows-*` | Nuitka's internal clcache | Built into Nuitka, on by default for MSVC.                              |
| `macos-*`   | none                      | ccache is disabled on macOS with `--disable-ccache`, see below.         |
| `linux-*`   | ccache `3.7.7`            | Installed from EPEL inside the `manylinux_2_28` container, non-fatally. |

ccache needs three non-default settings to hit across runner VMs, written into `ccache.conf` inside the persisted cache directory (a channel the compiler wrapper provably reads): `compiler_check = content` (the default hashes the compiler's mtime and size), `base_dir` pointing at the runner work root (see the root cause below), and `sloppiness = time_macros`. The last one is currently inert ([Nuitka#3998](https://github.com/Nuitka/Nuitka/issues/3998)): Nuitka unconditionally overrides `CCACHE_SLOPPINESS` with its own `include_file_ctime,include_file_mtime` value in the compiler environment, and environment variables outrank the config file.

Measured on `7.9.0.dev0` (Nuitka `4.1.3`, ~470-495 C files per binary), cold versus warm-cache execution time of the compile job:

| Target          | Cold     | Warm       | Warm hit rate    |
| --------------- | -------- | ---------- | ---------------- |
| `linux-arm64`   | 9.4 min  | 3.0 min    | 470/470          |
| `linux-x64`     | 11.9 min | 3.4 min    | 470/470          |
| `windows-x64`   | 17.4 min | 6.0 min    | 495/495          |
| `windows-arm64` | 15.5 min | 6.5 min    | 494/495          |
| `macos-arm64`   | 10.5 min | not cached | 0/470 across VMs |
| `macos-x64`     | 11.0 min | not cached | 0/470 across VMs |

The residual warm time is the part caching cannot touch: Nuitka's Python-level compilation, linking, onefile payload compression, and the self-test. The one cache-missing module on warm Windows builds is the version module the pre-bake step stamps per build.

macOS was the measured exception, and the cause is now proven, not mysterious: **uv extracts packages into a randomly-named cache directory (`…/setup-uv-cache/archive-v0/{random}/…`), and Nuitka's own include paths resolve through it into the compiler's `-I` flags**, so the hashed compiler arguments change on every VM. That defeats both of ccache's modes at once: the direct-mode manifest key hashes the raw arguments (two instrumented dispatches produced 471 manifest names each, zero overlap, with the argument lines byte-identical after normalizing the random segment), and preprocessor mode fails too because the preprocessed output's linemarkers embed the same paths. Linux escaped only by layout luck: its venv hardlinks keep stable in-venv paths. `base_dir` is the ccache feature built for this, and the cache-prep step now sets it, insuring the Linux cache against any future layout change.

macOS ccache stays disabled anyway (`--disable-ccache`): Nuitka's auto-downloaded `4.2.1` x86-64 build ships without a checksum ([Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997)) and runs under Rosetta on arm, where the wrapper overhead measurably slowed builds. Re-enabling would take a native, checksummed ccache handed to Nuitka via its `NUITKA_CCACHE_BINARY` hook, for a payoff bounded by the weekly-and-releases cadence.

To inspect a cache, the compile job logs Nuitka's own hit/miss summary (`Cached C files (using ccache) with result …`) plus a per-subdirectory size report. To reset one, delete its entries: `gh cache list --key nuitka-` then `gh cache delete {key}`; the next build re-seeds it.

## Supply-chain posture

The compile job resolves every input through a pinned, cooldown-gated channel: the CLI and its dependencies from the hash-pinned `uv.lock` under the workflow-wide `UV_EXCLUDE_NEWER`, Nuitka itself as an exact registry pin walked forward under `minimum-release-age`, actions as SHA pins, and the Linux ccache from EPEL, a GPG-verified distro archive that the cooldown doctrine deliberately leaves to the distro's own staging.

The compile cache is the one input no cooldown concept covers, and it gets a structural control instead. GitHub scopes cache writes by ref, so fork pull requests can never write entries the engine restores; the residual writer set is code already running in main-ref workflows, which is why every action there is SHA-pinned and cooled. Against the remaining sliver (a cache entry is a tar archive extracted into the workspace, and a poisoned compiler object would flow silently into a signed, published, immutable binary), **release commits neither restore nor save the cache**: every published artifact compiles from source on a fresh VM, and cache contents can influence only throwaway canary and weekly builds. macOS additionally runs with ccache disabled, removing the one unchecksummed download (Nuitka's `ccache-4.2.1.zip` from nuitka.net, fetched with no hash at the call site, [Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997)) from the pipeline.

Detection layers back the prevention: artifact attestation binds every binary to the exact run that built it, `verify-binary` checks architecture and OS floors, the self-test executes the binary, and every release is submitted to VirusTotal. None of these would catch a competent backdoor on their own; the cold-release rule is what keeps them from having to.

## Link-time optimization

Nuitka's `--lto=auto` (the default, and what repomatic runs) disables LTO outright above 250 compiled modules, and these builds sit far past that threshold, so no LTO runs and no flag is set in `[tool.nuitka]`. Forcing `--lto=yes` would slow every build for a marginal runtime gain on a CLI whose cost is cold-start, and on MSVC it would compile objects with `/GL`, which no object cache can store. Leave it alone.

## Upstream workarounds

Six Nuitka issues shape the integration; each workaround names its ticket and lives next to the code it patches. The last three were all filed the same day from auditing this page's own § Compile caching section, and cross-reference each other upstream:

- [Nuitka#3879](https://github.com/Nuitka/Nuitka/issues/3879): a `__main__.py` entry point must be compiled as its package directory plus `--python-flag=-m`, or the binary silently exits. {py:attr}`Metadata.nuitka_matrix <repomatic.metadata.matrix.MatrixMetadata.nuitka_matrix>` computes the flag per entry point; the same property documents why Nuitka 4.1's `--main-entry-point` flag cannot replace the positional form yet.
- [Nuitka#3909](https://github.com/Nuitka/Nuitka/issues/3909): Nuitka does not read `[tool.nuitka]` natively, so the [tool runner](tool-runner.md#nuitka) translates the section into CLI flags at build time.
- [Nuitka#3994](https://github.com/Nuitka/Nuitka/issues/3994): a symlink inside an `--include-data-dir` tree ships dangling (and crashes macOS codesigning), so the tool runner stages a symlink-free copy of any such directory before invoking Nuitka. Upstream confirmed the diagnosis; a real fix lands no earlier than Nuitka 4.3, and the staging shim stays until then.
- [Nuitka#3996](https://github.com/Nuitka/Nuitka/issues/3996): `enableCcache()` never sets `CCACHE_BASEDIR`, so a per-run install path (uv's randomly-named package cache) leaks into Nuitka's injected `-I` flags and defeats ccache across machines, the root cause of the macOS 0/470 hit rate documented above. The release engine's cache-prep step works around it by writing `base_dir` into `ccache.conf` directly, which also insures the Linux cache against the same fragility.
- [Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997): `getCachedDownload()` fetches and executes build tools (ccache, winlibs MinGW64, `depends.exe`, `appimagetool`, NSIS) with no integrity check, trusting TLS alone. repomatic's exposure is the macOS ccache auto-download; `--disable-ccache` (unconditional on macOS, see [supply-chain posture](#supply-chain-posture)) is what keeps that unverified binary out of the pipeline until Nuitka pins a digest at the call site.
- [Nuitka#3998](https://github.com/Nuitka/Nuitka/issues/3998): `enableCcache()` overwrites a user- or CI-set `CCACHE_SLOPPINESS` unconditionally, unlike the neighboring `CCACHE_DIR` handling, which is guarded. The `sloppiness = time_macros` line the cache-prep step writes into `ccache.conf` is consequently dead, same as the job-level `CCACHE_SLOPPINESS` env var above it: both are kept because they cost nothing and start working the moment Nuitka mirrors its own `CCACHE_DIR` guard.

```{todo}
Retire each shim as its ticket ships: the `--python-flag=-m` computation on [Nuitka#3879](https://github.com/Nuitka/Nuitka/issues/3879), the `[tool.nuitka]` translation on [Nuitka#3909](https://github.com/Nuitka/Nuitka/issues/3909), the symlink-free staging copy on [Nuitka#3994](https://github.com/Nuitka/Nuitka/issues/3994) (Nuitka 4.3 at the earliest), the `ccache.conf` `base_dir` write on [Nuitka#3996](https://github.com/Nuitka/Nuitka/issues/3996), and the unconditional macOS `--disable-ccache` on [Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997). The `sloppiness` and `CCACHE_SLOPPINESS` settings need no edit on [Nuitka#3998](https://github.com/Nuitka/Nuitka/issues/3998): they start working on their own.
```

## Troubleshooting

- **A build fails outright**: the job uploads Nuitka's `nuitka-crash-report.xml` as a run artifact before the failure gate trips.
- **`verify-binary` fails**: a toolchain, runner-image or container update raised the binary's actual glibc or macOS floor above the declared one. That gate exists precisely to stop the floor from drifting silently; fix the environment rather than the declaration.
- **A release shipped without some binaries**: expected behavior, not an incident. Publishing is deliberately not held hostage by a failing build cell, and immutable releases make the gap permanent for that version; the next release carries the missing target. See the [release workflow documentation](workflows.md#publish-release-publish-release).
- **A cache behaves suspiciously**: caches self-invalidate on compiler and source changes and auto-reset past 2 GiB, so poisoning is unlikely; deleting the target's `nuitka-*` cache entries forces a clean rebuild.
