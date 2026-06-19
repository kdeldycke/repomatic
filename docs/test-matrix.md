# Test matrix

`repomatic` builds two GitHub Actions test matrices for every project: a **full matrix** (pushes to the default branch and scheduled runs) and a reduced **pull-request matrix** (fast feedback on PRs). Both are pre-computed by the [`metadata` job](workflows.md) from the project's [`[tool.repomatic.test-matrix.*]`](configuration.md) configuration, so a project shapes its matrix without hand-editing workflow YAML.

This page is the guide: how to decide what the matrix should test, which GitHub-hosted runners exist and how they trade off on speed, and a worked example. For the per-key configuration reference (types, defaults), see the [configuration page](configuration.md).

## How the matrix is built

The base axes are `os` and `python-version`, seeded from `repomatic`'s defaults (`TEST_RUNNERS_FULL`/`TEST_RUNNERS_PR` and `TEST_PYTHON_FULL`/`TEST_PYTHON_PR` in `repomatic/test_matrix.py`). A project then reshapes the matrix through a fixed chain of transformations, each a `[tool.repomatic.test-matrix.*]` key, applied in this order:

1. `replace`: swap axis values in place.
2. `remove`: drop axis values from an axis.
3. `variations`: add extra axis values (full matrix only), including brand-new axes.
4. `exclude`: remove specific combinations.
5. `include`: add or augment combinations. GitHub processes `include` after `exclude`, so an `include` can add back a combination an `exclude` removed.

A separate `unstable` pass (full matrix only) flags matching combinations `continue-on-error`. Because the order is fixed, the transforms compose predictably. `variations` and `unstable` touch only the full matrix, keeping the PR matrix a small curated set. See [workflows § Dynamic test matrices](workflows.md) for why this exists (GitHub's static `strategy.matrix` cannot express it) and the [configuration reference](configuration.md) for each key.

## Choosing what to test

A matrix is a budget. Every cell costs runner minutes and adds to wall-clock. Spend the budget where a failure is both likely and informative; keep everything speculative cheap.

### Cover the shipped configuration broadly

The combination your users actually install — released dependencies on a stable Python — earns the widest spread of operating systems and Python versions. This is the core of the matrix: a regression here reaches everyone, so it is worth catching on every platform.

### Probe forward-looking axes narrowly

Anything not yet shipped is an early-warning signal, not a support promise: a prerelease or free-threaded Python, a dependency's development branch, an unreleased build. Run each on a *single* runner. If it breaks you want a heads-up, not a cross-platform report, and once that version ships the broad shipped-config coverage picks it up anyway. Flag these jobs `continue-on-error` through [`test-matrix.unstable`](configuration.md) so an expected breakage does not fail the build.

### Pin the dependency floor and any known-regression release

When a project supports a *range* of a core dependency (say `>= 2.3`), CI by default only ever exercises whatever the lockfile resolves to, usually the newest version. The floor is declared but never verified, so it rots silently until a downstream user on an older version hits the break. Add the floor as an explicit matrix value so the bottom of the range runs on every CI pass.

Add any single mid-range release whose behavior a workaround specifically targets, too. That release is the one version where the shim is load-bearing, so it is the one version that catches the shim regressing: bracketing the range with floor and latest alone would miss it.

### Select runners by measured speed, not architecture

When you reduce to one runner per OS, pick the *fastest* one for your workload, measured from your own CI. Do not reflexively choose the ARM image because it is "the future": architecture speed is not uniform across operating systems (see the inventory below), and the faster choice differs per platform. When you do not need to test both architectures of an OS, drop the slower twin entirely rather than carrying it.

## GitHub-hosted runner inventory

`repomatic`'s full matrix spans both architectures of each OS; the reduced PR set keeps one per OS. The runners (defined in `repomatic/test_matrix.py`):

| Runner             | OS      | Architecture          | In PR set | Notes                                     |
| :----------------- | :------ | :-------------------- | :-------- | :---------------------------------------- |
| `ubuntu-24.04-arm` | Linux   | ARM64                 | no        | Fastest Linux in measurements below.      |
| `ubuntu-slim`      | Linux   | x86-64                | yes       | Lean image; also the default light-job runner. |
| `macos-26`         | macOS   | ARM64 (Apple silicon) | yes       | Faster of the two macOS images.           |
| `macos-26-intel`   | macOS   | x86-64                | no        | Legacy Intel.                             |
| `windows-11-arm`   | Windows | ARM64                 | no        | Slower than `windows-2025` on recent Python. |
| `windows-2025`     | Windows | x86-64                | yes       | Faster of the two Windows images.         |

### Speed tendencies

Relative speed is workload-dependent, so the only authoritative numbers are your own. The tendencies below come from running one project's full test suite across all six runners, and they are consistent enough to plan around:

- **Linux: ARM is much faster.** `ubuntu-24.04-arm` ran the suite roughly twice as fast as `ubuntu-slim` at every Python version. `ubuntu-slim` is a deliberately lean image (`repomatic`'s default for light mechanical jobs, where small size and tool availability matter more than throughput), so it is the slower choice for a heavy test suite.
- **macOS: Apple silicon beats Intel**, by a smaller margin (high single digits to ~20%). macOS is the slowest tier overall by a wide margin and tends to gate the full matrix's wall-clock, so a single slow macOS cell can dominate total time.
- **Windows: x86 is faster than ARM on recent Python.** `windows-2025` beat `windows-11-arm` on free-threaded and prerelease builds, with ARM ahead only marginally on the latest stable build. The ARM-is-faster intuition inverts here.

```{caution}
These figures are one project's, from a single run, and they drift. Runner images are re-provisioned, new images appear and old ones are retired, and a slow outlier can be a transient runner stall rather than a property of the image. Treat them as a starting hypothesis, not a constant, and re-confirm against your own timings.
```

### Measuring your own

Read the per-job durations from a recent full-matrix run and compare the same configuration across architectures:

```shell-session
$ gh run list --workflow=tests.yaml --event=push --status=success --limit=5
$ gh run view {run-id} --json jobs
```

Each job carries `startedAt` and `completedAt`; the difference is its wall-clock. Compare cells that differ only in `os` (same Python, same dependency versions) to isolate the architecture's effect, and prefer the median across a few runs to smooth out stalls.

## Worked example: widening a dependency's supported range

Suppose a project lowers its floor on a core dependency `acme` from `>= 5` to `>= 4.2` to install in more environments. It carries small shims for APIs that changed in `acme 5.0`, and one of those shims works around a regression that existed only in `acme 5.0` (fixed in `5.0.1`). The matrix should verify the whole `>= 4.2` range without ballooning, and keep the speculative jobs fast.

```toml
[tool.repomatic]
# Drop the slower-architecture runner of each OS, keeping the faster twin
# (measured here: Intel macOS and ARM Windows are the slower images).
test-matrix.remove.os = ["macos-26-intel", "windows-11-arm"]

# Add the floor (4.2), the regression release (5.0), and the development
# branch alongside whatever the lockfile resolves to.
test-matrix.variations.acme-version = ["4.2", "5.0", "released", "main"]

# Pin the floor, the regression release, and the dev branch to the single
# fastest runner; the shipped config (released acme) keeps the full spread.
# After the remove above, the non-pinned runners are ubuntu-slim, macos-26,
# and windows-2025.
test-matrix.exclude = [
  { "os" = "ubuntu-slim", "acme-version" = "4.2" },
  { "os" = "macos-26", "acme-version" = "4.2" },
  { "os" = "windows-2025", "acme-version" = "4.2" },
  { "os" = "ubuntu-slim", "acme-version" = "5.0" },
  { "os" = "macos-26", "acme-version" = "5.0" },
  { "os" = "windows-2025", "acme-version" = "5.0" },
  { "os" = "ubuntu-slim", "acme-version" = "main" },
  { "os" = "macos-26", "acme-version" = "main" },
  { "os" = "windows-2025", "acme-version" = "main" },
]

# The unreleased acme branch is an early-warning probe: never fail the build on it.
test-matrix.unstable = [{ "acme-version" = "main" }]
```

The full matrix resolves to three slices:

| Slice                                  | Runs on                                   | `continue-on-error` |
| :------------------------------------- | :---------------------------------------- | :------------------ |
| `released` acme (the shipped config)   | all four retained OSes × every Python     | no                  |
| `4.2` floor and `5.0` regression       | `ubuntu-24.04-arm` × every Python         | no                  |
| `main` acme (dev-branch early warning) | `ubuntu-24.04-arm` × every Python         | yes                 |

The shipped configuration is exercised everywhere a regression would reach a user; the floor and the one regression-prone release are verified cheaply on the fastest runner; and the development branch gives a heads-up without the power to redden the build. The PR matrix stays the curated reduced set for fast feedback, since `variations` and `unstable` apply to the full matrix only. The same shape extends to a prerelease or free-threaded Python: add it as a `python-version` variation, pin it to one runner with `exclude`, and mark it `unstable`.
