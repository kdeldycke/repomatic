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

- **Ordinary pushes** compile only the `[tool.repomatic] nuitka.dev-targets` canary subset (default `["linux-arm64"]`, the fastest and cheapest builder; set `[]` to disable dev builds entirely). The binary self-tests in place inside the compile job and refreshes the rolling dev pre-release. This is a single job. It takes about 4.5 minutes when the compile cache hits, and about 12 minutes when it misses: [Compile caching](#compile-caching) says how often each one happens.
- **The weekly schedule** (Monday 00:43 UTC, batched with the other weekly maintenance jobs) rebuilds every target: it refreshes the dev pre-release with a complete binary set, and surfaces platform-specific breakage before release day. It also saves the compile cache of each target again. That does not keep the weekly-only targets warm: see [Compile caching](#compile-caching).
- **Release commits and manual `workflow_dispatch` runs** build the full fleet. Release commits additionally run the standalone `test-binaries` jobs, which re-validate each artifact on a pristine VM through the same upload/download round-trip a user's download takes.

Scheduled and dispatched runs own a run-scoped concurrency group, so a later push can neither cancel them nor be cancelled by them. Targets listed in `nuitka.unstable-targets` build with `continue-on-error`, and every compile job carries a 45-minute execution timeout so a hung build frees its runner slot instead of squatting it for GitHub's 360-minute default.

## Compile caching

Most of what Nuitka compiles is the standard library and dependencies, which barely change between builds, so the C-object caches are persisted across runs with `actions/cache` on canary and weekly builds. Release commits build cold, on purpose: see the [supply-chain posture](#supply-chain-posture) below. `NUITKA_CACHE_DIR` relocates every Nuitka cache (C objects, downloads, demoted-module bytecode) under the workspace, one cache entry per target, keyed per commit with a prefix `restore-keys` fallback: every caching run restores the latest previous snapshot and saves a fresh one, and the compiler caches hash compiler, flags and sources themselves, so a stale snapshot degrades to misses, never to wrong objects. A guard step resets any cache past 2 GiB, because Nuitka's clcache has no size cap. Since `4.3` it prunes by age: at most once a week, it deletes every entry written more than 30 days ago, and a cache hit does not refresh that date.

Each platform caches through a different mechanism:

| Target      | Object cache              | Provisioning                                                            |
| ----------- | ------------------------- | ----------------------------------------------------------------------- |
| `windows-*` | Nuitka's internal clcache | Built into Nuitka, on by default for MSVC.                              |
| `macos-*`   | none                      | ccache is disabled on macOS with `--disable-ccache`, see below.         |
| `linux-*`   | ccache `3.7.7`            | Installed from EPEL inside the `manylinux_2_28` container, non-fatally. |

The cache-prep step writes two non-default settings into `ccache.conf`, inside the persisted cache directory. `compiler_check = content` replaces the default, which hashes the compiler's mtime and size. `base_dir` points at the runner work root. ccache then hashes every path below that root as a relative path, so a work root that moves between machines keeps its cache keys. `base_dir` cannot hide a directory name that changes in the middle of a path, which is why the tool runner pins the path of Nuitka itself, as described below. The job also exports `CCACHE_DIR`. Since `4.3`, Nuitka gives ccache one subdirectory per Python ABI (`ccache/3.14`), and ccache reads `ccache.conf` from the directory it is given: without the variable, the file sits one level above and is never read.

Cold versus warm-cache execution time of the compile job, per target. The `linux-arm64` row holds the medians of the 43 builds measured below: the 23 builds without a hit give the cold figure, and the 20 builds with hits give the warm one. The other rows come from `7.9.0.dev0` (Nuitka `4.1.3`, ~470-495 C files per binary):

| Target          | Cold     | Warm       | Warm hit rate    |
| --------------- | -------- | ---------- | ---------------- |
| `linux-arm64`   | 11.8 min | 4.5 min    | 503 to 511 files |
| `linux-x64`     | 11.9 min | 3.4 min    | 470/470          |
| `windows-x64`   | 17.4 min | 6.0 min    | 495/495          |
| `windows-arm64` | 15.5 min | 6.5 min    | 494/495          |
| `macos-arm64`   | 10.5 min | not cached | 0/470 across VMs |
| `macos-x64`     | 11.0 min | not cached | 0/470 across VMs |

The residual warm time is the part caching cannot touch: Nuitka's Python-level compilation, linking, onefile payload compression, and the self-test. The one cache-missing module on warm Windows builds is the version module the pre-bake step stamps per build.

Nuitka turns its own install location into the `-I` flags of every C compilation, and ccache hashes those flags ([Nuitka#3996](https://github.com/Nuitka/Nuitka/issues/3996)). The compile cache therefore hits only while Nuitka sits at the same path. The [tool runner](tool-runner.md#nuitka) installs it into the project virtualenv before the build, and that path is the same on every runner, whatever the uv cache holds. {py:attr}`~repomatic.tooling.registry.ToolSpec.install_into_venv` is the switch.

In `7.17.1` and earlier releases, the tool runner layered Nuitka over the project virtualenv with `uv run --with`. uv keeps such a layer in a directory of its cache that it names at random (`…/setup-uv-cache/archive-v0/{random}/…`), so the hashed compiler arguments changed each time uv built the layer again. That defeated both of ccache's modes at once: the direct-mode manifest key hashes the raw arguments (two instrumented dispatches produced 471 manifest names each, zero overlap, with the argument lines byte-identical after normalizing the random segment), and preprocessor mode fails too because the preprocessed output's linemarkers embed the same paths. `base_dir` could not hide the directory: its random name sat below the runner work root.

uv gave the layer its random name when it built it, then kept it in its cache, so the name lasted as long as the uv cache did. `setup-uv` saves the uv cache under a key that names the architecture, the OS, the Python version and a hash of `pyproject.toml` and `uv.lock`. It saves a key once, and never writes to a key that exists.

On macOS and Windows the key carries the runner's own OS, and other jobs on the same runner share it. The uv cache that the compile job restores there holds no Nuitka, because another job saved it first. The compile job therefore built a new layer on every run. All 16 macOS and Windows compile jobs of the four weekly runs from 2026-09-21 to 2026-10-12 did so. This is the cause of the macOS 0/470 hit rate in the table above.

macOS ccache stays disabled anyway (`--disable-ccache`): Nuitka's auto-downloaded `4.2.1` x86-64 build ships without a checksum ([Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997)) and runs under Rosetta on arm, where the wrapper overhead measurably slowed builds. Re-enabling would take a native, checksummed ccache handed to Nuitka via its `NUITKA_CCACHE_BINARY` hook, for a payoff bounded by the weekly-and-releases cadence.

Linux resolved Nuitka through the same kind of directory, and hit anyway while its `setup-uv` key lasted. Its compile job runs in the `manylinux_2_28` container, so its key carries `almalinux-8.10`, which no job outside that container carries. The compile job saved the uv cache itself, with the Nuitka layer in it. Each later build under the same key got the same directory name back, and ccache hit.

The directory name on Linux therefore changed with the `setup-uv` key, each time `pyproject.toml` or `uv.lock` changed. Between 2026-09-20 and 2026-10-12, under that layered install, 43 `linux-arm64` builds used the compile cache, on Nuitka `4.2.1` to `4.3` and 508 to 512 C files. They fall into two groups:

| Builds | Cache hits       | Duration of the compile job   |
| -----: | ---------------- | ----------------------------- |
|     20 | 503 to 511 files | 3.6 to 5.8 min, median 4.5    |
|     23 | none             | 10.4 to 13.2 min, median 11.8 |

The 23 builds without a hit had five causes:

| Cause                                                                                                                                                                                                                            | Builds |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -----: |
| A change to `pyproject.toml` or `uv.lock` gave the job a new `setup-uv` key, and uv built a new Nuitka environment.                                                                                                              |     18 |
| The Nuitka pin moved and the key did not. The restored uv cache held no layer for the new version, so uv built one on every run, and `setup-uv` does not save under a key that exists. Every build missed until the key changed. |      2 |
| One of the 18 builds above also ran without ccache, because the EPEL download failed. The build after it found no object for the new environment.                                                                                |      1 |
| The 2 GiB guard reset the cache.                                                                                                                                                                                                 |      1 |
| On Nuitka `4.3`, a commit changed a header that every C file includes (see below).                                                                                                                                               |      1 |

A build that misses every file stores a complete new set of objects, about 93 MiB on `linux-arm64`, beside the sets that no build can use again. The limit of ccache itself is 5 GB by default, above the guard, so the cache only grows. It went from 747 MiB to 2064 MiB in 11 days, then the guard reset it.

The install into the virtualenv removes the first two causes, 20 builds of the 23. A probe measured it on two `linux-arm64` runners that shared a compile cache and no uv cache. With Nuitka in the virtualenv, the second runner hit 510 of 512 files. With the layered install, it hit 0.

A frame type description is a short string that lists the kinds of the local variables of one function. Since `4.3`, Nuitka declares every distinct description of the whole program in `__constants.h`, and every generated C file includes that header. A commit that adds or removes one distinct description therefore invalidates every object. `4.2.2` kept no program-wide list. The C code that `--generate-c-only` writes on macOS for [`ad9505be4`](https://github.com/kdeldycke/repomatic/commit/ad9505be46edd6cfaf1e48f2a653149e50f8d6dc) and for [`1db4858b3`](https://github.com/kdeldycke/repomatic/commit/1db4858b3ce0984085e33480d9d061aa3c4cd6a0) shows one such change. Of 334 declarations, the second commit adds one and drops one. The canary build of the second commit missed all 512 files, and a second build of the same commit hit 511. [Nuitka#4087](https://github.com/Nuitka/Nuitka/issues/4087) reports this behavior, and repomatic has no workaround for it.

The three targets that only the weekly run builds did not stay warm in that period. In the four weekly runs, `linux-x64`, `windows-x64` and `windows-arm64` found no compile cache to restore in 7 builds of 12. Each of the 12 builds saved its own. GitHub evicts an entry that no run used for 7 days, and the weekly run comes back after 7 days. Of the 5 builds that restored a cache, 4 hit nothing. `linux-x64` started all four builds under a new `setup-uv` key. The cause of the Windows misses is not identified. The one build with hits was `windows-x64`, with 472 files of 530.

To inspect a cache, the compile job logs Nuitka's own hit/miss summary (`Cached C files (using ccache) with result …`) plus a per-subdirectory size report. To reset one, delete its entries: `gh cache list --key nuitka-` then `gh cache delete {key}`; the next build re-seeds it.

## Supply-chain posture

The compile job resolves every input through a pinned, cooldown-gated channel: the CLI and its dependencies from the hash-pinned `uv.lock` under the workflow-wide `UV_EXCLUDE_NEWER`, Nuitka itself as an exact registry pin walked forward under `minimum-release-age`, actions as SHA pins, and the Linux ccache from EPEL, a GPG-verified distro archive that the cooldown doctrine deliberately leaves to the distro's own staging.

The compile cache is the one input no cooldown concept covers, and it gets a structural control instead. GitHub scopes cache writes by ref, so fork pull requests can never write entries the engine restores; the residual writer set is code already running in main-ref workflows, which is why every action there is SHA-pinned and cooled. Against the remaining sliver (a cache entry is a tar archive extracted into the workspace, and a poisoned compiler object would flow silently into a signed, published, immutable binary), **release commits neither restore nor save the cache**: every published artifact compiles from source on a fresh VM, and cache contents can influence only throwaway canary and weekly builds. macOS additionally runs with ccache disabled, removing the one unchecksummed download (Nuitka's `ccache-4.2.1.zip` from nuitka.net, fetched with no hash at the call site, [Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997)) from the pipeline.

Detection layers back the prevention: artifact attestation binds every binary to the exact run that built it, `verify-binary` checks architecture and OS floors, the self-test executes the binary, and every release is submitted to VirusTotal. None of these would catch a competent backdoor on their own; the cold-release rule is what keeps them from having to.

## Link-time optimization

Nuitka's `--lto=auto` (the default, and what repomatic runs) disables LTO outright above 250 compiled modules, and these builds sit far past that threshold, so no LTO runs and no flag is set in `[tool.nuitka]`. Forcing `--lto=yes` would slow every build for a marginal runtime gain on a CLI whose cost is cold-start, and on MSVC it would compile objects with `/GL`, which no object cache can store. Leave it alone.

## Upstream workarounds

Three Nuitka issues shape the integration; each workaround names its ticket and lives next to the code it patches:

- [Nuitka#3909](https://github.com/Nuitka/Nuitka/issues/3909): since `4.2` Nuitka reads `[tool.nuitka]`, but only under `--project`, a mode that takes its entry points from the project configuration and refuses `--main-entry-point`. repomatic compiles one entry point per job and names it on the command line, so the [tool runner](tool-runner.md#nuitka) keeps translating the section into CLI flags at build time. Two upstream defects block the swap even for a project willing to leave its entry points to that mode: `--project` needs `uv_build` inside Nuitka's own environment ([Nuitka#3967](https://github.com/Nuitka/Nuitka/issues/3967)), and warns on four standard `[tool.uv]` keys ([Nuitka#3968](https://github.com/Nuitka/Nuitka/issues/3968)).
- [Nuitka#3996](https://github.com/Nuitka/Nuitka/issues/3996): Nuitka passes its own include directories to the compiler as absolute `-I` paths, and `enableCcache()` never sets `CCACHE_BASEDIR`. A per-run install path then defeats ccache across machines. This caused the macOS 0/470 hit rate, and most of the Linux builds without a hit documented above. The tool runner works around it: it installs Nuitka into the project virtualenv, at a path that does not change. `CCACHE_BASEDIR` would not help, because it cannot hide a random directory name. The release engine's cache-prep step still writes `base_dir` into `ccache.conf`, which covers a work root that moves.
- [Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997): `getCachedDownload()` fetches and executes build tools (ccache, winlibs MinGW64, `depends.exe`, `appimagetool`, NSIS) with no integrity check, trusting TLS alone. repomatic's exposure is the macOS ccache auto-download; `--disable-ccache` (unconditional on macOS, see [supply-chain posture](#supply-chain-posture)) is what keeps that unverified binary out of the pipeline until Nuitka pins a digest at the call site.

Nuitka `4.3` fixed five other reports, and repomatic dropped the workaround for each: [](upstream.md#upstreamed-from-repomatic) lists them. One more report is open and has no workaround: [Nuitka#4087](https://github.com/Nuitka/Nuitka/issues/4087), which [Compile caching](#compile-caching) describes.

```{todo}
Delete the `ccache.conf` `base_dir` write once [Nuitka#3996](https://github.com/Nuitka/Nuitka/issues/3996) ships. Check then whether the install into the project virtualenv can go too. It can only if the fix keeps Nuitka's install path out of the hashed compiler arguments. The two other shims have no deadline. [Nuitka#3909](https://github.com/Nuitka/Nuitka/issues/3909) shipped in `4.2` and retired nothing, because the fix reaches `--project` builds only. On [Nuitka#3997](https://github.com/Nuitka/Nuitka/issues/3997) the maintainer has not declined the request: he asked how digests could stay current across architectures without breaking older releases whose downloads move, and said tooling for it would be welcome, so the unconditional macOS `--disable-ccache` stays until someone writes that.
```

## Troubleshooting

- **A build fails outright**: the job uploads Nuitka's `nuitka-crash-report.xml` as a run artifact before the failure gate trips.
- **`verify-binary` fails**: a toolchain, runner-image or container update raised the binary's actual glibc or macOS floor above the declared one. That gate exists precisely to stop the floor from drifting silently; fix the environment rather than the declaration.
- **A release shipped without some binaries**: expected behavior, not an incident. Publishing is deliberately not held hostage by a failing build cell, and immutable releases make the gap permanent for that version; the next release carries the missing target. See the [release workflow documentation](workflows.md#publish-release-publish-release).
- **A cache behaves suspiciously**: caches self-invalidate on compiler and source changes and auto-reset past 2 GiB, so poisoning is unlikely; deleting the target's `nuitka-*` cache entries forces a clean rebuild.
