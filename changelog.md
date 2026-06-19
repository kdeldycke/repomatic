# Changelog

## [`6.27.1.dev0` (unreleased)](https://github.com/kdeldycke/repomatic/compare/v6.27.0...main)

> [!WARNING]
> This version is **not released yet** and is under active development.

- `repomatic test-plan` runs its cases in parallel by default (one fewer than the CPU count); pass `--jobs 1` for sequential execution.
- Run the pull-request test matrix and the `format-markdown` autofix job on `ubuntu-24.04-arm` for faster Linux CI; the full test matrix still covers x86 Linux.
- `repomatic metadata` no longer prints a spurious `--overwrite` warning when writing to stdout, unless the flag is set explicitly.
- Add a [test-matrix guide](https://kdeldycke.github.io/repomatic/test-matrix.html) to the docs: choosing matrix targets, a GitHub-runner speed inventory, and a worked example.

## [`6.27.0` (2026-06-18)](https://github.com/kdeldycke/repomatic/compare/v6.26.0...v6.27.0)

- **Breaking:** Replace `fix-vulnerable-deps` with `audit`. `repomatic audit` reports vulnerable dependencies read-only; `repomatic audit --fix` performs the previous upgrade behavior.
- Stop forcing `pyproject-fmt` table expansion: `project.urls`, `project.scripts`, and similar sections now use its default compact (dotted-key) form.
- Recognize each bundled tool's native config files more accurately (`biome`, `gitleaks`, `ruff`, `typos`, `zizmor`, and others) and their config-file CLI flags.
- Preserve comments when materializing a `[tool.X]` section from `pyproject.toml` to a tool's native TOML config file (like `.gitleaks.toml`), instead of dropping them.
- Update `pyproject-fmt` to `2.25.0`, fixing the `format-pyproject` job writing invalid TOML when it reformats `[tool.repomatic.labels]` rule tables.
- Align the bundled `[tool.bumpversion]` and `[tool.lychee]` templates with `pyproject-fmt`'s canonical output, ending the reformatting pull-request loops they triggered.
- Fix cooldown bypasses (`[tool.uv] exclude-newer-package`) never expiring: `sync-uv-lock` now freezes each one at its locked version instead of a latest-tracking `"0 day"` span, and prunes it once that version ages past `exclude-newer`.
- Fix `uv.lock` ping-ponging on every `sync-uv-lock` run: `exclude-newer-package` freezes are now explicit UTC timestamps, not bare dates that uv re-expands in the locking machine's timezone.
- Fix the `repomatic.myst_docstrings` Sphinx extension corrupting two adjacent inline-code spans in a docstring when the second span starts with an underscore.
- Fix the `manpages` release job: attach the man-page tarball to the release draft before publishing, so it no longer fails under GitHub immutable releases.

## [`6.26.0` (2026-06-17)](https://github.com/kdeldycke/repomatic/compare/v6.25.1...v6.26.0)

> [!NOTE]
> `6.26.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.26.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.26.0).

- Add `[tool.repomatic] nuitka.extras` config to sync listed `[project.optional-dependencies]` extras into the venv before the Nuitka build, so optional features land in the binary.
- Add `[tool.repomatic.labels]` `extra`, `file-rules`, and `content-rules` config for inline label definitions and labeller rules, replacing the silently-ignored `extra-file-rules` and `extra-content-rules` fields.
- Stop version-bump PRs from upgrading dependencies: the bump and release jobs now run plain `uv lock`, leaving dependency refreshes to the `sync-uv-lock` job.
- Add a `[tool.repomatic] changelog.bullet-word-threshold` config: `lint-changelog` warns (non-fatally) about unreleased changelog bullets longer than the threshold (40 words by default).

## [`6.25.1` (2026-06-13)](https://github.com/kdeldycke/repomatic/compare/v6.25.0...v6.25.1)

> [!NOTE]
> `6.25.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.25.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.25.1).

- Fix `uvx repomatic@X.Y.Z` failing for end users with `No solution found` by dropping the `bump-my-version` dependency and reading the current version natively from `.bumpversion.toml` or `[tool.bumpversion]`.
- Remove the `uv-overrides.txt` file and all `UV_OVERRIDE` workflow env blocks.
- Render the Mermaid dependency graph in `docs/install.md` under a new `Default dependencies` section.

## [`6.25.0` (2026-06-13)](https://github.com/kdeldycke/repomatic/compare/v6.24.0...v6.25.0)

> [!NOTE]
> `6.25.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.25.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.25.0).

- Add man page generation to the release and docs pipelines via a `manpages` job, activated by `[tool.repomatic.manpages]` config keys (`script`, `asset-name`); requires `click-extra>=7.19`.
- Validate `[project.scripts]` entries when building the Nuitka matrix, rejecting path-shaped, empty, or malformed script names up front with a clear error.
- Replace the `tomlkit` and `tomli` dependencies with [`tomlrt`](https://dimbleby.github.io/tomlrt/) for all TOML reads and comment-preserving writes.
- Annotate `gh` and PAT permission check failures with the current [githubstatus.com](https://www.githubstatus.com) summary, and surface raw stderr on non-403 failures instead of misreporting missing scopes.
- Recognize friendly durations (`24 hours`, `30 minutes`) and ISO 8601 durations (`PT24H`, `P7D`) in `[tool.uv].exclude-newer` when computing the `repomatic sync-uv-lock` cooldown.
- Fix `UV_OVERRIDE` not reaching Renovate's child processes during `update-checksums`.
- Add `[tool.repomatic] abandoned-versions` to `lint-changelog`, reporting listed versions as skipped instead of warning that they are missing from PyPI.
- Tighten `/repomatic-ship`'s pre-push gate with `ruff format --check` and a `repomatic --version` dependency-resolution smoke run.

## [`6.24.0` (2026-05-28)](https://github.com/kdeldycke/repomatic/compare/v6.23.0...v6.24.0)

> [!NOTE]
> `6.24.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.24.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.24.0).

- Publish to PyPI right after the wheel builds instead of waiting for the full release engine, by splitting the build into a `_release-build.yaml` lane that `release.yaml`'s `publish-pypi` job depends on.

## [`6.23.0` (2026-05-28)](https://github.com/kdeldycke/repomatic/compare/v6.22.0...v6.23.0)

> [!NOTE]
> `6.23.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.23.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.23.0).

- Split `release.yaml` into a thin entry workflow and a new reusable `_release-engine.yaml` engine; the entry keeps `publish-pypi` so PyPI Trusted Publisher OIDC resolves to each repo's own `release.yaml`.
- Remove the `release-publish-pypi-job.yaml` data fragment; `release.yaml` is now the single source for the `publish-pypi` job.
- Require `uv` >= `0.11.15` for the vulnerability scan and parse `uv audit --output-format json` directly, raising a clear error on unsupported `uv` versions and deduplicating advisories across sources by alias.
- Refine `/repomatic-ship` to re-consolidate the changelog after the babysit phase and re-dispatch `changelog.yaml` after a code-only fix push so the release PR stays current.
- Extend `/babysit-ci` to also monitor `autofix.yaml`, diagnosing and fixing crashed mechanical-fix jobs instead of leaving them red on `main`.
- Fix `release.yaml`'s `compile-binaries` and `test-binaries` jobs aborting every non-release run with `Unexpected value ''` on projects with `nuitka.enabled = false`.

## [`6.22.0` (2026-05-25)](https://github.com/kdeldycke/repomatic/compare/v6.21.0...v6.22.0)

> [!NOTE]
> `6.22.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.22.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.22.0).

- `repomatic init` now prunes downstream orphans of renamed or removed skills, agents, and workflows; locally modified copies are reported for manual review, never deleted. Pass `--keep-removed` to report without deleting, or `--delete-removed-modified` to also delete modified ones.
- `/repomatic-ship` now closes with a reflect step that reviews the session for friction and proposes fixes to the upstream `repomatic` source.
- Fix the downstream caller's `publish-pypi` job aborting every non-release `release.yaml` run with `Unexpected value ''` when its `strategy.matrix` is empty.
- Fix `/repomatic-ship` and `/babysit-ci` dropping the `Co-Authored-By: Claude` trailer on their autonomous commits; both skills now require it self-containedly.
- `/babysit-ci` now treats a workflow run that fails with no individual job failure as a real workflow-level error to investigate.
- Fix the documentation site's live CLI-help and example blocks rendering empty since `click-extra` 7.15.0 made execution directives opt-in.
- Seed each tool section in the [tool-runner docs](https://kdeldycke.github.io/repomatic/tool-runner.html#available-tools) with a runnable `repomatic run` example and a minimal `[tool.X]` snippet.

## [`6.21.0` (2026-05-25)](https://github.com/kdeldycke/repomatic/compare/v6.20.0...v6.21.0)

> [!NOTE]
> `6.21.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.21.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.21.0).

- Replace the `repomatic-release` skill with `repomatic-ship`, a release orchestrator that reconciles changelog, code, and docs, then commits, pushes, and babysits CI until the release PR is ready. Review-gated by default, fully autonomous under `--dangerously-skip-permissions`.
- Add a `modernize` mode to the `repomatic-deps` skill that reads upgraded dependencies' changelogs and refactors code to adopt their new features, gating each change on the test suite.
- Extend the `babysit-ci` skill to also monitor and triage the Nuitka `compile-binaries` job in `release.yaml`.
- Decouple the downstream caller's `publish-pypi` job from the run's overall result: it now runs under `always()` and gates on a new `package_built` output, so a cleanly built wheel publishes even when an unrelated job fails.
- Remove the `repomatic-sync`, `repomatic-lint`, and `repomatic-test` skills, which only wrapped CLI commands CI already runs on every push.
- Fix the `bump-version` job in `changelog.yaml` leaving an orphan version-bump PR open after a competing bump merged into `main`.
- Switch `pytest-xdist` to `--dist=loadgroup` so `@pytest.mark.xdist_group("git")` markers are honored, fixing `.git/config.lock` contention on Windows.
- Enable myst-parser's `alert` extension so GitHub-style alerts (`> [!NOTE]`, `> [!IMPORTANT]`) render as admonitions on the documentation site.
- Give each tool section in the [tool-runner docs](https://kdeldycke.github.io/repomatic/tool-runner.html) a hand-maintained extra-docs region preserved across regenerations, seeded for Nuitka, and add Nuitka to the page's `[tool.X]`-support table.

## [`6.20.0` (2026-05-24)](https://github.com/kdeldycke/repomatic/compare/v6.19.0...v6.20.0)

> [!NOTE]
> `6.20.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.20.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.20.0).

- **Breaking:** remove `[tool.repomatic] nuitka.extra-args`. Configure Nuitka flags through `[tool.nuitka]` in `pyproject.toml` instead (`--include-data-files=SRC=DEST` becomes `include-data-files = ["SRC=DEST"]`).
- `repomatic run nuitka` now installs the pinned Nuitka, reads `[tool.nuitka]` from `pyproject.toml`, and passes the section as CLI flags; Nuitka appears in `repomatic run --list`.
- Build Nuitka binaries on Python 3.14.
- Switch `[tool.typos]` sync to `ONGOING`: canonical proper-noun identifiers merge into a pre-existing `[tool.typos]` section instead of skipping it, preserving local keys and entries.
- Add `[[tool.bumpversion.files]]` rules to the bundled template so downstream Python repos sync `[tool.nuitka]`'s numeric version keys without rewriting them on `[project]` bumps.
- Add `test-matrix.unstable` config: matrix-key dicts (like `{click-version = "main"}`) that mark matching full-matrix combinations `continue-on-error` in CI.
- Add a `lint-repo` check warning when a `[tool.repomatic.test-matrix] exclude` entry references a runner or Python version absent from the live matrix axes.
- Add `workflow_dispatch` triggers to `release.yaml` and `update-checksums.yaml` for manual re-runs.

## [`6.19.0` (2026-05-21)](https://github.com/kdeldycke/repomatic/compare/v6.18.4...v6.19.0)

> [!NOTE]
> `6.19.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.19.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.19.0).

- Add `repomatic close-stale-bump-pr --part minor|major` to close orphan version-bump PRs left by races between the `changelog.yaml` schedule and a competing push.
- Expand sponsor benefits in the awesome template's `contributing.md`: sponsors get a dedicated entry in the matching section and a waiver on the licensing-marker requirement.
- Switch the `Sync uv.lock` steps in `changelog.yaml` from `uv sync` to `uv lock --upgrade`, folding pending transitive refreshes into the bump commit.
- Make `lint-changelog --fix` refuse to rewrite admonitions when an upstream GitHub or PyPI lookup looks unhealthy, instead of applying a corrupted view.
- Skip CI for automated version-bump operations across `tests.yaml`, `lint.yaml`, `labels.yaml`, and `release.yaml` via a unified `metadata` gate.
- Reduce CI scheduling with `paths-ignore`/`paths:` filters and per-job gates that skip lint jobs when no relevant files changed.
- Bump Biome from `2.4.14` to `2.4.15`.

## [`6.18.4` (2026-05-14)](https://github.com/kdeldycke/repomatic/compare/v6.18.3...v6.18.4)

> [!NOTE]
> `6.18.4` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.4).

- Replace `RepoScope.NON_AWESOME` with `PYTHON_ONLY`, gating Python-flavored components on a PEP 621 `[project].name` so dotfiles repos carrying `pyproject.toml` only for `[tool.*]` config skip them by default.
- The bundled `release-publish-pypi-job.yaml` fragment now participates in the `@main` to `@vX.Y.Z` rewrite, so wheels built from a freeze commit ship with the pinned action ref.
- Bump Biome from `2.4.13` to `2.4.14` and Lychee from `0.24.1` to `0.24.2`.
- Fix `fix-vulnerable-deps` placing `exclude-newer-package` at the end of `[tool.uv]`, which triggered a spurious `format-pyproject` PR on the next run.

## [`6.18.3` (2026-05-11)](https://github.com/kdeldycke/repomatic/compare/v6.18.2...v6.18.3)

> [!NOTE]
> `6.18.3` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.3).

- Fix `autofix.yaml`'s `setup-guide` job being skipped on `workflow_dispatch` re-runs.
- Fix `release.yaml`'s `publish-pypi` job running against downstream callers and failing PyPI trusted publishing with a `job_workflow_ref` mismatch.
- Switch the `compile-binaries` job from `--onefile` to `--mode=onefile`, the documented spelling since Nuitka 4.0.

## [`6.18.2` (2026-05-08)](https://github.com/kdeldycke/repomatic/compare/v6.18.1...v6.18.2)

> [!NOTE]
> `6.18.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.2).

- Fix `release.yaml` uploading distributions to PyPI without PEP 740 attestations; the build job now signs each dist file and ships the `.publish.attestation` sidecars alongside it.

## [`6.18.1` (2026-05-08)](https://github.com/kdeldycke/repomatic/compare/v6.18.0...v6.18.1)

> [!NOTE]
> `6.18.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.1).

- Fix the `publish-pypi` composite action verifying build attestations on every workspace file instead of just the downloaded distribution artifacts.

## [`6.18.0` (2026-05-07)](https://github.com/kdeldycke/repomatic/compare/v6.17.0...v6.18.0)

> [!NOTE]
> `6.18.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.0).

> [!WARNING]
> `6.18.0` is **not available** on 🐍 PyPI.

- **Breaking:** drop `PYPI_TOKEN` from the `release.yaml` `workflow_call.secrets:` interface. Regenerate the thin-caller workflow with `repomatic init workflows` and register a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) for your own `release.yaml`.
- Add the [`publish-pypi`](https://github.com/kdeldycke/repomatic/blob/main/.github/actions/publish-pypi/action.yaml) composite action that publishes via OIDC Trusted Publishing with build-attestation verification; each downstream thin-caller now runs a generated `publish-pypi` job.
- Add a `check_pypi_trusted_publisher` probe to `lint-repo` and a `setup-guide-pypi-trusted-publisher` step that points to a pre-filled PyPI publisher settings URL and stays open until the first OIDC-attested upload.
- Add `release_commits_matrix` and `package_name` outputs to the reusable `release.yaml` so callers can drive their own matrix and gate jobs on a release commit.
- New composite actions under `.github/actions/` now participate in `@main` ↔ `@vX.Y.Z` ref freeze/unfreeze without code changes.
- Fix `sync-repomatic` proposing to delete `.github/actions/publish-pypi/action.yaml` when it matched the bundled default; the file must stay on disk for GitHub Actions to resolve the `uses:` path.

## [`6.17.0` (2026-05-04)](https://github.com/kdeldycke/repomatic/compare/v6.16.0...v6.17.0)

> [!NOTE]
> `6.17.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.17.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.17.0).

- Add `--template-file <path>` and `--template-arg KEY=VALUE` flags to `repomatic pr-body` so downstream repos can render project-specific PR templates without forking. `--template` and `--template-file` are mutually exclusive.
- Fix backslash-escaped brackets rendering literally in `docs/configuration.md` `**Type:**` lines (like `list\[dict[str, str]\]`).
- Fix doubled heading anchors on `docs/configuration.html` and `docs/workflows.html` (like `#dev-release-sync-dev-release-sync`).
- Collapse the most recent Python compatibility matrix row in `docs/install.md` to a major-version wildcard (like `6.x`) so the table stays stable across minor releases.

## [`6.16.0` (2026-04-29)](https://github.com/kdeldycke/repomatic/compare/v6.15.0...v6.16.0)

> [!NOTE]
> `6.16.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.16.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.16.0).

- Add the `sphinx-docs` agent to the `agents` component, deployed by `repomatic init agents` or via `[tool.repomatic] include = ["agents"]`.
- Add three `[tool.repomatic.workflow]` knobs for customizing `paths:` filters in generated thin callers: `extra-paths` appends repo-specific entries, `ignore-paths` strips canonical entries absent downstream, and `paths` replaces a filter wholesale per workflow.
- Add a Python compatibility matrix to `docs/install.md`, auto-generated from the `Programming Language :: Python` classifiers declared at every release tag.
- Render each command's `--help` live in `docs/cli.md` via `{click:run}` directives instead of captured plain-text help blocks.
- Replace the `Type` column in the `docs/configuration.md` summary table with a one-line description derived from each option's docstring, and lead each per-option section with that one-liner.
- Detect vulnerable dependencies from the GitHub Advisory Database alongside the PyPA database: `fix-vulnerable-deps` now unions `uv audit` with Dependabot alerts and credits each entry's source. Configurable via `[tool.repomatic] vulnerable-deps.sources`.
- Fix generated thin-caller fidelity: triggers mirror the canonical workflow verbatim instead of always injecting `workflow_dispatch`, universal path entries are preserved, and `repomatic workflow lint` now flags extra triggers absent upstream.
- Fix `sync-uv-lock` and `fix-vulnerable-deps` PR bodies showing `1-01-01` as the `exclude-newer` cutoff when `pyproject.toml` configures a relative span like `"1 week"`.
- Fix broken documentation links in all 18 PR body templates, now pointing at the published `configuration.html` and `workflows.html` anchors with each option name linked to its own anchor.
- Fix `release.yaml` discarding healthy binaries when one matrix cell crashed: the `compile-binaries` matrix sets `fail-fast: false` and `publish-release` uploads whatever built.
- Fix `update-docs` ↔ `format-markdown` ping-pong on `docs/cli.md` and `docs/configuration.md`.
- Bump pinned `uv` to `0.11.8` and `mdformat-pelican` to `1.0.0`, fixing non-ASCII anchor links being percent-encoded on every `format-markdown` run.

## [`6.15.0` (2026-04-27)](https://github.com/kdeldycke/repomatic/compare/v6.14.0...v6.15.0)

> [!NOTE]
> `6.15.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.15.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.15.0).

- Decode percent-encoded non-ASCII characters in Markdown link destinations back to their original form, so non-ASCII anchors no longer get rewritten to `%XX` on every `format-markdown` run.
- Add 💸/🆓 licensing markers to the awesome-list contributing guide, issue template, and PR template (English and Chinese mirrors): 💸 for a paid version atop an OSS core, 🆓 for fully open-source.
- Add the `agents` component to `repomatic init` for deploying Claude Code agents (`grunt-qa`, `qa-engineer`) downstream. Excluded by default; opt in via `[tool.repomatic] include = ["agents"]`. Destination set by `[tool.repomatic] agents.location`.
- Add `docs/benchmark.md` comparing repomatic against ten alternatives across template sync, repo governance, release automation, and changelog lifecycle.
- Switch MyST admonitions to backtick fences (```` ```{note} ````) instead of colon fences project-wide so `mdformat` preserves them; the `convert-to-myst` command now emits backtick fences.
- Expand the `myst_docstrings` Sphinx extension: convert plain triple-backtick code fences and footnotes to reST, and run MyST-to-reST conversion before `sphinx_autodoc_typehints`.
- Upgrade lychee to `0.24.1`, which reads its `[tool.lychee]` config directly from `pyproject.toml` so repomatic drops the TOML translation bridge.
- Fix `repomatic init` reporting unchanged files as updated; re-running against an unchanged tree is now a true no-op.
- Fix `update-docs` ↔ `format-markdown` ping-pong on `docs/tool-runner.md`.

## [`6.14.0` (2026-04-20)](https://github.com/kdeldycke/repomatic/compare/v6.13.0...v6.14.0)

> [!NOTE]
> `6.14.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.14.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.14.0).

- Add a Sphinx documentation site (Furo theme, MyST-Parser) splitting the monolithic `readme.md` into focused pages: installation, configuration, CLI parameters, reusable workflows, security, skills, and a tool runner tutorial. Deployed via `docs.yaml`.
- Add the `repomatic.myst_docstrings` Sphinx extension and `repomatic.myst_converter` utility, converting MyST markdown in docstrings to reST at build time so `sphinx.ext.autodoc` works unmodified. `convert-to-myst` rewrites source files in place.
- Add a `--sort-by` option to the `show-config`, `metadata --list-keys`, `run --list`, and `cache show` commands; each defaults to a natural sort column and accepts any column name.
- Add an incremental mode to the `brand-assets` skill: when base SVGs already exist, skip the design menu and fill gaps directly.
- Add a `check_stale_gh_pages_branch` lint check and setup-guide instructions for deleting leftover `gh-pages` branches after switching to GitHub Actions deployment.
- Fix `Matrix.prune()` keeping exclude directives that reference keys absent from the matrix axes, which GitHub Actions rejects.
- Fix the setup-guide Pages step for Sphinx projects: reopen the issue when Pages is unconfigured, and offer both first-time-enable and update commands.
- Fix the `sponsor-label` job in `labels.yaml` missing an `actions/checkout` step, which caused it to fail.

## [`6.13.0` (2026-04-15)](https://github.com/kdeldycke/repomatic/compare/v6.12.0...v6.13.0)

> [!NOTE]
> `6.13.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.13.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.13.0).

- **Breaking:** `Config` now uses nested dataclasses, so fields are accessed as `config.cache.dir` instead of `config.cache_dir`; the `[tool.repomatic]` TOML key structure is unchanged.
- Add `nuitka.entry-points` config option to select which `[project.scripts]` entries produce Nuitka binaries; aliases pointing to the same callable are deduplicated by default.
- Add two-phase VirusTotal scanning: an initial table with scan links, then a `--poll` pass that fills in a Detections column of `flagged / total` engine counts.
- Add `av-false-positive` skill to scan release binaries on VirusTotal and generate per-vendor false-positive submission files for flagged artifacts.
- Add `update-checksums.yaml` workflow that recomputes SHA-256 checksums for binary tools bumped by Renovate and commits the fix to the PR branch.
- Include release notes for every intermediate version in `sync-uv-lock` PR bodies, not just the target version.
- Config `include` entries now bypass `RepoScope` filtering, matching explicit CLI component naming; qualified entries like `skills/awesome-triage` implicitly select their parent component.
- Add baseline criteria for GitHub repositories in awesome list contributing guidelines: minimum 50 stars, not archived, and updated within 3 years.
- Add `--min-savings-bytes` option to `format-images` (default 1024) to skip images whose absolute byte savings are negligible.
- Add cross-platform binary support (macOS arm64/x64, Linux arm64/x64, Windows x64) for actionlint, biome, gitleaks, labelmaker, lychee, shfmt, and typos, plus ZIP archive extraction.
- Show a progress bar during binary tool downloads when the server reports `Content-Length`; interactive terminals only, silent in CI.
- Verify cached binaries with a two-layer integrity model: the registry checksum at download time and a `.sha256` sidecar on every cache hit.
- Enable `[tool.actionlint]` config support, translating it to `.github/actionlint.yaml` at invocation time.
- Cache downloaded tool binaries across CI runs with `actions/cache`, keyed per tool, OS, and architecture.
- Replace `peaceiris/actions-gh-pages` with GitHub's native `actions/upload-pages-artifact` and `actions/deploy-pages` for documentation deployment, plus a `lint-repo` check that the Pages source is set to GitHub Actions.
- Add `benchmark-update` skill to create and maintain competitive benchmark pages (`docs/benchmark.md`) with `audit`, `init`, `add`, and `refresh-badges` modes.
- Add `upstream-audit` skill to create and maintain upstream contribution tracking pages (`docs/upstream.md`) with `audit`, `init`, `refresh`, and `sync-git` modes.
- Upgrade the macOS Intel runner from `macos-15-intel` to `macos-26-intel` across binary builds, the test matrix, and Nuitka compilation.
- Run the `lint-repo` workflow job on all repositories, not just Python projects, so generic checks apply to awesome lists too.
- Centralize GitHub token resolution with priority `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN` and automatic fallback to `GITHUB_TOKEN` on an expired PAT; `--has-pat` on `setup-guide` and `lint-repo` now auto-detects from `REPOMATIC_PAT`.
- Fix `exclude-newer-package` pruning in `pyproject.toml` to remove orphaned comments and emit `pyproject-fmt`-compatible inline tables.
- Give a clear error when exiftool is not installed instead of a bare `FileNotFoundError`, and verify it is on PATH after the Windows install step.
- Create parent directories for `--output` file paths in `repomatic run`, fixing lychee write errors when the output directory is missing.
- Sanitize `@mentions`, `#issue` references, and `github.com` URLs in Lychee and Sphinx linkcheck output before embedding them in the broken-links issue.

## [`6.12.0` (2026-04-13)](https://github.com/kdeldycke/repomatic/compare/v6.11.3...v6.12.0)

> [!NOTE]
> `6.12.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.12.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.12.0).

- **Breaking:** rename the `shell_files` metadata key to `shfmt_files`, and exclude Zsh files and `.sh` files with a Zsh shebang from `shfmt` processing.
- Add `repomatic cache` subcommands (`show`, `clean`, `path`) and a global binary cache for downloaded tools; cached binaries are re-verified against their checksum and auto-purged after 30 days (configurable via `REPOMATIC_CACHE_MAX_AGE`). Add `--no-cache` to `repomatic run` to bypass it.
- Add an HTTP response cache for PyPI metadata and GitHub release bodies to avoid redundant API calls, plus `--namespace` on `repomatic cache clean` for targeted cleanup.
- Route generated tool configs through the cache directory and pass them explicitly via `--config`, instead of writing to `/tmp` or the repository root.
- Add `--version`, `--checksum`, and `--skip-checksum` options to `repomatic run` to override the pinned tool version and SHA-256 verification at invocation time.
- Add structured logging to `repomatic run`: `--verbosity INFO` reports config precedence, the full command, and exit code; `DEBUG` adds parsed config details.
- Add `skills.location` config option to override the Claude Code skills directory (default `./.claude/skills/`).
- Add `changelog.location` config option to override the changelog file path (default `./changelog.md`), honored by all CLI commands.
- Add `.claude/package-skills.sh` to package each Claude Code skill as a ZIP for manual upload to Claude Desktop.
- Sanitize `@mentions`, `#issue` references, and `github.com` URLs in upstream release notes embedded in `sync-uv-lock` PR bodies to prevent auto-linking and backlink cross-references.
- Use the `REPOMATIC_PAT` token in all `peter-evans/create-pull-request` steps so created PRs trigger other workflows.
- Make the `uv sync` step in `lint-types` conditional on `is_python_project`, so repos with Python files but no lockfile can still be type-checked.
- Fix `format-json` failing with a `--config-path` error when a `[tool.biome]` section exists.
- Improve the `file-bug-report` skill to check organization-level community health files before per-repo files.

## [`6.11.3` (2026-04-09)](https://github.com/kdeldycke/repomatic/compare/v6.11.2...v6.11.3)

> [!NOTE]
> `6.11.3` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.3).

- Add a `lint-repo` check warning when the GitHub Actions fork PR approval policy is weaker than `first_time_contributors`, with a setup guide step to fix it.
- Add a `readme.md` supply chain security section mapping Astral's security practices to concrete repomatic implementations.
- Fix `rst_to_myst` conversion leaving RST backslash escapes in headings and not wrapping dotted module names in backticks.
- Fix the `format-pyproject` autofix job failing with exit code 123.
- Disable the uv cache in the `publish-pypi` release job, which has no checkout and emitted spurious cache-miss warnings.

## [`6.11.2` (2026-04-08)](https://github.com/kdeldycke/repomatic/compare/v6.11.1...v6.11.2)

> [!NOTE]
> `6.11.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.2).

- Add the `shfmt` shell formatter to the tool runner (`repomatic run shfmt`).
- Add a `format-shell` autofix job to auto-format shell scripts with `shfmt`.
- Replace the `crazy-max/ghaction-virustotal` action with a native `repomatic scan-virustotal` command, fixing the silently skipped release-body update.
- Deduplicate release attestations: Python packages are now attested once in `build-package` instead of three times, and `.gitignore` is no longer accidentally attested.

## [`6.11.1` (2026-04-08)](https://github.com/kdeldycke/repomatic/compare/v6.11.0...v6.11.1)

> [!NOTE]
> `6.11.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.1).

- Parallelize the release workflow: `compile-binaries` starts right after `metadata`, and `publish-pypi` runs concurrently with `create-tag` and `create-release`, with binary and attestation uploads deferred to `publish-release`.
- Fall back to the PyPI `project_urls` changelog link when a package has no GitHub Release, so release notes render a `[Changelog]` link instead of omitting the package.
- Fix the release workflow uploading the attestation bundle before the GitHub release draft existed.
- Skip `exclude-newer-package` exemptions for packages whose fixed version already falls within the `exclude-newer` cooldown window.
- Fix `--delete-excluded` not detecting scope-excluded component files that still exist on disk.
- Fix awesome-template sync overwriting `pyproject.toml` instead of merging, which stripped user-managed `[tool.*]` sections.
- Fix `repomatic init <component>` silently ignoring an explicitly requested component when its scope did not match the repo.
- Fix `--delete-excluded` removing opt-in workflow files in the source repo by skipping config-key exclusions there.
- Fix the `format-pyproject` autofix step running with no input files and masking tool errors.

## [`6.11.0` (2026-04-07)](https://github.com/kdeldycke/repomatic/compare/v6.10.0...v6.11.0)

> [!NOTE]
> `6.11.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.0/).

> [!WARNING]
> `6.11.0` is **not available** on 🐙 GitHub.

- Preserve extra downstream jobs when syncing thin-caller workflows; the managed job is regenerated in place while project-specific jobs, comments, and blank lines are kept.
- Add a VirusTotal scanning job to the release workflow that uploads compiled binaries to seed AV databases. Requires the optional `VIRUSTOTAL_API_KEY` repository secret.
- Verify each attestation in CI right after `actions/attest` with `gh attestation verify`.
- Upload Sigstore attestation bundles (`.jsonl`) as GitHub release assets for compiled binaries and Python packages, enabling offline verification.
- Add a `lint-repo` warning when `VIRUSTOTAL_API_KEY` is missing and Nuitka binary compilation is active.
- Add a VirusTotal API key setup step to the setup guide issue, shown only when Nuitka compilation is active.
- Remove the one-time bumpversion dev-versioning migration code now that all downstream repos use PEP 440 dev versioning.

## [`6.10.0` (2026-04-03)](https://github.com/kdeldycke/repomatic/compare/v6.9.0...v6.10.0)

> [!NOTE]
> `6.10.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.10.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.10.0).

- **Breaking:** Remove the `-o` short option from `pr-body` and `format-images`; use `--output`.
- Add `brand-assets` skill to create and export project logo/banner SVG assets to light/dark PNG variants.
- Add `babysit-ci` skill to monitor CI test workflows, diagnose failures, fix code, and loop until stable jobs pass.
- Add `file-bug-report` skill to write upstream bug reports from contribution guidelines, issue templates, and community norms.
- Add `test-matrix.replace` and `test-matrix.remove` config to swap or drop axis values in the test matrices.
- Add `sync_mode=ONGOING` for tool configs to repeatedly sync while preserving local additions, starting with `sync-bumpversion` keeping local `[[tool.bumpversion.files]]` entries.
- Add `--output-format [markdown|github-actions]` to `sync-uv-lock`, `fix-vulnerable-deps`, `pr-body`, and `format-images`, replacing implicit `$GITHUB_OUTPUT` detection.
- Add `.claude/scheduled_tasks.lock` to the default `.gitignore` extra content.
- Add a collapsible workflow metadata table (trigger, actor, commit, job, workflow, run link) to issue lifecycle comments.
- Make the `setup-guide` issue body a set of collapsible per-step sections with status indicators, and close it only once PAT, permissions, vulnerability alerts, and branch protection are all verified.
- Add `--release-notes/--no-release-notes` and `--table/--no-table` flags to `sync-uv-lock`, defaulting to a terminal table and reserving markdown for `--output`.
- Prune stale `exclude-newer-package` entries from `pyproject.toml` before relocking in `sync-uv-lock`.
- Make the `renovate` component opt-in, and exclude `renovate` and `codecov` from awesome-list repositories.
- Remove Python `3.15t` (free-threaded) from the default test matrix.
- Warn instead of crashing on unknown `[tool.repomatic]` configuration keys.
- Echo `metadata` output to stderr when `--output` targets a file, so computed matrices stay visible in CI logs.
- Add the `repomatic update-docs` command to run `sphinx-apidoc`, RST-to-MyST conversion, and `docs/docs_update.py` in one step.
- Add `docs.apidoc-extra-args`, `docs.apidoc-exclude`, and `docs.update-script` configuration options.
- Move the `sync-uv-lock` job from `renovate.yaml` to `autofix.yaml` so it runs on every push to `main`.
- Fix a CLI crash when `test-matrix.variations` or `test-matrix.replace` contain nested keys.

## [`6.9.0` (2026-03-31)](https://github.com/kdeldycke/repomatic/compare/v6.8.0...v6.9.0)

> [!NOTE]
> `6.9.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.9.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.9.0).

- **Breaking:** Rename the `config` subcommand to `show-config` (it now resolves typed `[tool.repomatic]` config via click-extra).
- **Breaking:** Remove the `prebake-version` and `prebake-tag-sha` commands; use `click-extra prebake` instead.
- Add per-project test matrix configuration via `[tool.repomatic.test-matrix]`, supporting `exclude`, `include`, and `variations`.
- Replace the `audit-deps` lint job with a `fix-vulnerable-deps` autofix job that opens PRs upgrading vulnerable packages.
- Add a `codecov` bundled component that syncs `.github/codecov.yaml` to suppress noisy PR comments.
- Support tool-runner config for tools that discover config from the working directory rather than a `--config` flag.
- Move the mdformat `number` default to a bundled `mdformat.toml` so downstream repos can override it.
- Expand PAT validation in `lint-repo` and `check-renovate` with repository scope, tag ruleset, and permission checks.
- Auto-exclude `changelog.md` for awesome-list repositories.
- Migrate from `actions/attest-build-provenance` to `actions/attest`.
- Run granular PAT permission checks in `setup-guide`, keeping the issue open with a diagnostic table when permissions are incomplete.
- Fix the `setup-guide` job so PAT detection works everywhere.
- Fix an infinite cycle between the `migrate-to-renovate` and `sync-repomatic` jobs.
- Include git stderr in `git-tag` CLI error messages.

## [`6.8.0` (2026-03-27)](https://github.com/kdeldycke/repomatic/compare/v6.7.0...v6.8.0)

> [!NOTE]
> `6.8.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.8.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.8.0).

- **Breaking:** Rename `repomatic init --delete-redundant` to `--delete-unmodified`, which now also removes config files identical to bundled defaults.
- **Breaking:** Remove the deprecated `WORKFLOW_UPDATE_GITHUB_PAT` secret and its fallbacks; downstream repos must use `REPOMATIC_PAT`.
- **Breaking:** Stop persisting `[tool.ruff]` defaults into downstream `pyproject.toml`; bundled ruff config is now injected at runtime when none exists.
- **Breaking:** Remove the `sync-renovate` command, autofix job, `renovate.sync` config toggle, and PR body template; `sync-repomatic` and runtime materialization replace them.
- **Breaking:** Merge `/repomatic-deps-review` into `/repomatic-deps`, which now supports `graph` and `review` modes.
- Move the test matrix definition into `repomatic metadata` so it is available in job-level `if:` conditions.
- Reduce CI jobs on pull requests by skipping release builds, experimental Python versions, and redundant verification tests; the full matrix still runs on push to `main`.
- Make `exclude` config additive to the default exclusions (`labels`, `skills`), and add an `include` config to force-include default-excluded components.
- Auto-exclude the `awesome-triage` skill for non-awesome repositories.
- Add `--delete-excluded` to `repomatic init` to remove excluded files that still exist on disk.
- Replace the `sync-workflows` and `clean-unmodified-configs` autofix jobs with a single `sync-repomatic` job that syncs and prunes managed files in one PR.
- Add PAT capability and repo configuration checks to `lint-repo` (Renovate config, Dependabot security updates off, vulnerability alerts on, PAT permissions).
- Add stale draft release detection to `lint-repo`, warning about draft releases whose tag does not end with `.dev0`.
- Relax the abandoned-dependency threshold from 1 year to 2 years in the Renovate config.
- Fix thin-caller generation rendering `workflow_dispatch` inputs as Python dicts instead of YAML.
- Add the `/sphinx-docs-sync` skill for cross-project Sphinx documentation comparison and synchronization.
- Add the `/translation-sync` skill to detect and draft fixes for stale `readme.*.md` and `contributing.*.md` translations; auto-excluded for non-awesome repos.
- Streamline Dependabot guidance in the setup-guide issue.
- Allow `repomatic init` to accept qualified `component/file` selectors (like `repomatic init skills/repomatic-topics`).
- Only auto-include the `awesome-template` component for `awesome-*` repos when no explicit components are given.
- Add a package version diff table to `sync-uv-lock` PRs, listing updated, added, and removed packages with PyPI links and collapsible release notes.
- Document file naming conventions in `claude.md`: prefer `.yaml` over `.yml` and lowercase filenames, with a table of GitHub exceptions.
- Fix awesome-template URL rewriting to also process `.yml` files in `.github/`.
- Auto-exclude the `changelog.yaml`, `debug.yaml`, and `release.yaml` workflows for `awesome-*` repositories.
- Materialize the bundled `renovate.json5` at runtime when absent, so downstream repos can safely delete their own copy.
- Pin GitHub Actions to SHA digests via Renovate's `helpers:pinGitHubActionDigestsToSemver` preset.
- Add top-level `permissions: {}` to all workflow files, requiring each job to declare its own minimal permissions.
- Fix `sync-repomatic` deleting the upstream repo's own skills.
- Generalize the `opt_in_key` config option into `config_key`/`config_default`.

## [`6.7.0` (2026-03-24)](https://github.com/kdeldycke/repomatic/compare/v6.6.0...v6.7.0)

> [!NOTE]
> `6.7.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.7.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.7.0).

- **Breaking:** Remove the `sync-skills`, `workflow create`, and `workflow sync` commands; `repomatic init` handles all three.
- Bundle awesome-template boilerplate files in `repomatic` instead of cloning `kdeldycke/awesome-template` at runtime.
- Format every `pyproject.toml` in the repo in the `format-pyproject` job, not just the root file.
- Add a branch protection checklist to the setup-guide issue, linking to a pre-filled ruleset creation form.
- Add an opt-in `unsubscribe.yaml` reusable workflow for scheduled cleanup of closed notification threads, enabled via `notification.unsubscribe = true` and requiring `REPOMATIC_NOTIFICATIONS_PAT`.
- Surface actual `gh` CLI error messages in `unsubscribe-threads` warnings.
- Enable `delete-branch: true` on all `peter-evans/create-pull-request` invocations so stale automation PRs auto-close.
- Add `gitleaks` to the tool runner with binary download and `[tool.gitleaks]` config bridge, and migrate `lint-secrets` to `repomatic run gitleaks`.
- Move lychee config from `lychee.toml` to `[tool.lychee]` in `pyproject.toml`.
- Fix the `format-images` job by installing `oxipng` from its GitHub release `.deb` so it runs on `ubuntu-slim`.

## [`6.6.0` (2026-03-23)](https://github.com/kdeldycke/repomatic/compare/v6.5.0...v6.6.0)

> [!NOTE]
> `6.6.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.6.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.6.0).

- **Breaking:** downstream repos with `yamllint` or `zizmor` in their `[tool.repomatic] exclude` list must remove those entries.
- Remove `yamllint` and `zizmor` init components; the tool runner falls back to bundled default configs at runtime. Default `exclude` is now `["labels", "skills"]`.
- Add `repomatic clean-redundant-configs` command and autofix job that removes native config files identical to bundled defaults; `repomatic init` warns about redundant configs on disk.
- Rename the `WORKFLOW_UPDATE_GITHUB_PAT` secret to `REPOMATIC_PAT`; workflows accept both names. Old-name repos get a migration issue that auto-closes once `REPOMATIC_PAT` is detected.
- Add a `setup-guide` toggle to `[tool.repomatic]` to suppress the setup guide issue.
- Pre-fill the fine-grained PAT creation form via URL and provide `gh` CLI commands for adding the secret, configuring Dependabot, and triggering a verify run.
- Add a `lint-repo` check that warns when the owner has GitHub Sponsors enabled but `.github/FUNDING.yml` is missing.

## [`6.5.0` (2026-03-23)](https://github.com/kdeldycke/repomatic/compare/v6.4.1...v6.5.0)

> [!NOTE]
> `6.5.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.5.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.5.0).

- **Breaking:** the old `init.exclude` and `workflow.sync-exclude` keys are no longer recognized and raise a hard error.
- **Breaking:** remove legacy `[tool.gha-utils]` and `[tool.repokit]` config migration; rename old sections to `[tool.repomatic]` manually.
- Replace `init.exclude` and `workflow.sync-exclude` with a unified `exclude` key: bare names exclude whole components, `component/identifier` entries exclude specific files.
- Add `repomatic run <tool>` for unified tool invocation with managed config resolution (native file, `[tool.X]`, bundled default, bare); use `--list` to see managed tools and their active config source.
- Register actionlint, autopep8, biome, bump-my-version, labelmaker, lychee, mdformat, mypy, pyproject-fmt, ruff, typos, yamllint, and zizmor with `repomatic run`, and migrate all workflow tool invocations to it.
- Add a `yamllint` init component, excluded from init by default like `zizmor`.
- Add `repomatic update-checksums --registry` to refresh SHA-256 hashes for binary tools.
- Add `[tool.lychee]` and `[tool.biome]` config translation, so downstream repos can configure lychee and biome from `pyproject.toml` without separate config files.

## [`6.4.1` (2026-03-11)](https://github.com/kdeldycke/repomatic/compare/v6.4.0...v6.4.1)

> [!NOTE]
> `6.4.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.4.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.4.1).

- Add a `github-json` output dialect to `repomatic metadata` that bundles all keys into a single `metadata` output, accessed via `fromJSON(needs.metadata.outputs.metadata).key_name`.
- Add key filtering to `repomatic metadata`: pass key names as arguments to output only those values.
- Add a `--list-keys` flag to `repomatic metadata` to list all available keys with descriptions.
- Rename the `project-metadata` job and step IDs to `metadata` across all workflows.
- Rename the `linters` init component to `zizmor`; default `init.exclude` is now `["labels", "skills", "zizmor"]`.
- Remove the `sync-zizmor` job, CLI command, and `zizmor.sync` toggle; `zizmor.yaml` is now user-owned and created by `repomatic init zizmor` if missing.
- Rename the `bump-versions` job to `bump-version` in `changelog.yaml`.
- Upgrade zizmor to `1.23.0` and re-enable the `template-injection` audit.
- Fix `repomatic metadata` list values breaking GitHub Actions `${{ }}` interpolation: lists are now pre-formatted (file lists as quoted strings, plain lists space-separated, dict lists as JSON).
- Fix `repomatic workflow sync --format header-only` erroring when a target workflow file is absent downstream; missing default files are skipped and named missing files warn instead.
- Enable parallel test execution by default via `--numprocesses=auto`.

## [`6.4.0` (2026-03-10)](https://github.com/kdeldycke/repomatic/compare/v6.3.2...v6.4.0)

> [!NOTE]
> `6.4.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.4.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.4.0).

- Rename `optimize-images` to `format-images`, aligning it with the `format-*` naming convention, and add a matching PR body template.
- Allow `--prefix` and `--template` to be combined in `repomatic pr-body`; the prefix is prepended before the rendered template.
- Add `awesome-template-sync`, `bumpversion-sync`, `dev-release-sync`, `gitignore-sync`, `labels-sync`, `mailmap-sync`, `uv-lock-sync`, and `zizmor-sync` toggles to `[tool.repomatic]`, so each sync operation can be individually disabled.
- Rename `sync-linter-configs` to `sync-zizmor` (and `linter-sync` to `zizmor-sync`), naming the sync job after the tool it syncs.
- Add a `repomatic sync-labels` command wrapping `labelmaker` with toggle check, profile detection, and extra label file handling.
- Replace `AndreasAugustin/actions-template-sync` with a native `repomatic sync-awesome-template` command.
- Add `repomatic init typos` to sync the shared typos spell-checker config into `pyproject.toml`, with proper-noun corrections and `<!-- typos:off -->` / `<!-- typos:on -->` block markers.
- Skip Ruff config injection in `format-python` for non-Python projects, and skip `sync-bumpversion` for non-Python projects.
- Use TOML sub-keys for grouped `[tool.repomatic]` options (like `nuitka.enabled`, `gitignore.location`, `test-plan.file`); only `pypi-package-history` stays flat.
- Add a `workflow-source-paths` option to `[tool.repomatic]`: thin-caller and header-only workflows gain `paths:` filters for the project's source directory, auto-derived from `[project.name]`.
- Add a `repomatic config` command that renders the `[tool.repomatic]` reference table.
- Add `### Configuration` sections to PR body templates listing the relevant `[tool.repomatic]` options.

## [`6.3.2` (2026-03-08)](https://github.com/kdeldycke/repomatic/compare/v6.3.1...v6.3.2)

> [!NOTE]
> `6.3.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.2).

- Add `--all-extras` to the `uv sync` step in `tests.yaml` to catch incompatibilities between optional dependency groups.
- Add a `test-package-install` job to `tests.yaml` that verifies every `[project.scripts]` entry point installs and runs via `uvx`, `uv run --with`, module invocation, `uv tool install`, and `pipx run`, from PyPI and GitHub. Add a `cli_scripts` metadata output.
- Sync `customManagers` to downstream `renovate.json5` so Renovate can update inline version pins in workflow files.
- Fix thin-caller generation stripping `paths` and `paths-ignore` filters, which incorrectly restricted CI triggers downstream.
- Fix the `optimize-images` job failing on `ubuntu-slim` where `oxipng` is unavailable.
- Add a `citation.cff` `date-released` update to the bundled `bumpversion.toml` template so downstream repos keep their release date in sync on version bumps.

## [`6.3.1` (2026-03-07)](https://github.com/kdeldycke/repomatic/compare/v6.3.0...v6.3.1)

> [!NOTE]
> `6.3.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.1).

- Sync the `repomatic-audit` skill to downstream repos.

## [`6.3.0` (2026-03-06)](https://github.com/kdeldycke/repomatic/compare/v6.2.1...v6.3.0)

> [!NOTE]
> `6.3.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.0).

- `repomatic init` now always overwrites managed files (workflows, configs, skills) by default; remove the `--overwrite` flag. `changelog.md` is never overwritten once it exists.
- `repomatic init` output now distinguishes created, updated, and skipped files, and warns about excluded files still on disk.
- Auto-remove legacy `.claude/skills/gha-*/` skill directories during `repomatic init`, completing the `gha-utils` to `repomatic` rename.
- `sync-bumpversion`, `sync-linter-configs`, and `sync-skills` now report both created and updated files.
- `sync-bumpversion` now replaces the whole `[tool.bumpversion]` section from the bundled template instead of applying incremental migrations.
- Use the short SHA in release workflow job names instead of the full commit hash.

## [`6.2.1` (2026-03-06)](https://github.com/kdeldycke/repomatic/compare/v6.2.0...v6.2.1)

> [!NOTE]
> `6.2.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.2.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.2.1).

- Fix `actions/checkout` wiping downloaded Python package artifacts before `gh release create` could attach them, so release drafts now include the distribution files.
- Fix `fix-changelog` marking releases as not available on GitHub while the release was still a draft.

## [`6.2.0` (2026-03-05)](https://github.com/kdeldycke/repomatic/compare/v6.1.0...v6.2.0)

> [!NOTE]
> `6.2.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.2.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.2.0).

- Add the `repomatic optimize-images` CLI command (lossless `oxipng` for PNG, `jpegoptim` for JPEG), replacing `calibreapp/image-actions`.
- Add the `sync-dev-release` CLI command and workflow job to maintain a rolling dev pre-release on GitHub with the latest binaries and Python package.
- Add the `repomatic-topics` skill for optimizing GitHub repository topics for discoverability.
- Add a `lint-repo` check that warns when GitHub topics are not a subset of `pyproject.toml` keywords.
- Add the `init-exclude` config option to skip components during `repomatic init`, defaulting to `["labels", "linters", "skills"]`; `workflow-sync-exclude` now also applies to `repomatic init`.
- Add `rename-from` rules to migrate all 9 default GitHub labels.
- Add the package version to compiled binary filenames (`repomatic-6.2.0-linux-arm64.bin`).
- Automatically migrate `[tool.gha-utils]` and `[tool.repokit]` config sections to `[tool.repomatic]` during `repomatic init`; commands fall back to legacy section names when `[tool.repomatic]` is absent.
- Support GitHub immutable releases by drafting releases then publishing them.
- Replace `softprops/action-gh-release` with `gh release create`; all release operations now use the `gh` CLI.
- Freeze readme binary download URLs to versioned `/releases/download/vX.Y.Z/` paths during releases.
- GitHub releases now include PyPI and GitHub availability links at creation time.
- Fix Nuitka-compiled binaries silently producing no output when the entry point is a `__main__.py` inside a package.
- Fix `update-checksums` leaving stale SHA-256 hashes when the hash and `sha256sum --check` keyword span multiple lines.
- Fix Windows ARM64 test runners using x86_64 emulation by forcing native ARM64 Python via `UV_PYTHON`.
- Fix `fix-changelog` producing a trailing blank line when the last changelog section is modified.

## [`6.1.0` (2026-02-27)](https://github.com/kdeldycke/repomatic/compare/v6.0.1...v6.1.0)

> [!NOTE]
> `6.1.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.1.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.1.0).

- Add the `unsubscribe-threads` CLI command to unsubscribe from closed, inactive GitHub notification threads.
- Add the `prebake-version` CLI command to inject the Git commit hash into `__version__` before Nuitka compilation, so binaries report the exact commit they were built from (e.g., `6.1.0.dev0+abc1234`).
- Add the `list-skills` CLI command to display all available Claude Code skills grouped by lifecycle phase.
- Add the `sync-github-releases` CLI command to sync GitHub release notes from `changelog.md`.
- Add the `pypi-package-history` config option so `lint-changelog` fetches releases from former package names and generates correct PyPI URLs for renamed projects.
- `lint-changelog` now detects orphaned versions (git tags, GitHub releases, or PyPI packages with no changelog entry) and inserts placeholder sections in `--fix` mode.
- Rename the `lint-changelog` workflow job to `fix-changelog`; the CLI command remains `lint-changelog`.
- Make changelog entries and GitHub release bodies template-driven via `release-notes.md` and `github-releases.md`, so editing one template affects only its destination.
- Group CLI commands into sections (Project setup, Release & versioning, Sync, Linting & checks, GitHub issues & PRs) in help output.
- Add next-step handoff suggestions to all Claude Code skills, and document skills with a grouped table and walkthrough in `readme.md`.
- Generate thin caller workflows with explicit secret forwarding instead of `secrets: inherit`.
- Move zizmor config from `.github/zizmor.yml` to `zizmor.yaml` at repo root.

## [`6.0.1` (2026-02-24)](https://github.com/kdeldycke/repomatic/compare/v6.0.0...v6.0.1)

> [!NOTE]
> First release under the [`repomatic`](https://pypi.org/project/repomatic/) name on PyPI, after `repokit` was rejected for typo-squatting ([see `6.0.0` below](#600-2026-02-24)). The GitHub repository is [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic).

> [!NOTE]
> `6.0.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.0.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.0.1).

- Rename project from `repokit` to `repomatic`. Rename GitHub repository from `kdeldycke/repokit` to `kdeldycke/repomatic`.

## [`6.0.0` (2026-02-24)](https://github.com/kdeldycke/repomatic/compare/v5.14.1...v6.0.0)

> [!CAUTION]
> This release was deleted from PyPI. It was supposed to be published as `repokit`, but PyPI flagged the name as typo-squatting the pre-existing [`repo-kit`](https://pypi.org/project/repo-kit/) package.

> [!NOTE]
> `6.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.0.0).

> [!WARNING]
> `6.0.0` is **not available** on 🐍 PyPI.

- Rename project from `gha-utils` to `repokit`. Rename GitHub repository from `kdeldycke/workflows` to `kdeldycke/repokit`.

## [`5.14.1` (2026-02-24)](https://github.com/kdeldycke/repomatic/compare/v5.14.0...v5.14.1)

> [!NOTE]
> `5.14.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.14.1/).

> [!WARNING]
> `5.14.1` is **not available** on 🐙 GitHub.

- Re-release `5.14.0` as `5.14.1` on PyPI but update Python package metadata to point to the new `repomatic` name and repository URL instead of `repokit`.

## [`5.14.0` (2026-02-24)](https://github.com/kdeldycke/repomatic/compare/v5.13.0...v5.14.0)

> [!CAUTION]
> `5.14.0` has been [yanked from PyPI](https://pypi.org/project/gha-utils/5.14.0/).

> [!WARNING]
> Attempt to be the final release under [`gha-utils`](https://pypi.org/project/gha-utils/) name on PyPI, with metadata pointing to `repokit`. This release was yanked after the `repokit` name was rejected by PyPI for typo-squatting the pre-existing [`repo-kit`](https://pypi.org/project/repo-kit/) package.

> [!NOTE]
> `5.14.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.14.0).

- Add messages to redirect PyPI package from `gha-utils` to `repokit` and GitHub repository from `kdeldycke/workflows` to `kdeldycke/repokit`.

## [`5.13.0` (2026-02-23)](https://github.com/kdeldycke/repomatic/compare/v5.12.0...v5.13.0)

> [!NOTE]
> `5.13.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.13.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.13.0).

- Harmonize sync/update naming across CLI commands, workflow jobs, PR branches, and templates. `sync-X` means regenerate from a canonical source; `update-X` means compute from project state. Breaking renames: `update-gitignore` → `sync-gitignore`, `mailmap-sync` → `sync-mailmap`, `deps-graph` → `update-deps-graph`. New `sync-bumpversion` CLI command replaces `init bumpversion` in the autofix job. Fix `autofix-typo` branch → `fix-typos`.
- Add Claude Code skills (`.claude/skills/`) wrapping `gha-utils` CLI commands as slash commands: `/gha-init`, `/gha-changelog`, `/gha-release`, `/gha-lint`, `/gha-sync`, `/gha-deps`, `/gha-test`, `/gha-metadata`. Distribute skills via `gha-utils init skills` and `gha-utils sync-skills` for downstream repos.
- Add `sync-renovate` CLI command and autofix workflow job to keep downstream `renovate.json5` in sync with the canonical reference from `gha-utils`. Opt out via `renovate-sync = false` in `[tool.gha-utils]`.
- Add `workflow-sync` and `workflow-sync-exclude` config keys to `[tool.gha-utils]` for per-project control over which workflows are synced. Explicit CLI positional arguments override both settings.
- Add `--format header-only` mode to `gha-utils workflow sync` for syncing headers (`name`, `on`, `concurrency`) of non-reusable workflows like `tests.yaml`.
- Use explicit `--format thin-caller` in `autofix.yaml` sync step.
- Fix race condition where binary builds could finish before `create-release`, causing `gh release upload` to fail with "release not found". Make `compile-binaries` depend on `create-release`.
- Add `lint-workflow-security` job to lint workflow using [`zizmor`](https://github.com/woodruffw/zizmor) to detect security vulnerabilities in GitHub Actions workflows. Closes #1478.
- Add `sync-linter-configs` CLI command and autofix job to sync `.github/zizmor.yml` to downstream repos. Add `linters` init component.
- Add per-project dependency graph configuration to `[tool.gha-utils]`: `dependency-graph-all-groups`, `dependency-graph-all-extras`, `dependency-graph-no-groups`, `dependency-graph-no-extras`, and `dependency-graph-level`. The `update-deps-graph` command reads these as defaults, with CLI flags taking precedence. The autofix workflow no longer hardcodes `--all-groups --all-extras`.

## [`5.12.0` (2026-02-22)](https://github.com/kdeldycke/repomatic/compare/v5.11.1...v5.12.0)

> [!NOTE]
> `5.12.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.12.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.12.0).

- Decouple GitHub release creation from binary builds. The release is published immediately after tagging with the Python package only; each binary uploads itself independently as its build completes.
- Remove `gha-utils collect-artifacts` subcommand. Binary renaming and upload is now handled directly in the `compile-binaries` workflow job.
- Add `nuitka-extra-args` config field to `[tool.gha-utils]` for project-specific Nuitka flags. Remove hard-coded Nuitka includes from the workflow. Fixes downstream repos failing on `gha_utils/templates/*.md` include.
- Add `🪫 AI slop` label. Merge `🎁 feature request` into `✨ enhancement`. Fix label descriptions and normalize hex color casing.

## [`5.11.1` (2026-02-21)](https://github.com/kdeldycke/repomatic/compare/v5.11.0...v5.11.1)

> [!NOTE]
> `5.11.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.11.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.11.1).

- Declare `WORKFLOW_UPDATE_GITHUB_PAT` as an optional `workflow_call` secret to fix downstream compatibility.
- Fix Renovate `postUpgradeTasks` failing to update SHA-256 checksums. Replace containerbase `install-tool uv` with a direct pinned binary download verified by SHA-256.
- Fix `gha-utils workflow sync` defaulting to `@main` instead of the version tag when run from a released package.

## [`5.11.0` (2026-02-21)](https://github.com/kdeldycke/repomatic/compare/v5.10.4...v5.11.0)

> [!NOTE]
> `5.11.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.11.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.11.0).

- Add `--no-group`, `--no-extra`, `--only-group`, and `--only-extra` options to `deps-graph` command.
- Make `gha-utils init bumpversion` idempotent: updates existing `[tool.bumpversion]` configs with dev versioning keys (`parse`, `serialize`, `[parts.dev]`) and bumps `current_version` to `.dev0`.
- Add `::error::` annotation when `create-pull-request` fails due to missing `WORKFLOW_UPDATE_GITHUB_PAT` secret.
- Add `Dependabot alerts` and `Issues` permissions to setup-guide issue template, matching the readme.
- Document which jobs use `WORKFLOW_UPDATE_GITHUB_PAT` and why each permission is needed.
- Skip PyPI package build for the post-release bump commit during releases.

## [`5.10.4` (2026-02-19)](https://github.com/kdeldycke/repomatic/compare/v5.10.3...v5.10.4)

> [!NOTE]
> `5.10.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.10.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.10.4).

- Replace `revert-squash-merge` auto-revert with `detect-squash-merge` notification. On accidental squash merge of a release PR, the job now opens a GitHub issue and fails the workflow instead of reverting on `main`. The release is skipped; the maintainer releases the next version when ready.
- Skip `lint-changelog` job on release and post-release commits.
- Skip Nuitka binary builds for the post-release bump commit during releases, halving the number of builds.
- Fix freeze/unfreeze cycle corrupting YAML and JSON5 comments that mention `--from . gha-utils`.

## [`5.10.3` (2026-02-19)](https://github.com/kdeldycke/repomatic/compare/v5.10.2...v5.10.3)

> [!NOTE]
> `5.10.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.10.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.10.3).

- Add `--include-package-data=extra_platforms` to Nuitka build to fix `FileNotFoundError` on `architecture_data.py` in compiled binaries.
- Add `sync-uv-lock` command that runs `uv lock` and avoid `exclude-newer-package` timestamp noise.
- Add `sync-uv-lock` job to `autofix.yaml` to replace Renovate's `lockFileMaintenance`, which cannot reliably revert timestamp-only noise in `uv.lock`.
- Remove `update-exclude-newer` command.
- Remove Renovate `lockFileMaintenance` configuration and its `postUpgradeTasks` rule.
- Revert `exclude-newer` in `pyproject.toml` from a fixed date to `"1 week"`.

## [`5.10.2` (2026-02-18)](https://github.com/kdeldycke/repomatic/compare/v5.10.1...v5.10.2)

> [!NOTE]
> `5.10.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.10.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.10.2).

- Add `gha-utils lint-changelog` command to verify changelog release dates against PyPI upload dates, with git tag fallback for non-PyPI projects.
- Add auto-correct changelog capabilities to fix dates and add availability admonitions for PyPI and GitHub releases.
- Add `lint-changelog` job to `autofix.yaml` to auto-fix changelog dates and admonitions via PR.
- Add PyPI and GitHub release availability admonitions in changelog.
- Change development warning from `[!IMPORTANT]` to `[!WARNING]` GFM alert.
- Document version and tag naming conventions.
- Fix Renovate `postUpgradeTasks` failing with `uvx: not found` by installing tools via `binarySource: install` inside the Renovate Docker container.
- Fix Renovate aborting entirely when python.org API is rate-limited.
- Move `exclude-newer` date update from direct push to `main` into Renovate's lock file maintenance PR via `postUpgradeTasks`.
- Skip Nuitka binary builds for non-code pushes to `main`.

## [`5.10.1` (2026-02-17)](https://github.com/kdeldycke/repomatic/compare/v5.10.0...v5.10.1)

> [!NOTE]
> `5.10.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.10.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.10.1).

- Fix `publish-github-release` job failing because `actions/checkout` wiped downloaded artifacts.
- Fix broken links issue being created when lychee exits with a non-zero code but produces no output file.

## [`5.10.0` (2026-02-16)](https://github.com/kdeldycke/repomatic/compare/v5.9.1...v5.10.0)

> [!NOTE]
> `5.10.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.10.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.10.0).

- Add `gha-utils init` command to bootstrap repositories for reusable workflows.
- Add `sync-workflows` job to `autofix.yaml` for downstream repos to keep their thin-caller workflow files in sync.
- Add `setup-guide` job to `autofix.yaml` for user onboarding and configuration validation.
- Add reopen support to issue lifecycle management. Previously closed issues are reopened instead of creating duplicates.
- Add SHA-256 checksum verification for all `lychee`, `typos`, `Biome` and `labelmaker` binary downloads, with `gha-utils update-checksums` command and Renovate `postUpgradeTasks` for automatic updates.
- Fix creation of GitHub release in `create-release` job.
- Replace thin-wrapper `lycheeverse/lychee-action`, `crate-ci/typos` and `biomejs/setup-biome` actions by direct binary downloads for better performance and reliability.
- Auto-detect options values from `$GITHUB_REPOSITORY` for `broken-links`, `lint-repo` and `release-prep` commands.
- Auto-detect version in `pr-body` from `[tool.bumpversion]` in `pyproject.toml`.
- Add `.dev0` suffix to development versions per PEP 440, with `--version` flag appending the short git commit hash for dev versions (e.g., `5.9.2.dev0+abc1234`). Closes #169.
- Add quick start tutorial to readme.
- Consolidate Lychee and Sphinx linkcheck broken link reports into a single "Broken links" issue.
- Add `Full Changelog` comparison URL link to GitHub release notes.
- Default `mailmap-sync` destination to the source file path (in-place update) instead of stdout.
- Enable consecutive ordered list numbering (`--number`) in mdformat.
- Display auto-detected environment from extra-platforms in `debug.yaml` workflow.
- Move release notes, PR metadata block, and refresh tip to markdown templates in `gha_utils/templates/`.
- Regroup GitHub-specific modules under `gha_utils/github/`.
- Remove `merge-method-notice` job.
- Remove `gha-utils bundled` subcommand group.
- Experiment with Claude agents.

## [`5.9.1` (2026-02-15)](https://github.com/kdeldycke/repomatic/compare/v5.9.0...v5.9.1)

> [!NOTE]
> `5.9.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.9.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.9.1).

- Refactor PR body templates from Python literals to markdown files with YAML frontmatter for metadata (titles, commit messages). The `pr-body` CLI now outputs `title` and `commit_message` alongside `body`.
- Add and enrich PR templates for all autofix jobs, version bumps, release preparation, and `.gitignore` updates.
- Dogfood `gha-utils` from local source on `main` branch via `uvx --from . gha-utils`. During release freeze, CLI invocations are frozen back to a PyPI version for downstream compatibility.
- Add fallback jobs to `release.yaml` to detect and auto-reverts squash-merged release PRs.
- Rebase lock file maintenance PR whenever `main` advances, not just on conflicts.
- Remove `--insecure` flag and redirect-suppression excludes from lychee configuration.

## [`5.9.0` (2026-02-14)](https://github.com/kdeldycke/repomatic/compare/v5.8.0...v5.9.0)

> [!NOTE]
> `5.9.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.9.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.9.0).

- Add `gha-utils workflow` command group with `create`, `sync`, and `lint` subcommands.
- Add `[tool.gha-utils] nuitka` option to opt out of Nuitka binary compilation.
- Move all reusable workflow inputs to `[tool.gha-utils]` in `pyproject.toml`.
- Add `gha-utils update-gitignore` subcommand replacing shell-based `.gitignore` generation.
- Have `lint-repo` read `package_name`, `is_sphinx`, and `project_description` directly from `pyproject.toml`.
- Add `--source-url` option to `gha-utils sphinx-linkcheck`.
- Include actual output in test-plan assertion error messages.
- Replace `pr-metadata` composite action with `gha-utils pr-body` subcommand.
- Have `test-plan` and `deps-graph` read `[tool.gha-utils]` config directly from `pyproject.toml`.
- Drop `--force` from `sphinx-apidoc` in `update-docs` job to preserve downstream RST customizations.
- Recreate version bump PRs on every push to `main` to prevent merge conflicts.
- Fix `<details>` tag in PR body rendered as code block due to indentation.
- Deduplicate `gitignore-extra-categories` with base categories while preserving order.
- Add `--template` option to `gha-utils pr-body` with built-in `bump-version` and `prepare-release` templates.
- Add refresh tip admonition to all auto-created PR bodies.
- Pin lychee binary version in `docs.yaml` and add Renovate custom manager for updates.

## [`5.8.0` (2026-02-11)](https://github.com/kdeldycke/repomatic/compare/v5.7.2...v5.8.0)

> [!NOTE]
> `5.8.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.8.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.8.0).

- Fix stale checkout in `bump-versions` causing merge conflicts after releases.
- Add `cancel-runs.yaml` workflow to cancel in-progress and queued runs when a PR is closed.
- Add `gha-utils pr-body` subcommand to generate PR body with workflow metadata.
- Remove `update-cli-pins` job from `release.yaml`. Renovate already handles updating `gha-utils` version.

## [`5.7.2` (2026-02-11)](https://github.com/kdeldycke/repomatic/compare/v5.7.1...v5.7.2)

> [!NOTE]
> `5.7.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.7.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.7.2).

- Replace `taiki-e/install-action` with direct `curl` download for `labelmaker` binary.
- Fix `update-cli-pins` job failing due to shallow clone.
- Fix `changelog.yaml` concurrency race from fast-completing `release.yaml`.
- Include version numbers in post-release commit message (e.g. `[changelog] Post-release bump v5.7.1 → v5.7.2`).
- Only run `debug.yaml` workflow manually and once a month.
- Document the freeze/unfreeze release commit vocabulary.
- Document the ✅/⁉️ convention for stable vs. unstable jobs.

## [`5.7.1` (2026-02-10)](https://github.com/kdeldycke/repomatic/compare/v5.7.0...v5.7.1)

> [!NOTE]
> `5.7.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.7.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.7.1).

- Fix `gha-utils` CLI version pins stuck at `5.6.1` by moving pin updates to a post-release `update-cli-pins` job in `release.yaml` that runs after the new version is published to PyPI.
- Add build provenance attestation to `build-package` job for defense-in-depth supply chain security.
- Add `.mdx` to recognized Markdown file extensions for ruff formatting and file discovery.
- Preserve `[project.entry-points]` table format in `format-pyproject` autofix job.
- Move 6 autofix jobs (`fix-typos`, `optimize-images`, `update-mailmap`, `update-deps-graph`, `update-docs`, `sync-awesome-template`) from `docs.yaml` to `autofix.yaml`.
- Move `check-broken-links` job from `lint.yaml` to `docs.yaml`.
- Harmonize job IDs to consistent `verb-target` naming: `autofix-typo` to `fix-typos`, `awesome-template-sync` to `sync-awesome-template`, `lint-mypy` to `lint-types`, `lint-github-action` to `lint-github-actions`, `check-secrets` to `lint-secrets`, `package-build` to `build-package`, `pypi-publish` to `publish-pypi`, `github-release` to `create-release`, `git-tag` to `create-tag`, `version-increments` to `bump-versions`, `sphinx-linkcheck` to `check-sphinx-links`, `broken-links` to `check-broken-links`.

## [`5.7.0` (2026-02-09)](https://github.com/kdeldycke/repomatic/compare/v5.6.2...v5.7.0)

> [!NOTE]
> `5.7.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.7.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.7.0).

- Add `gha-utils sphinx-linkcheck` command to detect broken auto-generated links.
- Replace `blacken-docs` by `ruff` for Markdown code formatting.
- Remove `blacken_docs_params` from `gha-utils metadata` output.
- Fix `update-deps-graph` job.
- Fix `pr-metadata` action stripping backticks from PR body.
- Fix `mappingproxy` object pickling error in test plan execution on Python < 3.13.
- Fix `UnicodeEncodeError` on Windows for `mailmap-sync` command and test plan execution.
- Simplify `changelog.yaml` concurrency to be always-cancellable.
- Prevent redundant `prepare-release` double-runs on every push to `main`.
- Add `workflow_dispatch` trigger to all workflows except `release.yaml` for manual re-runs from the Actions UI.
- Skip expensive test matrix on doc-only and workflow-only changes.

## [`5.6.2` (2026-02-02)](https://github.com/kdeldycke/repomatic/compare/v5.6.1...v5.6.2)

> [!NOTE]
> `5.6.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.6.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.6.2).

- Auto-update `gha-utils==X.Y.Z` CLI version pins in workflow files during release.

## [`5.6.1` (2026-02-02)](https://github.com/kdeldycke/repomatic/compare/v5.6.0...v5.6.1)

> [!NOTE]
> `5.6.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.6.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.6.1).

- Add missing `renovate.json5` template file from the bundled Python package.
- Keep bundled `renovate.json5` configuration in sync with root file from this repository.
- Add `--no-progress` to all `uvx` commands in workflows for cleaner CI logs.
- Add `--frozen --no-progress` to `uv run` commands in workflows for reproducible builds.
- Detect Renovate PRs by branch name pattern (`renovate/*`) to skip labellers when Renovate runs as a user account.
- Rename `update-autodoc` job to `update-docs` and run `docs/docs_update.py` if present to generate dynamic content (tables, diagrams, directives) after `sphinx-apidoc`.

## [`5.6.0` (2026-02-02)](https://github.com/kdeldycke/repomatic/compare/v5.5.1...v5.6.0)

> [!NOTE]
> `5.6.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.6.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.6.0).

- Add `migrate-to-renovate` job to `renovate.yaml` workflow that creates a PR that create a `renovate.json5` and remove Dependabot config.
- Add dynamic prerequisites status table to migration PR body with links to settings pages.
- Add `--format` option to `gha-utils check-renovate` for JSON and GitHub Actions output formats.
- Move prerequisite validation to `renovate` job to fail fast if requirements aren't met.
- Rename `gha-utils check-renovate-prereqs` to `gha-utils check-renovate`.
- Fix `gha-utils update-exclude-newer` to handle relative date strings.

## [`5.5.1` (2026-01-30)](https://github.com/kdeldycke/repomatic/compare/v5.5.0...v5.5.1)

> [!NOTE]
> `5.5.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.5.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.5.1).

- Replace workflow shell scripts with CLI commands.
- Add `pr-metadata` composite action to deduplicate PR body metadata across workflows.
- Increase Renovate `minimumReleaseAge` for patches from 5 to 8 days to avoid proposing updates blocked by uv's `exclude-newer` setting.
- Enhance `gha-utils update-exclude-newer` to add missing `exclude-newer` when `[tool.uv]` section exists.
- Add `renovate.json5` to bundled exports for Dependabot-to-Renovate migration.
- Add Dependabot config check to `lint-repo` command with migration guidance.
- Fix Codecov upload by splitting `report_type` into separate action calls.

## [`5.5.0` (2026-01-29)](https://github.com/kdeldycke/repomatic/compare/v5.4.0...v5.5.0)

> [!NOTE]
> `5.5.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.5.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.5.0).

- Add `gha-utils update-exclude-newer` command to update the exclude-newer date in pyproject.toml.
- Add `gha-utils check-renovate-prereqs` command to validate Renovate prerequisites.
- Add `gha-utils lint-repo` command to run repository consistency checks.
- Add `gha-utils git-tag` command for idempotent Git tag creation and pushing.
- Add `gha-utils verify-binary` command to verify compiled binary architectures using exiftool.
- Add `gha-utils collect-artifacts` command to collect and rename artifacts for GitHub releases.
- Add `gha-utils broken-links` command to manage broken link issue lifecycle.
- Add `gha-utils deps-graph` command to generate Mermaid dependency graphs from uv lockfile.
- Replace `pipdeptree` with `uv export --format cyclonedx1.5` for dependency graph generation.
- Consolidate all bundled files under `gha-utils bundled export <filename>` command.
- Add bundled mypy and pytest configuration templates.
- Use actual filenames with extensions as type IDs (e.g., `labels.toml`, `labeller-file-based.yaml`).
- Add smart default output paths for each file type (shown with `--list`).
- Replace `eslint` with `biome` for JSON formatting.
- Format `pyproject.toml` file with `pyproject-fmt`.

## [`5.4.0` (2026-01-25)](https://github.com/kdeldycke/repomatic/compare/v5.3.1...v5.4.0)

> [!NOTE]
> `5.4.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.4.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.4.0).

- Add unified `gha-utils config` command group with `init`, `export`, `labels`, and `workflows` subcommands.
- Move `gha-utils labels` to `gha-utils config labels` subcommand.
- Move `gha-utils workflows` to `gha-utils config workflows` subcommand.
- Merge all configuration modules (`labels.py`, `workflows.py`) into unified `bundled_config` module.
- Add bundled Ruff configuration template (`[tool.ruff]`). Closes #659.
- Fix data files not being included in published package. Data files are now stored directly in `gha_utils/data/`.

## [`5.3.1` (2026-01-24)](https://github.com/kdeldycke/repomatic/compare/v5.3.0...v5.3.1)

> [!NOTE]
> `5.3.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.3.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.3.1).

- Add `gha-utils sponsor-label` command to label issues/PRs from GitHub sponsors.
- Replace unmaintained `JasonEtco/is-sponsor-label-action` with `gha-utils sponsor-label`.
- Change `gha-utils metadata` output path from positional argument to `-o/--output` option.
- Make `--overwrite` the default behavior for `gha-utils metadata`.
- Fix `labels`, `workflows` and `bumpversion` commands fetching of `gha_utils/data/` content.
- Fix race condition where `version-increments` job would skip due to missing tags.
- Fallback to getting version from commits when tags aren't available yet.
- Replace push-based post-release trigger with `workflow_run` trigger for version increments.

## [`5.3.0` (2026-01-24)](https://github.com/kdeldycke/repomatic/compare/v5.2.0...v5.3.0)

> [!NOTE]
> `5.3.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.3.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.3.0).

- Rely on `gha-utils labels` to dump label configuration files for inspection and bootstrapping.
- Enhance `gha-utils bumpversion` command to sync the template directly into `pyproject.toml`.
- Sync bumpversion config in the `autofix` workflow.
- Sync `uv.lock` file on version increment and post-release bump commits.
- Fail job on project description mismatch.
- Skip binary compilation for branches that don't affect code (`.mailmap`, docs, images, `.gitignore`, JSON, Markdown).
- Remove `GITHUB_CONTEXT` env var requirement from workflows. Now reads event data directly from `GITHUB_EVENT_PATH`.

## [`5.2.0` (2026-01-23)](https://github.com/kdeldycke/repomatic/compare/v5.1.0...v5.2.0)

> [!NOTE]
> `5.2.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.2.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.2.0).

- Add `gha-utils labels` command to dump bundled label configuration files.
- Add `gha-utils workflows` command to dump bundled workflow templates for inspection and bootstrapping.
- Add `gha-utils bumpversion` command to dump bundled bump-my-version configuration template.
- Rely on `gha-utils release-prep` for release preparation steps.
- Protect the release commit from cancellation by giving it its own concurrency group.
- Fix metadata extraction failing on tag push events due to null SHA.
- Trigger version increment PRs immediately after a release instead of waiting for next scheduled run.

## [`5.1.0` (2026-01-23)](https://github.com/kdeldycke/repomatic/compare/v5.0.1...v5.1.0)

> [!NOTE]
> `5.1.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.1.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.1.0).

- Add new `gha-utils release-prep` command to consolidate all release preparation steps.
- Add new `gha-utils version-check` command to prevent double version increments within a development cycle.
- Switch from from `requirements/*.txt` files to hard-coding version dependencies for workflow tools.
- Check consistency between project and repository metadata. Don't fix them automatically. Refs #93.
- Use fixed `exclude-newer` date instead of relative `1 week` to prevent `uv.lock` timestamp churn.
- Reduce frequency of version increment jobs to once a day.
- Only update dependency graph on release. Closes #176.
- Only bump citation date on a release.
- Unfreeze `bump-my-version` from 1.1.0.

## [`5.0.1` (2026-01-22)](https://github.com/kdeldycke/repomatic/compare/v5.0.0...v5.0.1)

> [!NOTE]
> `5.0.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/5.0.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.0.1).

- Fix publishing to PyPI by removing URL-based dependency on `mdformat-pelican`.
- Protect both release commits and post-release bump commits from cancellation.

## [`5.0.0` (2026-01-22)](https://github.com/kdeldycke/repomatic/compare/v4.25.5...v5.0.0)

> [!NOTE]
> `5.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v5.0.0).

> [!WARNING]
> `5.0.0` is **not available** on 🐍 PyPI.

- Duplicate workflow dependencies from `requirements/*.txt` files to `gha-utils` package as extra dependencies.
- Replace Dependabot by Renovate for dependency updates. Closes #1728.
- Flag abandoned dependencies with `⚠️ stale dependency` label.
- Check repository settings requirements for Renovate.
- Use Renovate to sync `uv.lock` files. Remove `sync-uv-lock` job.
- Cancel in-progress jobs more aggressively on new commits.
- Add `toml_files` and `project_description` fields to `gha-utils metadata` output.
- Replace `julb/action-manage-label` by `labelmaker` for label management. Closes #1914.
- Do not let `actions/labeler` remove labels that don't strictly match its file-based rules.
- Add Rust-related entries to `.gitignore` file.
- Sync GitHub repository description from `pyproject.toml` file. Closes #93.
- Document workflow concurrency.
- Add unittests for workflow content.

## [`4.25.5` (2026-01-09)](https://github.com/kdeldycke/repomatic/compare/v4.25.4...v4.25.5)

> [!NOTE]
> `4.25.5` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.25.5/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.25.5).

- Fix call to deprecated `extra-platforms` method.

## [`4.25.4` (2025-12-31)](https://github.com/kdeldycke/repomatic/compare/v4.25.3...v4.25.4)

> [!NOTE]
> `4.25.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.25.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.25.4).

- Move auto-lock time from 8:43 to 4:43.
- Let projects defined their own cooldown period via the `pyproject.toml`.
- Replace deprecated `codecov/test-results-action` by `codecov/codecov-action`.

## [`4.25.3` (2025-12-19)](https://github.com/kdeldycke/repomatic/compare/v4.25.2...v4.25.3)

> [!NOTE]
> `4.25.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.25.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.25.3).

- Add Download link to project metadata.
- Include license file in package.
- Remove utilization workaround for `macos-15-intel`.

## [`4.25.2` (2025-12-07)](https://github.com/kdeldycke/repomatic/compare/v4.25.1...v4.25.2)

> [!NOTE]
> `4.25.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.25.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.25.2).

- Use uncap dependencies everywhere.

## [`4.25.1` (2025-12-06)](https://github.com/kdeldycke/repomatic/compare/v4.25.0...v4.25.1)

> [!NOTE]
> `4.25.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.25.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.25.1).

- Replace `tool.uv` section by `build-system`.

## [`4.25.0` (2025-12-06)](https://github.com/kdeldycke/repomatic/compare/v4.24.6...v4.25.0)

> [!NOTE]
> `4.25.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.25.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.25.0).

- Add cooldown period for dependabot updates: 7 days by default, 29 days for major updates, 11 days for minor updates, 5 days for patch updates.
- Sets `--exclude-newer` to 7 days ago when syncing `uv.lock` in `autofix` workflow.
- Merge all label syncing jobs into a single one.
- Change the `test` and `typing` extra dependency groups into development dependency groups.
- Make all documentation-related and typing-related jobs depends on the new development dependency groups.
- Remove forcing Python `3.14` in documentation-related jobs.
- Unlock a CPU core stuck at 100% utilization on `macos-15-intel`.
- Document all reusable workflows jobs and their requirements. Closes #60.
- Reintroduce Python 3.10 support.

## [`4.24.6` (2025-12-01)](https://github.com/kdeldycke/repomatic/compare/v4.24.5...v4.24.6)

> [!NOTE]
> `4.24.6` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.6/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.6).

- Do not check for broken links in pull requests.

## [`4.24.5` (2025-11-28)](https://github.com/kdeldycke/repomatic/compare/v4.24.4...v4.24.5)

> [!NOTE]
> `4.24.5` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.5/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.5).

- Use released versions of `mdformat-myst` plugin.

## [`4.24.4` (2025-11-27)](https://github.com/kdeldycke/repomatic/compare/v4.24.3...v4.24.4)

> [!NOTE]
> `4.24.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.4).

- Add `ubuntu-slim` to the list of platforms in debug workflows.

## [`4.24.3` (2025-11-24)](https://github.com/kdeldycke/repomatic/compare/v4.24.2...v4.24.3)

> [!NOTE]
> `4.24.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.3).

- Replace `mdformat_frontmatter` by `mdformat-front-matters` to fix compatibility with `mdformat` `v1.0.0`.
- Activate strict front-matter checking in `mdformat` when auto-formatting Markdown files.

## [`4.24.2` (2025-11-23)](https://github.com/kdeldycke/repomatic/compare/v4.24.1...v4.24.2)

> [!NOTE]
> `4.24.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.2).

- Fix all issues related to the use of `ubuntu-slim` runners.

## [`4.24.1` (2025-11-23)](https://github.com/kdeldycke/repomatic/compare/v4.24.0...v4.24.1)

> [!NOTE]
> `4.24.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.1).

- Keep using `ubuntu-24.04` for Nuitka builds.

## [`4.24.0` (2025-11-23)](https://github.com/kdeldycke/repomatic/compare/v4.23.4...v4.24.0)

> [!NOTE]
> `4.24.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.24.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.24.0).

- Replace `ubuntu-24.04` runner by `ubuntu-slim` in all jobs not relying on Docker, and in Nuitka build matrix.
- Bump `actionlint` to `v1.7.9`.
- Ignore GitHub links pointing to stable release assets when checking for broken links.

## [`4.23.4` (2025-11-19)](https://github.com/kdeldycke/repomatic/compare/v4.23.3...v4.23.4)

> [!NOTE]
> `4.23.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.23.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.23.4).

- Force use of latest Python 3.14 for docs generation.

## [`4.23.3` (2025-11-18)](https://github.com/kdeldycke/repomatic/compare/v4.23.2...v4.23.3)

> [!NOTE]
> `4.23.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.23.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.23.3).

- Fix usage against last Click Extra release.
- Run tests on Python `3.14t` and `3.15t` free-threaded variants.

## [`4.23.2` (2025-11-17)](https://github.com/kdeldycke/repomatic/compare/v4.23.1...v4.23.2)

> [!NOTE]
> `4.23.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.23.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.23.2).

- Fix lychee runs in `lint` workflow.

## [`4.23.1` (2025-11-02)](https://github.com/kdeldycke/repomatic/compare/v4.23.0...v4.23.1)

> [!NOTE]
> `4.23.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.23.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.23.1).

- Fix some incompatibilities between `mdformat` plugins.

## [`4.23.0` (2025-10-25)](https://github.com/kdeldycke/repomatic/compare/v4.22.0...v4.23.0)

> [!NOTE]
> `4.23.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.23.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.23.0).

- Remove maximum capped version of all dependencies (relax all `~=` specifiers to `>=`). This gives more freedom to downstream and upstream packagers. Document each minimal version choice.
- Add new `--command` parameter to `gha-utils test-plan` command as an alias to `--binary`.
- Allow `gha-utils test-plan` to accept a full command line with parameters as input for `--command`/`--binary` option.
- Self-check of `gha-utils test-plan` command in its own test plan.
- Dynamiccaly deepen shallow clones of Git repositories when fetching new commit ranges.
- Only runs `optimize-images` job if there are image files in the repository.
- Move runner architecture validation to `gha-utils`-only job.
- Remove dependency on `mdformat_tables` plugin which has been merged into `mdformat-gfm`.
- Use un-released versions of `mdformat` plugins until their compatibility is restored.
- Move all typing-related imports behind a hard-coded `TYPE_CHECKING` guard to avoid runtime imports.
- Fix builds on `macos-26`.
- Skip tests on intermediate Python versions (`3.12` and `3.13`) to reduce CI load.

## [`4.22.0` (2025-10-12)](https://github.com/kdeldycke/repomatic/compare/v4.21.0...v4.22.0)

> [!NOTE]
> `4.22.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.22.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.22.0).

- Add new `image_files` field to `gha-utils metadata`.
- Only runs `lint-yaml` job if there are YAML files in the repository.
- Only runs `lint-github-action` job if there are workflow files in the repository.
- Only runs `broken-links` job if there are Markdown or rST files in the repository.
- Only runs `update-mailmap` job if `.mailmap` file exists.
- The `gha-utils test-plan` command now reports the detailed line differences when a `*_regex_fullmatch` check fails.
- Fix `commit_range` field when there is only one commit in the range.
- Flag `macos-26` as unstable target by default for Nuitka builds while we wait for a solution upstream.
- Upload Nuitka crash report as artifacts when the build fails.
- Validate architecture of binaries produced by Nuitka builds.

## [`4.21.0` (2025-10-11)](https://github.com/kdeldycke/repomatic/compare/v4.20.0...v4.21.0)

> [!NOTE]
> `4.21.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.21.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.21.0).

- Use `astral-sh/setup-uv` action to install `uv` instead of manually installing it with `pip`.
- Remove `requirements/uv.txt` file.
- Add new fields to `gha-utils metadata`:
  - `yaml_files`
  - `workflow_files`
  - `mailmap_exists`
- Check that start and end commit of `commit_range` exist in the repository before trying to traverse commits with PyDriller.
- Add `check-runners` job to always verify the architecture of each runner used to compile binaries with Nuitka.
- Use `macos-28` runner instead of `macos-15` to build binaries for `arm64`.
- Use `macos-15-intel` runner instead of `macos-13` to build binaries for `x64`.
- Run tests on `macos-28` and `macos-15-intel` runners instead of `macos-15` and `macos-13`.
- Only parse `.gitignore` file once, when first needed, and cache the matching function.
- Run `gha-utils` commands without `--verbosity DEBUG` option in jobs to reduce noise.
- Silence overly verbose debug messages from `py-walk` logger.
- Run `debug` workflow on all platforms targeted by Nuitka builds.
- Only runs `debug` workflow manually, on demand.
- Pin version of `awesome-lint` to `v2.2.2`.
- Pin version of `actionlint` to `v1.7.7`.

## [`4.20.0` (2025-10-10)](https://github.com/kdeldycke/repomatic/compare/v4.19.1...v4.20.0)

> [!NOTE]
> `4.20.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.20.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.20.0).

- Add new fields to `gha-utils metadata`:
  - `is_bot` to detect if the current run is not triggered by a human.
  - `build_targets` to list all supported Nuitka build targets.
  - `markdown_files` to list all Markdown files in the repository.
  - `zsh_files` to list all Zsh files in the repository.
  - `json_files` to list all JSON files in the repository.
- Include `*.pyi`, `*.pyw`, `*.pyx` and `*.ipynb` files in `python_files` field.
- Include `*.mdown`, `*.mkdn`, `*.mdwn`, `*.mkd`, `*.mdtxt` and `*.mdtext` files in `doc_files` field.
- Replace `gitignore-parser` dependency by `py-walk` to fix patterns matching both files and directories.
- Add support for Python 3.14 syntax in blacken-docs.
- Rename `ghdelimiter_XXXXX` tags in GitHub action multiline text blocks to `GHA_DELIMITER_XXXXX` for better visibility.
- Check that `gha-utils` CLI can be run as a Python module and with `uv run` and `uvx`.
- Add official support of Python 3.14.
- Run tests on Python 3.15-dev.

## [`4.19.1` (2025-09-25)](https://github.com/kdeldycke/repomatic/compare/v4.19.0...v4.19.1)

> [!NOTE]
> `4.19.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.19.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.19.1).

- Bump to Click Extra 6.0.0.

## [`4.19.0` (2025-09-25)](https://github.com/kdeldycke/repomatic/compare/v4.18.1...v4.19.0)

> [!NOTE]
> `4.19.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.19.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.19.0).

- Check for URL fragments when checking links with Lychee.
- Fix compilation of `rfc3987_syntax` data file thanks to Nuitka `v2.7.14`.
- Remove local patch of `gitignore-parser`, rely on `v0.1.13` release instead.
- Add dependency on `mdformat-recover-urls` to fix URL encoding in Markdown files.
- Force installation of all dependencies before running Mypy in lint workflow to ensure all `typeshed-*` packages are present.
- Cap `click` to `8.2.x` series when installing `bump-my-version` to avoid incompatible API changes.
- Skip linting and sponsoring jobs on Dependabot PRs and `prepare-release` branch.

## [`4.18.1` (2025-08-18)](https://github.com/kdeldycke/repomatic/compare/v4.18.0...v4.18.1)

> [!NOTE]
> `4.18.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.18.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.18.1).

- Patch `gitignore-parser` locally to support Windows paths.

## [`4.18.0` (2025-08-17)](https://github.com/kdeldycke/repomatic/compare/v4.17.9...v4.18.0)

> [!NOTE]
> `4.18.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.18.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.18.0).

- Adds `--format json` option.
- Remove `--format plain` option.
- Returns file paths relative to the current directory whenever we can in `gha-utils metadata` output.
- Ignore files matching `.gitignore` rules in the `python_files` and `doc_files` fields of `gha-utils metadata` output.
- Force all VSCode JSON files to be formatted in `jsonc` dialect.
- Prevent overlapping matching of JSON files by different dialect linters.
- Share linter's file exclusion list between dialects.
- Bump hard-coded `eslint` and `eslint/json` packages to their latest versions.

## [`4.17.9` (2025-07-17)](https://github.com/kdeldycke/repomatic/compare/v4.17.8...v4.17.9)

> [!NOTE]
> `4.17.9` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.9/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.9).

- Bump `gha-utils`.

## [`4.17.8` (2025-07-17)](https://github.com/kdeldycke/repomatic/compare/v4.17.7...v4.17.8)

> [!NOTE]
> `4.17.8` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.8/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.8).

- Normalized and deduplicate file paths in `gha-utils metadata` output.
- Ignore non-existing files and broken symlinks in `gha-utils metadata` output.

## [`4.17.7` (2025-07-17)](https://github.com/kdeldycke/repomatic/compare/v4.17.6...v4.17.7)

> [!NOTE]
> `4.17.7` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.7/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.7).

- Replace `Superseded by #None` comment by `No more broken links` when closing issues in `broken-links` job.
- Run lychee with `--hidden`, `--suggest`, `--insecure`, `--include-fragments` and `--exclude-all-private` options.
- Hard-code lychee version and freeze it to `v0.19.1`.

## [`4.17.6` (2025-07-17)](https://github.com/kdeldycke/repomatic/compare/v4.17.5...v4.17.6)

> [!NOTE]
> `4.17.6` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.6/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.6).

- Use `uv`-provided ARM64 Python on `windows-11-arm` platform for Nuitka builds.
- Force use of latest `3.13` Python for all platforms for Nuitka builds.
- Fix quoting of file path in `python_files` and `doc_files` matrix fields.

## [`4.17.5` (2025-06-26)](https://github.com/kdeldycke/repomatic/compare/v4.17.4...v4.17.5)

> [!NOTE]
> `4.17.5` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.5/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.5).

- Bump `uv`.

## [`4.17.4` (2025-06-20)](https://github.com/kdeldycke/repomatic/compare/v4.17.3...v4.17.4)

> [!NOTE]
> `4.17.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.4).

- Remove hard-coded domains to skip when checking URLs. Use a `.lycheeignore` file instead.
- Fix auto-closing and updating of open broken links issues.

## [`4.17.3` (2025-06-08)](https://github.com/kdeldycke/repomatic/compare/v4.17.2...v4.17.3)

> [!NOTE]
> `4.17.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.3).

- Remove temporary `node_modules` subfolder when linting JSON files.
- Do not fail on Lychee finding bad URLs.

## [`4.17.2` (2025-06-08)](https://github.com/kdeldycke/repomatic/compare/v4.17.1...v4.17.2)

> [!NOTE]
> `4.17.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.2).

- Ignore `node_modules` subfolder when linting JSON files.
- Skip `Sci-Hub`, `x.com` and `archive.ph` when checking URLs because they restricts access to crawlers.
- Force uv to ignore managed Python on Windows ARM 64.

## [`4.17.1` (2025-05-27)](https://github.com/kdeldycke/repomatic/compare/v4.17.0...v4.17.1)

> [!NOTE]
> `4.17.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.1).

- Add new `unstable-targets` parameter to release workflow.
- Release binaries without the `-build` suffix in their names.

## [`4.17.0` (2025-05-27)](https://github.com/kdeldycke/repomatic/compare/v4.16.7...v4.17.0)

> [!NOTE]
> `4.17.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.17.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.17.0).

- Add a new `-u`/`--unstable-target` option to `metadata` command to allow some Nuitka builds to fail.
- Do not flag `windows-11-arm` as unstable by default for Nuitka builds.
- Refactor management of Nuitka build parameters.
- Remove `-build` suffix in binary names produced by Nuitka.

## [`4.16.7` (2025-05-24)](https://github.com/kdeldycke/repomatic/compare/v4.16.6...v4.16.7)

> [!NOTE]
> `4.16.7` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.7/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.7).

- Always allows `windows-11-arm` target to fails for Nuitka builds.

## [`4.16.6` (2025-05-24)](https://github.com/kdeldycke/repomatic/compare/v4.16.5...v4.16.6)

> [!NOTE]
> `4.16.6` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.6/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.6).

- Add a `state` parameter to the Nuitka build matrix and mark `windows-11-arm` as unstable target while we wait for `lxml` to work on it.

## [`4.16.5` (2025-05-19)](https://github.com/kdeldycke/repomatic/compare/v4.16.4...v4.16.5)

> [!NOTE]
> `4.16.5` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.5/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.5).

- Print binary metadata after compiling them with Nuitka.
- Fix production of arm64 binaries on Windows.

## [`4.16.4` (2025-05-18)](https://github.com/kdeldycke/repomatic/compare/v4.16.3...v4.16.4)

> [!NOTE]
> `4.16.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.4).

- Keep to the top the first comment in `.mailmap` files.

## [`4.16.3` (2025-05-13)](https://github.com/kdeldycke/repomatic/compare/v4.16.2...v4.16.3)

> [!NOTE]
> `4.16.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.3).

- Bump dependencies.

## [`4.16.2` (2025-04-28)](https://github.com/kdeldycke/repomatic/compare/v4.16.1...v4.16.2)

> [!NOTE]
> `4.16.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.2).

- Add a new `--show-trace-on-error`/`--hide-trace-on-error` option to `gha-utils test-plan` command to show execution trace of CLI on error.

## [`4.16.1` (2025-04-26)](https://github.com/kdeldycke/repomatic/compare/v4.16.0...v4.16.1)

> [!NOTE]
> `4.16.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.1).

- Use latest `gha-utils` CLI to build ARM64 binaries by default.

## [`4.16.0` (2025-04-26)](https://github.com/kdeldycke/repomatic/compare/v4.15.6...v4.16.0)

> [!NOTE]
> `4.16.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.16.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.16.0).

- Add a new `--exit-on-error`/`-x` option to `gha-utils test-plan` command to exit right away on the first failing test.
- Add a new `--select-test`/`-t` option to `gha-utils test-plan` command to run specific test cases.
- Rename short option for `--timeout` in `gha-utils test-plan` command from `-t` to `-T`.
- Add a new `--stats`/`--no-stats` option to `gha-utils test-plan` command to control display of statistics at the end of test execution.
- Use `windows-11-arm` to build Windows binaries for arm64 with Nuitka.
- Add `windows-11-arm` to the test matrix.
- Remove tests on `ubuntu-22.04-arm`, `ubuntu-22.04`, `windows-2022` and `windows-2019` to keep matrix small.

## [`4.15.6` (2025-04-20)](https://github.com/kdeldycke/repomatic/compare/v4.15.5...v4.15.6)

> [!NOTE]
> `4.15.6` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.6/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.6).

- Avoid `bump-my-version` `v1.1.1` due to [regression](https://github.com/callowayproject/bump-my-version/issues/331).

## [`4.15.5` (2025-03-13)](https://github.com/kdeldycke/repomatic/compare/v4.15.4...v4.15.5)

> [!NOTE]
> `4.15.5` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.5/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.5).

- Re-release to fix GitHub release notes.

## [`4.15.4` (2025-03-13)](https://github.com/kdeldycke/repomatic/compare/v4.15.3...v4.15.4)

> [!NOTE]
> `4.15.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.4).

- Fix fetching of released version and notes on release commits.

## [`4.15.3` (2025-03-12)](https://github.com/kdeldycke/repomatic/compare/v4.15.2...v4.15.3)

> [!NOTE]
> `4.15.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.3).

- Use latest `gha-utils` CLI to fix release notes in GitHub releases.

## [`4.15.2` (2025-03-12)](https://github.com/kdeldycke/repomatic/compare/v4.15.1...v4.15.2)

> [!NOTE]
> `4.15.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.2).

- Use uv-provided Python to compile binaries with Nuitka on Linux.
- Populate `current_version` and `release_notes` field in `gha-utils metadata` output for unreleased versions.

## [`4.15.1` (2025-03-10)](https://github.com/kdeldycke/repomatic/compare/v4.15.0...v4.15.1)

> [!NOTE]
> `4.15.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.1).

- Remove deprecated `--plan` option.
- Remove Nuitka script command extension workaround.
- Fix arguments normalization on Windows for CLI parameters in test plans.

## [`4.15.0` (2025-03-05)](https://github.com/kdeldycke/repomatic/compare/v4.14.2...v4.15.0)

> [!NOTE]
> `4.15.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.15.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.15.0).

- Add support for `only_platforms` and `skip_platforms` settings in test plans, to finely select platforms to run tests on.
- Add a `--skip-platform`/`-s` option to `gha-utils test-plan` to allow skipping of test plan on a whole set of platforms.
- Rename `--plan` option to `--plan-file`.
- Allow `--plan-file`/`-F` option to be used multiple times in `gha-utils test-plan` to merge multiple test plans.
- Add a new `--plan-envvar`/`-E` option to allow `gha-utils test-plan` command to read test plan from environment variables.
- Allow ad-hoc YAML `test-plan` to be passed as a input parameter in reused `release` workflow.
- Fix running of Nuitka-compiled `gha-utils metadata` command.
- Drop support for Python 3.10.
- Use `windows-2025` instead of `windows-2022` for Nuitka builds.
- Add `windows-2025` to the test matrix.

## [`4.14.2` (2025-02-19)](https://github.com/kdeldycke/repomatic/compare/v4.14.1...v4.14.2)

> [!NOTE]
> `4.14.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.14.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.14.2).

- Fix update of `.gitignore` with `gitignore-extra-content` content.
- Fix `--timeout` parameter on `gha-utils test-plan` call in release workflow.
- Move all high level CLI tests to test plan file.

## [`4.14.1` (2025-02-16)](https://github.com/kdeldycke/repomatic/compare/v4.14.0...v4.14.1)

> [!NOTE]
> `4.14.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.14.1/).

> [!WARNING]
> `4.14.1` is **not available** on 🐙 GitHub.

- Add a new `test-plan-file` parameter to the `release` workflow.
- Remove the `binaries-test-plan` parameter in `release` workflow.
- Allow for setting a specific `timeout` for each CLI test case.
- Allow `timeout` to be floats.
- Fix production of `nuitka_matrix` field in `gha-utils metadata` output.
- Add `junit.xml` file in default `.gitignore` extra directive.

## [`4.14.0` (2025-02-15)](https://github.com/kdeldycke/repomatic/compare/v4.13.4...v4.14.0)

> [!NOTE]
> `4.14.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.14.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.14.0).

- Add new `gha-utils test-plan` subcommand.
- Replace ad-hoc custom matrix code by generic matrix model.
- Replace test matrix pre-computation by native features.
- Remove `ruff_py_version` field from `gha-utils metadata` output: Ruff is extracting it automaticcaly from the `pyproject.toml` file of the project.
- Inline all forced Ruff configuration to CLI parameters.

## [`4.13.4` (2025-02-02)](https://github.com/kdeldycke/repomatic/compare/v4.13.3...v4.13.4)

> [!NOTE]
> `4.13.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.13.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.13.4).

- Fix uploads of Python packages to GitHub release when binaries are not produced.

## [`4.13.3` (2025-01-29)](https://github.com/kdeldycke/repomatic/compare/v4.13.2...v4.13.3)

> [!NOTE]
> `4.13.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.13.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.13.3).

- Fix uploads of Python packages to GitHub release when binaries are not produced.

## [`4.13.2` (2025-01-28)](https://github.com/kdeldycke/repomatic/compare/v4.13.1...v4.13.2)

> [!NOTE]
> `4.13.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.13.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.13.2).

- Fix permission for GitHub release publishing.

## [`4.13.1` (2025-01-28)](https://github.com/kdeldycke/repomatic/compare/v4.13.0...v4.13.1)

> [!NOTE]
> `4.13.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.13.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.13.1).

- Fix publishing of GitHub release if no binary artefacts have been produced.

## [`4.13.0` (2025-01-21)](https://github.com/kdeldycke/repomatic/compare/v4.12.0...v4.13.0)

> [!NOTE]
> `4.13.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.13.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.13.0).

- Generates attestion for Python packages and standalone binaries on release.

## [`4.12.0` (2025-01-20)](https://github.com/kdeldycke/repomatic/compare/v4.11.1...v4.12.0)

> [!NOTE]
> `4.12.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.12.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.12.0).

- Let `uv` choose the appropriate Python version depending on context.
- Remove dependency on `twine` and `check-wheel-contents`.

## [`4.11.1` (2025-01-18)](https://github.com/kdeldycke/repomatic/compare/v4.11.0...v4.11.1)

> [!NOTE]
> `4.11.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.11.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.11.1).

- Re-release to build Linux `arm64` binaries by default.

## [`4.11.0` (2025-01-18)](https://github.com/kdeldycke/repomatic/compare/v4.10.1...v4.11.0)

> [!NOTE]
> `4.11.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.11.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.11.0).

- Use `ubuntu-24.04-arm` to build Linux binaries for `arm64`.

## [`4.10.1` (2025-01-08)](https://github.com/kdeldycke/repomatic/compare/v4.10.0...v4.10.1)

> [!NOTE]
> `4.10.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.10.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.10.1).

- Re-release with latest `gha-utils`.

## [`4.10.0` (2025-01-08)](https://github.com/kdeldycke/repomatic/compare/v4.9.0...v4.10.0)

> [!NOTE]
> `4.10.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.10.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.10.0).

- Replace unmaintained `jsonlint` by ESLint.
- Add new `gitignore_exists` metadata output.
- Add `node` artefacts to the list of default files in `.gitignore`.

## [`4.9.0` (2024-12-27)](https://github.com/kdeldycke/repomatic/compare/v4.8.4...v4.9.0)

> [!NOTE]
> `4.9.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.9.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.9.0).

- Use `uv` instead of `setup-python` action to install Python. On all platforms but `windows-2019`.
- Remove auto-generated dummy `pyproject.toml` used to hack `setup-python` caching.
- Run all jobs on Python 3.13.
- Move coverage configuration to pytest invocation.
- Do not let `uv sync` operation update the `uv.lock` file.
- Depends on released version of `mdformat_deflist`.

## [`4.8.4` (2024-11-22)](https://github.com/kdeldycke/repomatic/compare/v4.8.3...v4.8.4)

> [!NOTE]
> `4.8.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.8.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.8.4).

- Run binaries tests into a shell subprocess.

## [`4.8.3` (2024-11-21)](https://github.com/kdeldycke/repomatic/compare/v4.8.2...v4.8.3)

> [!NOTE]
> `4.8.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.8.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.8.3).

- Fix parsing of default timeout.
- Do not force encoding when running CLI in binary test job.

## [`4.8.2` (2024-11-20)](https://github.com/kdeldycke/repomatic/compare/v4.8.1...v4.8.2)

> [!NOTE]
> `4.8.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.8.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.8.2).

- Add a `timeout` parameter to release workflow test execution.

## [`4.8.1` (2024-11-19)](https://github.com/kdeldycke/repomatic/compare/v4.8.0...v4.8.1)

> [!NOTE]
> `4.8.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.8.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.8.1).

- Fix permissions for tagging in release workflow.

## [`4.8.0` (2024-11-19)](https://github.com/kdeldycke/repomatic/compare/v4.7.2...v4.8.0)

> [!NOTE]
> `4.8.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.8.0).

> [!WARNING]
> `4.8.0` is **not available** on 🐍 PyPI.

- Run Nuitka binary builds on Python 3.13.
- Run a series of test calls on the binaries produced by the build job.
- Replace unmaintained `hub` CLI by `gh` in broken links job.

## [`4.7.2` (2024-11-10)](https://github.com/kdeldycke/repomatic/compare/v4.7.1...v4.7.2)

> [!NOTE]
> `4.7.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.7.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.7.2).

- Fix installation of `hub` on broken links job.

## [`4.7.1` (2024-11-03)](https://github.com/kdeldycke/repomatic/compare/v4.7.0...v4.7.1)

> [!NOTE]
> `4.7.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.7.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.7.1).

- Fix upload to PyPI on release.
- Remove unused `uv_requirement_params` in metadata.

## [`4.7.0` (2024-11-03)](https://github.com/kdeldycke/repomatic/compare/v4.6.1...v4.7.0)

> [!NOTE]
> `4.7.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.7.0).

> [!WARNING]
> `4.7.0` is **not available** on 🐍 PyPI.

- Remove `extra_python_params` variant in `nuitka_matrix` metadata.
- Add official support of Python 3.13.
- Drop support for Python 3.9.
- Use `macos-15` instead of `macos-14` to build binaries for `arm64`.
- Use `ubuntu-24.04` instead of `ubuntu-22.04` to built binaries for Linux.
- Run tests on Python 3.14-dev.

## [`4.6.1` (2024-09-26)](https://github.com/kdeldycke/repomatic/compare/v4.6.0...v4.6.1)

> [!NOTE]
> `4.6.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.6.1).

> [!WARNING]
> `4.6.1` is **not available** on 🐍 PyPI.

- Use `uv` to publish Python packages.

## [`4.6.0` (2024-09-20)](https://github.com/kdeldycke/repomatic/compare/v4.5.4...v4.6.0)

> [!NOTE]
> `4.6.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.6.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.6.0).

- Use `uv` to build Python packages.
- Remove dependency on `build` package.
- Fix coverage report upload.
- Upload test results to coverage.

## [`4.5.4` (2024-09-04)](https://github.com/kdeldycke/repomatic/compare/v4.5.3...v4.5.4)

> [!NOTE]
> `4.5.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.5.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.5.4).

- Rerelease to stabilize changelog updates.

## [`4.5.3` (2024-09-04)](https://github.com/kdeldycke/repomatic/compare/v4.5.2...v4.5.3)

> [!NOTE]
> `4.5.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.5.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.5.3).

- Fix changelog indention.
- Add changelog unittests.

## [`4.5.2` (2024-08-26)](https://github.com/kdeldycke/repomatic/compare/v4.5.1...v4.5.2)

> [!NOTE]
> `4.5.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.5.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.5.2).

- Rerelease to fix admonition in changelog.
- Fix changelog new entry format.

## [`4.5.1` (2024-08-25)](https://github.com/kdeldycke/repomatic/compare/v4.5.0...v4.5.1)

> [!NOTE]
> `4.5.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.5.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.5.1).

- Fix over-escaping of `[!IMPORTANT]` admonition in changelog.
- Fix content writing into output files.

## [`4.5.0` (2024-08-24)](https://github.com/kdeldycke/repomatic/compare/v4.4.5...v4.5.0)

> [!NOTE]
> `4.5.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.5.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.5.0).

- Replace `mdformat-black` by `mdformat-ruff`.
- Install `mdformat`, `gha-utils`, `yamllint`, `bump-my-version`, `ruff`, `blacken-docs` and `autopep8` as a global tool to not interfere with the project dependencies.
- Fix `mdformat-pelican` compatibility with `mdformat-gfm`.
- Upgrade job runs from `ubuntu-22.04` to `ubuntu-24.04`.
- Mark python 3.13-dev tests as stable.
- Fix empty entry composition.
- Remove local workaround for Nuitka.

## [`4.4.5` (2024-08-18)](https://github.com/kdeldycke/repomatic/compare/v4.4.4...v4.4.5)

> [!NOTE]
> `4.4.5` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.4.5/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.4.5).

- Bump `gha-utils` CLI.

## [`4.4.4` (2024-08-18)](https://github.com/kdeldycke/repomatic/compare/v4.4.3...v4.4.4)

> [!NOTE]
> `4.4.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.4.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.4.4).

- Fix update of changelog without past entries.

## [`4.4.3` (2024-08-12)](https://github.com/kdeldycke/repomatic/compare/v4.4.2...v4.4.3)

> [!NOTE]
> `4.4.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.4.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.4.3).

- Release with relaxed dependencies.

## [`4.4.2` (2024-08-02)](https://github.com/kdeldycke/repomatic/compare/v4.4.1...v4.4.2)

> [!NOTE]
> `4.4.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.4.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.4.2).

- Add local workaround for Nuitka to fix bad packaging of `license_expression` package at build time.

## [`4.4.1` (2024-08-01)](https://github.com/kdeldycke/repomatic/compare/v4.4.0...v4.4.1)

> [!NOTE]
> `4.4.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.4.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.4.1).

- Bump Nuitka and `uv`.

## [`4.4.0` (2024-07-27)](https://github.com/kdeldycke/repomatic/compare/v4.3.4...v4.4.0)

> [!NOTE]
> `4.4.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.4.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.4.0).

- Drop support for Python 3.8.
- Rely on released version of `mdformat-pelican`.
- Fix invocation of installed `mdformat` and its plugin.

## [`4.3.4` (2024-07-24)](https://github.com/kdeldycke/repomatic/compare/v4.3.3...v4.3.4)

> [!NOTE]
> `4.3.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.3.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.3.4).

- Do not maintain `.mailmap` files on Awesome repositories.

## [`4.3.3` (2024-07-24)](https://github.com/kdeldycke/repomatic/compare/v4.3.2...v4.3.3)

> [!NOTE]
> `4.3.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.3.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.3.3).

- Bump `uv` and Nuitka.

## [`4.3.2` (2024-07-22)](https://github.com/kdeldycke/repomatic/compare/v4.3.1...v4.3.2)

> [!NOTE]
> `4.3.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.3.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.3.2).

- Always use frozen `uv.lock` file on `uv run` invocation.

## [`4.3.1` (2024-07-18)](https://github.com/kdeldycke/repomatic/compare/v4.3.0...v4.3.1)

> [!NOTE]
> `4.3.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.3.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.3.1).

- Do not print progress bars on `uv` calls.

## [`4.3.0` (2024-07-17)](https://github.com/kdeldycke/repomatic/compare/v4.2.1...v4.3.0)

> [!NOTE]
> `4.3.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.3.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.3.0).

- Add a new job to keep `uv.lock` updated and in sync.
- Exclude auto-updated `uv.lock` files from PRs produced from `uv run` and `uv tool run` invocations.

## [`4.2.1` (2024-07-15)](https://github.com/kdeldycke/repomatic/compare/v4.2.0...v4.2.1)

> [!NOTE]
> `4.2.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.2.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.2.1).

- Fix options in `gha-utils mailmap-sync` calls.
- Use latest `gha-utils` release in workflows.

## [`4.2.0` (2024-07-15)](https://github.com/kdeldycke/repomatic/compare/v4.1.4...v4.2.0)

> [!NOTE]
> `4.2.0` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.2.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.2.0).

- Rename `gha-utils mailmap` command to `gha-utils mailmap-sync`.
- Add new `--create-if-missing`/`--skip-if-missing` option to `gha-utils mailmap-sync` command.
- Do not create `.mailmap` from scratch in workflows: only update existing ones.
- Normalize, deduplicate and sort identities in `.mailmap` files.
- Keep comments attached to their mapping when re-sorting `.mailmap` files.
- Do not duplicate header metadata on `.mailmap` updates.
- Do not update `.mailmap` files if no changes are detected.
- Add new `boltons` dependency.

## [`4.1.4` (2024-07-02)](https://github.com/kdeldycke/repomatic/compare/v4.1.3...v4.1.4)

> [!NOTE]
> `4.1.4` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.1.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.1.4).

- Bump `gha-utils` CLI.

## [`4.1.3` (2024-07-02)](https://github.com/kdeldycke/repomatic/compare/v4.1.2...v4.1.3)

> [!NOTE]
> `4.1.3` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.1.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.1.3).

- Fix recreation of specifiers.

## [`4.1.2` (2024-07-02)](https://github.com/kdeldycke/repomatic/compare/v4.1.1...v4.1.2)

> [!NOTE]
> `4.1.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.1.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.1.2).

- Revert to rely entirely on released `gha-utils` CLI for release workflow.

## [`4.1.1` (2024-07-02)](https://github.com/kdeldycke/repomatic/compare/v4.1.0...v4.1.1)

> [!NOTE]
> `4.1.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.1.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.1.1).

- Pre-compute repository initial state before digging into commit log history.
- Redo release as `v4.1.0` has been broken.
- Rely on old `v4.0.2` standalone metadata script temporarily to fix release process.
- Remove failing `--statistics` production on `ruff` invocation.

## [`4.1.0` (2024-07-01)](https://github.com/kdeldycke/repomatic/compare/v4.0.2...v4.1.0)

> [!WARNING]
> `4.1.0` is **not available** on 🐍 PyPI and 🐙 GitHub.

- Replace in-place `metadata.py`, `update_changelog.py` and `update_mailmap.py` scripts by `gha-utils` CLI.
- Remove pre-workflow `check-mailmap` job.
- Bump Python minimal requirement to 3.8.6.
- Fix computation of lower bound Python version support if minimal requirement is not contained to `major.minor` specifier.
- Add dependency on `backports.strenum` for `Python < 3.11`.
- Change dependency on `mdformat-pelican` from personal fork to unreleased upstream.
- Remove dependency on `black` and `mypy`.

## [`4.0.2` (2024-06-29)](https://github.com/kdeldycke/repomatic/compare/v4.0.1...v4.0.2)

> [!NOTE]
> `4.0.2` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.0.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.0.2).

- Remove comments in GitHub action's environment variable files.
- Test CLI invocation.

## [`4.0.1` (2024-06-29)](https://github.com/kdeldycke/repomatic/compare/v4.0.0...v4.0.1)

> [!NOTE]
> `4.0.1` is available on [🐍 PyPI](https://pypi.org/project/gha-utils/4.0.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.0.1).

- Re-release to register PyPI project.

## [`4.0.0` (2024-06-29)](https://github.com/kdeldycke/repomatic/compare/v3.5.11...v4.0.0)

> [!NOTE]
> `4.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v4.0.0).

- Package all utilities in a `gha_utils` CLI.
- Remove support for Poetry-based projects. All Python projects are expected to follow [standard `pyproject.toml` conventions](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/).
- Sort contributors in `.mailmap` files regardless of case sensitivity.
- Force default values of workflow's inputs when triggered from other events (i.e. in non-reusable contexts).
- Run all Python-based commands via `uv run` and `uv tool run`.
- Replace `is_poetry_project` metadata by `is_python_project`.
- Add new `uv_requirement_params` metadata output.
- Remove dependency on `poetry` package.
- Add new dependencies on `build`, `packaging`, `pyproject-metadata` and `click-extra`.

## [`3.5.11` (2024-06-22)](https://github.com/kdeldycke/repomatic/compare/v3.5.10...v3.5.11)

> [!NOTE]
> `3.5.11` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.11).

- Read `pyproject.toml` without relying on Poetry.

## [`3.5.10` (2024-06-20)](https://github.com/kdeldycke/repomatic/compare/v3.5.9...v3.5.10)

> [!NOTE]
> `3.5.10` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.10).

- Replace Myst admonition in changelog by GFM alerts.

## [`3.5.9` (2024-06-20)](https://github.com/kdeldycke/repomatic/compare/v3.5.8...v3.5.9)

> [!NOTE]
> `3.5.9` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.9).

- Restrict removal of changelog warning admonition to `{important}` class on version bump.

## [`3.5.8` (2024-06-20)](https://github.com/kdeldycke/repomatic/compare/v3.5.7...v3.5.8)

> [!NOTE]
> `3.5.8` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.8).

- Fix dependency graph generation by replacing Poetry by `uv`.

## [`3.5.7` (2024-06-05)](https://github.com/kdeldycke/repomatic/compare/v3.5.6...v3.5.7)

> [!NOTE]
> `3.5.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.7).

- Use `uv` to install and run tools.
- Fix markdown autofix.

## [`3.5.6` (2024-06-05)](https://github.com/kdeldycke/repomatic/compare/v3.5.5...v3.5.6)

> [!NOTE]
> `3.5.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.6).

- Use `uv` to install `mdformat`.

## [`3.5.5` (2024-06-05)](https://github.com/kdeldycke/repomatic/compare/v3.5.4...v3.5.5)

> [!NOTE]
> `3.5.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.5).

- Run Nuitka builds on Python 3.12.
- Auto cleanup PRs produced by awesome template sync job.

## [`3.5.4` (2024-05-23)](https://github.com/kdeldycke/repomatic/compare/v3.5.3...v3.5.4)

> [!NOTE]
> `3.5.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.4).

- Fix `mypy` run for Poetry projects.

## [`3.5.3` (2024-05-23)](https://github.com/kdeldycke/repomatic/compare/v3.5.2...v3.5.3)

> [!NOTE]
> `3.5.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.3).

- Pin `uv` version everywhere to improve stability.
- Fix `mypy` execution and dependency installation.

## [`3.5.2` (2024-05-22)](https://github.com/kdeldycke/repomatic/compare/v3.5.1...v3.5.2)

> [!NOTE]
> `3.5.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.2).

- Install all extra dependencies before checking typing with `mypy`.

## [`3.5.1` (2024-05-22)](https://github.com/kdeldycke/repomatic/compare/v3.5.0...v3.5.1)

> [!NOTE]
> `3.5.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.1).

- Requires typing dependencies to be set in a `typing` group in `pyproject.toml`.
- Install all extra dependencies on doc generation.

## [`3.5.0` (2024-05-22)](https://github.com/kdeldycke/repomatic/compare/v3.4.7...v3.5.0)

> [!NOTE]
> `3.5.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.5.0).

- Requires Sphinx dependencies to be set in a `docs` group in `pyproject.toml`.
- Let `pipdeptree` resolve the Python executable to use in a virtual environment.
- Do not let Nuitka assume a Python package is bundled with its unittests in a `tests` subfolder.
- Reduce number of `git` calls to produce `.mailmap`. Refs #984.

## [`3.4.7` (2024-04-26)](https://github.com/kdeldycke/repomatic/compare/v3.4.6...v3.4.7)

> [!NOTE]
> `3.4.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.7).

- Update dependencies.

## [`3.4.6` (2024-04-18)](https://github.com/kdeldycke/repomatic/compare/v3.4.5...v3.4.6)

> [!NOTE]
> `3.4.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.6).

- Dynamically search the Python executable used by Poetry.

## [`3.4.5` (2024-04-18)](https://github.com/kdeldycke/repomatic/compare/v3.4.4...v3.4.5)

> [!NOTE]
> `3.4.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.5).

- Support dependency graph generation for both package and non-package Poetry projects.
- Provides venv's Python to `pipdeptree` to bypass non-detection of active venv.

## [`3.4.4` (2024-04-17)](https://github.com/kdeldycke/repomatic/compare/v3.4.3...v3.4.4)

> [!NOTE]
> `3.4.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.4).

- Name is optional for non-`package-mode` Poetry projects.

## [`3.4.3` (2024-04-14)](https://github.com/kdeldycke/repomatic/compare/v3.4.2...v3.4.3)

> [!NOTE]
> `3.4.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.3).

- Fix incompatibility between `mdformat-gfm` and `mdformat-pelican`.

## [`3.4.2` (2024-04-04)](https://github.com/kdeldycke/repomatic/compare/v3.4.1...v3.4.2)

> [!NOTE]
> `3.4.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.2).

- Fix template URL in `awesome-template-sync` job PR body.

## [`3.4.1` (2024-03-19)](https://github.com/kdeldycke/repomatic/compare/v3.4.0...v3.4.1)

> [!NOTE]
> `3.4.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.1).

- Fix variable substitution in `awesome-template-sync` job PR body.

## [`3.4.0` (2024-03-18)](https://github.com/kdeldycke/repomatic/compare/v3.3.6...v3.4.0)

> [!NOTE]
> `3.4.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.4.0).

- Support GitHub admonition in Markdown linting.
- Add new dependency on `mdformat_gfm_alerts`.
- Use pre-commit hooks in `awesome-template-sync` job to replace URLs.
- Source remote requirement files from `uv` CLI.

## [`3.3.6` (2024-03-04)](https://github.com/kdeldycke/repomatic/compare/v3.3.5...v3.3.6)

> [!NOTE]
> `3.3.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.6).

- Fix `awesome-template-sync` job.

## [`3.3.5` (2024-03-03)](https://github.com/kdeldycke/repomatic/compare/v3.3.4...v3.3.5)

> [!NOTE]
> `3.3.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.5).

- Set `ignore_missing_files` option globally in `pyproject.toml`.
- Remove temporary hack to make `uv` use system Python in workflows.

## [`3.3.4` (2024-03-01)](https://github.com/kdeldycke/repomatic/compare/v3.3.3...v3.3.4)

> [!NOTE]
> `3.3.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.4).

- Add some debug messages.

## [`3.3.3` (2024-03-01)](https://github.com/kdeldycke/repomatic/compare/v3.3.2...v3.3.3)

> [!NOTE]
> `3.3.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.3).

- Fix updating of existing PR from `awesome-template-sync`.

## [`3.3.2` (2024-03-01)](https://github.com/kdeldycke/repomatic/compare/v3.3.1...v3.3.2)

> [!NOTE]
> `3.3.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.2).

- Fix fetching of newly created PR in `awesome-template-sync`.

## [`3.3.1` (2024-03-01)](https://github.com/kdeldycke/repomatic/compare/v3.3.0...v3.3.1)

> [!NOTE]
> `3.3.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.1).

- Update repository URLs in `awesome-template-sync` job before re committing the PR.

## [`3.3.0` (2024-02-26)](https://github.com/kdeldycke/repomatic/compare/v3.2.4...v3.3.0)

> [!NOTE]
> `3.3.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.3.0).

- Start collecting `bump-my-version` rules from different projects.
- Move all `*-requirements.txt` files to `requirements` subfolder.
- Remove generation of Pip's `--requirement` parameters in metadata script.
- Reuse `requirements.txt` root file to install dependencies in `mypy-lint` job.
- Add emoji label to awesome template sync PR.

## [`3.2.4` (2024-02-24)](https://github.com/kdeldycke/repomatic/compare/v3.2.3...v3.2.4)

> [!NOTE]
> `3.2.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.2.4).

- Remove labels in `awesome-template-sync` job while we wait for upstream fix.

## [`3.2.3` (2024-02-24)](https://github.com/kdeldycke/repomatic/compare/v3.2.2...v3.2.3)

> [!NOTE]
> `3.2.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.2.3).

- Try to hack `actions-template-sync` labels, again.

## [`3.2.2` (2024-02-24)](https://github.com/kdeldycke/repomatic/compare/v3.2.1...v3.2.2)

> [!NOTE]
> `3.2.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.2.2).

- Try to hack `actions-template-sync` labels.

## [`3.2.1` (2024-02-24)](https://github.com/kdeldycke/repomatic/compare/v3.2.0...v3.2.1)

> [!NOTE]
> `3.2.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.2.1).

- Add label to awesome template sync PR.

## [`3.2.0` (2024-02-24)](https://github.com/kdeldycke/repomatic/compare/v3.1.0...v3.2.0)

> [!NOTE]
> `3.2.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.2.0).

- Add a job to sync awesome repository project from the `awesome-template` repository.

## [`3.1.0` (2024-02-18)](https://github.com/kdeldycke/repomatic/compare/v3.0.0...v3.1.0)

> [!NOTE]
> `3.1.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.1.0).

- Produce `arm64` binaries with Nuitka by using `macos-14` runners.

## [`3.0.0` (2024-02-17)](https://github.com/kdeldycke/repomatic/compare/v2.26.6...v3.0.0)

> [!NOTE]
> `3.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v3.0.0).

- Start replacing `pip` invocations by `uv`.
- Split Python dependencies into several `*requirements.txt` files.
- Let metadata script generates Pip's `--requirement` parameters.
- Add new dependency on `wcmatch` and `uv`.
- Ignore all files from local `.venv/` subfolder.
- Tie Pip cache to `**/pyproject.toml` and `**/*requirements.txt` files.
- Lint and format Jupyter notebooks with ruff.
- Update default ruff config file to new `0.2.x` series.
- Remove installation of unused `bump-my-version` in Git tagging job.
- Document setup and rationale of custom PAT and `*requirements.txt` files.

## [`2.26.6` (2024-01-31)](https://github.com/kdeldycke/repomatic/compare/v2.26.5...v2.26.6)

> [!NOTE]
> `2.26.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.6).

- Remove temporary `pyproject.toml` dummy file after ruff invocation not to let it fail due to missing reference.

## [`2.26.5` (2024-01-31)](https://github.com/kdeldycke/repomatic/compare/v2.26.4...v2.26.5)

> [!NOTE]
> `2.26.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.5).

- Generate dummy `pyproject.toml` instead of `requirements.txt` everywhere to bypass `setup-python` cache limits for non-Python repositories. Remove the temporary `pyproject.toml` dummy after the fact.

## [`2.26.4` (2024-01-30)](https://github.com/kdeldycke/repomatic/compare/v2.26.3...v2.26.4)

> [!NOTE]
> `2.26.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.4).

- Generate a dummy `pyproject.toml` instead of `requirements.txt` to make our ruff local conf work.

## [`2.26.3` (2024-01-30)](https://github.com/kdeldycke/repomatic/compare/v2.26.2...v2.26.3)

> [!NOTE]
> `2.26.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.3).

- Fix Python job on non-Python repositories.

## [`2.26.2` (2024-01-30)](https://github.com/kdeldycke/repomatic/compare/v2.26.1...v2.26.2)

> [!NOTE]
> `2.26.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.2).

- Fix absence of version in non-Python repositories.

## [`2.26.1` (2024-01-30)](https://github.com/kdeldycke/repomatic/compare/v2.26.0...v2.26.1)

> [!NOTE]
> `2.26.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.1).

- Add workaround to allow caching on non-Python repositories.
- Remove hard-coded commit version for `mdformat-gfm`.

## [`2.26.0` (2024-01-17)](https://github.com/kdeldycke/repomatic/compare/v2.25.0...v2.26.0)

> [!NOTE]
> `2.26.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.26.0).

- Replace unmaintained `misspell-fixer` by `typos` to autofix typos. Closes #650.

## [`2.25.0` (2024-01-17)](https://github.com/kdeldycke/repomatic/compare/v2.24.3...v2.25.0)

> [!NOTE]
> `2.25.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.25.0).

- Add a content-based labeller job for issues and PRs.

## [`2.24.3` (2024-01-16)](https://github.com/kdeldycke/repomatic/compare/v2.24.2...v2.24.3)

> [!NOTE]
> `2.24.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.24.3).

- Use `macos-13` instead of `macos-12` for Nuitka builds.

## [`2.24.2` (2024-01-06)](https://github.com/kdeldycke/repomatic/compare/v2.24.1...v2.24.2)

> [!NOTE]
> `2.24.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.24.2).

- Use `bump-my-version` to remove admonition in changelog.

## [`2.24.1` (2024-01-06)](https://github.com/kdeldycke/repomatic/compare/v2.24.0...v2.24.1)

> [!NOTE]
> `2.24.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.24.1).

- Expose current and released version in metadata script.
- Fix fetching of changelog entry for release notes.

## [`2.24.0` (2024-01-06)](https://github.com/kdeldycke/repomatic/compare/v2.23.0...v2.24.0)

> [!NOTE]
> `2.24.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.24.0).

- Add latest changelog entries in GitHub release notes.

## [`2.23.0` (2024-01-05)](https://github.com/kdeldycke/repomatic/compare/v2.22.0...v2.23.0)

> [!NOTE]
> `2.23.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.23.0).

- Produce GitHub release notes dynamically.
- Augment all commits matrix with current version from `bump-my-version`.
- Use new artifact features and scripts.

## [`2.22.0` (2024-01-05)](https://github.com/kdeldycke/repomatic/compare/v2.21.0...v2.22.0)

> [!NOTE]
> `2.22.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.22.0).

- Update default file-based labelling rules for new configuration format.
- Run `autopep8` before `ruff`.

## [`2.21.0` (2024-01-04)](https://github.com/kdeldycke/repomatic/compare/v2.20.9...v2.21.0)

> [!NOTE]
> `2.21.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.21.0).

- Use `ruff` instead of `docformatter` to format docstrings inside Python files.
- Remove dependency on `docformatter`.
- Only run `ruff` once for autofix and linting. Removes `lint-python` job.
- Auto-generate local configuration for `ruff` instead of passing parameters.
- Split generation of Python target version from CLI parameters.
- Rename `black_params` metadata variable to `blacken_docs_params`.
- Remove `ruff_params` metadata variable.

## [`2.20.9` (2023-11-13)](https://github.com/kdeldycke/repomatic/compare/v2.20.8...v2.20.9)

> [!NOTE]
> `2.20.9` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.20.9).

- Do not cache dependency-less mailmap update workflow step.

## [`2.20.8` (2023-11-12)](https://github.com/kdeldycke/repomatic/compare/v2.20.7...v2.20.8)

> [!NOTE]
> `2.20.8` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.20.8).

- Cache Python setups.

## [`2.20.7` (2023-11-09)](https://github.com/kdeldycke/repomatic/compare/v2.20.6...v2.20.7)

> [!NOTE]
> `2.20.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.20.7).

- Run Nuitka builds on Python 3.11 while we wait for 3.12 support upstream.

## [`2.20.6` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.20.5...v2.20.6)

> [!NOTE]
> `2.20.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.20.6).

- Remove hard-coded permissions for release action.

## [`2.20.5` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.20.4...v2.20.5)

> [!NOTE]
> `2.20.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.20.5).

- Increase scope of hard-coded permissions for release action.
- Use custom token for GitHub release creation.

## [`2.20.4` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.20.3...v2.20.4)

> [!WARNING]
> `2.20.4` is **not available** on 🐙 GitHub.

- Increase token permissions to full write.

## [`2.20.3` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.20.2...v2.20.3)

> [!WARNING]
> `2.20.3` is **not available** on 🐙 GitHub.

- Test release action.

## [`2.20.2` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.20.1...v2.20.2)

> [!WARNING]
> `2.20.2` is **not available** on 🐙 GitHub.

- Increase scope of hard-coded token contents permission.

## [`2.20.1` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.20.0...v2.20.1)

> [!WARNING]
> `2.20.1` is **not available** on 🐙 GitHub.

- Hard-code token contents permission for creation of GitHub release.

## [`2.20.0` (2023-11-05)](https://github.com/kdeldycke/repomatic/compare/v2.19.1...v2.20.0)

> [!NOTE]
> `2.20.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.20.0).

- Upgrade to `bump-my-version` `0.12.x` series.
- Upgrade to Poetry `1.7.x` series.

## [`2.19.1` (2023-10-26)](https://github.com/kdeldycke/repomatic/compare/v2.19.0...v2.19.1)

> [!NOTE]
> `2.19.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.19.1).

- Activates `ruff` preview and unsafe rules.
- Run actions on Python 3.12.

## [`2.19.0` (2023-09-15)](https://github.com/kdeldycke/repomatic/compare/v2.18.0...v2.19.0)

> [!NOTE]
> `2.19.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.19.0).

- Replace `black` with `ruff`'s autoformatter.
- Rely even more on `bump-my-version` for string replacement.

## [`2.18.0` (2023-09-06)](https://github.com/kdeldycke/repomatic/compare/v2.17.8...v2.18.0)

> [!NOTE]
> `2.18.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.18.0).

- Upgrade to `bump-my-version` `0.10.x` series.
- Remove the step updating the release date of `citation.cff` in `changelog` job. This can be done with `bump-my-version` now.
- Trigger changelog updates on `requirements.txt` changes.

## [`2.17.8` (2023-07-16)](https://github.com/kdeldycke/repomatic/compare/v2.17.7...v2.17.8)

> [!NOTE]
> `2.17.8` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.8).

- Upgrade to `bump-my-version` `0.8.0`.

## [`2.17.7` (2023-07-12)](https://github.com/kdeldycke/repomatic/compare/v2.17.6...v2.17.7)

> [!NOTE]
> `2.17.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.7).

- Replace some Perl oneliners with `bump-my-version` invocation.

## [`2.17.6` (2023-07-06)](https://github.com/kdeldycke/repomatic/compare/v2.17.5...v2.17.6)

> [!NOTE]
> `2.17.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.6).

- Fix retrieval of tagged version in release workflow.

## [`2.17.5` (2023-07-01)](https://github.com/kdeldycke/repomatic/compare/v2.17.4...v2.17.5)

> [!NOTE]
> `2.17.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.5).

- Use bump-my-version `v0.6.0` to fetch current version.

## [`2.17.4` (2023-06-22)](https://github.com/kdeldycke/repomatic/compare/v2.17.3...v2.17.4)

> [!NOTE]
> `2.17.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.4).

- Use patched version of `mdformat-web` to fix formatting of HTML code in code blocks.

## [`2.17.3` (2023-06-14)](https://github.com/kdeldycke/repomatic/compare/v2.17.2...v2.17.3)

> [!NOTE]
> `2.17.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.3).

- Reactive maximum concurrency in `lychee`, but ignore checks on `twitter.com` and `ycombinator.com`.

## [`2.17.2` (2023-06-12)](https://github.com/kdeldycke/repomatic/compare/v2.17.1...v2.17.2)

> [!NOTE]
> `2.17.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.2).

- Limit `lychee` max concurrency and sacrifice performances, to prevent false positives.
- Do not triggers docs workflow on tagging. There is not enough metadata on these events to complete the workflow.
- Skip broken links check on release merge: the tag is created asynchronously which produce false positive reports.

## [`2.17.1` (2023-06-12)](https://github.com/kdeldycke/repomatic/compare/v2.17.0...v2.17.1)

> [!NOTE]
> `2.17.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.1).

- Fix parsing of `lychee` exit code.

## [`2.17.0` (2023-06-12)](https://github.com/kdeldycke/repomatic/compare/v2.16.2...v2.17.0)

> [!NOTE]
> `2.17.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.17.0).

- Check and report links with `lychee`. Closes [`#563`](https://github.com/kdeldycke/repomatic/issues/563).

## [`2.16.2` (2023-06-11)](https://github.com/kdeldycke/repomatic/compare/v2.16.1...v2.16.2)

> [!NOTE]
> `2.16.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.16.2).

- Use `mdformat_simple_breaks` plugin to format long `<hr>` rules.
- Format bash code blocks in Markdown via `mdformat-shfmt`.
- Install `shfmt` before calling `mdformat`.
- Add dependencies on `mdformat_deflist` and `mdformat_pelican`.

## [`2.16.1` (2023-06-10)](https://github.com/kdeldycke/repomatic/compare/v2.16.0...v2.16.1)

> [!NOTE]
> `2.16.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.16.1).

- Replace long `____(....)____` `<hr>` rule produced by `mdformat` with canonical `---` form. Refs [`hukkin/mdformat#328`](https://github.com/hukkin/mdformat/issues/328#issuecomment-1585775337).
- Apply Markdown fixes for awesome lists to localized versions.

## [`2.16.0` (2023-06-08)](https://github.com/kdeldycke/repomatic/compare/v2.15.2...v2.16.0)

> [!NOTE]
> `2.16.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.16.0).

- Replace `bump2version` with `bump-my-version`. Closes [`#162`](https://github.com/kdeldycke/repomatic/issues/162).
- Move version bumping configuration from `.bumpversion.cfg` to `pyproject.toml`.
- Cap `mdformat_admon == 1.0.1` to prevent `mdit-py-plugins >= 0.4.0` conflict.

## [`2.15.2` (2023-06-04)](https://github.com/kdeldycke/repomatic/compare/v2.15.1...v2.15.2)

> [!NOTE]
> `2.15.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.15.2).

- Upgrade Nuitka builds to Python 3.11.
- Remove `--no-ansi` option on Poetry calls.

## [`2.15.1` (2023-05-22)](https://github.com/kdeldycke/repomatic/compare/v2.15.0...v2.15.1)

> [!NOTE]
> `2.15.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.15.1).

- Force colorized output of Mypy, as in CI it defaults to no color.
- Only activates all `ruff` rules for autofix, not linting.
- Ignore `D400` rule in `ruff` to allow for docstrings first line finishing with a punctuation other than a period.

## [`2.15.0` (2023-05-06)](https://github.com/kdeldycke/repomatic/compare/v2.14.1...v2.15.0)

> [!NOTE]
> `2.15.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.15.0).

- Fix hard-coding of tagged external asset's URLs on release and version bump.
- Forces `ruff` to check and autofix against all rules.

## [`2.14.1` (2023-05-04)](https://github.com/kdeldycke/repomatic/compare/v2.14.0...v2.14.1)

> [!NOTE]
> `2.14.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.14.1).

- Reverts publishing via trusted channel: it doesn't work with reusable workflows. See #528.

## [`2.14.0` (2023-05-04)](https://github.com/kdeldycke/repomatic/compare/v2.13.5...v2.14.0)

> [!NOTE]
> `2.14.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.14.0).

- Publish packages to PyPI with OIDC workflow for trusted publishing.

## [`2.13.5` (2023-05-02)](https://github.com/kdeldycke/repomatic/compare/v2.13.4...v2.13.5)

> [!NOTE]
> `2.13.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.13.5).

- Update `docformatter`, `ruff` and `nuitka`.

## [`2.13.4` (2023-04-23)](https://github.com/kdeldycke/repomatic/compare/v2.13.3...v2.13.4)

> [!NOTE]
> `2.13.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.13.4).

- Use `docformatter 1.6.2`.

## [`2.13.3` (2023-04-22)](https://github.com/kdeldycke/repomatic/compare/v2.13.2...v2.13.3)

> [!NOTE]
> `2.13.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.13.3).

- Use `docformatter 1.6.1`.

## [`2.13.2` (2023-04-07)](https://github.com/kdeldycke/repomatic/compare/v2.13.1...v2.13.2)

> [!NOTE]
> `2.13.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.13.2).

- Various dependency updates.

## [`2.13.1` (2023-04-04)](https://github.com/kdeldycke/repomatic/compare/v2.13.0...v2.13.1)

> [!NOTE]
> `2.13.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.13.1).

- Use final version of `docformatter 1.6.0`.

## [`2.13.0` (2023-03-29)](https://github.com/kdeldycke/repomatic/compare/v2.12.4...v2.13.0)

> [!NOTE]
> `2.13.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.13.0).

- Update default destination folder of dependency graph from `images` to `assets`.

## [`2.12.4` (2023-03-29)](https://github.com/kdeldycke/repomatic/compare/v2.12.3...v2.12.4)

> [!NOTE]
> `2.12.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.12.4).

- Skip running `autopep8` if no Python files found.
- Only install main dependencies to generate dependency graph.

## [`2.12.3` (2023-03-27)](https://github.com/kdeldycke/repomatic/compare/v2.12.2...v2.12.3)

> [!NOTE]
> `2.12.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.12.3).

- Try out `docformatter 1.6.0-rc7`.

## [`2.12.2` (2023-03-07)](https://github.com/kdeldycke/repomatic/compare/v2.12.1...v2.12.2)

> [!NOTE]
> `2.12.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.12.2).

- Try out `docformatter 1.6.0-rc6`.

## [`2.12.1` (2023-03-05)](https://github.com/kdeldycke/repomatic/compare/v2.12.0...v2.12.1)

> [!NOTE]
> `2.12.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.12.1).

- Tweak extra content layout.

## [`2.12.0` (2023-03-05)](https://github.com/kdeldycke/repomatic/compare/v2.11.1...v2.12.0)

> [!NOTE]
> `2.12.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.12.0).

- Add new `gitignore-extra-content` parameter to `update-gitignore` job to append extra content to `.gitignore`.

## [`2.11.1` (2023-03-05)](https://github.com/kdeldycke/repomatic/compare/v2.11.0...v2.11.1)

> [!NOTE]
> `2.11.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.11.1).

- Fix Mermaid graph rendering colliding with reserved words.

## [`2.11.0` (2023-03-03)](https://github.com/kdeldycke/repomatic/compare/v2.10.0...v2.11.0)

> [!NOTE]
> `2.11.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.11.0).

- Add `certificates`, `gpg` and `ssh` artefacts to the list of default files in `.gitignore`.
- Fix production of dependency graph in Mermaid format.

## [`2.10.0` (2023-02-25)](https://github.com/kdeldycke/repomatic/compare/v2.9.0...v2.10.0)

> [!NOTE]
> `2.10.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.10.0).

- Lint GitHub Actions workflows with `actionlint`.

## [`2.9.0` (2023-02-18)](https://github.com/kdeldycke/repomatic/compare/v2.8.3...v2.9.0)

> [!NOTE]
> `2.9.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.9.0).

- Renders dependency graph in Mermaid Markdown instead of Graphviz's dot.
- Removes `dependency-graph-format` input variable to `docs.yaml` workflow.

## [`2.8.3` (2023-02-17)](https://github.com/kdeldycke/repomatic/compare/v2.8.2...v2.8.3)

> [!NOTE]
> `2.8.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.8.3).

- Test unreleased `docformatter 1.6.0-rc5` to fix link wrapping.
- Create missing parent folders of dependency graph.

## [`2.8.2` (2023-02-16)](https://github.com/kdeldycke/repomatic/compare/v2.8.1...v2.8.2)

> [!NOTE]
> `2.8.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.8.2).

- Fix subtle bug in `.gitignore` production due to collapsing multiline command block starting with `>` because of variable interpolation.
- Tweak PR titles.

## [`2.8.1` (2023-02-16)](https://github.com/kdeldycke/repomatic/compare/v2.8.0...v2.8.1)

> [!NOTE]
> `2.8.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.8.1).

- Test unreleased `docformatter 1.6.0-rc4` to fix admonition wrapping.

## [`2.8.0` (2023-02-14)](https://github.com/kdeldycke/repomatic/compare/v2.7.6...v2.8.0)

> [!NOTE]
> `2.8.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.8.0).

- Replace `isort`, `pyupgrade`, `pylint`, `pycln` and `pydocstyle` with `ruff`.
- Run `autopep8` before `black` to that longline edge-cases get wrapped first.
- Provides `autopep8` with explicit list of Python files to force it to handle dot-prefixed subdirectories.

## [`2.7.6` (2023-02-13)](https://github.com/kdeldycke/repomatic/compare/v2.7.5...v2.7.6)

> [!NOTE]
> `2.7.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.6).

- Test-drive unreleased `docformatter 1.6.0-rc3` to fix URL wrapping and admonition edge-cases.

## [`2.7.5` (2023-02-12)](https://github.com/kdeldycke/repomatic/compare/v2.7.4...v2.7.5)

> [!NOTE]
> `2.7.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.5).

- Fix collection of artifact files from their folder.

## [`2.7.4` (2023-02-12)](https://github.com/kdeldycke/repomatic/compare/v2.7.3...v2.7.4)

> [!NOTE]
> `2.7.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.4).

- Update artifact name to add `-poetry-` suffix for those to be published on PyPI.
- Fix collection of artifact files from their folder.

## [`2.7.3` (2023-02-12)](https://github.com/kdeldycke/repomatic/compare/v2.7.2...v2.7.3)

> [!NOTE]
> `2.7.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.3).

- Fix attachment of artifacts to GitHub release on tagging.

## [`2.7.2` (2023-02-12)](https://github.com/kdeldycke/repomatic/compare/v2.7.1...v2.7.2)

> [!NOTE]
> `2.7.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.2).

- Remove broken print debug statement.

## [`2.7.1` (2023-02-12)](https://github.com/kdeldycke/repomatic/compare/v2.7.0...v2.7.1)

> [!NOTE]
> `2.7.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.1).

- Fix attachment of artifacts to GitHub release on tagging.

## [`2.7.0` (2023-02-11)](https://github.com/kdeldycke/repomatic/compare/v2.6.2...v2.7.0)

> [!NOTE]
> `2.7.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.7.0).

- Add new dependency on `mdformat_footnote` to properly wrap long footnotes when autofixing Markdown.
- Add new dependency on `mdformat_admon` to future-proof upcoming admonition support.
- Add new dependency on `mdformat_pyproject` so that each project reusing the `autofix.yaml` workflow can setup local configuration for `mdformat` via its `pyproject.toml` file.

## [`2.6.2` (2023-02-11)](https://github.com/kdeldycke/repomatic/compare/v2.6.1...v2.6.2)

> [!NOTE]
> `2.6.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.6.2).

- Do not try to attach non-existing artifacts to GitHub release.

## [`2.6.1` (2023-02-11)](https://github.com/kdeldycke/repomatic/compare/v2.6.0...v2.6.1)

> [!WARNING]
> `2.6.1` is **not available** on 🐙 GitHub.

- Fix attachment of artifacts to GitHub release.

## [`2.6.0` (2023-02-10)](https://github.com/kdeldycke/repomatic/compare/v2.5.1...v2.6.0)

> [!NOTE]
> `2.6.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.6.0).

- Rename artifacts attached to each GitHub release to remove the build ID (i.e. the `-build-6f27db4` suffix). That way we can have stable download URLs pointing to the latest release in the form of: `https://github.com/<user_id>/<project_id>/releases/latest/download/<entry_point>-<platform>-<arch>.{bin,exe}`.
- Normalize binary file names produced by Nuitka with `-` (dash) separators.

## [`2.5.1` (2023-02-09)](https://github.com/kdeldycke/repomatic/compare/v2.5.0...v2.5.1)

> [!NOTE]
> `2.5.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.5.1).

- Remove Pip cache, which breaks with our reusable workflows architecture.

## [`2.5.0` (2023-02-09)](https://github.com/kdeldycke/repomatic/compare/v2.4.3...v2.5.0)

> [!NOTE]
> `2.5.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.5.0).

- Cache dependencies installed by Pip.

## [`2.4.3` (2023-01-31)](https://github.com/kdeldycke/repomatic/compare/v2.4.2...v2.4.3)

> [!NOTE]
> `2.4.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.4.3).

- Bump Nuitka to `1.4.1`.

## [`2.4.2` (2023-01-27)](https://github.com/kdeldycke/repomatic/compare/v2.4.1...v2.4.2)

> [!NOTE]
> `2.4.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.4.2).

- Export full Nuitka build matrix from release workflow.

## [`2.4.1` (2023-01-27)](https://github.com/kdeldycke/repomatic/compare/v2.4.0...v2.4.1)

> [!NOTE]
> `2.4.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.4.1).

- Reuse and align commit metadata.
- Fix module path provided to Nuitka.

## [`2.4.0` (2023-01-27)](https://github.com/kdeldycke/repomatic/compare/v2.3.7...v2.4.0)

> [!NOTE]
> `2.4.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.4.0).

- Pre-compute the whole Nuitka build matrix.
- Pre-compute matrix variations with long and short SHA values in commit lists.

## [`2.3.7` (2023-01-25)](https://github.com/kdeldycke/repomatic/compare/v2.3.6...v2.3.7)

> [!NOTE]
> `2.3.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.7).

- Change the order of Python auto-formatting pipeline to `pycln` > `isort` > `black` > `blacken-docs` > `autopep8` > `docformatter`.
- Target unreleased `docformatter 1.6.0-rc2` to fix admonition formatting.
- Ignore failing of `docformatter` as `1.6.x` series returns non-zero exit code if files needs to be reformatted.

## [`2.3.6` (2023-01-24)](https://github.com/kdeldycke/repomatic/compare/v2.3.5...v2.3.6)

> [!NOTE]
> `2.3.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.6).

- Reverts to skipping the full `1.5.x` series and `1.6.0rc1` of `docformatter` which struggle on long URLs and admonitions.

## [`2.3.5` (2023-01-24)](https://github.com/kdeldycke/repomatic/compare/v2.3.4...v2.3.5)

> [!NOTE]
> `2.3.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.5).

- Empty release.

## [`2.3.4` (2023-01-24)](https://github.com/kdeldycke/repomatic/compare/v2.3.3...v2.3.4)

> [!NOTE]
> `2.3.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.4).

- Target unreleased `docformatter 1.6.0.rc1` to fix long URL rewrapping. Closes [`#397`](https://github.com/kdeldycke/repomatic/pull/397).
- Remove thoroughly all unused imports.

## [`2.3.3` (2023-01-21)](https://github.com/kdeldycke/repomatic/compare/v2.3.2...v2.3.3)

> [!NOTE]
> `2.3.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.3).

- Update dependencies.

## [`2.3.2` (2023-01-16)](https://github.com/kdeldycke/repomatic/compare/v2.3.1...v2.3.2)

> [!NOTE]
> `2.3.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.2).

- Force refresh of `apt` before installing anything.

## [`2.3.1` (2023-01-13)](https://github.com/kdeldycke/repomatic/compare/v2.3.0...v2.3.1)

> [!NOTE]
> `2.3.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.1).

- Force refresh of `apt` before installing `graphviz`.

## [`2.3.0` (2023-01-10)](https://github.com/kdeldycke/repomatic/compare/v2.2.3...v2.3.0)

> [!NOTE]
> `2.3.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.3.0).

- Format python code blocks in documentation files with `blacken-docs`.
- Let metadata script locate Markdown, reStructuredText and Tex files under the `doc_files` field.
- Add new dependency on `blacken-docs`.
- Allow metadata script to be run on non-GitHub environment.

## [`2.2.3` (2023-01-09)](https://github.com/kdeldycke/repomatic/compare/v2.2.2...v2.2.3)

> [!NOTE]
> `2.2.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.2.3).

- Re-parse dependency graph to stabilize its output, customize its style and make it deterministic.
- Unpin dependency on `yamllint` and depends on latest version.

## [`2.2.2` (2023-01-09)](https://github.com/kdeldycke/repomatic/compare/v2.2.1...v2.2.2)

> [!NOTE]
> `2.2.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.2.2).

- Fix default dependency graph extension.

## [`2.2.1` (2023-01-09)](https://github.com/kdeldycke/repomatic/compare/v2.2.0...v2.2.1)

> [!NOTE]
> `2.2.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.2.1).

- Fix inplace customization of dependency graph.

## [`2.2.0` (2023-01-09)](https://github.com/kdeldycke/repomatic/compare/v2.1.1...v2.2.0)

> [!NOTE]
> `2.2.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.2.0).

- Change the default dependency graph format from `PNG` to `dot` file.
- Add a `dependency-graph-format` parameter to the documentation workflow.
- Customize the style of dependency graph when Graphviz code is produced.
- Install Graphviz when we produce the documentation so we can use `sphinx.ext.graphviz` plugin.
- Add list of projects relying on these scripts.

## [`2.1.1` (2022-12-30)](https://github.com/kdeldycke/repomatic/compare/v2.1.0...v2.1.1)

> [!WARNING]
> `2.1.1` is **not available** on 🐙 GitHub.

- Fix fetching of commit matrix.

## [`2.1.0` (2022-12-30)](https://github.com/kdeldycke/repomatic/compare/v2.0.6...v2.1.0)

> [!WARNING]
> `2.1.0` is **not available** on 🐙 GitHub.

- Rewrite new and release commit detection code from YAML to Python.
- Add dependency on `PyDriller`.
- Trigger debug traces on `pull_request` events.

## [`2.0.6` (2022-12-29)](https://github.com/kdeldycke/repomatic/compare/v2.0.5...v2.0.6)

> [!NOTE]
> `2.0.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.6).

- Fix export of binary name from build workflow.

## [`2.0.5` (2022-12-29)](https://github.com/kdeldycke/repomatic/compare/v2.0.4...v2.0.5)

> [!NOTE]
> `2.0.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.5).

- Export binary name from build workflow.

## [`2.0.4` (2022-12-27)](https://github.com/kdeldycke/repomatic/compare/v2.0.3...v2.0.4)

> [!NOTE]
> `2.0.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.4).

- Fix skipping of Nuitka compiling step for projects without entry points.
- Skip the whole `1.5.x` series of `docformatter` which struggles with long URLs.

## [`2.0.3` (2022-12-26)](https://github.com/kdeldycke/repomatic/compare/v2.0.2...v2.0.3)

> [!NOTE]
> `2.0.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.3).

- Fix fetching of absent entry points in project metadata.

## [`2.0.2` (2022-12-19)](https://github.com/kdeldycke/repomatic/compare/v2.0.1...v2.0.2)

> [!NOTE]
> `2.0.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.2).

- Fix uploading of artifacts to GitHub release on tagging.

## [`2.0.1` (2022-12-19)](https://github.com/kdeldycke/repomatic/compare/v2.0.0...v2.0.1)

> [!NOTE]
> `2.0.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.1).

- Use short SHA commit in build artifacts.
- Fix uploading of Nuitka binaries to GitHub release on tagging.

## [`2.0.0` (2022-12-17)](https://github.com/kdeldycke/repomatic/compare/v1.10.0...v2.0.0)

> [!NOTE]
> `2.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v2.0.0).

- Add Nuitka-based compiling of Poetry's script entry-points into standalone binaries for Linux, macOS and Windows.
- Upload binaries to GitHub releases on tagging.
- Extract Poetry script entry-points in Python metadata script.
- Produce Nuitka-specific main module path from script entry-points.
- Allow rendering of data structure in JSON for inter-job outputs.
- Print Python metadata output before writing to env for debugging.
- Add dependency on `nuitka`, `ordered-set` and `zstandard`.

## [`1.10.0` (2022-12-02)](https://github.com/kdeldycke/repomatic/compare/v1.9.2...v1.10.0)

> [!NOTE]
> `1.10.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.10.0).

- Run all Python-based workflows on 3.11.

## [`1.9.2` (2022-11-14)](https://github.com/kdeldycke/repomatic/compare/v1.9.1...v1.9.2)

> [!NOTE]
> `1.9.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.9.2).

- Fix production of multiline commit list in build and release workflow.

## [`1.9.1` (2022-11-12)](https://github.com/kdeldycke/repomatic/compare/v1.9.0...v1.9.1)

> [!NOTE]
> `1.9.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.9.1).

- Fix tagging.

## [`1.9.0` (2022-11-12)](https://github.com/kdeldycke/repomatic/compare/v1.8.9...v1.9.0)

> [!WARNING]
> `1.9.0` is **not available** on 🐙 GitHub.

- Remove use of deprecated `::set-output` directives and replace them by environment files.

## [`1.8.9` (2022-11-09)](https://github.com/kdeldycke/repomatic/compare/v1.8.8...v1.8.9)

> [!NOTE]
> `1.8.9` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.9).

- Install project with Poetry before generating a dependency graph.

## [`1.8.8` (2022-11-09)](https://github.com/kdeldycke/repomatic/compare/v1.8.7...v1.8.8)

> [!NOTE]
> `1.8.8` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.8).

- Update all dependencies.

## [`1.8.7` (2022-09-26)](https://github.com/kdeldycke/repomatic/compare/v1.8.6...v1.8.7)

> [!NOTE]
> `1.8.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.7).

- Allow the use of project's own Mypy in Poetry virtual environment to benefits from typeshed dependencies.

## [`1.8.6` (2022-09-23)](https://github.com/kdeldycke/repomatic/compare/v1.8.5...v1.8.6)

> [!NOTE]
> `1.8.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.6).

- Do not let `sphinx-apidoc` CLI produce ToC file.

## [`1.8.5` (2022-09-19)](https://github.com/kdeldycke/repomatic/compare/v1.8.4...v1.8.5)

> [!NOTE]
> `1.8.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.5).

- Print raw `pipdeptree` output for debug.

## [`1.8.4` (2022-09-19)](https://github.com/kdeldycke/repomatic/compare/v1.8.3...v1.8.4)

> [!NOTE]
> `1.8.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.4).

- Fix installation of `graphviz` dependency in Poetry venv.

## [`1.8.3` (2022-09-19)](https://github.com/kdeldycke/repomatic/compare/v1.8.2...v1.8.3)

> [!NOTE]
> `1.8.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.3).

- Run `pipdeptree` in Poetry venv to produce dependency graph.

## [`1.8.2` (2022-09-18)](https://github.com/kdeldycke/repomatic/compare/v1.8.1...v1.8.2)

> [!NOTE]
> `1.8.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.2).

- Fix workflow continuation on successful `pyupgrade` run.
- Fix quoting of CLI parameters fed to `black`.

## [`1.8.1` (2022-09-18)](https://github.com/kdeldycke/repomatic/compare/v1.8.0...v1.8.1)

> [!NOTE]
> `1.8.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.1).

- Fix version setup in Python metadata script.

## [`1.8.0` (2022-09-08)](https://github.com/kdeldycke/repomatic/compare/v1.7.5...v1.8.0)

> [!NOTE]
> `1.8.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.8.0).

- Upgrade to `poetry` 1.2.0.
- Allow dependency graph to be continuously updated. Closes [`#176`](https://github.com/kdeldycke/repomatic/issues/176).
- In Python project metadata fetcher, double-quote file list's items to allow use of path with spaces in workflows.
- Ignore broken symlinks pointing to non-existing files in Python metadata fetcher.
- Fix default `pyupgrade` option produced by new Poetry.

## [`1.7.5` (2022-08-25)](https://github.com/kdeldycke/repomatic/compare/v1.7.4...v1.7.5)

> [!NOTE]
> `1.7.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.7.5).

- Use stable release of `calibreapp/image-actions`.

## [`1.7.4` (2022-08-06)](https://github.com/kdeldycke/repomatic/compare/v1.7.3...v1.7.4)

> [!NOTE]
> `1.7.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.7.4).

- Fix `mypy` parameters passing.
- Upgrade job runs from `ubuntu-20.04` to `ubuntu-22.04`.

## [`1.7.3` (2022-08-06)](https://github.com/kdeldycke/repomatic/compare/v1.7.2...v1.7.3)

> [!NOTE]
> `1.7.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.7.3).

- Fix `mypy` parameters passing.

## [`1.7.2` (2022-08-06)](https://github.com/kdeldycke/repomatic/compare/v1.7.1...v1.7.2)

> [!NOTE]
> `1.7.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.7.2).

- Skip Python-specific jobs early if no Python files found in repository.
- Allow execution of `pyupgrade` on non-Poetry-based projects.
- Default `pyupgrade` parameter to `--py3-plus`.
- Use auto-generated parameter for `mypy`'s minimal Python version.
- Merge all Poetry and Sphinx metadata fetching into a Python script, as we cannot have reusable workflows use reusable workflows. Closes #160.

## [`1.7.1` (2022-08-05)](https://github.com/kdeldycke/repomatic/compare/v1.7.0...v1.7.1)

> [!NOTE]
> `1.7.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.7.1).

- Add direct dependency on `poetry`.

## [`1.7.0` (2022-08-05)](https://github.com/kdeldycke/repomatic/compare/v1.6.2...v1.7.0)

> [!NOTE]
> `1.7.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.7.0).

- Auto-generate the set of python minimal version parameters for `mypy`, `black` and `pyupgrade`. Addresses [`python/mypy#13294`](https://github.com/python/mypy/issues/13294), [`psf/black#3124`](https://github.com/psf/black/issues/3124) and [`asottile/pyupgrade#688`](https://github.com/asottile/pyupgrade/issues/688).

## [`1.6.2` (2022-07-31)](https://github.com/kdeldycke/repomatic/compare/v1.6.1...v1.6.2)

> [!NOTE]
> `1.6.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.6.2).

- Remove upper limit of `pyupgrade` automatic `--py3XX-plus` option generation.
- Allow `gitleaks` to use GitHub token to scan PRs.

## [`1.6.1` (2022-07-05)](https://github.com/kdeldycke/repomatic/compare/v1.6.0...v1.6.1)

> [!NOTE]
> `1.6.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.6.1).

- Keep the release date of `citation.cff` up-to-date in `changelog` job.

## [`1.6.0` (2022-07-01)](https://github.com/kdeldycke/repomatic/compare/v1.5.1...v1.6.0)

> [!NOTE]
> `1.6.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.6.0).

- Check for typing. Add dependency on `mypy`.

## [`1.5.1` (2022-06-25)](https://github.com/kdeldycke/repomatic/compare/v1.5.0...v1.5.1)

> [!NOTE]
> `1.5.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.5.1).

- Revert workflow concurrency logic.

## [`1.5.0` (2022-06-23)](https://github.com/kdeldycke/repomatic/compare/v1.4.2...v1.5.0)

> [!NOTE]
> `1.5.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.5.0).

- Auto-remove unused imports in Python code. Add dependency on `pycln`.
- Freeze Python version used to run all code to the `3.10` series.

## [`1.4.2` (2022-05-22)](https://github.com/kdeldycke/repomatic/compare/v1.4.1...v1.4.2)

> [!NOTE]
> `1.4.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.4.2).

- Group workflow jobs so new commits cancels in-progress execution triggered by previous commits.
- Reduce minimal Pylint success score to `7.0/10`.

## [`1.4.1` (2022-04-16)](https://github.com/kdeldycke/repomatic/compare/v1.4.0...v1.4.1)

> [!NOTE]
> `1.4.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.4.1).

- Fix admonition rendering in changelog template.

## [`1.4.0` (2022-04-16)](https://github.com/kdeldycke/repomatic/compare/v1.3.1...v1.4.0)

> [!NOTE]
> `1.4.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.4.0).

- Use `autopep8` to wrap Python comments at 88 characters length.

## [`1.3.1` (2022-04-16)](https://github.com/kdeldycke/repomatic/compare/v1.3.0...v1.3.1)

> [!NOTE]
> `1.3.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.3.1).

- Bump `actions/checkout` action to fix run in containers jobs.

## [`1.3.0` (2022-04-13)](https://github.com/kdeldycke/repomatic/compare/v1.2.1...v1.3.0)

> [!NOTE]
> `1.3.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.3.0).

- Auto-format docstrings in Python files. Add dependency on `docformatter`.
- Auto-format `JS`, `CSS`, `HTML` and `XML` code blocks in Markdown files. Add
  dependency on `mdformat-web`.
- Lint Python docstrings. Add dependency on `pydocstyle`.
- Use `isort` profile to aligns with `black`. Removes `.isort.cfg`.
- Tweak `🙏 help wanted` label description.

## [`1.2.1` (2022-04-12)](https://github.com/kdeldycke/repomatic/compare/v1.2.0...v1.2.1)

> [!NOTE]
> `1.2.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.2.1).

- Fix Sphinx auto-detection by relying on static syntax analyzer instead of
  trying to import the executable configuration.

## [`1.2.0` (2022-04-11)](https://github.com/kdeldycke/repomatic/compare/v1.1.0...v1.2.0)

> [!NOTE]
> `1.2.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.2.0).

- Detect Sphinx's `autodoc` extension to create a PR updating documentation.
- Auto deploy Sphinx documentation on GitHub pages if detected.
- Update `ℹ️ help wanted` label to `🙏 help wanted`.
- Triggers `docs` workflow on tagging to fix dependency graph generation.
- Allows `release` workflow to be re-launched on tagging error.

## [`1.1.0` (2022-03-30)](https://github.com/kdeldycke/repomatic/compare/v1.0.1...v1.1.0)

> [!NOTE]
> `1.1.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.1.0).

- Dynamically add `⚖️ curation`, `🆕 new link` and `🩹 fix link` labels on
  awesome list projects.

## [`1.0.1` (2022-03-30)](https://github.com/kdeldycke/repomatic/compare/v1.0.0...v1.0.1)

> [!NOTE]
> `1.0.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.0.1).

- Remove the title of the section containing the TOC in awesome lists to fix
  the linter.

## [`1.0.0` (2022-03-30)](https://github.com/kdeldycke/repomatic/compare/v0.9.1...v1.0.0)

> [!NOTE]
> `1.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v1.0.0).

- Lint awesome list repositories.
- Update `👷 CI/CD` label to `🤖 ci`.
- Update `📗 documentation` label to` 📚 documentation`.
- Update `🔄 duplicate` label to `🧑‍🤝‍🧑 duplicate`.
- Update `🆕 feature request` label to `🎁 feature request`.
- Update `❓ question` label to `❔ question`.
- Let Pylint discover Python files and modules to lint.
- Do not generate a `.gitignore` or `.mailmap` if none exist. Only update it.
- Do not run the daily `prepare-release` job to reduce the number of
  notifications. Add instructions on PR on how to refresh it.
- Auto-update TOC in Markdown. Add dependency on `mdformat-toc`.
- Remove forbidden TOC entries in Markdown for awesome lists.
- Remove wrapping of Markdown files to 79 characters.
- Use the `tomllib` from the standard library starting with Python 3.11.

## [`0.9.1` (2022-03-09)](https://github.com/kdeldycke/repomatic/compare/v0.9.0...v0.9.1)

> [!NOTE]
> `0.9.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.9.1).

- Fix search of Python files in `lint-python` workflow.

## [`0.9.0` (2022-03-09)](https://github.com/kdeldycke/repomatic/compare/v0.8.6...v0.9.0)

> [!NOTE]
> `0.9.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.9.0).

- Add Zsh script linter.
- Search for leaked tokens and credentials in code.
- Add new `💣 security` label.
- Adjust `🐛 bug` label color.
- Add new `gitignore-location` and `gitignore-extra-categories` parameters to
  `update-gitignore` workflow.
- Fix usage of default values of reused workflows which are called naked. In
  which case they're not fed with the default from input's definition.

## [`0.8.6` (2022-03-04)](https://github.com/kdeldycke/repomatic/compare/v0.8.5...v0.8.6)

> [!NOTE]
> `0.8.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.6).

- Reactivate sponsor auto-tagging workflow now that it has been fixed upstream.

## [`0.8.5` (2022-03-02)](https://github.com/kdeldycke/repomatic/compare/v0.8.4...v0.8.5)

> [!NOTE]
> `0.8.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.5).

- Update dependencies.

## [`0.8.4` (2022-02-21)](https://github.com/kdeldycke/repomatic/compare/v0.8.3...v0.8.4)

> [!NOTE]
> `0.8.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.4).

- Replace hard-coded PyPI package link in GitHub release text with dynamic
  value from Poetry configuration.

## [`0.8.3` (2022-02-13)](https://github.com/kdeldycke/repomatic/compare/v0.8.2...v0.8.3)

> [!NOTE]
> `0.8.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.3).

- Allow the location of the dependency graph image to be set with the
  `dependency-graph-output` parameter for reused workflow.

## [`0.8.2` (2022-02-13)](https://github.com/kdeldycke/repomatic/compare/v0.8.1...v0.8.2)

> [!NOTE]
> `0.8.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.2).

- Fix generation of `pyupgrade` Python version parameter.

## [`0.8.1` (2022-02-13)](https://github.com/kdeldycke/repomatic/compare/v0.8.0...v0.8.1)

> [!NOTE]
> `0.8.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.1).

- Fix installation of `tomli` dependency for dependency graph generation.
- Fix installation of Poetry in Python modernization workflow.

## [`0.8.0` (2022-02-13)](https://github.com/kdeldycke/repomatic/compare/v0.7.25...v0.8.0)

> [!NOTE]
> `0.8.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.8.0).

- Add new workflow proposing PRs to modernize Python code for Poetry-based
  projects.
- Add new workflow to produce dependency graph of Poetry-based project.
- Auto-detect minimal Python version targeted by Poetry projects.
- Add dependency on `pipdeptree`, `pyupgrade` and `tomli`.

## [`0.7.25` (2022-01-16)](https://github.com/kdeldycke/repomatic/compare/v0.7.24...v0.7.25)

> [!NOTE]
> `0.7.25` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.25).

- Fix fetching of new commits in PRs.

## [`0.7.24` (2022-01-15)](https://github.com/kdeldycke/repomatic/compare/v0.7.23...v0.7.24)

> [!NOTE]
> `0.7.24` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.24).

- Fix upload of build artifacts in GitHub release.

## [`0.7.23` (2022-01-15)](https://github.com/kdeldycke/repomatic/compare/v0.7.22...v0.7.23)

> [!NOTE]
> `0.7.23` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.23).

- Fix use of token for Git tagging.

## [`0.7.22` (2022-01-15)](https://github.com/kdeldycke/repomatic/compare/v0.7.21...v0.7.22)

> [!WARNING]
> `0.7.22` is **not available** on 🐙 GitHub.

- Generate list of all new and release commits in the first job of the release
  workflow.

## [`0.7.21` (2022-01-13)](https://github.com/kdeldycke/repomatic/compare/v0.7.20...v0.7.21)

> [!WARNING]
> `0.7.21` is **not available** on 🐙 GitHub.

- Fix regex matching the release commit.

## [`0.7.20` (2022-01-13)](https://github.com/kdeldycke/repomatic/compare/v0.7.19...v0.7.20)

> [!WARNING]
> `0.7.20` is **not available** on 🐙 GitHub.

- Refactor release workflow to rely on a new matrix-based multi-commit
  detection strategy.
- Trigger tagging by monitoring `main` branch commit messages instead of
  `prepare-release` PR merge event.
- Upload build artifacts for each commit.
- Fix addition of PyPI link in GitHub release content.

## [`0.7.19` (2022-01-11)](https://github.com/kdeldycke/repomatic/compare/v0.7.18...v0.7.19)

> [!NOTE]
> `0.7.19` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.19).

- Secret token need to be passed explicitly in reused workflow for PyPI
  publishing.

## [`0.7.18` (2022-01-11)](https://github.com/kdeldycke/repomatic/compare/v0.7.17...v0.7.18)

> [!NOTE]
> `0.7.18` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.18).

- Add version in the name of built artifacts.

## [`0.7.17` (2022-01-11)](https://github.com/kdeldycke/repomatic/compare/v0.7.16...v0.7.17)

> [!NOTE]
> `0.7.17` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.17).

- Fix detection of Poetry-based projects.

## [`0.7.16` (2022-01-11)](https://github.com/kdeldycke/repomatic/compare/v0.7.15...v0.7.16)

> [!NOTE]
> `0.7.16` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.16).

- Remove temporary debug steps.
- Do not trigger debugging and linters on `pull_request`: it duplicates the
  `push` event.
- Skip file-based labeller workflow for dependabot triggered PRs.

## [`0.7.15` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.14...v0.7.15)

> [!NOTE]
> `0.7.15` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.15).

- Use PAT token to auto-tag releases.

## [`0.7.14` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.13...v0.7.14)

> [!WARNING]
> `0.7.14` is **not available** on 🐙 GitHub.

- Use `actions/checkout` to fetch last 10 commits of PR during release tagging.
- Use commit message to identify release commit.
- Hard-code fetching of `main` branch on tagging to identify the release
  commit.
- Attach the release commit to the GitHub release.

## [`0.7.13` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.12...v0.7.13)

> [!WARNING]
> `0.7.13` is **not available** on 🐙 GitHub.

- Checkout tag within job to create a new GitHub release instead of relying on
  previous job's SHA identification. The latter being different right after it
  has been merged in `main`.

## [`0.7.12` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.11...v0.7.12)

> [!WARNING]
> `0.7.12` is **not available** on 🐙 GitHub.

- Fix variable name used to attach the tagged commit to new GitHub release.

## [`0.7.11` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.10...v0.7.11)

> [!WARNING]
> `0.7.11` is **not available** on 🐙 GitHub.

- Force attachment of new GitHub release to the tagged commit.

## [`0.7.10` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.9...v0.7.10)

> [!WARNING]
> `0.7.10` is **not available** on 🐙 GitHub.

- Trigger changelog workflow on any other workflow change to make sure
  hard-coded versions in URLs are kept in sync.
- Resort to explicit fetching of past commits to identify the first one of the
  `prepare-release` PR on tagging.
- Use `base_ref` variable instead of hard-coding `main` branch in release
  workflow.

## [`0.7.9` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.8...v0.7.9)

> [!WARNING]
> `0.7.9` is **not available** on 🐙 GitHub.

- Force fetching of past 10 commits to identify `prepare-release` PR's first
  commit.
- Do not fetch the final merge commit silently produced by `actions/checkout`
  for PRs. Get `HEAD` instead.

## [`0.7.8` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.7...v0.7.8)

> [!WARNING]
> `0.7.8` is **not available** on 🐙 GitHub.

- Fix local `prepare-release` branch name to search for first commit of PR.

## [`0.7.7` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.6...v0.7.7)

> [!WARNING]
> `0.7.7` is **not available** on 🐙 GitHub.

- Use `git log` to identify the first commit SHA of the `prepare-release` PR.

## [`0.7.6` (2022-01-10)](https://github.com/kdeldycke/repomatic/compare/v0.7.5...v0.7.6)

> [!WARNING]
> `0.7.6` is **not available** on 🐙 GitHub.

- Merge the post-release version bump job into `prepare-release` branch
  creation workflow, the result being a 2 commits PR.
- Allow for empty release notes during the generation of a new changelog entry.

## [`0.7.5` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.7.4...v0.7.5)

> [!NOTE]
> `0.7.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.5).

- Force `push` and `create` events to match on tags in release workflow.

## [`0.7.4` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.7.3...v0.7.4)

> [!NOTE]
> `0.7.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.7.4).

- Do not try to fetch build artifacts if the publishing step has been skipped.
- Do not trigger debug workflow on `pull_request` events.

## [`0.7.3` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.7.2...v0.7.3)

> [!WARNING]
> `0.7.3` is **not available** on 🐙 GitHub.

- Always execute the last `github-release` job in the release workflow, even if
  the project is not Poetry-based.
- Catch `create` events so tagging triggers a post-release version bump job.

## [`0.7.2` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.7.1...v0.7.2)

> [!WARNING]
> `0.7.2` is **not available** on 🐙 GitHub.

- Untie `git-tag` and `post-release-version-bump` events. Trigger the later on
  Git tagging.
- Move the detection logic of the `prepare-release` PR merge event to a
  dedicated job.

## [`0.7.1` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.7.0...v0.7.1)

> [!WARNING]
> `0.7.1` is **not available** on 🐙 GitHub.

- Fix detection of `prepare-release` PR merge event.

## [`0.7.0` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.6.3...v0.7.0)

> [!WARNING]
> `0.7.0` is **not available** on 🐙 GitHub.

- Detect Poetry-based project, then auto-build and publish packages on PyPI on
  release.
- Always test builds on each commit.
- Add build artifacts to GitHub releases.

## [`0.6.3` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.6.2...v0.6.3)

> [!NOTE]
> `0.6.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.6.3).

- Skip labelling on `prepare-release` branch.
- Skip version increment updates on release commits.
- Tighten up changelog job's trigger conditions.

## [`0.6.2` (2022-01-09)](https://github.com/kdeldycke/repomatic/compare/v0.6.1...v0.6.2)

> [!NOTE]
> `0.6.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.6.2).

- Fix generation of file-based labelling rules.

## [`0.6.1` (2022-01-08)](https://github.com/kdeldycke/repomatic/compare/v0.6.0...v0.6.1)

> [!NOTE]
> `0.6.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.6.1).

- Fix extension of default labelling rules.

## [`0.6.0` (2022-01-08)](https://github.com/kdeldycke/repomatic/compare/v0.5.5...v0.6.0)

> [!NOTE]
> `0.6.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.6.0).

- Add a reusable workflow to automatically label issues and PRs depending on
  changed files.
- Allow extra labelling rules to be specified via custom input.
- Let sponsor labelling workflow to be reused.

## [`0.5.5` (2022-01-07)](https://github.com/kdeldycke/repomatic/compare/v0.5.4...v0.5.5)

> [!NOTE]
> `0.5.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.5.5).

- Replace custom version of `julb/action-manage-label` by upstream.

## [`0.5.4` (2022-01-05)](https://github.com/kdeldycke/repomatic/compare/v0.5.3...v0.5.4)

> [!NOTE]
> `0.5.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.5.4).

- Checkout repository before syncing labels so local extra definitions can be
  used.

## [`0.5.3` (2022-01-05)](https://github.com/kdeldycke/repomatic/compare/v0.5.2...v0.5.3)

> [!NOTE]
> `0.5.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.5.3).

- Fix download of remote file in label workflow.

## [`0.5.2` (2022-01-05)](https://github.com/kdeldycke/repomatic/compare/v0.5.1...v0.5.2)

> [!NOTE]
> `0.5.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.5.2).

- Use my own fork of `julb/action-manage-label` while we wait for upstream fix.
- Rename label workflow's `label-files` input variable to `extra-label-files`.

## [`0.5.1` (2022-01-05)](https://github.com/kdeldycke/repomatic/compare/v0.5.0...v0.5.1)

> [!NOTE]
> `0.5.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.5.1).

- Disable sponsor auto-tagging while we wait for upstream fix.

## [`0.5.0` (2022-01-05)](https://github.com/kdeldycke/repomatic/compare/v0.4.8...v0.5.0)

> [!NOTE]
> `0.5.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.5.0).

- Add a reusable workflow to maintain GitHub labels.
- Add a set of default emoji-based labels.
- Add dedicated `changelog`, `sponsor` and `dependencies` labels.
- Update `CI/CD` label icon.
- Auto-tag issues and PRs opened by sponsors.
- Allow for sourcing of additional labels when reusing workflow.

## [`0.4.8` (2022-01-04)](https://github.com/kdeldycke/repomatic/compare/v0.4.7...v0.4.8)

> [!NOTE]
> `0.4.8` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.8).

- Use more recent `calibreapp/image-actions` action.
- Remove unused custom variable for reusable GitHub release job.

## [`0.4.7` (2022-01-04)](https://github.com/kdeldycke/repomatic/compare/v0.4.6...v0.4.7)

> [!NOTE]
> `0.4.7` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.7).

- Fix use of GitHub token for workflow auto-updates on release.
- Allow typo autofix job to propose changes in workflow files.

## [`0.4.6` (2022-01-04)](https://github.com/kdeldycke/repomatic/compare/v0.4.5...v0.4.6)

> [!NOTE]
> `0.4.6` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.6).

- Let GitHub release produced on tagging to be customized with user's content
  and uploaded files.
- Expose tagged version from reusable `release` workflow.

## [`0.4.5` (2022-01-03)](https://github.com/kdeldycke/repomatic/compare/v0.4.4...v0.4.5)

> [!NOTE]
> `0.4.5` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.5).

- Fix use of the right token for reused `changelog` and `release` workflows.
- Restrict comparison URL steps to source workflow.

## [`0.4.4` (2022-01-03)](https://github.com/kdeldycke/repomatic/compare/v0.4.3...v0.4.4)

> [!NOTE]
> `0.4.4` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.4).

- Do not rely on `bumpversion` for comparison URL update on release tagging.

## [`0.4.3` (2022-01-03)](https://github.com/kdeldycke/repomatic/compare/v0.4.2...v0.4.3)

> [!NOTE]
> `0.4.3` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.3).

- Only match first occurrence of triple-backticks delimited block text in
  `changelog.md` in `prepare-release` job. Also matches empty line within the
  block.
- Make GitHub changelog URL update more forgiving.

## [`0.4.2` (2022-01-03)](https://github.com/kdeldycke/repomatic/compare/v0.4.1...v0.4.2)

> [!NOTE]
> `0.4.2` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.2).

- Skip steps in workflows which are specific to the source repository.
- Aligns all PR content.

## [`0.4.1` (2022-01-03)](https://github.com/kdeldycke/repomatic/compare/v0.4.0...v0.4.1)

> [!NOTE]
> `0.4.1` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.1).

- Allow changelog and release workflows to be reusable.

## [`0.4.0` (2021-12-31)](https://github.com/kdeldycke/repomatic/compare/v0.3.5...v0.4.0)

> [!NOTE]
> `0.4.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.4.0).

- Factorize version increment jobs.

## [`0.3.5` (2021-12-31)](https://github.com/kdeldycke/repomatic/compare/v0.3.4...v0.3.5)

> [!NOTE]
> `0.3.5` is the *first version* available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v0.3.5).

- Provide tag version for GitHub release creation.

## [`0.3.4` (2021-12-31)](https://github.com/kdeldycke/repomatic/compare/v0.3.3...v0.3.4)

- Chain `post-release-version-bump` job with automatic git tagging.
- Auto-commit `post-release-version-bump` results.
- Create a GitHub release on tagging.

## [`0.3.3` (2021-12-30)](https://github.com/kdeldycke/repomatic/compare/v0.3.2...v0.3.3)

- Bump YAML linting max line length to 120.
- Auto-tag release after the `prepare-release` PR is merged back into `main`.

## [`0.3.2` (2021-12-30)](https://github.com/kdeldycke/repomatic/compare/v0.3.1...v0.3.2)

- Refresh every day the date in `prepare-release` job.
- Skip linting on `prepare-release` job as it does not points to tagged URLs
  yet.
- Reduce changelog PRs refresh rate based on changed files.
- Rely on `create-pull-request` action default to set authorship.
- Fix `autofix` workflow reusability.

## [`0.3.1` (2021-12-23)](https://github.com/kdeldycke/repomatic/compare/v0.3.0...v0.3.1)

- Hard-code tagged version for executed Python script.
- Activate debugging workflow on all branches and PRs.
- Allows debug workflow to be reused.

## [`0.3.0` (2021-12-16)](https://github.com/kdeldycke/repomatic/compare/v0.2.0...v0.3.0)

- Add a reusable workflow to fix typos.
- Add a reusable workflow to optimize images.
- Add a reusable workflow to auto-format Python files with isort and Black.
- Add a reusable workflow to auto-format Markdown files with mdformat.
- Add a reusable workflow to auto-format JSON files with `jsonlint`.
- Add a reusable workflow to auto-update .gitignore file.
- Add a reusable workflow to auto-update .mailmap file.
- Force retargeting of workflow dependencies to `main` branch on post-release
  version bump.

## [`0.2.0` (2021-12-15)](https://github.com/kdeldycke/repomatic/compare/v0.1.0...v0.2.0)

- Add autolock reusable workflow for closed issues and PRs.
- Automate changelog and version management.
- Add workflow to create ready-to-use PRs to prepare a release, post-release
  version bump and minor & major version increments.
- Add a debug workflow to print action context and environment variables.
- Set Pylint failure threshold at 80%.

## [`0.1.0` (2021-12-12)](https://github.com/kdeldycke/repomatic/compare/v0.0.1...v0.1.0)

- Install project with Poetry before calling Pylint if `pyproject.toml`
  presence is detected.
- Hard-code tagged version in requirement URL for reusable workflows.
- Document the release process.

## [`0.0.1` (2021-12-11)](https://github.com/kdeldycke/repomatic/compare/5cbdbb...v0.0.1)

- Initial public release.
