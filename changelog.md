# Changelog

## [`6.24.1.dev0` (unreleased)](https://github.com/kdeldycke/repomatic/compare/v6.24.0...main)

> [!WARNING]
> This version is **not released yet** and is under active development.

- Validate `[project.scripts]` entries when assembling the Nuitka build matrix. A script name that PyPI and [uv-build](https://github.com/astral-sh/uv/pull/19495) would refuse (path-shaped like `../escape` or `nested/script`, empty, dot-only, or containing characters outside `[A-Za-z0-9._-]`) is now rejected up front instead of flowing into the binary file path template and the release workflow's artifact, `chmod`, and attestation commands. A malformed value (no colon, multiple colons, empty side) raises a descriptive error instead of crashing later with a tuple-unpacking `ValueError`.

## [`6.24.0` (2026-05-28)](https://github.com/kdeldycke/repomatic/compare/v6.23.0...v6.24.0)

> [!NOTE]
> `6.24.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.24.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.24.0).

- Split the package build into its own reusable `_release-build.yaml` lane (squash-merge guard, project metadata, and the signed `build-package` job), so the entry `release.yaml`'s `publish-pypi` job depends only on the freshly-built wheel and ships to PyPI right after the build instead of after the whole engine (binary compilation, VirusTotal scan) completes. `_release-engine.yaml` keeps the binary and release-finalization jobs and is gated on the build lane (`needs: build`) so its `create-release` and `sync-dev-release` jobs reuse the same run-scoped wheel (one build, one attestation, shared to both PyPI and the GitHub release). Downstream `release.yaml` is regenerated with three jobs (`build`, `publish-pypi`, `release`) by copying the canonical entry and rewriting each `uses:` to the pinned cross-repo ref; `publish-pypi`'s `package_built`, `release_commits_matrix`, and `release_notes_with_admonition` now come from the build lane.

## [`6.23.0` (2026-05-28)](https://github.com/kdeldycke/repomatic/compare/v6.22.0...v6.23.0)

> [!NOTE]
> `6.23.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.23.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.23.0).

- Split `release.yaml` into a thin entry workflow (`release.yaml`, owns `push`/`workflow_dispatch` triggers and the `publish-pypi` job) and a new reusable engine (`_release-engine.yaml`, `workflow_call`-only). Keeping `publish-pypi` in the entry is required so each repo's OIDC `job_workflow_ref` claim resolves to its own `release.yaml` for PyPI's Trusted Publisher (see [pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096)). Downstream repos receive a regenerated `release.yaml` from `sync-repomatic`: its `uses:` line now points at `_release-engine.yaml@vX.Y.Z`, and its `publish-pypi` job is derived from the canonical entry's own job (dogfooding checkout dropped, action ref pinned to release tag, runner changed from `ubuntu-latest` to `ubuntu-slim`). The separate `release-publish-pypi-job.yaml` data fragment is removed; `release.yaml` is the single source for the job, keeping third-party action SHA pins Renovate-visible.
- The vulnerability scan now requires `uv` >= `0.11.15` and parses `uv audit --output-format json` directly, replacing the regex-scraping of `uv audit`'s human-readable text. An older `uv`, or an unrecognized `uv audit` JSON `schema.version`, raises a clear error instead of silently reporting no vulnerabilities. Advisory `aliases` from the structured output drive cross-source deduplication, so an advisory reported by `uv audit` under a PYSEC/OSV ID merges with the same advisory reported by Dependabot under its GHSA ID.
- Refine `/repomatic-ship`'s reconciliation sweep: it now re-consolidates the changelog after the babysit phase to drop entries for bugs fixed in code introduced earlier the same release cycle, assigns docstring cross-reference correctness to the code pass (the docs pass surfaces such warnings through the Sphinx build but cannot fix them), and has the code pass verify types with the CI-equivalent `repomatic run mypy` to avoid Python-version-divergence false positives.
- `/repomatic-ship` now smoke-runs the `autofix` tool-runner commands (`repomatic run typos`, the `uv audit`-backed vulnerable-deps scan) in its pre-push gate, catching a binary-checksum drift or a tool-output schema change before CI rather than in the `autofix` run. After the babysit phase it re-dispatches `changelog.yaml` so a code-only fix push, which does not match that workflow's `paths:` filter, still regenerates the release PR on the final green `main`.
- `/babysit-ci` now also monitors `autofix.yaml`, so a crashed mechanical-fix job (a tool-runner binary checksum mismatch, or a parser choking on changed external-tool output) is diagnosed and fixed instead of left red on `main`.
- Fix `release.yaml`'s `compile-binaries` and `test-binaries` jobs aborting every non-release run with `Unexpected value ''` on projects that disable binary builds (`nuitka.enabled = false`). Their `strategy.matrix` now falls back to an empty `{"include":[]}` matrix when `nuitka_matrix` is null, matching the `publish-pypi` fix from `6.22.0`: GitHub resolves `strategy.matrix` (and the matrix-derived `runs-on: ${{ matrix.os }}`) during job setup even when `if:` will skip the job, so a null matrix failed the whole run. Binary-disabled projects now skip these jobs cleanly.

## [`6.22.0` (2026-05-25)](https://github.com/kdeldycke/repomatic/compare/v6.21.0...v6.22.0)

> [!NOTE]
> `6.22.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.22.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.22.0).

- `repomatic init` now prunes downstream orphans of skills, agents, and workflows that repomatic has renamed or removed, so an upstream change (a skill rename like `repomatic-release` becoming `repomatic-ship`, or the `label-sponsors` and `labeller-*` workflows being merged into `labels.yaml`) propagates automatically instead of leaving a stale file behind. A skill or agent copy is deleted when its content matches any version repomatic shipped; a workflow thin-caller is deleted when its `uses:` line still points at the dropped upstream workflow. A locally modified copy (edited content, or a thin-caller carrying extra jobs) is reported for manual review, never deleted. The seed list covers every asset dropped across the project's `gha-utils`, `repokit`, and `repomatic` naming eras, so a repo first set up under an earlier name sheds its legacy files on the next run. The `sync-repomatic` autofix job applies this on every push. Pass `--keep-removed` to report orphans without deleting them, or `--delete-removed-modified` to also delete locally modified ones.
- `/repomatic-ship` now closes with a reflect-and-contribute-back step: once the release PR is ready, it reviews the session for skill, workflow, or convention friction hit during the release and proposes concrete fixes to the upstream `repomatic` source (proposing only, never committing upstream unprompted).
- Fix the downstream caller's `publish-pypi` job aborting every non-release `release.yaml` run with `Unexpected value ''` when evaluating its `strategy`. Its matrix now falls back to an empty `{"include":[]}` matrix when `release_commits_matrix` is empty, since GitHub evaluates `strategy.matrix` even for jobs that `if:` will skip; non-release pushes now skip the job cleanly instead of failing the run.
- Fix `/repomatic-ship` and `/babysit-ci` dropping the `Co-Authored-By: Claude` trailer on their autonomous commits when a downstream `CLAUDE.md` had not synced the attribution-exception section the skills cross-referenced. Both skills now require the trailer self-containedly, overriding any no-AI-attribution rule (project or global), and `/repomatic-ship` instructs its spawned `babysit-ci` agent to keep it.
- `/babysit-ci` now treats a workflow run whose `conclusion` is `failure` while no individual job failed as a real workflow-level error (such as a `strategy.matrix` evaluating `fromJSON('')`) to investigate via the run's annotations, instead of a benign artifact to skip.
- Fix the documentation site's live CLI-help and example blocks rendering empty since `click-extra` 7.15.0 made execution directives opt-in (`click_extra_enable_exec_directives` defaults to `False`). `docs/conf.py` now opts in.
- Seed the hand-maintained extra-docs region of every tool section in the [tool-runner docs](https://kdeldycke.github.io/repomatic/tool-runner.html#available-tools) with a runnable `repomatic run` example and, for configurable tools, a minimal `[tool.X]` snippet.

## [`6.21.0` (2026-05-25)](https://github.com/kdeldycke/repomatic/compare/v6.20.0...v6.21.0)

> [!NOTE]
> `6.21.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.21.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.21.0).

- Replace the `repomatic-release` skill with `repomatic-ship`, a release orchestrator: it reconciles the changelog, code, and docs to the net release state, then commits, pushes, and babysits CI (delegating to `babysit-ci`) until the release PR is ready to rebase-and-merge. Review-gated in normal use, fully autonomous under `--dangerously-skip-permissions`. It does not run the `release-prep` CLI itself (CI's `prepare-release` job does); the version read is advisory and never merges a bump PR.
- Add a `modernize` mode to the `repomatic-deps` skill: it reads the changelogs of dependencies upgraded since the last release and autonomously refactors code to adopt their new features (dropping workarounds, replacing hand-rolled logic, migrating off deprecations), gating each change on the local test suite.
- Extend the `babysit-ci` skill to also monitor and triage the Nuitka `compile-binaries` job in `release.yaml`, catching binary-build breakage before a release reaches the immutable-release wall.
- Decouple the downstream caller's `publish-pypi` job from the reusable workflow's overall result. The job now runs under `always()` and gates on a new `package_built` workflow output (`"true"` when the `build-package` job succeeded) instead of the run's success, so a wheel that built cleanly still publishes to PyPI even when an unrelated job (like a binary test) fails the run.
- Remove the `repomatic-sync`, `repomatic-lint`, and `repomatic-test` skills. Each only wrapped a CLI command that CI already runs on every push, duplicating the mechanical layer.
- Fix the `bump-version` job in `changelog.yaml` leaving an orphan minor/major version-bump PR open after a competing bump merged into `main`. The job's checkout now sets `fetch-tags: true` so the `close-stale-bump-pr` step's `is_version_bump_allowed` re-check can resolve the latest release tag. Previously the tag-less checkout made the re-check fall back to "allow", so it never closed the stale PR even though the `metadata` job (which does fetch tags) had correctly flagged it for cleanup.
- Switch `pytest-xdist` distribution mode from `--dist=loadfile` to `--dist=loadgroup` so `@pytest.mark.xdist_group("git")` markers are honored. With `loadfile`, xdist ignores group markers and assigns each file to its own worker, allowing `test_metadata.py` and `test_git_ops.py` workers to race for `.git/config.lock` simultaneously on Windows. With `loadgroup`, all tests sharing `xdist_group("git")` run on a single worker and the lock contention disappears.
- Enable myst-parser's native `alert` extension in `docs/conf.py` so GitHub-style alerts (`> [!NOTE]`, `> [!IMPORTANT]`, ...) render as admonitions on the documentation site instead of plain blockquotes showing a literal `[!NOTE]` marker.
- Give each tool section in the [tool-runner docs](https://kdeldycke.github.io/repomatic/tool-runner.html) a hand-maintained extra-docs region that `repomatic update-docs` preserves across regenerations, and seed Nuitka's with a `repomatic run nuitka` example plus a minimal `[tool.nuitka]` snippet. Add Nuitka to the page's `[tool.X]`-support table; its native `pyproject.toml` support is tracked upstream at [Nuitka#2136](https://github.com/Nuitka/Nuitka/issues/2136).

## [`6.20.0` (2026-05-24)](https://github.com/kdeldycke/repomatic/compare/v6.19.0...v6.20.0)

> [!NOTE]
> `6.20.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.20.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.20.0).

- **Breaking:** remove `[tool.repomatic] nuitka.extra-args`. Configure Nuitka flags through `[tool.nuitka]` in `pyproject.toml` instead: `--include-data-files=SRC=DEST` becomes `include-data-files = ["SRC=DEST"]`.
- Integrate Nuitka with the tool runner: `repomatic run nuitka` installs the pinned Nuitka, reads `[tool.nuitka]` from `pyproject.toml`, and passes the section as CLI flags (a `true` value becomes a bare `--flag`, a string or number becomes `--key=value`, a list repeats the flag per item). Nuitka now appears in `repomatic run --list` and the tool-runner docs. Add binary metadata to repomatic's `[tool.nuitka]`: `product-name`, `file-description`, `copyright`, native per-OS icons (`icon.png`, `icon.icns`, `icon.ico`), `include-package-data = ["click_extra"]`, and numeric `file-version`/`product-version` kept in sync via a dedicated `[tool.bumpversion]` rule. Build Nuitka binaries on Python 3.14.
- Switch `[tool.typos]` sync mode from `BOOTSTRAP` to `ONGOING`: canonical proper-noun identifiers (`GitHub`, `macOS`, `PyPI`, and the rest) are now merged into any pre-existing `[tool.typos]` section instead of skipping it. Local-only keys, extra `extend-identifiers`, `extend-words`, `extend-exclude`, and `[[files]]` entries survive the merge.
- Add two `[[tool.bumpversion.files]]` rules to the bundled template so downstream Python repos receive them: the `[project]` version search is anchored to `(?m)^version = ` to avoid rewriting `[tool.nuitka]`'s `file-version`/`product-version` keys, and a dedicated rule syncs those numeric keys with `ignore_missing_version = true` (a no-op in repos without `[tool.nuitka]`). Repos carrying the old unanchored `[project]` entry converge on the anchored form without duplication.
- Add `test-matrix.unstable` config: a list of matrix-key dicts (like `{click-version = "main"}`) that marks every matching full-matrix combination `continue-on-error` in CI. Applied to the full matrix only, not the PR matrix.
- Add a `lint-repo` check that warns when a `[tool.repomatic.test-matrix] exclude` entry references a runner or Python version absent from the live matrix axes. Such entries are silently ignored; the most common cause is an upstream runner rename (like `macos-15-intel` becoming `macos-26-intel`).
- Add `workflow_dispatch` triggers to `release.yaml` and `update-checksums.yaml` for manual re-runs.

## [`6.19.0` (2026-05-21)](https://github.com/kdeldycke/repomatic/compare/v6.18.4...v6.19.0)

> [!NOTE]
> `6.19.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.19.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.19.0).

- Fix a flaky `.git/config.lock` conflict on `ubuntu-24.04-arm / py3.14t`: `test_skip_binary_build_property_is_bool` in `test_binary.py` accesses `Metadata.skip_binary_build` which, in a CI push event, triggers `Metadata.git` → `pydriller.Git(".")` on a separate xdist worker. The cross-worker lock race caused an `OSError` when the git-group worker ran `test_is_version_bump_allowed_current_repo` simultaneously. Adding `@pytest.mark.xdist_group("git")` to that test serializes it onto the same worker as the other git-touching tests.
- Reconcile orphan version-bump PRs created by races between the `changelog.yaml` schedule and a competing push. The `bump-version` matrix job now always checks out the repo and runs `setup-uv`, and adds a new `Close stale ${{ matrix.part }} bump PR` step that runs when `${part}_bump_allowed` is `false`. The step invokes the new `repomatic close-stale-bump-pr --part minor|major` CLI command, which re-evaluates `is_version_bump_allowed` and closes any open PR on the matching `${part}-version-increment` branch (deleting the branch). Previously the gated steps silently skipped, leaving an orphan PR whose only diff against `main` was `uv.lock` churn from `uv lock --upgrade`. The new step is idempotent: a no-op when no open PR exists on the branch.
- Expand sponsor benefits in the awesome template's `contributing.md`: sponsors now get a dedicated entry in the matching section (visible to readers and LLMs scanning the list body, not only the header banner) and a waiver on the licensing-marker requirement. Add a cross-reference note in the licensing-markers section and explicit heading anchors to support the links in both the English and Chinese variants.
- Skip CI for automated version-bump operations in `tests.yaml`, `lint.yaml`, `labels.yaml`, and `release.yaml::compile-binaries` via a unified `metadata` gate. On the PR side, a `github.head_ref` check against `VERSION_BUMP_BRANCHES` (`major-version-increment`, `minor-version-increment`, `prepare-release`) short-circuits all dependent jobs and collapses eight per-job guards in `lint.yaml` to one. On the push side, a `startsWith` check against `VERSION_BUMP_COMMIT_PREFIXES` (`Bump major version to `, `Bump minor version to `, `[changelog] Post-release bump `) does the same, while preserving the post-merge safety run for Nuitka and tests by excluding the post-release bump prefix from those gates. The trigger-level `branches-ignore` listings are kept as intent documentation: `branches-ignore` matches the PR's *base* branch, not its head ([actions/runner#1591](https://github.com/actions/runner/issues/1591)). `VERSION_BUMP_BRANCHES` in `repomatic.binary` is the single source of truth, enforced by `tests/test_workflows.py`.
- Reduce unnecessary CI scheduling via two complementary mechanisms. Static `paths-ignore`/`paths:` trigger filters cut upfront cost: `lint.yaml`'s `push: main` trigger now ignores image extensions, and `docs.yaml`'s `push: main` trigger is restricted to `docs/**`, `**/*.py`, `pyproject.toml`, `uv.lock`, `readme.md`, `changelog.md`, `citation.cff`, and the workflow file. Dynamic per-job gates in `lint.yaml` driven by new `yaml_changed`, `zsh_changed`, and `workflows_changed` boolean outputs from the `metadata` job (computed against the event's commit range, falling back to "repo contains any such file" for `workflow_dispatch`) skip `lint-yaml`, `lint-zsh`, `lint-github-actions`, and `lint-workflow-security` when no relevant files changed; `lint-types` and `lint-secrets` retain the broader gate.
- Switch the `Sync uv.lock` steps in `changelog.yaml` (`bump-version` and `prepare-release` jobs) from `uv sync` to `uv lock --upgrade`, aligning with the `sync-uv-lock` autofix job. Absorbs pending transitive refreshes into the bump commit instead of producing a trailing lockfile-only PR on the next push.
- Make `lint-changelog --fix` refuse to rewrite admonitions when an upstream lookup looks unhealthy. `get_github_releases` now raises a typed `GitHubReleasesUnavailable` instead of silently returning a partial dict on network, timeout, or JSON-parse errors during pagination (the prior `break`-on-error path produced a corrupted view that the rewriter happily applied). The `lint_changelog_dates` call site catches the exception and, before touching the file, gates the rewrite: a raised GitHub exception combined with any existing GitHub coverage exits with code `2`; an empty PyPI fetch combined with three or more existing PyPI links does the same. The PyPI side needs a coverage threshold because `_fetch_json` collapses every failure mode into a single `None` return, and even if the HTTP status were preserved a `404` from `/pypi/<name>/json` is not authoritative ([pypi/warehouse#1388](https://github.com/pypi/warehouse/issues/1388), [pypi/warehouse#9536](https://github.com/pypi/warehouse/issues/9536)). Replays the `kdeldycke/click-extra#1702` failure, where a transient API hiccup silently stripped every GitHub link from the changelog.
- Make `release.yaml` thin-caller generation robust against drift in the bundled `release-publish-pypi-job.yaml` fragment: match the action ref via regex instead of a literal string, and raise `RuntimeError` when no recognizable ref is found rather than silently emitting a stale ref.
- Bump Biome from `2.4.14` to `2.4.15` in the tool registry.

## [`6.18.4` (2026-05-14)](https://github.com/kdeldycke/repomatic/compare/v6.18.3...v6.18.4)

> [!NOTE]
> `6.18.4` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.4).

- Replace the `RepoScope.NON_AWESOME` variant with `PYTHON_ONLY`, gating affected components on the presence of a PEP 621 `[project].name` in `pyproject.toml` (detected via the new `repomatic.pyproject.is_python_project` helper). Repos that carry `pyproject.toml` only for `[tool.*]` configuration (like dotfiles) are now classified as non-Python and skip Python-flavored components by default. `RepoScope.matches()` now takes both `is_awesome` and `is_python` traits. Existing entries are reclassified: `codecov`, `publish-pypi-action`, `changelog.yaml`, `debug.yaml`, `release.yaml`, and the generated `changelog.md` move to `PYTHON_ONLY`; `renovate` moves to `ALL` (Renovate is language-agnostic). Explicit CLI naming and `[tool.repomatic] include` continue to bypass scope, so a dotfiles repo can still opt into `publish-pypi-action` if needed.
- Extend `release_prep.freeze_workflow_urls` and `unfreeze_workflow_urls` to walk non-symlink YAML files under `repomatic/data/` in addition to `.github/workflows/`. The bundled `release-publish-pypi-job.yaml` fragment now participates in the `@main` ↔ `@vX.Y.Z` rewrite, so wheels built from a freeze commit ship with the pinned action ref baked in. Symlinks in the data dir are skipped to avoid double-processing. `repomatic.github.workflow_sync._PUBLISH_PYPI_DEFAULT_ACTION_REF` is now derived from `DEFAULT_VERSION` so the search string always matches the ref in the (possibly frozen) bundled fragment.
- Bump Biome from `2.4.13` to `2.4.14` and Lychee from `0.24.1` to `0.24.2` in the tool registry.
- Fix `add_exclude_newer_packages` appending `exclude-newer-package` at the end of `[tool.uv]` instead of inserting it right after `exclude-newer`. The wrong position caused `pyproject-fmt` to reorder the key on the next `format-pyproject` run, producing a spurious format PR alongside every `fix-vulnerable-deps` PR.
- Fix flaky `test_metadata.py` and `test_git_ops.py` failures on macOS with Python 3.14 caused by a stale `.git/config.lock` surviving test teardown. `pydriller.Git(".")` holds a `Conf._data["git"]` back-reference creating a reference cycle; Python 3.14's incremental GC defers collection of such cycles, leaving the lock file on disk when `rmfile()` races. A new `_cleanup_git_config_lock` autouse fixture in `conftest.py` removes any stale lock before each test.

## [`6.18.3` (2026-05-11)](https://github.com/kdeldycke/repomatic/compare/v6.18.2...v6.18.3)

> [!NOTE]
> `6.18.3` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.3).

- Fix `autofix.yaml` `setup-guide` job being skipped on `workflow_dispatch` re-runs. The `if:` condition now allows both `push` and `workflow_dispatch` events instead of `push` only, so manual re-runs from the Actions UI re-evaluate the setup guide and update the issue accordingly.
- Fix `release.yaml` `publish-pypi` job running against downstream callers and failing PyPI trusted publishing with a `job_workflow_ref` mismatch. The gate now uses `github.repository == 'kdeldycke/repomatic'` (which reflects the caller's repo under `workflow_call`) instead of `github.event_name == 'push'` (which stays `push` in both self-release and downstream contexts), so the upstream-only job is properly skipped when invoked from a downstream caller. Downstream repos publish through their own thin-caller `publish-pypi` job, which inherits the correct OIDC context.
- Switch the `compile-binaries` job from `--onefile` to `--mode=onefile`, the documented spelling since Nuitka 4.0 (`--onefile` is now hidden but still accepted).

## [`6.18.2` (2026-05-08)](https://github.com/kdeldycke/repomatic/compare/v6.18.1...v6.18.2)

> [!NOTE]
> `6.18.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.2).

- Fix `release.yaml` uploading distributions to PyPI without PEP 740 attestations. The build job now signs each dist file with `pypi-attestations sign` (using the job's OIDC token via Sigstore), writing `<dist>.publish.attestation` sidecars directly into `./dist/` so the dist artifact carries its own attestations. The `publish-pypi` composite action shrinks to setup-uv → download artifact → `uv publish dist/*`. Replaces the previous `actions/attest` + GitHub-attestation-API flow for Python distributions: the Nuitka binary attestation flow is unchanged, and PyPI's PEP 740 provenance is now populated so the `setup-guide` `check_pypi_trusted_publisher` probe can confirm the trusted publisher is wired up. Removes the `attestation-signer-repo` input from the composite action and the separate `attestation-<artifact-name>` artifact: dist files and their `.publish.attestation` sidecars travel together in a single artifact and end up alongside each other on the GitHub release page.

## [`6.18.1` (2026-05-08)](https://github.com/kdeldycke/repomatic/compare/v6.18.0...v6.18.1)

> [!NOTE]
> `6.18.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.1).

- Fix `publish-pypi` composite action attempting to verify build attestations on every file in the workflow workspace (including `changelog.md`, `pyproject.toml`, etc.) instead of just the downloaded distribution artifacts. The `actions/download-artifact` step now downloads to `dist/` so the verification loop and `uv publish` only target the actual artifact files.

## [`6.18.0` (2026-05-07)](https://github.com/kdeldycke/repomatic/compare/v6.17.0...v6.18.0)

> [!NOTE]
> `6.18.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.0).

> [!WARNING]
> `6.18.0` is **not available** on 🐍 PyPI.

- **Breaking**: drop `PYPI_TOKEN` from the `release.yaml` `workflow_call.secrets:` interface. Downstream repos must regenerate their thin-caller workflow with `repomatic init workflows` and register a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) for their own `release.yaml`. This re-enables OIDC-based PyPI publishing previously reverted in [#528](https://github.com/kdeldycke/repomatic/issues/528), this time via a composite action that sidesteps the reusable-workflow `job_workflow_ref` mismatch ([pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096)).
- Add `check_pypi_trusted_publisher` to `lint-repo` and a new `setup-guide-pypi-trusted-publisher` step. The probe reads PEP 740 provenance from the public PyPI integrity API for the latest released file, asserts that a publisher bundle names the downstream's own repo and `release.yaml` workflow, and surfaces a pre-filled link to the project's PyPI publishing settings when the entry is missing or mismatched. The `setup-guide` issue stays open until the first OIDC-attested upload is observed.
- Add the [`publish-pypi`](https://github.com/kdeldycke/repomatic/blob/main/.github/actions/publish-pypi/action.yaml) composite action wrapping `uv publish --trusted-publishing automatic` with build-attestation verification. Each downstream thin-caller now contains a generated `publish-pypi` job that invokes this action with `permissions: id-token: write`, inheriting the caller's OIDC context.
- Add `release_commits_matrix` and `package_name` outputs to the reusable `release.yaml` so caller-side jobs can drive their own matrix and condition execution on a release commit being present.
- Generalize composite-action ref freeze/unfreeze in `release_prep.py` to enumerate every `.github/actions/*/action.y*ml` directory rather than hardcoding a single name. New composite actions now participate in `@main` ↔ `@vX.Y.Z` rewrites without requiring code changes.
- Fix `sync-repomatic` autofix proposing to delete `.github/actions/publish-pypi/action.yaml` whenever it matched the bundled default. The `publish-pypi-action` `BundledComponent` now sets `keep_unmodified=True`: GitHub Actions resolves `uses: ./.github/actions/publish-pypi` and `uses: kdeldycke/repomatic/.github/actions/publish-pypi@vX.Y.Z` directly from the repo path, so the file must remain on disk regardless of content equality. Add a conformance test that asserts every `BundledComponent` shipping a target under `.github/actions/` carries the flag.
- The `setup-guide-pypi-trusted-publisher` issue step and the `check_pypi_trusted_publisher` mismatch warning now point at a pre-filled PyPI settings URL (`?provider=github&owner=…&repository=…&workflow_filename=release.yaml`). PyPI's [`manage_project_oidc_publishers_prefill`](https://github.com/pypi/warehouse/blob/main/warehouse/manage/views/oidc_publishers.py) view reads these parameters to select the GitHub tab and populate the publisher form. New `pypi_trusted_publisher_settings_url` helper centralizes URL construction; `PYPI_TRUSTED_PUBLISHER_WORKFLOW` moves from `lint_repo.py` to `pypi.py` next to the URL constants.

## [`6.17.0` (2026-05-04)](https://github.com/kdeldycke/repomatic/compare/v6.16.0...v6.17.0)

> [!NOTE]
> `6.17.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.17.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.17.0).

- Fix parallel `OSError: Lock for file .git/config did already exist` test failures. `tests/test_git_ops.py` and `tests/test_metadata.py` share `pytestmark = pytest.mark.xdist_group("git")` so pydriller's `Git(".")` initialization (which acquires `.git/config.lock` on every call) is serialized to a single pytest-xdist worker. Add `mypy_path = "docs"` to `[tool.mypy]` so mypy resolves `import docs_update` in `tests/test_readme.py` against `docs/docs_update.py`.
- Fix backslash-escaped brackets rendering literally in `docs/configuration.md` per-option `**Type:**` lines (like `list\[dict[str, str]\]`). The escape, needed only for raw GFM table cells in the `repomatic show-config` CLI output (where `mdformat` would otherwise interpret `[…]` as a markdown link), was leaking into inline-code spans where backslashes are literal in CommonMark. `_format_type` now returns clean Python type strings; the new `escape_type_for_gfm_table` helper is applied only at the CLI rendering layer.
- Add `--template-file <path>` and `--template-arg KEY=VALUE` flags to `repomatic pr-body` so downstream repos can render project-specific PR templates without forking repomatic. `--template` and `--template-file` are mutually exclusive. `load_template`, `render_template`, `render_title`, `render_commit_message`, and `template_args` now accept a {class}`~pathlib.Path` to read a template from disk in addition to a packaged-resource name. `render_title` returns an empty string instead of raising `KeyError` when the frontmatter defines no `title`.
- Fix doubled heading anchors on `docs/configuration.html` and `docs/workflows.html` (like `#dev-release-sync-dev-release-sync`). Per-option headings in `configuration.md` no longer carry a redundant `{#slug}` attribute since the natural Sphinx slug already matches. Headings in `workflows.md` use the canonical `(anchor)=` MyST block-attribute syntax instead of the trailing `{#anchor}` form, which was being kept as literal text in the heading and concatenated into the auto-slug.
- Collapse the most recent row of the Python compatibility matrix in `docs/install.md` to a major-version wildcard (like `4.25.x` → `6.x`) so the table stays unchanged across new minor releases that share the same Python compatibility window. Closed historical rows keep their precise minor-version bounds.

## [`6.16.0` (2026-04-29)](https://github.com/kdeldycke/repomatic/compare/v6.15.0...v6.16.0)

> [!NOTE]
> `6.16.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.16.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.16.0).

- Add `sphinx-docs` agent to the `agents` component (deployed by `repomatic init agents` alongside `grunt-qa` and `qa-engineer`, or via `[tool.repomatic] include = ["agents"]`). The agent carries a canonical Sphinx extension set with notes on `sphinx_click` vs `click_extra.sphinx`, a standard octicon registry, governance rules for `suppress_warnings`/`nitpick_ignore`/`linkcheck_ignore` entries, the `<!-- {feature}-{kind}-start -->` auto-region marker convention, and a Sphinx cross-reference render test recipe. Drops `sphinx_issues` from the set: its `:issue:`/`:pr:`/`:user:`/`:commit:` roles render only inside Sphinx, appearing as raw role text in GitHub's repo browser, IDE previews, and PyPI descriptions; a Python migration script (covering MyST roles, reST roles, and cross-repo prefixed forms) and a matching `sphinx-docs-sync` audit category are included. `docs/myst-docstrings.md` documents the load-order failure mode (backtick-doubled `:py:obj:` roles) when `repomatic.myst_docstrings` loads after `sphinx_autodoc_typehints`.
- Add three `[tool.repomatic.workflow]` configuration knobs for customizing `paths:` filters in generated thin callers and synced headers: `extra-paths` appends repo-specific entries (like `install.sh`, `dotfiles/**`) to every workflow's filter, `ignore-paths` strips canonical entries that don't exist downstream (like `tests/**`, `uv.lock`) by exact match, and `paths` keyed by workflow filename replaces a workflow's filter wholesale (skipping the other knobs and `--source-paths` for that filename).
- Add a Python compatibility matrix to `docs/install.md`, auto-generated from the `Programming Language :: Python` classifiers declared at every `vX.Y.Z` release tag. Newest release ranges sit on top so the supported-versions streak progresses toward the upper-left corner over time. Each row shows the version range and the date of the first release tag in it.
- Replace captured plain-text help blocks in `docs/cli.md` with `{click:run}` directives so Sphinx renders each command's `--help` output live with ANSI colors via `click_extra.sphinx`. The shared `repomatic` symbol is seeded once via a leading `{click:source}` `:hide-source:` block, which persists it to the per-document runner namespace (a bare `import` inside a `{click:run}` block lands in `local_vars` and is discarded after the block).
- Replace the `Type` column in the `docs/configuration.md` summary table with a one-line `Description` derived from each option's docstring. Per-option sections now lead with the one-liner, then `**Type:** | **Default:**`, the full docstring, and an `**Example:**` TOML block pinned to the field's default. The `repomatic show-config` CLI table keeps its existing four columns.
- Bump pinned `uv` from `0.11.6` to `0.11.8` in `renovate.json5` and the bundled template. Drop the lock-file noise-detection layer (`is_lock_diff_only_timestamp_noise`, `revert_lock_if_noise`, `_TIMESTAMP_LINE_RE`, `SyncResult.reverted`) now that [astral-sh/uv#19022](https://github.com/astral-sh/uv/pull/19022) and [astral-sh/uv#19101](https://github.com/astral-sh/uv/pull/19101) make `uv lock` write the `0001-01-01T00:00:00Z` sentinel for relative `exclude-newer` spans, eliminating the per-run timestamp churn.
- Bump `mdformat-pelican` to `1.0.0`, the first release that ships the link-renderer fix from [`gaige/mdformat-pelican` PR #11](https://github.com/gaige/mdformat-pelican/pull/11). Without the fix, plugin loading order on Linux let `mdformat-pelican`'s fallback link renderer win under `mdformat-gfm` 1.0.0, leaving non-ASCII anchor links percent-encoded on every `format-markdown` run.
- Fix generated thin caller workflow fidelity: triggers now mirror the canonical workflow verbatim instead of always injecting `workflow_dispatch`; path filters preserve universal entries (`pyproject.toml`, `renovate.json5`, workflow self-references) instead of being stripped wholesale. `repomatic workflow lint` now flags thin callers with extra triggers absent from the canonical workflow, in addition to the existing check for missing ones.
- Fix `sync-uv-lock` and `fix-vulnerable-deps` PR bodies showing `1-01-01` as the `exclude-newer` cutoff when `pyproject.toml` configures a relative span like `"1 week"`. Recent uv versions write the `0001-01-01T00:00:00Z` sentinel to `options.exclude-newer` in `uv.lock` and store the real value in the new `options.exclude-newer-span` field as an ISO 8601 duration. `parse_lock_exclude_newer` now detects the sentinel and computes the effective cutoff as `now - span`.
- Fix broken documentation links in PR body templates. All 18 templates under `repomatic/templates/` now point at `kdeldycke.github.io/repomatic/configuration.html` and the matching `workflows.html` anchors. Each `### Configuration` block is reshaped from an opaque TOML dump into a sorted bulleted list where every option name links to its own anchor on the configuration page.
- Fix `update-docs` ↔ `format-markdown` ping-pong on `docs/cli.md` and `docs/configuration.md`. In `cli_reference()`, a blank line between the seed `{click:source}` directive's `:hide-source:` option and its source line was rewritten by `mdformat-myst` as a YAML block and stripped back by `_fix_myst_directive_options`, causing alternating insertion and removal. In `config_reference()` and `config_full_descriptions()`, `.replace()` post-processing mangled triple-backtick fences in MyST docstrings.
- Fix `release.yaml` discarding healthy binaries when one matrix cell crashed. The `compile-binaries` matrix now sets `fail-fast: false` and `publish-release` uploads whatever did build by gating on `compile-binaries.result != 'skipped'` instead of `== 'success'`.
- Fix `python_compat_table` test always failing in CI: add `fetch-tags: true` to the `tests.yaml` checkout step so git tags are available when the generator walks release history. Fix `UnicodeDecodeError` on Windows runners where `git show tag:pyproject.toml` output was decoded with `cp1252` instead of `utf-8`, leaving `show.stdout` as `None` and crashing `classifier_re.findall()`.
- Fix mypy error in `Metadata.dump()`: suppress `[misc]` false-positive on the `lambda name=f.name: ...` closure used to bind the loop variable and avoid late-binding pitfalls.
- Fix `Metadata.dump()` eagerly computing every metadata key when `keys` filtered to a small subset. Values are now pulled lazily from a key-keyed factory map, so requesting `("is_python_project", "current_version")` no longer walks git history via `nuitka_matrix` → `current_commit`, removing a `pydriller` `.git/config.lock` race against parallel `pytest-xdist` workers.
- Bump `extra-platforms` floor from `>=8` to `>=12.0.2`. `12.0.0` reimplements `is_macos()` with `sys.platform == "darwin"` instead of `platform.platform()`, which on Windows shelled out to `cmd /c ver` and triggered a CPython 3.10.x bug in `_syscmd_ver` that raised `TypeError` from `_ver_output.match()` on the cache-dir resolution path used by `tool_runner` tests.

## [`6.15.0` (2026-04-27)](https://github.com/kdeldycke/repomatic/compare/v6.14.0...v6.15.0)

> [!NOTE]
> `6.15.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.15.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.15.0).

- Set `validate = false` in the bundled `mdformat.toml` so `mdformat-recover-urls` decodes percent-encoded non-ASCII characters in link destinations back to their original form. Without this, anchor links with Chinese, accented, or other non-ASCII characters got rewritten to `%XX` sequences on every `format-markdown` run.
- Add 💸/🆓 licensing markers to the awesome-list contributing guide, issue template, and PR template (English and Chinese mirrors). Tool, dataset, and project entries are now flagged with 💸 (commercial vendor selling a paid version on top of the OSS core) or 🆓 (fully open-source: foundation-governed, community-driven, corporate-OSS without a paid product, or support-only commercial model). Articles, papers, blog posts, news items, and curation lists remain unmarked. The two markers are mutually exclusive and the awesome-list entry format keeps `awesome-lint`-compatible `-` syntax.
- Detect vulnerable dependencies from the GitHub Advisory Database in addition to the PyPA Advisory Database. The `fix-vulnerable-deps` job now unions `uv audit` and the repository's Dependabot alerts feed, deduplicates by `(package, advisory_id)`, credits each entry's source(s) in the PR body, and shows the `exclude-newer` cooldown cutoff above the updated packages table so reviewers can confirm which upgrades required a cooldown bypass. Configurable via `[tool.repomatic] vulnerable-deps.sources`.
- Add `agents` component to `repomatic init` for deploying Claude Code agent definitions (`.claude/agents/grunt-qa.md`, `.claude/agents/qa-engineer.md`) to downstream repositories. Excluded by default, opt in with `repomatic init agents` or `[tool.repomatic] include = ["agents"]`. The destination is configurable via `[tool.repomatic] agents.location` (default `./.claude/agents/`), mirroring the existing `skills.location` option for repositories where `.claude/` is not at the root (like dotfiles repos). Agents are auto-invoked by Claude based on their `description:` frontmatter, so deploying them gives downstream repos the same QA enforcement loop the upstream repo uses.
- Add `docs/benchmark.md` comparing repomatic against ten alternatives: cookiecutter, semantic-release, copier, cruft, allstar, tf-github, settings, safe-settings, and all-repos. Covers feature parity across template sync, repo governance, release automation, and changelog lifecycle, and surfaces four gaps where alternatives have capabilities repomatic does not yet cover (branch protection management, template conflict resolution, org-wide settings inheritance, and commit-based version computation).
- Expand `myst_docstrings` Sphinx extension: add plain triple-backtick code fence conversion (```` ```python ```` → `.. code-block:: python`), add footnote conversion (`[^label]` references and `[^label]: text` definitions → reST footnote syntax), fix hook priority to guarantee MyST-to-reST conversion runs before `sphinx_autodoc_typehints` (priority 400 vs default 500, with `conf.py` ordering enforced at load time via `ExtensionError`), and align the extension version with the `repomatic` package version.
- Switch MyST admonitions to backtick fences (```` ```{note} ````) instead of colon fences (`:::{note}`) project-wide so that `mdformat` preserves them instead of escaping the colons. The Sphinx extension still recognizes both, and the `convert-to-myst` migration command now emits backtick fences.
- Drop explicit `({slug})=` anchors emitted by the configuration docs generator. The natural heading anchor produced by docutils for `### option.name` already matches the desired `option-name` slug.
- Upgrade lychee to `0.24.1`. The `0.24.x` series adds native `[tool.lychee]` support in `pyproject.toml`, so repomatic skips the TOML translation bridge and lets lychee read its config directly. macOS arm64 and Windows x86_64 asset names changed upstream (now `aarch64-apple-darwin.tar.gz` and `x86_64-pc-windows-msvc.zip`), the Windows download is now a `zip` archive instead of a raw `.exe`, and tarballs now wrap the binary in a per-target directory that the extractor strips.
- Remove unused `sphinx-issues` dependency. No `:issue:`, `:pr:`, `:user:`, or `:commit:` roles were referenced anywhere in the documentation.
- Split `claude.md` into a downstream-portable core and a separate `docs/upstream-development.md` page that holds rules applicable only when developing the `kdeldycke/repomatic` package itself (Documentation sync table referencing `repomatic/github/token.py`, `repomatic/lint_repo.py`, etc., the Tool runner flags-vs-config rules for `TOOL_REGISTRY`, and the upstream release checklist). Sections that remain in `claude.md` but apply only to upstream maintainers are now wrapped in a `{note}` admonition. Generic principles (versioning, naming, testing, idempotency) stay in `claude.md` for use in any repo. `docs/contributing.md` continues to render the full set on the docs site by including both files.
- Fix `update-docs` ↔ `format-markdown` ping-pong on `docs/tool-runner.md`. The `tool_summary()` generator was missing a trailing newline, so the regenerated section had no blank line between the GFM table and the closing `<!-- tool-summary-end -->` HTML comment. `mdformat-gfm` reinserted that blank line on every run, generating an endless stream of "Update docs" PRs.
- Fix `repomatic init` reporting unchanged files as updated. Workflows, skills, the awesome-template tree, and non-reusable workflow headers now compare on-disk content to what would be written and skip the file (without touching its mtime) when the two match. Re-running `repomatic init` against an unchanged tree is now a true no-op.
- Fix mypy failures under `--python-version 3.10` for StrEnum-typed enums by adding `backports-strenum` to the typing dependency group. Without the stubs, StrEnum subclasses leaked `str` instead of the concrete enum type, producing 21 type errors across four source files and two test files. Remove the `# type: ignore` workarounds that masked the bad inference.

## [`6.14.0` (2026-04-20)](https://github.com/kdeldycke/repomatic/compare/v6.13.0...v6.14.0)

> [!NOTE]
> `6.14.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.14.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.14.0).

- Add Sphinx documentation site with Furo theme and MyST-Parser. Splits the monolithic `readme.md` (1139 lines) into focused pages: installation, configuration reference (auto-generated from `Config` dataclass docstrings), CLI parameters (auto-generated via `click_extra.sphinx`), reusable workflow reference, security practices, Claude Code skills, and a tool runner tutorial covering the 4-level config precedence chain, available tools, `[tool.X]` bridge, binary caching, and version overrides. Deployed to GitHub Pages via the existing `docs.yaml` workflow.
- Add `repomatic.myst_docstrings` Sphinx extension and `repomatic.myst_converter` utility module. The extension hooks into `autodoc-process-docstring` to convert MyST markdown in Python docstrings to reST at Sphinx build time, so `sphinx.ext.autodoc` works without modification. Supports colon-fenced and backtick-fenced directives (`:::{note}` and ```` ```{note} ````), cross-references with hyphenated names (like `{eval-rst}`, `{code-block}`), markdown links, and inline code. The converter is a standalone migration utility that rewrites Python source files from reST to MyST in place. Both are idempotent.
- Add `--sort-by` option to `show-config`, `metadata --list-keys`, `run --list`, and `cache show` commands via click-extra `7.11.0`'s `SortByOption`. Each command defaults to a natural sort column and accepts any column name.
- Add incremental mode to the `brand-assets` skill. When base SVGs already exist, skip the design exploration menu and proceed directly: scan existing assets, identify gaps against the four expected variants, create missing SVGs and PNGs, and wire new assets into `docs/conf.py`.
- Add `check_stale_gh_pages_branch` lint check and setup guide instructions for deleting leftover `gh-pages` branches after switching to GitHub Actions deployment.
- Fix `Matrix.prune()` keeping exclude directives that reference keys absent from the matrix axes. GitHub Actions rejects these with "Matrix exclude key does not match any key within the matrix". Affects downstream projects whose `[tool.repomatic.test-matrix]` config defines `variations` and `exclude` entries referencing those axes: the PR matrix (which omits variations) retained the stale excludes.
- Fix setup guide Pages step: treat `None` (unconfigured) as incomplete for Sphinx projects so the issue reopens with the Pages setup step, and add both `POST` (first-time enable) and `PUT` (update existing) commands to the template. Previously, `check_pages_deployment_source` returning `None` was treated as passing, and the template assumed Pages was already enabled.
- Fix `sponsor-label` workflow job missing `actions/checkout` step in `labels.yaml`, causing the job to fail.

## [`6.13.0` (2026-04-15)](https://github.com/kdeldycke/repomatic/compare/v6.12.0...v6.13.0)

> [!NOTE]
> `6.13.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.13.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.13.0).

- Add `nuitka.entry-points` config option to select which `[project.scripts]` entries produce Nuitka binaries. When unset, deduplicates by callable target: keeps the first entry point for each unique `module:callable` pair, so alias entry points (like both `mpm` and `meta-package-manager` pointing to the same function) don't produce duplicate binaries.
- Add two-phase VirusTotal scanning: phase 1 uploads binaries and writes an initial table with scan links (binary names link to their GitHub release download URLs), phase 2 (`--poll`) polls for analysis completion and replaces the table with a Detections column showing `flagged / total` engine counts. The workflow uses two sequential steps so users see scan links immediately while detection stats populate asynchronously.
- Add `av-false-positive` skill to scan release binaries on VirusTotal and generate per-vendor false-positive submission files for flagged artifacts. Derives project metadata (name, license, maintainer, URLs) from `pyproject.toml` and git config instead of hardcoding.
- Add `update-checksums.yaml` workflow that triggers on Renovate pushes to `renovate/**` branches modifying `repomatic/tool_runner.py`. Downloads each binary tool at its new version, computes the SHA-256, and commits corrected checksums to the PR branch. Works around [renovatebot/renovate#42263](https://github.com/renovatebot/renovate/discussions/42263) where `postUpgradeTasks` silently drops changes to the same file the regex manager updated.
- Include release notes for all intermediate versions in `sync-uv-lock` PR bodies. When a package jumps from `11.0.3` to `11.0.5`, both `11.0.4` and `11.0.5` release notes are shown.
- Config `include` entries now bypass `RepoScope` filtering, matching the behavior of explicit CLI component naming. `include = ["skills"]` in a non-awesome repository produces all skills including awesome-only ones; qualified entries like `include = ["skills/awesome-triage"]` implicitly select the parent component. Scope-bypassed components fall through to config-key and file-level checks instead of being unconditionally skipped.
- Add baseline criteria for GitHub repositories in awesome list contributing guidelines: minimum 50 stars, not archived, and updated within 3 years. Includes exceptions for static resources and high-adoption archived repos.
- Add `--min-savings-bytes` option to `format-images` (default 1024 bytes). Skips optimized files where the absolute byte savings are negligible, preventing noisy diffs for tiny images where even a high percentage represents minimal actual reduction.
- Add cross-platform binary support for all tools in the registry. actionlint, biome, gitleaks, labelmaker, lychee, shfmt, and typos now ship binaries for macOS (arm64, x64), Linux (arm64, x64), and Windows (x64) where upstream releases are available. Add ZIP archive extraction and per-platform archive format overrides to `BinarySpec`. Replace hand-rolled OS/arch detection in `_get_platform_key()` with extra-platforms' `current_platform()` and `current_architecture()`, and replace string platform keys with `PlatformKey` tuples (`(Platform | Group, Architecture)`) for richer detection heuristics.
- Add a progress bar during binary tool downloads when the server provides a `Content-Length` header. Displays on interactive terminals only; silent in CI.
- Add two-layer integrity model for cached binaries. The archive checksum from the registry is verified at download time. After extraction, a `.sha256` sidecar file is written next to the binary and checked on every subsequent cache hit, defending against local cache tampering between runs.
- Enable `[tool.actionlint]` bridge support. actionlint config from `pyproject.toml` is now translated to YAML and written to `.github/actionlint.yaml` at invocation time (CWD-discovery, cleaned up after the run).
- Cache downloaded tool binaries across CI runs with `actions/cache`. Each workflow job that invokes a binary tool (actionlint, biome, gitleaks, labelmaker, lychee, shfmt, typos) restores from a per-tool cache key scoped to OS, architecture, and tool registry hash.
- Replace `peaceiris/actions-gh-pages` with GitHub's native `actions/upload-pages-artifact` + `actions/deploy-pages` for Sphinx documentation deployment. Add a `lint-repo` check and setup guide step that verify the repository's Pages source is set to "GitHub Actions" instead of branch-based deployment.
- Add `benchmark-update` skill to create and maintain competitive benchmark pages (`docs/benchmark.md`) comparing projects against alternatives. Supports `audit`, `init`, `add`, and `refresh-badges` modes.
- Add `upstream-audit` skill to create and maintain upstream contribution tracking pages (`docs/upstream.md`). Discovers merged PRs, reported issues, workarounds, and declined features across dependencies. Supports `audit`, `init`, `refresh`, and `sync-git` modes.
- Restructure `Config` to use nested dataclasses (`CacheConfig`, `DependencyGraphConfig`, `DocsConfig`, `GitignoreConfig`, `LabelsConfig`, `TestPlanConfig`, `WorkflowConfig`) with `click_extra.config_path` metadata. Config fields are now accessed as `config.cache.dir` instead of `config.cache_dir`. The `[tool.repomatic]` TOML key structure is unchanged. Eliminates the `_NESTED_PREFIXES` mapping in favor of click-extra's schema-aware dataclass instantiation.
- Upgrade macOS Intel runner from `macos-15-intel` to `macos-26-intel` across binary builds, test matrix, and Nuitka compilation.
- Run `lint-repo` workflow job on all repositories, not just Python projects. Generic checks (funding file, stale draft releases, tag protection rules, fork PR approval policy, workflow permissions, PAT checks) were being skipped on non-Python repos like awesome lists.
- Centralize GitHub token resolution in `run_gh_command`: priority is `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`, with automatic fallback to `GITHUB_TOKEN` on 401 Bad Credentials (expired or revoked PAT). The `--has-pat` flag on `setup-guide` and `lint-repo` auto-detects from the `REPOMATIC_PAT` environment variable when not specified, replacing the `HAS_REPOMATIC_PAT` env var. Normalize all workflow token references to `GH_TOKEN` with `github.token` as the default, and fix missing `GH_TOKEN` in `sync-dev-release`, `sponsor-label`, `setup-guide`, `lint-repo`, `check-renovate`, and `scan-virustotal` workflow steps.
- Fix `exclude-newer-package` handling in `pyproject.toml`: remove orphaned comments when the section is fully pruned, and fix inline table rendering to produce `pyproject-fmt`-compatible formatting (`tomlkit`'s `InlineTable.append()` left malformed whitespace: doubled spaces after commas, missing inner-brace spaces).
- Add pre-flight `shutil.which` check in `run_exiftool` for a clear error when exiftool is not installed, instead of a bare `FileNotFoundError`. Verify exiftool is on PATH after `choco install` in the Windows binary build step, catching silent Chocolatey CDN failures.
- Create parent directories for `--output` file paths in `repomatic run`, fixing lychee `Cannot write status output to file` errors when the output directory doesn't exist.
- Sanitize `@mentions`, `#issue` references, and `github.com` URLs in Lychee and Sphinx linkcheck output before embedding in the broken links GitHub issue body. Reuses the same zero-width space sanitization already applied to upstream release notes in `sync-uv-lock` PR bodies.

## [`6.12.0` (2026-04-13)](https://github.com/kdeldycke/repomatic/compare/v6.11.3...v6.12.0)

> [!NOTE]
> `6.12.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.12.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.12.0).

- Add `repomatic cache` subcommands (`show`, `clean`, `path`) and a global binary cache for downloaded tool executables under `~/Library/Caches/repomatic` on macOS and `~/.cache/repomatic` on Linux. Cached binaries are re-verified against their registry SHA-256 checksum on every use; stale entries are auto-purged after 30 days (configurable via `REPOMATIC_CACHE_MAX_AGE`). Add `--no-cache` to `repomatic run` to bypass the cache.
- Add HTTP response cache for PyPI metadata (24h TTL) and GitHub release bodies (24h for all-releases, 7d for single-release) to avoid redundant API calls across `changelog`, `sync-uv-lock`, and `sync-github-releases` invocations. Add `--namespace` to `repomatic cache clean` for targeted cleanup.
- Route generated tool configs through the cache directory (`~/.cache/repomatic/config/{tool}/{filename}`) instead of `/tmp` or the repository root. Tools with a `--config` flag now receive an explicit `--config <cache_path>` argument, making repomatic's involvement visible in logs and eliminating cleanup issues from interrupted runs. `.editorconfig` serialization for CWD-discovery tools like `shfmt` now translates `[tool.X]` sections from `pyproject.toml` and writes the result to the cache. Tools without a `--config` flag (mdformat, shfmt) remain CWD-discovery exceptions.
- Add `--version`, `--checksum`, and `--skip-checksum` options to `repomatic run` to override the pinned tool version and SHA-256 verification at invocation time.
- Add structured logging to `repomatic run`. `--verbosity INFO` reports the config precedence level, full command line, and exit code. `--verbosity DEBUG` adds the parsed `[tool.X]` content and translated config file details.
- Add `skills.location` config option to override the Claude Code skills directory. Defaults to `./.claude/skills/`; useful for repos where `.claude/` is not at the root.
- Add `changelog.location` config option to override the changelog file path. Defaults to `./changelog.md`. All CLI commands (`changelog`, `release-prep`, `sync-github-releases`, `sync-dev-release`), metadata extraction, and `init` now read from this single source.
- Add `.claude/package-skills.sh` to package each Claude Code skill as a ZIP for manual upload to Claude Desktop via Settings > Customize > Skills. Refs #2540.
- Sanitize `@mentions`, `#issue` references, and `github.com` URLs in upstream release notes embedded in `sync-uv-lock` PR bodies. Inserts Unicode zero-width spaces to prevent GitHub from auto-linking mentions and issue references. Rewrites `github.com` URLs to `redirect.github.com` to prevent backlink cross-references. Code fences and inline code spans are preserved. Closes #2542.
- Use `REPOMATIC_PAT` token in all `peter-evans/create-pull-request` steps across `autofix.yaml`, `changelog.yaml`, and `renovate.yaml`. PRs created with the default `GITHUB_TOKEN` do not trigger other workflows.
- Rename `shell_files` metadata key to `shfmt_files`. Exclude Zsh files (`.zsh`, `.zshrc`, etc.) and `.sh` files with a Zsh shebang from `shfmt` processing. `shfmt`'s Zsh support is experimental (v3.13.0+) and fails on common Zsh constructs (see [mvdan/sh#1203](https://github.com/mvdan/sh/issues/1203)).
- Make the `uv sync` step in the `lint-types` job conditional on `is_python_project`, so repositories with Python files but no lockfile can still be type-checked.
- Fix `format-json` autofix job failing with `flag --config-path is not valid in this context` when a `[tool.biome]` section exists. Biome's bpaf-based CLI scopes `--config-path` inside the subcommand; the flag is now spliced after the subcommand name via a new `config_after_subcommand` field in `ToolSpec`.
- Improve `file-bug-report` skill to check organization-level community health files from the org's `.github` repo before scanning per-repo files. GitHub allows orgs to define default CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md, and issue templates that repos inherit unless they provide their own copy.

## [`6.11.3` (2026-04-09)](https://github.com/kdeldycke/repomatic/compare/v6.11.2...v6.11.3)

> [!NOTE]
> `6.11.3` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.3).

- Add `lint-repo` check warning when the GitHub Actions fork PR workflow approval policy is weaker than `first_time_contributors`. Queries `/repos/{repo}/actions/permissions/fork-pr-contributor-approval` and flags the default `first_time_contributors_new_to_github` setting, which only catches brand-new GitHub accounts. The setup guide includes a new step with a pre-filled `gh api` one-liner to fix the policy in place.
- Add `readme.md` § Supply chain security section mapping Astral's [Open Source Security at Astral](https://astral.sh/blog/open-source-security-at-astral) practices to concrete repomatic implementations, with a known-gap admonition for multi-person release approval.
- Fix `rst_to_myst` conversion leaving RST backslash escapes (`\_`) in headings and not wrapping dotted Python module names in backticks. `sphinx-apidoc` uses `\_` to prevent RST reference interpretation; these are meaningless in markdown and cause `mdformat` to strip them on every reformat pass.
- Fix `format-pyproject` autofix job failing with exit code 123. `xargs` translates `pyproject-fmt`'s exit code 1 (file reformatted) to 123, and the default `bash -e` shell aborted before the exit code guard could run.
- Disable uv cache in the `publish-pypi` release job, which has no checkout step. Without a workspace, `setup-uv` cannot find lockfiles to hash and emits spurious cache-miss warnings on every run.

## [`6.11.2` (2026-04-08)](https://github.com/kdeldycke/repomatic/compare/v6.11.1...v6.11.2)

> [!NOTE]
> `6.11.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.2).

- Add `shfmt` shell formatter to the tool runner (`repomatic run shfmt`). Downloads the binary for the current platform and invokes it with managed version pinning and `.editorconfig` discovery.
- Add `format-shell` autofix job to auto-format shell scripts with `shfmt`.
- Replace `crazy-max/ghaction-virustotal` GitHub Action with a native `repomatic scan-virustotal` CLI command using `vt-py`. Fixes the release body update that was silently skipped because the action required a `release` event but the workflow triggers on `push`.
- Deduplicate release attestations: Python packages are now attested once in `build-package` instead of three times across `build-package`, `publish-pypi`, and `create-release`. The attestation bundle is forwarded as an artifact to `create-release` for upload to the GitHub release. Also fixes `create-release` accidentally attesting `.gitignore` via the `release_artifact/*` glob.

## [`6.11.1` (2026-04-08)](https://github.com/kdeldycke/repomatic/compare/v6.11.0...v6.11.1)

> [!NOTE]
> `6.11.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.1).

- Parallelize release workflow: `compile-binaries` now starts right after `metadata` instead of waiting for `create-release`, and `publish-pypi` runs concurrently with `create-tag` and `create-release`. Binary and attestation uploads to the GitHub release are deferred to `publish-release`. The PyPI admonition in release notes is applied by `publish-release` after confirming PyPI publication succeeded.
- Fall back to PyPI `project_urls` changelog link when no GitHub Release exists for a package. Release notes sections now render a `[Changelog](url)` link instead of silently omitting the package.
- Fix release workflow uploading attestation bundle before the GitHub release draft exists. The upload step now runs after release creation.
- Skip `exclude-newer-package` exemptions for packages whose fixed version already falls within the `exclude-newer` cooldown window. Previously, `fix-vulnerable-deps` persisted `"0 day"` overrides for all upgraded packages unconditionally.
- Fix `--delete-excluded` not detecting scope-excluded `BundledComponent` files that still exist on disk. Component-level scope exclusion (e.g., `codecov` in awesome repos) skipped recording file entries, so stale files were invisible to the deletion pass.
- Fix awesome-template sync overwriting `pyproject.toml` instead of merging. `_copy_template_tree` replaced the entire file with the bundled template, stripping user-managed `[tool.*]` sections (e.g., `[tool.gitleaks]`). The lychee config is now a `ToolConfigComponent` with `AWESOME_ONLY` scope, so it goes through the standard `_init_tool_configs` merge path. `pyproject.toml` is removed from the awesome-template bundle.
- Fix `repomatic init <component>` silently ignoring explicitly-requested components in repos where their scope doesn't match. Scope exclusions now only apply during bare `repomatic init`, matching the existing guard on user-config exclusions. This fixes `repomatic init renovate` failing in awesome repos where the renovate workflow materializes `renovate.json5` at runtime.
- Fix `--delete-excluded` removing opt-in workflow files in the source repo. Config-key exclusions (e.g., `notification.unsubscribe`) now skip the source repo, matching the existing scope-exclusion guard. Previously, the `sync-repomatic` job would delete `.github/workflows/unsubscribe.yaml` from upstream, breaking the symlink in `repomatic/data/` and all downstream `workflow_call` references.
- Fix `format-pyproject` autofix step running with no input files and masking tool errors. The `pyproject_files` metadata key was missing, so pyproject-fmt ran against an empty file list and `|| true` swallowed all non-zero exit codes. The key is now requested and the step tolerates only exit code 1 (file reformatted).

## [`6.11.0` (2026-04-07)](https://github.com/kdeldycke/repomatic/compare/v6.10.0...v6.11.0)

> [!NOTE]
> `6.11.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.0/).

> [!WARNING]
> `6.11.0` is **not available** on 🐙 GitHub.

- Preserve extra downstream jobs when syncing thin-caller workflows. Previously, `repomatic init` and `workflow sync` in `thin-caller` mode would overwrite the entire file, destroying any project-specific jobs appended after the managed caller job (e.g., a Chocolatey publishing step in `release.yaml`). The managed job is now regenerated in place while extra jobs, comments, and blank lines are preserved.
- Add VirusTotal scanning job to the release workflow. Uploads compiled binaries to VirusTotal after each release, seeding AV databases to reduce false positive detections on downstream distributors (Chocolatey, Scoop, etc.). Requires the optional `VIRUSTOTAL_API_KEY` repository secret.
- Add attestation self-verification steps after each `actions/attest` invocation in the release workflow. Each attestation is immediately verified with `gh attestation verify --signer-repo kdeldycke/repomatic` to catch signing issues in CI before users encounter them.
- Upload Sigstore attestation bundles (`.jsonl`) as GitHub release assets for compiled binaries and Python packages, enabling offline verification.
- Add `VIRUSTOTAL_API_KEY` warning to `lint-repo`. Emits a non-fatal warning when the secret is missing and Nuitka binary compilation is active.
- Add VirusTotal API key setup step to the setup guide issue. The step only appears when Nuitka compilation is active (i.e., `nuitka-enabled` is `true` and the project has script entries). When shown, a missing key keeps the setup guide issue open.
- Remove bumpversion dev-versioning migration code. All downstream repos have been migrated to PEP 440 dev versioning; the one-time `.dev0` suffix injection and managed-file update logic is no longer needed.

## [`6.10.0` (2026-04-03)](https://github.com/kdeldycke/repomatic/compare/v6.9.0...v6.10.0)

> [!NOTE]
> `6.10.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.10.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.10.0).

- Add `brand-assets` skill to create and export project logo/banner SVG assets to light/dark PNG variants.
- Add `babysit-ci` skill to monitor CI test workflows, diagnose failures, fix code, and loop until all stable jobs pass.
- Add `file-bug-report` skill to write upstream bug reports. Exhaustively reads contribution guidelines, issue templates, security policies, and community norms before producing a markdown file.
- Add `test-matrix.replace` and `test-matrix.remove` configuration to modify axis values in both full and PR test matrices. `replace` swaps values (e.g., `replace.os = { "ubuntu-slim" = "ubuntu-24.04" }`), `remove` drops values before `solve()` runs. Stale exclude directives referencing removed values are pruned with a warning.
- Add `SyncMode` enum and `preserved_keys` field to `ToolConfigComponent`. Tool configs can now declare `sync_mode=ONGOING` to opt into repeated syncing that preserves local additions. Preserve local `[[tool.bumpversion.files]]` entries during `sync-bumpversion` as the first consumer.
- Add `--output-format [markdown|github-actions]` to `sync-uv-lock`, `fix-vulnerable-deps`, `pr-body`, and `format-images`. Replaces implicit `$GITHUB_OUTPUT` environment variable detection with an explicit flag. Remove `-o` short option from `pr-body` and `format-images` (prefer long-form `--output`).
- Add `.claude/scheduled_tasks.lock` to the default `.gitignore` extra content.
- Append workflow metadata block to issue lifecycle comments (`Condition recurred.`, `Superseded by #N.`, close comments). Each comment now includes a collapsible table with trigger, actor, commit, job, workflow, and run link.
- Make `setup-guide` issue body conditional: each step is a collapsible section with a status indicator (completed steps are collapsed with a checkmark, incomplete steps are expanded with a warning). Add branch ruleset and Dependabot settings checks to the closing criteria. The issue now closes only when PAT, permissions, vulnerability alerts, and branch protection are all verified.
- Rework `sync-uv-lock` CLI output: default to a terminal table via click-extra's `--table-format`, add `--release-notes/--no-release-notes` and `--table/--no-table` flags, move markdown formatting to `--output` for CI use only. Prune stale `exclude-newer-package` entries from `pyproject.toml` before relocking.
- Change `renovate` component to opt-in (`init_default=EXCLUDE`). Exclude `renovate` and `codecov` components from awesome-list repositories.
- Echo `metadata` output to stderr when `--output` targets a file, so computed matrices are visible in CI logs.
- Remove Python `3.15t` (free-threaded) from the default test matrix.
- Warn instead of crashing on unknown `[tool.repomatic]` configuration keys. Old repomatic versions encountering config from a newer release now log a warning and continue.
- Add `repomatic update-docs` CLI command consolidating `sphinx-apidoc`, RST-to-MyST conversion, and `docs/docs_update.py` execution into a single orchestrated step. Projects using MyST-Parser get RST stubs automatically converted to markdown with `{eval-rst}` blocks.
- Add `docs.apidoc-extra-args`, `docs.apidoc-exclude`, and `docs.update-script` configuration options.
- Add `uses_myst` metadata property detecting MyST-Parser in Sphinx configuration.
- Move `sync-uv-lock` job from `renovate.yaml` to `autofix.yaml`. The renovate workflow's path filter prevented the job from running on most pushes to `main`, leaving stale PRs with merge conflicts.
- Fix CLI crash when `test-matrix.variations` or `test-matrix.replace` contain nested keys. Click-extra's generic config flattening was converting them to invalid field names. The CLI now routes config loading through `load_repomatic_config` which preserves the `test-matrix` sub-section structure.

## [`6.9.0` (2026-03-31)](https://github.com/kdeldycke/repomatic/compare/v6.8.0...v6.9.0)

> [!NOTE]
> `6.9.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.9.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.9.0).

- Add per-project test matrix configuration via `[tool.repomatic.test-matrix]` in `pyproject.toml`. Supports `exclude`, `include`, and `variations` fields that map directly to the `Matrix` class API.
- Replace `audit-deps` lint job with `fix-vulnerable-deps` autofix job. Creates PRs that upgrade affected packages using `uv lock --upgrade-package` with `--exclude-newer-package` bypass for security fixes.
- Add `codecov` bundled component. Syncs `.github/codecov.yaml` to suppress PR comments unless coverage drops or the diff introduces uncovered lines.
- Extend tool runner config resolution to support CWD-discovery tools (no `--config` flag). Bundled defaults are written to disk and cleaned up after invocation. Move mdformat `number` default to a bundled `mdformat.toml` so downstream repos can override via `[tool.mdformat]` or `.mdformat.toml`.
- Replace dict-based config loading with click-extra's typed config resolution. `[tool.repomatic]` is auto-discovered from `pyproject.toml` and exposed as a typed `Config` dataclass via `get_tool_config()`. Rename `config` subcommand to `show-config` to resolve a naming conflict with click-extra's `--config` option.
- Expand PAT validation in `lint-repo` and `check-renovate`. `lint-repo` gains repository scope check, tag ruleset detection, and workflows permission probe. `check-renovate` validates all PAT permissions (contents, issues, pull requests, vulnerability alerts, workflows) as warnings.
- Auto-exclude `changelog.md` for awesome-list repositories and remove redundant `[tool.repomatic]` exclude entries from the awesome template. Refactor the component registry to support `scope`, `target`, and `config_key` fields, replacing hardcoded dispatch with a type-driven loop. `init` CLI help text is now auto-generated from the registry.
- Remove `prebake-version` and `prebake-tag-sha` CLI commands. Use `click-extra prebake all`, `click-extra prebake version`, or `click-extra prebake field` instead. Rename `__tag_sha__` to `__git_tag_sha__` to align with click-extra's `git_*` naming convention.
- Migrate from `actions/attest-build-provenance` to `actions/attest`.
- Rename `pr-metadata.md` template to `.md.noformat` extension to prevent mdformat from mangling the `$rerun_row` table-row placeholder.
- Skip Codecov uploads on `sync-repomatic` PRs.
- Unify PAT permission validation into `check_all_pat_permissions()` in `token.py`. `lint-repo`, `setup-guide`, and `check-renovate` all use the same code path. `setup-guide` now runs granular permission checks when the PAT is present and keeps the issue open with a diagnostic table when permissions are incomplete.
- Fix `setup-guide` job: add missing `HAS_REPOMATIC_PAT` env var and remove upstream repo carve-out so PAT detection works everywhere.
- Fix infinite cycle between `migrate-to-renovate` and `sync-repomatic` jobs.
- Include git stderr in `git-tag` CLI error messages.

## [`6.8.0` (2026-03-27)](https://github.com/kdeldycke/repomatic/compare/v6.7.0...v6.8.0)

> [!NOTE]
> `6.8.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.8.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.8.0).

- Move test matrix definition from inline YAML to `repomatic metadata` using the `Matrix` class. The `tests` job now depends on the `metadata` job and consumes pre-computed `test_matrix` / `test_matrix_pr` outputs. Fixes the `matrix` context being unavailable in job-level `if:`.
- Reduce CI jobs on pull requests by skipping release builds, experimental Python versions, redundant architecture variants, and install/architecture verification tests. Full matrix still runs on push to main.
- Make `exclude` config additive to default exclusions (`labels`, `skills`). User `exclude` entries now add to the defaults instead of replacing them. Add `include` config to force-include components that are excluded by default.
- Add `--delete-unmodified` flag to `repomatic init` to automatically remove config files identical to bundled defaults instead of only reporting them. Renamed from `--delete-redundant`.
- Remove deprecated `WORKFLOW_UPDATE_GITHUB_PAT` secret and all backward-compatibility fallbacks. Downstream repos must use `REPOMATIC_PAT`.
- Auto-exclude `awesome-triage` skill for non-awesome repositories. The skill is only relevant to `awesome-*` repos and was previously installed in all downstream projects.
- Add `--delete-excluded` flag to `repomatic init` to remove excluded files that still exist on disk. Detects component-level exclusions, file-level exclusions, auto-excluded files (e.g., `awesome-triage` on non-awesome repos), and disabled opt-in workflows.
- Replace `sync-workflows` and `clean-unmodified-configs` autofix jobs with a single `sync-repomatic` job. Runs `repomatic init --delete-unmodified --delete-excluded` to sync all managed files and clean up stale ones in one PR. Renamed from `clean-redundant-configs` and `--delete-redundant`.
- Remove `PAT setup hint` steps and `HAS_REPOMATIC_PAT` env var from `autofix.yaml` and `changelog.yaml` workflows. The `setup-guide` job already creates an issue when the PAT is missing.
- Add PAT capability checks and repo configuration validation to `lint-repo`. Checks Renovate config exists, Dependabot security updates disabled, and vulnerability alerts enabled. When `REPOMATIC_PAT` is configured, validates contents, issues, pull requests, Dependabot alerts, and commit statuses permissions. Add `REQUIRED_PAT_PERMISSIONS` constant in `token.py` as the single source of truth for expected permissions.
- Add stale draft release detection to `lint-repo`. Warns about draft releases whose tag does not end with `.dev0`, which are leftovers from abandoned or failed release attempts.
- Relax abandoned dependency threshold from 1 year to 2 years in Renovate config.
- Fix thin-caller generation rendering `workflow_dispatch` input definitions as Python dict literals instead of block-style YAML.
- Add `/sphinx-docs-sync` skill for cross-project Sphinx documentation comparison and synchronization.
- Add `/translation-sync` skill to detect stale translations in `readme.*.md` and `contributing.*.md` files. Compares structure and content against the English source, flags drift, and drafts updated translations. Auto-excluded for non-awesome repos.
- Streamline Dependabot guidance in setup guide issue. Clarify that Renovate handles security PRs and that `renovate.yaml` auto-removes `dependabot.yml`. Link Actions tab directly to the `autofix.yaml` workflow.
- Allow `repomatic init` to accept qualified `component/file` selectors (e.g., `repomatic init skills/repomatic-topics`). Uses the same syntax as the `exclude` config option. Rename `parse_exclude` to `parse_component_entries` and `_valid_file_ids` to `valid_file_ids` to support the shared parsing path.
- Only auto-include `awesome-template` component for `awesome-*` repos when no explicit components are given. Previously, `repomatic init skills/repomatic-topics` on an awesome repo would also sync all awesome-template files.
- Stop persisting `[tool.ruff]` defaults into downstream `pyproject.toml`. Bundled ruff config is now injected at runtime via `--config` when no `[tool.ruff]`, `ruff.toml`, or `.ruff.toml` exists. Remove the `repomatic init ruff` step from the `format-python` workflow job.
- Add package version diff table to `sync-uv-lock` PRs. Lists updated, added, and removed packages with their old and new versions. Package names link to PyPI. Shows release date and `exclude-newer` cutoff for visual verification. Includes collapsible release notes fetched from GitHub Releases.
- Document file naming conventions in `claude.md`: prefer `.yaml` over `.yml` and lowercase filenames everywhere, with a table of GitHub exceptions that require `.yml` or uppercase.
- Fix awesome-template URL rewriting to also process `.yml` files in `.github/`.
- Auto-exclude `changelog.yaml`, `debug.yaml`, and `release.yaml` workflows for `awesome-*` repositories. These package-oriented workflows are irrelevant to curated lists.
- Materialize bundled `renovate.json5` at runtime in the Renovate workflow when the file is absent. Downstream repos can safely delete their `renovate.json5` via `clean-redundant-configs` without breaking Renovate runs.
- Remove `sync-renovate` CLI command, autofix job, `renovate.sync` config toggle, and PR body template. The `sync-repomatic` job (via `repomatic init --delete-redundant`) already handles Renovate config sync, and the Renovate workflow now materializes the bundled default at runtime when the file is absent.
- Remove `sync-bundled-config` job from `renovate.yaml`. The bundled `repomatic/data/renovate.json5` is now regenerated by `repomatic init` when it detects it's running in the source repository, folded into the `sync-repomatic` autofix job.
- Remove upstream exclusion from `sync-repomatic` job. The upstream repo's `[tool.repomatic] exclude = ["workflows"]` prevents thin-caller generation, while `repomatic init` handles the bundled renovate config regeneration.
- Pin GitHub Actions to SHA digests via Renovate's `helpers:pinGitHubActionDigestsToSemver` preset. Prevents supply chain attacks from mutable tags while keeping automated version updates.
- Add top-level `permissions: {}` to all workflow files. Denies all `GITHUB_TOKEN` permissions by default, requiring each job to declare its own minimal permissions explicitly.
- Merge `/repomatic-deps-review` into `/repomatic-deps`. The unified skill now supports two modes: `graph` (dependency tree visualization, previously the only mode) and `review` (declaration audit against version policy). Also checks for stale `exclude-newer-package` cooldown exceptions in `[tool.uv]`.
- Consolidate 12+ scattered init constants into a declarative component registry (`repomatic/registry.py`). Each component declares its kind, init default, file entries, and behavioral flags in one place. All derived constants (`ALL_COMPONENTS`, `COMPONENT_FILES`, `REUSABLE_WORKFLOWS`, `SKILL_PHASES`, etc.) are computed from the registry.
- Fix `sync-repomatic` deleting the upstream repo's own skills. The source repo guard only covered auto-exclusions, not default exclusions (`labels`, `skills`).
- Rename "redundant" terminology to "unmodified" across the CLI, output, and codebase. `--delete-redundant` becomes `--delete-unmodified`, `clean-redundant-configs` becomes `clean-unmodified-configs`.
- Generalize `opt_in_key` into `config_key`/`config_default` on both `FileEntry` and `Component`. Replace the procedural `awesome-template.sync` check with a registry declaration.

## [`6.7.0` (2026-03-24)](https://github.com/kdeldycke/repomatic/compare/v6.6.0...v6.7.0)

> [!NOTE]
> `6.7.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.7.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.7.0).

- Bundle awesome-template boilerplate files in `repomatic` instead of cloning from `kdeldycke/awesome-template` at runtime.
- Add `pyproject_files` metadata key listing all `pyproject.toml` files. The `format-pyproject` job now formats all of them instead of only the root file.
- Remove `sync-skills`, `workflow create`, and `workflow sync` CLI commands. All three are superseded by `repomatic init` which handles skills, thin-caller generation, and header-only sync in a single invocation.
- Add branch protection checklist to the setup guide issue. Links to a pre-filled ruleset creation form targeting the default branch with force-push and deletion protection.
- Add `unsubscribe.yaml` reusable workflow for scheduled cleanup of closed notification threads. Opt-in via `notification.unsubscribe = true` in `[tool.repomatic]`. Requires a classic PAT with `notifications` scope stored as `REPOMATIC_NOTIFICATIONS_PAT`. Introduce `OPT_IN_WORKFLOWS` mechanism for workflows that are excluded from thin-caller generation unless explicitly enabled.
- Surface actual `gh` CLI error messages in `unsubscribe-threads` warnings. Previously, `RuntimeError` details from failed API calls were discarded, making Phase 1 REST failures impossible to diagnose from CI logs.
- Enable `delete-branch: true` on all `peter-evans/create-pull-request` invocations. Stale automation PRs are now auto-closed when a subsequent workflow run finds no changes to commit.
- Add `gitleaks` to the tool runner registry with binary download support and `[tool.gitleaks]` configuration bridge. Migrate `lint-secrets` workflow job from `gitleaks/gitleaks-action` to `repomatic run gitleaks`.
- Move lychee config from `lychee.toml` to `[tool.lychee]` in `pyproject.toml`. The tool runner's TOML bridge translates it to a native config file at runtime. Downstream repos with a standalone `lychee.toml` can do the same.
- Fix `format-images` job failing because `oxipng` is not packaged in Ubuntu. Install from the GitHub release `.deb` instead, allowing the job to run on `ubuntu-slim`.

## [`6.6.0` (2026-03-23)](https://github.com/kdeldycke/repomatic/compare/v6.5.0...v6.6.0)

> [!NOTE]
> `6.6.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.6.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.6.0).

- Remove `yamllint` and `zizmor` init components. The tool runner already falls back to bundled default configs at runtime, so copying them into downstream repos was redundant. Default `exclude` reduced to `["labels", "skills"]`. Downstream repos with `yamllint` or `zizmor` in their `[tool.repomatic] exclude` list must remove those entries.
- Add `repomatic clean-redundant-configs` command and autofix workflow job. Detects native config files (`.yamllint.yaml`, `zizmor.yaml`, etc.) that are identical to bundled defaults and removes them. `repomatic init` now warns about redundant configs found on disk.
- Rename `WORKFLOW_UPDATE_GITHUB_PAT` secret to `REPOMATIC_PAT`. Workflows accept both names for backward compatibility. Downstream repos with the old name get a migration issue prompting them to rename; the issue closes automatically once `REPOMATIC_PAT` is detected. Add `setup-guide` config toggle to `[tool.repomatic]` to suppress the setup guide issue entirely. Pre-fill the fine-grained PAT creation form via URL parameters and provide `gh` CLI commands for adding the secret, configuring Dependabot, and triggering a verify run. Skip the immutable releases setup step for repositories without a `changelog.md`.
- Close orphaned setup guide issues that still reference the old `WORKFLOW_UPDATE_GITHUB_PAT` title. The title change to `REPOMATIC_PAT` made old issues invisible to the exact-title matching in `manage_issue_lifecycle`.
- Add `lint-repo` check that warns when the repository owner has GitHub Sponsors enabled but `.github/FUNDING.yml` is missing. Skips forks and owners without a Sponsors listing.
- Drop `--aggressive` from autopep8 flags. It naively wraps lines inside f-string expressions, producing Python 3.12+-only syntax (PEP 701) that breaks on Python 3.10/3.11.
- Surface actual `gh` CLI error messages in `unsubscribe-threads` warnings. Previously, `RuntimeError` details from failed API calls were discarded, making Phase 1 REST failures impossible to diagnose from CI logs.

## [`6.5.0` (2026-03-23)](https://github.com/kdeldycke/repomatic/compare/v6.4.1...v6.5.0)

> [!NOTE]
> `6.5.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.5.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.5.0).

- Replace `init.exclude` and `workflow.sync-exclude` config keys with a unified `exclude` key. Bare names exclude entire components (e.g., `"skills"`); qualified `component/identifier` entries exclude specific files (e.g., `"workflows/debug.yaml"`, `"skills/repomatic-audit"`, `"labels/labeller-content-based.yaml"`). Old keys are no longer recognized and will produce a hard error.
- Remove legacy `[tool.gha-utils]` and `[tool.repokit]` config section migration. Projects still using old section names must manually rename to `[tool.repomatic]`.
- Remove automatic cleanup of legacy `.github/zizmor.{yml,yaml}` files and `.claude/skills/gha-*/` directories during init.
- Add `repomatic run <tool>` command for unified tool invocation with managed config resolution. Resolves config through a 4-level precedence chain: native config file, `[tool.X]` in `pyproject.toml`, bundled default, bare invocation. Use `--list` to see all managed tools and their active config source.
- Add `yamllint` init component with bundled default config (`data/yamllint.yaml`). Excluded from init by default like `zizmor`.
- Add 13 tools to the `repomatic run` registry: actionlint, autopep8, biome, bump-my-version, labelmaker, lychee, mdformat, mypy, pyproject-fmt, ruff, typos, yamllint, zizmor. Includes `computed_params` support for tools that derive CLI args from project metadata (e.g., mypy's `--python-version` from `requires-python`).
- Add binary download infrastructure for tools distributed as platform binaries (actionlint, biome, labelmaker, lychee, typos). Downloads are SHA-256 verified with streaming checksums. `sync-labels` downloads labelmaker on-demand via `binary_tool_context()`.
- Migrate all workflow tool invocations to `repomatic run`, removing `uv tool install`, `curl`/`sha256sum`/`tar` install steps, and inline version pins from workflow YAML.
- Add `repomatic update-checksums --registry` to update SHA-256 hashes in the tool runner registry.
- Update Renovate custom managers to target the tool runner registry for binary tool versions.
- Add TOML and JSON serialization for `[tool.X]` config translation. Downstream repos can now configure lychee via `[tool.lychee]` and biome via `[tool.biome]` in `pyproject.toml` without creating separate config files.

## [`6.4.1` (2026-03-11)](https://github.com/kdeldycke/repomatic/compare/v6.4.0...v6.4.1)

> [!NOTE]
> `6.4.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.4.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.4.1).

- Add `github-json` output dialect for `repomatic metadata`. Bundles all metadata keys into a single `metadata` JSON output, eliminating manual per-key `outputs:` declarations in GitHub Actions workflows. Downstream jobs access values via `fromJSON(needs.metadata.outputs.metadata).key_name`.
- Migrate all workflow `metadata` jobs to `--format github-json` with explicit key filtering, reducing each job's `outputs:` section to a single static line.
- Rename `project-metadata` job and step IDs to `metadata` in all workflows, shortening downstream expressions.
- Add key filtering to `repomatic metadata`. Pass key names as arguments to output only specific metadata values.
- Add `--list-keys` flag to `repomatic metadata` to display all available keys with descriptions.
- Rename `linters` init component to `zizmor`. Default `init.exclude` updated to `["labels", "skills", "zizmor"]`.
- Remove `sync-zizmor` autofix job, CLI command, and `zizmor.sync` config toggle. `zizmor.yaml` is now user-owned: `repomatic init zizmor` creates a default if missing, the lint workflow generates an ephemeral default at CI time, and downstream repos have full control of their config without overwrite.
- Rename `bump-versions` job to `bump-version` in `changelog.yaml`, matching the PR body template name.
- Upgrade zizmor from `1.22.0` to `1.23.0`. Re-enable the `template-injection` audit now that the multiline expression crash is fixed upstream ([zizmor#1669](https://github.com/zizmorcore/zizmor/issues/1669)).
- Fix `github-json` metadata dialect serializing list values as JSON arrays. GitHub Actions stringifies arrays as the literal word "Array" when interpolated in `${{ }}` expressions, breaking jobs that pass file lists, `cli_scripts`, or `build_targets` to shell commands or environment variables. All list/tuple metadata values are now pre-formatted via `format_github_value()`: file lists become space-separated quoted strings, plain string lists become space-separated unquoted strings, and dict lists become JSON strings (enabling the double `fromJSON()` pattern for matrix inclusion).
- Fix YAML line-length violation in `docs.yaml` workflow template.
- Fix `repomatic workflow sync --format header-only` failing when a target workflow file does not exist in the downstream repo. Missing files in the default set are now silently filtered out, and explicitly named missing files produce a warning instead of an error.
- Remove unused `pytest-cases` test dependency.
- Add `pytest-xdist` test dependency and enable parallel test execution by default via `--numprocesses=auto` in `addopts`.

## [`6.4.0` (2026-03-10)](https://github.com/kdeldycke/repomatic/compare/v6.3.2...v6.4.0)

> [!NOTE]
> `6.4.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.4.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.4.0).

- Rename `optimize-images` to `format-images`. Image lossless optimization is a format operation (semantics-preserving rewrite using external tools), aligning it with the `format-*` naming convention.
- Add `format-images` PR body template and use `--template format-images` in the workflow, aligning with the standard format job pattern.
- Allow `--prefix` and `--template` to be combined in `repomatic pr-body`. When both are given, the prefix is prepended before the rendered template content.
- Add `uv-lock-sync` toggle to `[tool.repomatic]`. Projects can now disable `sync-uv-lock` by setting `uv-lock-sync = false`.
- Add `dev-release-sync` toggle to `[tool.repomatic]`. Projects can now disable `sync-dev-release` by setting `dev-release-sync = false`.
- Rename `sync-linter-configs` to `sync-zizmor` and `linter-sync` toggle to `zizmor-sync`. Sync jobs are named after the specific tool they sync, not a generic category.
- Add `awesome-template-sync`, `bumpversion-sync`, `gitignore-sync`, `labels-sync`, `zizmor-sync`, and `mailmap-sync` toggles to `[tool.repomatic]`. All sync operations can now be individually disabled.
- Add `repomatic sync-labels` CLI command wrapping `labelmaker` invocation with toggle check, profile detection, and extra label file handling.
- Replace `AndreasAugustin/actions-template-sync` with native `repomatic sync-awesome-template` CLI command. Removes a third-party action dependency and aligns the job with the standard sync pattern.
- Centralize all CI context reads (repository slug, server URL, SHA, run ID, actor, etc.) into `Metadata` cached properties. Modules no longer read `GITHUB_*` environment variables directly.
- Remove `--include-package-data=extra_platforms` from Nuitka extra args. `extra-platforms` 11.0.0 has no non-Python data files — all `*_data` modules are regular imports traced by Nuitka automatically.
- Make `Metadata` a singleton: every `Metadata()` call returns the same instance within a process, eliminating redundant instantiation across the codebase.
- Remove `get_repo_slug()` from `gh.py` in favor of `Metadata.repo_slug`.
- Skip `init ruff` config injection in `format-python` job for non-Python projects. Doc-only repos use Ruff for code-block formatting without needing a `[tool.ruff]` section.
- Add `repomatic init typos` to sync shared typos spell checker configuration into `pyproject.toml`. Includes proper noun corrections and `<!-- typos:off -->` / `<!-- typos:on -->` block markers for section-level suppression.
- Use TOML sub-keys for all grouped options in `[tool.repomatic]`. Fields sharing a feature area are now nested under a common prefix (e.g., `nuitka.enabled`, `gitignore.location`, `test-plan.file`, `labels.extra-files`, `workflow.sync-exclude`). Only `pypi-package-history` remains flat.
- Add `workflow-source-paths` option to `[tool.repomatic]`. Thin-caller and header-only workflows now include `paths:` filters adapted for the downstream project's source directory, reducing unnecessary CI runs. Auto-derived from `[project.name]` when not explicitly configured.
- Add `repomatic config` command that renders the `[tool.repomatic]` configuration reference table from `Config` dataclass docstrings.
- Add `### Configuration` sections to PR body templates listing relevant `[tool.repomatic]` options.

## [`6.3.2` (2026-03-08)](https://github.com/kdeldycke/repomatic/compare/v6.3.1...v6.3.2)

> [!NOTE]
> `6.3.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.2).

- Add `--all-extras` to the `uv sync` step in `tests.yaml` to catch incompatibilities between optional dependency groups. No-op for projects with no extras.
- Add `test-package-install` job to `tests.yaml`: verifies all `[project.scripts]` entry points install and run correctly via `uvx`, `uv run --with`, module invocation, `uv tool install`, and `pipx run`, from both PyPI and GitHub. Runs once on a single stable OS/Python instead of repeating across the full matrix. Add `cli_scripts` output to `repomatic metadata`.
- Sync `customManagers` to downstream `renovate.json5` so Renovate can update inline version pins in workflow files (Python packages in `uvx`/`uv pip` commands, binary tool versions in download URLs, npm packages). Only the self-referencing uv entry (which targets `renovate.json5` itself) is excluded to prevent an endless sync loop.
- Fix `generate_thin_caller()` stripping `paths` and `paths-ignore` filters from generated thin callers. Canonical workflow paths reference the repomatic source tree and would incorrectly restrict CI triggers in downstream repos.
- Auto-remove legacy `.github/zizmor.yml` and `.github/zizmor.yaml` during `repomatic init` and `sync-linter-configs`, completing the migration to root-level `zizmor.yaml`.
- Fix `optimize-images` job failing on `ubuntu-slim` because `oxipng` is not available there.
- Add `date-released` update entry for `citation.cff` to the bundled `bumpversion.toml` template so downstream repos keep their release date in sync during version bumps.
- Reduce PR noise with more specific uv version package update regex.
- Skip `sync-bumpversion` job for non-Python projects by gating on `is_python_project` metadata.

## [`6.3.1` (2026-03-07)](https://github.com/kdeldycke/repomatic/compare/v6.3.0...v6.3.1)

> [!NOTE]
> `6.3.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.1).

- Register `repomatic-audit` skill in `COMPONENT_FILES`, data symlinks, and `_SKILL_PHASES` so it syncs to downstream repos.
- Remove stale `repomatic-metadata` entry from `_SKILL_PHASES`.
- Add `test_skills_consistency` test to catch mismatches between `.claude/skills/` directories and their registrations in code.

## [`6.3.0` (2026-03-06)](https://github.com/kdeldycke/repomatic/compare/v6.2.1...v6.3.0)

> [!NOTE]
> `6.3.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.0).

- Make `repomatic init` always overwrite managed files (workflows, configs, skills) by default. Remove the `--overwrite` flag. `changelog.md` is never overwritten once it exists.
- Remove `extend-include` for Markdown files now that ruff 0.15.5 discovers `*.md` by default in preview mode.
- Use short SHA in release workflow job names instead of the full 40-character commit hash.
- Make `repomatic init` output contextual: distinguish created vs updated vs skipped files, warn about excluded files still on disk, and simplify next steps to point to the `setup-guide` workflow.
- Auto-remove legacy `.claude/skills/gha-*/` skill directories during `repomatic init`, completing the `gha-utils` → `repomatic` rename migration.
- Make `sync-bumpversion`, `sync-linter-configs`, and `sync-skills` CLI commands report both created and updated files.
- Fix all new ruff `0.15.5` preview violations.
- Add `LOG015` to ruff ignore list: root logger calls (`logging.info()`, etc.) are intentional since the CLI configures the root logger via Click.
- Refactor `sync-bumpversion` to replace the entire `[tool.bumpversion]` section from the bundled template instead of applying incremental migrations. Remove legacy backtick migration code.
- Add parametrized sync tests to catch drift between all bundled data files and repomatic's own config: template TOML settings vs `pyproject.toml` (ruff, mypy, pytest, bumpversion), zizmor vs root config, and a loadability check for all registered data files (no dangling symlinks).

## [`6.2.1` (2026-03-06)](https://github.com/kdeldycke/repomatic/compare/v6.2.0...v6.2.1)

> [!NOTE]
> `6.2.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.2.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.2.1).

- Fix `actions/checkout` wiping downloaded Python package artifacts (`.whl`, `.tar.gz`) before `gh release create` could attach them. Move checkout before artifact download so the release draft includes the Python distribution files.
- Fix `fix-changelog` marking releases as "not available on GitHub" when the GitHub release is still a draft. Move the job from `autofix.yaml` to `changelog.yaml` which has a `workflow_run` trigger that fires after "Build & release" completes, when the release is published and visible to the public API.

## [`6.2.0` (2026-03-05)](https://github.com/kdeldycke/repomatic/compare/v6.1.0...v6.2.0)

> [!NOTE]
> `6.2.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.2.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.2.0).

- Extract hand-written GFM alert blocks (editorial admonitions) from changelog entries.
- Fix Nuitka-compiled binaries silently producing no output when the entry point is a `__main__.py` inside a package. Auto-detect this pattern and pass the package directory with `--python-flag=-m` instead of the file path.
- Consolidate documentation.
- Trim verbose workflow YAML comments that are now documented in Python code, replacing them with brief pointers to the relevant module.
- Add knowledge placement guide to `CLAUDE.md` with audience-based routing table and YAML→Python distillation pattern.
- Add misplaced knowledge detection to QA agent checklists.
- Add `rename-from` rules to migrate all 9 default GitHub labels.
- Add `init-exclude` config option to skip components during `repomatic init`. Extend `workflow-sync-exclude` to also apply to `repomatic init` workflows. Default `init-exclude` to `["labels", "linters", "skills"]` since these files are regenerated by workflows at execution time.
- Pre-compute availability admonition in `release_notes_with_admonition` metadata output so GitHub releases include PyPI and GitHub links at creation time, without waiting for the `fix-changelog` + `sync-github-releases` round-trip.
- Support GitHub immutable releases by restructuring the release workflow to draft-then-publish.
- Add `repomatic-topics` skill for optimizing GitHub repository topics for discoverability.
- Add `lint-repo` check that warns when GitHub topics are not a subset of `pyproject.toml` keywords.
- Replace `calibreapp/image-actions` with `repomatic optimize-images` CLI command using lossless `oxipng` (PNG) and `jpegoptim` (JPEG). Exclude WebP and AVIF for lack of stability.
- Use GFM admonitions (`> [!WARNING]`) instead of emoji-prefixed blockquotes in the setup guide template.
- Add `sync-dev-release` CLI command and workflow job to maintain a rolling dev pre-release on GitHub with latest binaries and Python package.
- Create dev pre-releases as drafts to stay compatible with GitHub immutable releases. Clean up all stale `.dev0` releases on each sync, not just the current version's.
- Preserve dev release assets (compiled binaries) across documentation-only pushes by editing the existing release instead of deleting and recreating it.
- Automatically migrate `[tool.gha-utils]` and `[tool.repokit]` config sections to `[tool.repomatic]` during `repomatic init`. All commands fall back to legacy section names when `[tool.repomatic]` is absent.
- Add version to compiled binary filenames (`repomatic-6.2.0-linux-arm64.bin` instead of `repomatic-linux-arm64.bin`), matching Python package naming conventions.
- Freeze readme binary download URLs during releases, replacing `/releases/latest/download/` with versioned `/releases/download/vX.Y.Z/` paths.
- Replace `softprops/action-gh-release` with `gh release create` in the release workflow. All release operations now use the `gh` CLI.
- Move dev release asset upload from shell script into `sync-dev-release` CLI command with `--upload-assets` option, replacing the workflow's inline script.
- Fix `update-checksums` skipping multi-line `echo ... \` + `| sha256sum --check` patterns where the hash and `sha256sum` keyword are on different lines. This caused Renovate `postUpgradeTasks` to silently leave stale SHA-256 hashes after version bumps.
- Force native ARM64 Python on Windows ARM64 test runners via `UV_PYTHON` to work around [uv defaulting to x86_64 emulation](https://github.com/astral-sh/uv/issues/12906). Fixes `test-results-parser` (from `codecov-cli`) failing to compile its Rust extension under cross-architecture emulation.
- Fix `skip_binary_build` test expecting `False` in CI push events where only non-binary-affecting files changed.
- Fix `fix-changelog` and `freeze_file` producing a trailing blank line when the last changelog section is modified. Both write sites now normalize to exactly one trailing newline.

## [`6.1.0` (2026-02-27)](https://github.com/kdeldycke/repomatic/compare/v6.0.1...v6.1.0)

> [!NOTE]
> `6.1.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.1.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.1.0).

- Add `unsubscribe-threads` CLI command to unsubscribe from closed, inactive GitHub notification threads.
- Generate thin caller workflows with explicit secret forwarding instead of `secrets: inherit`, eliminating zizmor's `secrets-inherit` warnings in downstream repositories.
- Move zizmor config from `.github/zizmor.yml` to `zizmor.yaml` at repo root for visibility and consistency with other root-level config files.
- Centralize PAT existence check via workflow-level `env` block, replacing duplicated `secrets.*` ternary expressions in PAT hint steps and the `setup-guide` invocation.
- Restrict binary compilation to the HEAD commit for non-release pushes, avoiding redundant Nuitka builds across all commits in multi-commit push events.
- Add `prebake-version` CLI command to inject Git commit hash into `__version__` before Nuitka compilation. Standalone binaries now report the exact commit they were built from (e.g., `6.1.0.dev0+abc1234`).
- Pre-bake dev versions automatically in the `compile-binaries` workflow job before Nuitka runs.
- Detect orphaned versions in `lint-changelog`: versions that exist as git tags, GitHub releases, or PyPI packages but have no changelog entry. In `--fix` mode, insert placeholder sections with correct comparison URLs.
- Rename `lint-changelog` workflow job to `fix-changelog` since it always runs with `--fix`. The CLI command remains `lint-changelog`.
- Group CLI commands into sections (Project setup, Release & versioning, Sync, Linting & checks, GitHub issues & PRs) for better discoverability in help output.
- Add `list-skills` CLI command to display all available Claude Code skills grouped by lifecycle phase (Setup, Development, Quality, Release).
- Add next-step handoff suggestions to all Claude Code skills, creating a guided workflow where each skill suggests what to run next.
- Document skills with a grouped table, recommended workflow sequence, and walkthrough example in `readme.md`.
- Add `pypi-package-history` config option for renamed projects. `lint-changelog` now fetches releases from former package names and generates correct PyPI URLs for each version.
- Add `sync-github-releases` CLI command to sync GitHub release notes from `changelog.md`.
- Make changelog entry and GitHub release body assembly template-driven. `release-notes.md` controls `changelog.md` layout (including the `##` version heading), `github-releases.md` controls GitHub release bodies. Editing one template affects only its destination.
- Converge `freeze()` and `update()` onto the same decompose→render→replace pattern used by `lint_changelog_dates()`, with the `release-notes.md` template as the single source of truth for section layout.
- Only produce XML coverage and JUnit XML output when running `pytest` in the CI workflow.

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
