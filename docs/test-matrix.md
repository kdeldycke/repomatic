# {octicon}`pivot-column` Test matrix

`repomatic` builds two GitHub Actions test matrices for every project: a **full matrix** (pushes to the default branch and scheduled runs) and a reduced **pull-request matrix** (fast feedback on PRs). Both are pre-computed by the [`metadata` job](workflows.md) from the project's [`[tool.repomatic.test-matrix.*]`](configuration.md) configuration, so a project shapes its matrix without hand-editing workflow YAML.

This page is the guide: how to decide what the matrix should test, which GitHub-hosted runners exist and how they trade off on speed, and a worked example. For the per-key configuration reference (types, defaults), see the [configuration page](configuration.md).

## How the matrix is built

The base axes are `os` and `python-version`, seeded from `repomatic`'s defaults (`TEST_RUNNERS_FULL`/`TEST_RUNNERS_PR` and `TEST_PYTHON_FULL`/`TEST_PYTHON_PR` in `repomatic/matrix_axes.py`). A project then reshapes the matrix through a fixed chain of transformations, each a `[tool.repomatic.test-matrix.*]` key, applied in this order:

1. `replace`: swap axis values in place.
2. `remove`: drop axis values from an axis.
3. `variations`: add extra axis values (full matrix only), including brand-new axes.
4. `exclude`: remove specific combinations.
5. `include`: add or augment combinations. GitHub processes `include` after `exclude`. A directive that merges into at least one surviving job augments those jobs only; a directive that matches no surviving job (because it fully re-specifies an excluded combination) is appended as a new standalone job. A partial `include` does not resurrect excluded slices.

A separate `unstable` pass (full matrix only) flags matching combinations `continue-on-error`. Because the order is fixed, the transforms compose predictably. `variations` and `unstable` touch only the full matrix, keeping the PR matrix a small curated set. See [workflows § Dynamic test matrices](workflows.md) for why this exists (GitHub's static `strategy.matrix` cannot express it) and the [configuration reference](configuration.md) for each key.

## Inspect the computed matrix

To see the matrix your configuration actually produces, render it as a grid with [`repomatic show-test-matrix`](cli.md): one row per Python version, one column per runner, each cell flagging whether that job runs `stable`, `unstable` (continue-on-error), or is absent (`—`).

```{click:source}
:hide-source:
from repomatic.cli import repomatic
```

```{click:run}
invoke(repomatic, args=['show-test-matrix', 'full'])
```

With no `[tool.repomatic.test-matrix.*]` overrides, this is the built-in default: the six runners from the inventory below, the default Python versions, and the rows the transform chain contributes: the `3.15` prerelease flagged `unstable`, the free-threaded `3.14t` build pinned to a single runner as a stable smoke test, and `windows-11-arm` dropped on `3.10`.

The reduced pull-request matrix keeps one runner per OS and two Python versions, for faster feedback:

```{click:run}
invoke(repomatic, args=['show-test-matrix', 'pr'])
```

A grid has two axes and a matrix may vary on more: a repository testing several dependency versions per job (a `click-version` axis, say) lands all of them on the same Python-by-runner intersection. Such a cell states how many jobs it stands for, `✅ stable ×5`, so it never reads as a single job. To break them apart, lay the grid out on the axes you care about with `--row-axis` and `--col-axis`, naming any job key the matrix carries:

```{click:run}
invoke(repomatic, args=['show-test-matrix', '--row-axis', 'os', '--col-axis', 'python-version'])
```

Or drop the grid entirely: `--flat` lists one row per job under a column per axis, collapsing nothing, which is the closest view to the job list CI schedules. Shown here on the smaller pull-request matrix:

```{click:run}
invoke(repomatic, args=['show-test-matrix', 'pr', '--flat'])
```

The grid honors the global `--table-format` option, so the same view renders as GitHub-flavored Markdown, CSV, JSON, and the rest. For the raw GitHub Actions matrix the [`metadata` job](workflows.md) hands to CI: the `os` and `python-version` axes plus the `include`/`exclude` directives that shape them, request the `test_matrix` (or `test_matrix_pr`) key from [`repomatic metadata`](cli.md):

```{click:run}
invoke(repomatic, args=['metadata', 'test_matrix', '--format', 'json'])
```

## Choosing what to test

A matrix is a budget. Every cell costs runner minutes and adds to wall-clock. Spend the budget where a failure is both likely and informative; keep everything speculative cheap.

### Cover the shipped configuration broadly

The combination your users actually install — released dependencies on a stable Python — earns the widest spread of operating systems and Python versions. This is the core of the matrix: a regression here reaches everyone, so it is worth catching on every platform.

### Probe forward-looking axes narrowly

Anything not yet shipped is an early-warning signal, not a support promise: a prerelease Python, a dependency's development branch, an unreleased build. Run each on a *single* runner. If it breaks you want a heads-up, not a cross-platform report, and once that version ships the broad shipped-config coverage picks it up anyway. Flag these jobs `continue-on-error` through [`test-matrix.unstable`](configuration.md) so an expected breakage does not fail the build.

A prerelease Python says so in its job title. `repomatic` attaches a `python-label` key to those cells, spelling the version the way [pyenv](https://github.com/pyenv/pyenv) and [`actions/setup-python`](https://github.com/actions/setup-python/blob/main/docs/advanced-usage.md) name a development build, so the job reads `⁉️ ubuntu-26.04 / py3.15-dev` and its `continue-on-error` marking carries its own explanation. That label is display-only: `uv` does not parse the `-dev` form, so the `python-version` axis keeps the bare `3.15`, and `3.15` remains the value a `test-matrix` directive has to name.

(smoke-test-released-build-flavors-on-one-runner)=

### Smoke-test released build flavors on one runner

A free-threaded build (the `t` suffix, [officially supported since 3.14](https://peps.python.org/pep-0779/)) is a released *flavor* of a version the shipped-config slice already covers on every platform: the same interpreter, just compiled without the GIL. Because it is released, it is expected to work, so it runs **stable**, not `continue-on-error` like a prerelease. But re-running the whole suite on every platform buys little: one runner catches a free-threading-specific break, and the base version's cross-platform coverage handles the rest. So a flavor takes the narrow spread of a forward-looking probe with the stable outcome of shipped config. `repomatic` pins `3.14t` to its fastest Linux runner by default; to add another flavor, give it a `python-version` variation pinned to one runner with `exclude` (the worked example's pattern) and leave it *out* of `test-matrix.unstable`.

### Pin the dependency floor and any known-regression release

When a project supports a *range* of a core dependency (say `>= 2.3`), CI by default only ever exercises whatever the lockfile resolves to, usually the newest version. The floor is declared but never verified, so it rots silently until a downstream user on an older version hits the break. Add the floor as an explicit matrix value so the bottom of the range runs on every CI pass.

Add any single mid-range release whose behavior a workaround specifically targets, too. That release is the one version where the shim is load-bearing, so it is the one version that catches the shim regressing: bracketing the range with floor and latest alone would miss it.

When the dependency's patch releases are not reliably behavior-stable — some projects re-cut a patch to fix a mid-stream regression — go further and pin *every* release in the range, not just the floor and the one regression you happen to know about. You cannot predict which patch shifts behavior, so testing each release is the only way to bound the perimeter. The newest is covered by the moving `released` value; pin every earlier one. That list grows by one each time the dependency ships, so back it with a test (see [Guard the matrix with a test](#guard-the-matrix-with-a-test) below) that fails when the matrix falls behind.

### Pin each dependency-version to one Python

A pinned dependency-version is there to test the *dependency*, and a dependency's behavior rarely turns on the Python version: its shims are version-of-the-dependency logic, not version-of-Python logic. Python compatibility is already covered broadly by the shipped-config slice (every Python on the released dependency). So run each pinned version on a *single* Python rather than the full set. The floor Python is the natural pick: min-dependency × min-Python is the realistic oldest-environment corner, and pinning to one Python keeps the dependency × Python product from multiplying.

For the same reason, keep pinned (old) dependency-versions *off* the prerelease Python. "Oldest supported dependency × a Python that is not released yet" is a combination no user runs; reserve the prerelease-Python probe for the released dependency.

Pinning a value to a single cell is verbose in the exclude model. Say you carry a floor (`4.2`) and one regression-prone release (`5.0`) of `acme`, and want each on a single cell: the floor Python of the fastest runner. You add them as matrix values, which multiplies them across every OS and Python, then exclude every combination but the one you want, including the prerelease Python (per the rule above). With the slow-architecture twins removed (as in the worked example below) four runners and three Pythons remain, so each pinned version costs five excludes:

```toml
[tool.repomatic]
# Released acme everywhere, plus the floor and the regression release.
test-matrix.variations.acme-version = ["4.2", "5.0", "released"]
# Pin 4.2 and 5.0 each to (ubuntu-26.04-arm, 3.10) by dropping every other cell.
test-matrix.exclude = [
  { "os" = "ubuntu-26.04", "acme-version" = "4.2" },
  { "os" = "macos-26", "acme-version" = "4.2" },
  { "os" = "windows-2025", "acme-version" = "4.2" },
  { "python-version" = "3.14", "acme-version" = "4.2" },
  { "python-version" = "3.15", "acme-version" = "4.2" },
  { "os" = "ubuntu-26.04", "acme-version" = "5.0" },
  { "os" = "macos-26", "acme-version" = "5.0" },
  { "os" = "windows-2025", "acme-version" = "5.0" },
  { "python-version" = "3.14", "acme-version" = "5.0" },
  { "python-version" = "3.15", "acme-version" = "5.0" },
]
```

[`test-matrix.full-include`](configuration.md) states each cell directly instead, dropping the `acme-version` axis altogether: released becomes the default and each pin is one explicit exception that lists only what differs from the shipped configuration (unset axes inherit the defaults: released dependencies, stable state). The variation and its ten excludes become a one-line `include` and two rows:

```toml
[tool.repomatic]
# Released acme everywhere (the broad shipped-config slice)...
test-matrix.include = [{ "acme-version" = "released" }]
# ...plus the floor and regression release pinned to one cell each.
test-matrix.full-include = [
  { "os" = "ubuntu-26.04-arm", "python-version" = "3.10", "acme-version" = "4.2" },
  { "os" = "ubuntu-26.04-arm", "python-version" = "3.10", "acme-version" = "5.0" },
]
```

Both produce the same jobs: released `acme` across every runner and Python, plus `4.2` and `5.0` on the single floor cell. The `full-include` rows join the full matrix only; the PR matrix ignores them. Reach for `variations` plus `exclude` when a pinned version should instead span every Python, as in the worked example below.

### Select runners by measured speed, not architecture

When you reduce to one runner per OS, pick the *fastest* one for your workload, measured from your own CI. Do not reflexively choose the ARM image because it is "the future": architecture speed is not uniform across operating systems (see the inventory below), and the faster choice differs per platform. When you do not need to test both architectures of an OS, drop the slower twin entirely rather than carrying it.

The phrase *for your workload* is load-bearing. The architecture gap is wide for a parallel, compute-heavy job (a `pytest --numprocesses=auto` suite that scales with cores) and narrow-to-nonexistent for a job dominated by checkout and dependency install. So the right runner differs by job *type*, not just by project: see [§ Architecture speed is workload-dependent](#architecture-speed-is-workload-dependent) for the split `repomatic` measured between its heavy test suite and its light mechanical jobs.

For a compute-bound parallel workload, that measurement keeps landing on the same runner: `ubuntu-26.04-arm` is the fastest `repomatic` has measured and sits in the cheapest tier (GitHub bills hosted macOS at roughly 10x Linux minutes, and ARM Linux matches or beats x86 Linux on both speed and price). So when you need a *single* fast runner — the PR Linux slot, a [single-runner flavor smoke test](#smoke-test-released-build-flavors-on-one-runner), or a pinned dependency cell — `ubuntu-26.04-arm` is the default pick. `macos-26` is fast too, but its minute multiplier makes it a poor default; reserve it and the Windows runners for the OS coverage only they provide.

(guard-the-matrix-with-a-test)=

### Guard the matrix with a test

A test matrix is configuration, and configuration rots silently: a new dependency release, a raised floor, or a typo'd runner name does not announce itself. Back the matrix with a unit test that re-derives what *should* be tested from the project's own metadata and compares it to what the matrix *does* test, turning drift into a failing CI check instead of a bug a user reports later.

The highest-value check ties a pinned dependency axis to its declared specifier: assert that the pinned versions equal the releases the specifier allows (reading the release list from the package index), minus the newest, which the `released` value already covers. A freshly published release then fails the test until it is pinned; a pin that drops below a raised floor, or that gets yanked, fails until it is removed. A cheaper, network-free companion asserts the lowest pinned version equals the specifier's floor, catching a floor change that forgot the matrix even when the index is unreachable.

The same spirit covers the matrix's other invariants: its lowest Python should equal the project's `requires-python` floor, and every `exclude` should reference a real axis value: a misspelled runner silently excludes nothing and runs the job anyway, which `repomatic`'s `lint-repo` check flags as a no-op exclude.

## GitHub-hosted runner inventory

`repomatic`'s full matrix spans both architectures of each OS; the reduced PR set keeps one per OS. The runners (defined in `repomatic/matrix_axes.py`):

| Runner                                                                                                            | OS      | Architecture          | In PR set | Notes                                                                                                                                |
| :---------------------------------------------------------------------------------------------------------------- | :------ | :-------------------- | :-------- | :----------------------------------------------------------------------------------------------------------------------------------- |
| [`ubuntu-26.04-arm`](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Arm64-Readme.md) | Linux   | ARM64                 | yes       | Fastest measured on the parallel suite, cheapest tier; default single-runner pick (PR Linux slot, flavor smoke tests, pinned cells). |
| [`ubuntu-26.04`](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md)           | Linux   | x86-64                | no        | x86 Linux coverage in the full matrix. Still labelled preview by GitHub, see below.                                                  |
| [`macos-26`](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md)            | macOS   | ARM64 (Apple silicon) | yes       | Faster macOS image, fast overall, but billed at ~10x Linux minutes; use only when macOS coverage is needed.                          |
| [`macos-26-intel`](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-Readme.md)            | macOS   | x86-64                | no        | Legacy Intel; ~2x slower than `macos-26`.                                                                                            |
| [`windows-11-arm`](https://github.com/actions/runner-images/blob/main/images/windows/Windows11-Arm64-Readme.md)   | Windows | ARM64                 | no        | Compute ties `windows-2025`; full-matrix only, for native ARM64 execution coverage.                                                  |
| [`windows-2025`](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md)         | Windows | x86-64                | yes       | Compute tied with `windows-11-arm`; the PR-set Windows pick.                                                                         |

**Every job runs on one of these six.** The light mechanical jobs and the Linux Nuitka build hosts included: "where is the suite exercised" and "what may a job run on" are one question, so `lint-repo` fails any `runs-on:` naming something else. Each extra image is one more to track, pin and migrate, and the one that used to sit outside the axes lost the measurement that justified it (see [§ The lean-image question, settled](#the-lean-image-question-settled)).

### Preview images and what "stable" means here

GitHub still labels the Ubuntu 26.04 pair **preview**, and `repomatic` ships them as stable test axes anyway. That is a deliberate reading of what the label governs, worth stating because it is the one place this project overrides a vendor's own classification.

An image is treated as stable here once it has been validated against this suite, not once GitHub relabels it. The preview flag primarily gates whether an image is eligible to sit behind `ubuntu-latest` and the other `-latest` aliases. This project never uses those aliases: a floating alias re-points to a new image with no commit to review, so a breakage arrives detached from any change, which is why `lint-repo` rejects a `-latest` runner outright. With the alias question off the table, what remains is whether the image runs the suite correctly and quickly, and that is measurable.

It was measured before the swap. Both images ran the full matrix as `continue-on-error` cells over consecutive pushes, alongside the GA runners they would replace:

| Python | `ubuntu-24.04-arm` (GA) | `ubuntu-26.04-arm` (preview) |
| :----- | ----------------------: | ---------------------------: |
| 3.10   |                     66s |                          56s |
| 3.14   |                    114s |                          82s |
| 3.15   |                    120s |                         118s |

Faster at two of three versions, tied at the third, with nothing failing. That is the evidence the swap rests on, and the [§ Measuring your own](#measuring-your-own) recipe is how to reproduce it.

```{caution}
The residual risk is capacity, not correctness. GitHub warns that a preview image's capacity "will be balanced only throughout the next weeks", so queue time can be worse than the runtimes above suggest, and queue time already dominates this project's CI. That risk was weighed against fleet homogeneity and lost: keeping the release binaries on their own GA images meant maintaining a second Linux pair purely to hedge a queue, and every extra image is one more to track, pin and migrate. The Linux Nuitka builds therefore run on the same axes as the suite. A project that would rather wait for GA can pin the old images back with one line: `test-matrix.replace.os = { "ubuntu-26.04-arm" = "ubuntu-24.04-arm" }`.
```

The same reasoning is what keeps `3.15` flagged `unstable` while these runners are not: a prerelease Python can still change before its final release, so its cells are an early-warning signal rather than a verdict. A runner image that passes the suite today is simply passing the suite.

### Speed tendencies

Relative speed is workload-dependent, so the only authoritative numbers are your own. The tendencies below come from `repomatic`'s own full test suite, taken as the median across the five most recent successful runs on all six runners. Two numbers matter and can disagree: **job wall-clock** (the `startedAt`/`completedAt` delta, what you pay in CI minutes) and **compute** (just the test-execution steps, with checkout and environment setup stripped out). When they diverge, a non-compute step is the cause.

- **Linux: ARM is much faster.** ARM Linux ran the suite two to three times faster than the lean `ubuntu-slim` that used to hold the x86 axis (median 2.9x on job wall-clock, at every Python version), and the gap holds on compute alone. That lean image was the slowest tier overall for a heavy suite: its free-threaded `3.14t` cell was the single slowest cell in the matrix at ~250s. That is why the flavor smoke test moved to ARM Linux (see [§ Smoke-test released build flavors on one runner](#smoke-test-released-build-flavors-on-one-runner)), and it was the first sign of a gap later measured across the whole fleet ([§ The lean-image question, settled](#the-lean-image-question-settled)).
- **macOS: Apple silicon beats Intel** by roughly 1.8-2x (about 1.8x on job wall-clock, about 2x on compute), not a single-digit margin. `macos-26` is in fact one of the *fastest* runners overall; `macos-26-intel` is the slow one. macOS as a tier does not gate the matrix; the slowest x86 Linux cell does.
- **Windows: compute is a tie.** On test-execution time the two images sit within ~6% (ARM is marginally ahead on the prerelease Python). The two used to diverge on per-job wall-clock, but that gap came from a coverage-upload step no job runs any more. `windows-2025` stays the PR-set Windows pick.

```{caution}
These figures are one project's, and they drift. Runner images are re-provisioned, new images appear and old ones are retired, and an outlier can be a transient stall rather than a property of the image. Some gaps are systematic, though: the 2-3x ARM-versus-x86 Linux ratio above shows up in every run. Per-job wall-clock also folds in checkout and setup, so isolate the test steps before attributing a gap to the image's compute. Treat all of this as a starting hypothesis, not a constant, and re-confirm against your own timings.
```

(architecture-speed-is-workload-dependent)=

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

Of the 1.43x end-to-end gain, most is architecture (1.26x) and a little is leanness (1.13x). Every other tool (`ruff`, `mypy`, `gitleaks`, `actionlint`, `zizmor`, `typos`, `yamllint`) finished in 1-4s on all three runners, within noise: those jobs are dominated by checkout and `uv` install (~15-20s), which a faster CPU barely touches, and ARM setup was if anything marginally slower. Those linters all ran on ARM Linux with no missing binaries, but `mdformat` is the exception that matters (see the decision below).

The decisions that follow:

- **Test PR slot uses `ubuntu-26.04-arm`.** The heavy parallel suite genuinely runs ~2-3x faster on ARM, so PR feedback is quicker; x86 Linux stays covered in the full matrix.
- **`Format Markdown` stays on x86.** `mdformat-config` pulls `taplo`, which ships no linux-aarch64 wheel and has a broken `0.9.3` sdist, so a fresh ARM install fails to build it. That constraint is the job's alone, and it is the reason the fleet's x86 axis is worth keeping rather than going ARM-only.

```{caution}
The conclusion this section originally drew, that the light jobs should stay on a lean image, did not survive being measured end to end. See [§ The lean-image question, settled](#the-lean-image-question-settled). The 1.13x/1.26x decomposition above remains a fair reading of *tool execution* on one commit; it was simply the wrong quantity to decide a runner on. Always include a full-x86 runner in an architecture A/B, and time whole jobs rather than the tool pass.
```

(the-lean-image-question-settled)=

### The lean-image question, settled

`ubuntu-slim` held every light mechanical job for a long time, on the reasoning above: those jobs are setup-bound, so a faster CPU buys nothing, and a smaller image ought to provision quicker. The A/B decomposition supported the first half and nobody tested the second.

Measuring it settled the question in one pass. Every `runs-on: ubuntu-slim` moved to `ubuntu-26.04`, and whole-job wall-clock was compared against the preceding runs:

| Workflow | `ubuntu-slim` | `ubuntu-26.04` |                |
| :------- | ------------: | -------------: | -------------: |
| Lint     |          146s |           106s | **27%** faster |
| Autofix  |          477s |           325s | **32%** faster |

Twenty of twenty-two jobs improved, by 20-56%. One tied and one was 5% slower, both inside the noise. `Format Markdown`, the only compute-bound job, went from 151s to 101s, far past the 1.13x that timing the tool pass alone had predicted.

The lean image was never faster; it was slower almost everywhere, and most of the gap sits in exactly the setup phase the earlier measurement could not see. So `ubuntu-slim` is retired, and `lint-repo` now rejects it like any other untracked image.

```{caution}
The `ubuntu-26.04` column is a single run against a seven-to-nine run baseline, so treat the *magnitude* as provisional. What makes the direction trustworthy is that twenty of twenty-two independent jobs moved the same way at once, which noise does not usually do. Re-confirm against your own timings before copying the conclusion: a project whose light jobs are dominated by something else may still find the lean image wins.
```

### Measuring your own

```shell-session
$ repomatic job-timings --workflow tests.yaml --limit 5
```

That reports the median whole-job wall-clock per runner image across the most recent successful runs, attributing each job to the image its name carries. Only successful runs are sampled: a failed run's jobs stop early and time where the failure landed rather than what the image costs. The median across several runs is what turns a queue stall into noise rather than a verdict.

Whole-job is the figure that matters and the one this reads, because the jobs API reports a start and an end timestamp and nothing finer. That is deliberate: timing the tool pass alone is exactly the measurement that kept the lean image in place for years (see [§ The lean-image question, settled](#the-lean-image-question-settled)), and it is not expressible through this command.

Pass `--output` to write the table as Markdown, which is how the inventory above is refreshed. It is regenerated by hand rather than by a job: these numbers move on every run, so a workflow rewriting them would open a pull request forever and never converge.

To isolate one variable, compare cells that differ only in `os` at the same Python and dependency versions. `--sort-by` reorders the table, and the durations are zero-padded so that ordering is chronological rather than alphabetical.

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
# After the remove above, the non-pinned runners are ubuntu-26.04, macos-26,
# and windows-2025.
test-matrix.exclude = [
  { "os" = "ubuntu-26.04", "acme-version" = "4.2" },
  { "os" = "macos-26", "acme-version" = "4.2" },
  { "os" = "windows-2025", "acme-version" = "4.2" },
  { "os" = "ubuntu-26.04", "acme-version" = "5.0" },
  { "os" = "macos-26", "acme-version" = "5.0" },
  { "os" = "windows-2025", "acme-version" = "5.0" },
  { "os" = "ubuntu-26.04", "acme-version" = "main" },
  { "os" = "macos-26", "acme-version" = "main" },
  { "os" = "windows-2025", "acme-version" = "main" },
]

# The unreleased acme branch is an early-warning probe: never fail the build on it.
test-matrix.unstable = [{ "acme-version" = "main" }]
```

On top of the built-in `3.14t` flavor smoke test (stable, on `ubuntu-26.04-arm` with released `acme`), the `acme` config resolves to three slices:

| Slice                                  | Runs on                                    | `continue-on-error` |
| :------------------------------------- | :----------------------------------------- | :------------------ |
| `released` acme (the shipped config)   | all four retained OSes × every base Python | no                  |
| `4.2` floor and `5.0` regression       | `ubuntu-26.04-arm` × every base Python     | no                  |
| `main` acme (dev-branch early warning) | `ubuntu-26.04-arm` × every base Python     | yes                 |

The shipped configuration is exercised everywhere a regression would reach a user; the floor and the one regression-prone release are verified cheaply on the fastest runner; and the development branch gives a heads-up without the power to redden the build. The PR matrix stays the curated reduced set for fast feedback, since `variations` and `unstable` apply to the full matrix only. The same shape extends to a prerelease Python (add it as a `python-version` variation, pin it to one runner with `exclude`, and mark it `unstable`) or to a released free-threaded flavor (the same, but stable: leave it out of `unstable`, as [§ Smoke-test released build flavors on one runner](#smoke-test-released-build-flavors-on-one-runner) explains).

## `repomatic.matrix_axes` API

```{eval-rst}
.. automodule:: repomatic.matrix_axes
   :members:
   :undoc-members:
   :show-inheritance:
```

## `repomatic.metadata` API

```{eval-rst}
.. autoclasstree:: repomatic.metadata
   :strict:

.. automodule:: repomatic.metadata
   :members:
   :undoc-members:
   :show-inheritance:
```
