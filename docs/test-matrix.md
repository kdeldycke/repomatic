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

The phrase *for your workload* is load-bearing. The architecture gap is wide for a parallel, compute-heavy job (a `pytest --numprocesses=auto` suite that scales with cores) and narrow-to-nonexistent for a job dominated by checkout and dependency install. So the right runner differs by job *type*, not just by project: see [§ Architecture speed is workload-dependent](#architecture-speed-is-workload-dependent) for the split `repomatic` measured between its heavy test suite and its light mechanical jobs.

## GitHub-hosted runner inventory

`repomatic`'s full matrix spans both architectures of each OS; the reduced PR set keeps one per OS. The runners (defined in `repomatic/test_matrix.py`):

| Runner             | OS      | Architecture          | In PR set | Notes                                                                    |
| :----------------- | :------ | :-------------------- | :-------- | :----------------------------------------------------------------------- |
| `ubuntu-24.04-arm` | Linux   | ARM64                 | yes       | Fastest on the parallel test suite; the PR Linux pick.                   |
| `ubuntu-slim`      | Linux   | x86-64                | no        | Lean light-job default; full test matrix only; slowest on a heavy suite. |
| `macos-26`         | macOS   | ARM64 (Apple silicon) | yes       | Faster macOS image, and fast overall.                                    |
| `macos-26-intel`   | macOS   | x86-64                | no        | Legacy Intel; ~2x slower than `macos-26`.                                |
| `windows-11-arm`   | Windows | ARM64                 | no        | Compute ties `windows-2025`; ~50s slower per job (Codecov upload).       |
| `windows-2025`     | Windows | x86-64                | yes       | Faster per job; compute tied with `windows-11-arm`.                      |

### Speed tendencies

Relative speed is workload-dependent, so the only authoritative numbers are your own. The tendencies below come from `repomatic`'s own full test suite, taken as the median across the five most recent successful runs on all six runners. Two numbers matter and can disagree: **job wall-clock** (the `startedAt`/`completedAt` delta, what you pay in CI minutes) and **compute** (just the test-execution steps, with checkout, environment setup, and coverage upload stripped out). When they diverge, a non-compute step is the cause.

- **Linux: ARM is much faster.** `ubuntu-24.04-arm` ran the suite two to three times faster than `ubuntu-slim` (median 2.9x on job wall-clock, at every Python version), and the gap holds on compute alone. `ubuntu-slim` is a deliberately lean image (`repomatic`'s default for light mechanical jobs, where small size and tool availability matter more than throughput), and for a heavy suite it is the slowest tier overall: its `py3.14t` cell (~250s) is the single slowest in the matrix and gates total wall-clock.
- **macOS: Apple silicon beats Intel** by roughly 1.8-2x (about 1.8x on job wall-clock, about 2x on compute), not a single-digit margin. `macos-26` is in fact one of the *fastest* runners overall; `macos-26-intel` is the slow one. macOS as a tier does not gate the matrix: `ubuntu-slim` does.
- **Windows: compute is a tie; x86 wins on wall-clock for a non-compute reason.** On test-execution time the two images sit within ~6% (ARM is marginally ahead on free-threaded and prerelease). `windows-2025` still finishes each job ~50s sooner, because `windows-11-arm` pays a systematic penalty on the Codecov upload step (~56s versus ~6s: the uploader is slow on ARM64 Windows). Pick `windows-2025` for the wall-clock saving, but do not read it as x86 computing faster.

```{caution}
These figures are one project's, and they drift. Runner images are re-provisioned, new images appear and old ones are retired, and an outlier can be a transient stall rather than a property of the image. Some gaps are systematic, though: the ARM-Windows Codecov penalty above shows up in every run. Per-job wall-clock also folds in checkout, setup, and upload, so isolate the test steps before attributing a gap to the image's compute. Treat all of this as a starting hypothesis, not a constant, and re-confirm against your own timings.
```

### Architecture speed is workload-dependent

The ratios above are the *test suite's*, and they do not generalize to every job. The suite runs `pytest --numprocesses=auto`, so it parallelizes across cores and leans on Python startup and subprocess spawns: exactly where ARM pulls ahead. `repomatic`'s light mechanical jobs (the linters and formatters that run on every push) behave differently, and a controlled A/B shows why.

Three Linux runners ran the real tool commands on the same commit, which separates two effects the headline "2.9x" had conflated:

- **Leanness**: `ubuntu-slim` (lean x86) versus `ubuntu-24.04` (full x86).
- **Architecture**: `ubuntu-24.04` (full x86) versus `ubuntu-24.04-arm` (full ARM).

Only one mechanical job is compute-bound enough to matter: `mdformat` (the autofix `Format Markdown` job, which spawns one `mdformat` process per file).

| Step            | Runner                        |  `mdformat` |
| :-------------- | :---------------------------- | ----------: |
| baseline        | `ubuntu-slim` (lean x86)      |        110s |
| remove leanness | `ubuntu-24.04` (full x86)     | 97s (1.13x) |
| remove x86      | `ubuntu-24.04-arm` (full ARM) | 77s (1.26x) |

Of the 1.43x end-to-end gain, most is architecture (1.26x) and a little is leanness (1.13x). Every other tool (`ruff`, `mypy`, `gitleaks`, `actionlint`, `zizmor`, `typos`, `yamllint`) finished in 1-4s on all three runners, within noise: those jobs are dominated by checkout and `uv` install (~15-20s), which a faster CPU barely touches, and ARM setup was if anything marginally slower. Every tool ran on ARM Linux with no missing binaries.

The decisions that follow:

- **Test PR slot uses `ubuntu-24.04-arm`.** The heavy parallel suite genuinely runs ~2-3x faster on ARM, so PR feedback is quicker; x86 Linux stays covered in the full matrix.
- **Light mechanical jobs keep `ubuntu-slim`.** They are setup-bound, so ARM buys ~nothing while adding an architecture variable across the whole fleet. The real lever for them is caching setup, not the CPU.
- **The one compute-heavy light job, `Format Markdown`, uses `ubuntu-24.04-arm`.** It already could not use the lean image (it needs `shfmt`), and ARM runs its per-file pass ~1.26x faster, so it takes the full ARM image rather than the full x86 one.

```{note}
The mechanical-job split is a single controlled run, where the test-suite ratios are medians of several: treat the 1.13x/1.26x decomposition as one measurement to re-confirm. And always include a full-x86 runner in an architecture A/B: comparing only the lean x86 image against full ARM credits the architecture for the image's leanness too.
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
# (measured here: Intel macOS and ARM Windows finish each job slower).
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

| Slice                                  | Runs on                               | `continue-on-error` |
| :------------------------------------- | :------------------------------------ | :------------------ |
| `released` acme (the shipped config)   | all four retained OSes × every Python | no                  |
| `4.2` floor and `5.0` regression       | `ubuntu-24.04-arm` × every Python     | no                  |
| `main` acme (dev-branch early warning) | `ubuntu-24.04-arm` × every Python     | yes                 |

The shipped configuration is exercised everywhere a regression would reach a user; the floor and the one regression-prone release are verified cheaply on the fastest runner; and the development branch gives a heads-up without the power to redden the build. The PR matrix stays the curated reduced set for fast feedback, since `variations` and `unstable` apply to the full matrix only. The same shape extends to a prerelease or free-threaded Python: add it as a `python-version` variation, pin it to one runner with `exclude`, and mark it `unstable`.
