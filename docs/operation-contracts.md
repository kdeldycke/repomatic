# {octicon}`tasklist` Automated operation contracts

> Referenced from `claude.md` [§ Automated operation contracts](https://github.com/kdeldycke/repomatic/blob/main/claude.md#automated-operation-contracts). This file contains the detailed checklists for each operation type.

## Sync job contract

Every `sync-*` operation modifies or overwrites user-controlled files or resources. Users must retain full control: each sync operation must be individually disableable via `[tool.repomatic]`.

**Required properties** (checklist for adding or auditing a sync job):

1. **Config toggle.** A `*_sync: bool = True` field in the `Config` dataclass. Dotted sub-key in `[tool.repomatic]` (e.g., `gitignore.sync = false`). Alphabetically sorted among existing sync fields.
2. **CLI command.** A `repomatic sync-*` command that loads config, checks the toggle, and exits cleanly (`ctx.exit(0)`) when disabled. Uses `@pass_context` to receive `ctx`.
3. **Toggle enforcement.** For CLI-based syncs: the toggle field goes in `SUBCOMMAND_CONFIG_FIELDS` (checked in the CLI, not exposed as metadata). For workflow-only syncs (no CLI command): the toggle is exposed as a metadata output and checked in the job's `if:` condition. For syncs whose workflow also gates other steps on the toggle (like `sync-binaries`, whose output a separate `git-commit-push` step commits): both at once, a CLI check plus a metadata output kept out of `SUBCOMMAND_CONFIG_FIELDS`.
4. **Workflow job.** A `sync-*` job in the appropriate workflow file (usually `autofix.yaml`, but lifecycle-specific syncs may live elsewhere — e.g., `sync-dev-release` in `_release-engine.yaml`, `sync-labels` in `labels.yaml`). Requires: metadata `needs:` when applicable, prerequisite `if:` conditions, PR creation via `repomatic pr-sync --template sync-*` (branch, body, labels and commit message all derive from the template and its frontmatter). Exceptions: syncs targeting API resources (e.g., labels) rather than repo files apply changes directly, and `sync-binaries` runs as release-lane recording whose output is pushed straight to the default branch (see [§ Release-lane direct commits](#release-lane-direct-commits)).
5. **Documentation.** Config table row and TOML example in `docs/configuration.md`. Job description with "Skipped if" clause in `docs/workflows.md`. Changelog entry.
6. **Tests.** Default and custom value assertions in `test_repomatic_config_defaults` and `test_repomatic_config_custom_values`.

**Invariants:**

- A disabled toggle must produce **zero side effects**: no file writes, no API calls, no PRs.

## Update job contract

Every `update-*` operation computes derived artifacts from project state (lockfiles, git history, source code). Unlike sync operations, these generate computed output rather than overwriting user-authored content.

**Required properties:**

1. **CLI command.** A `repomatic update-*` command.
2. **Workflow job.** An `update-*` job in the appropriate workflow file with PR creation via `repomatic pr-sync --template update-*` (branch, body, labels and commit message all derive from the template and its frontmatter).
3. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.

**Optional properties:**

- **CLI command.** A CLI wrapper is only required when the update runs custom repomatic Python logic (e.g., `update-dep-graph`). Updates that invoke external tools or standalone scripts (e.g., `sphinx-apidoc`) may call them directly from the workflow without a `repomatic update-*` wrapper.
- **Config toggle.** Add a `*_update: bool = True` toggle only when the generated output involves files the user may want to manage independently. If added, follow the sync toggle pattern (Config field, `SUBCOMMAND_CONFIG_FIELDS`, tests).
- **Config parameters.** Output paths, filtering options, or depth limits belong as Config fields (e.g., `dependency-graph.output`, `dependency-graph.level`). These configure behavior without enabling/disabling the operation.

## Format and fix job contract

Every `format-*` and `fix-*` operation rewrites files using a pinned external tool. `format-*` enforces canonical style (semantics-preserving); `fix-*` corrects content errors such as typos (semantics-altering). The naming convention table in `CLAUDE.md` § Naming conventions for automated operations defines when to use each prefix.

**Required properties:**

1. **CLI command.** A `repomatic format-*` or `repomatic fix-*` command that wraps a pinned external tool (e.g., ruff, mdformat, jq, typos).
2. **Workflow job.** A job in the appropriate workflow file (usually `autofix.yaml`) with PR creation via `repomatic pr-sync --template verb-noun` (branch, body, labels and commit message all derive from the template and its frontmatter).
3. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.

**Invariants:**

- No config toggle. Format jobs gate on metadata file-detection outputs (e.g., `python_files`, `markdown_files`, `json_files`) making them self-skipping when irrelevant. Fix jobs may run unconditionally when the tool applies to all file types.
- The external tool version must be pinned in the CLI command for reproducibility.

(fix-steps-inside-another-job)=

### Fix steps inside another job

`fix-awesome-toc` is the one operation with no job, branch or template of its own. It corrects the table of contents that `format-markdown` has just regenerated, so the two have to share a working tree: given its own job, they would land in separate PRs and undo each other on every push, `format-markdown` re-adding the entries `fix-awesome-toc` had removed. It therefore runs as a step of `format-markdown`, gated on an `awesome-*` repository name, and its changes ship in that job's PR.

Reach for this shape only when a correction is inseparable from the operation that produced the content. A fix that merely runs *after* another one, without contending for the same lines, gets a job.

## Lint job contract

Every `lint-*` operation checks content without modifying it. Lint operations are **read-only**.

**Required properties:**

1. **CLI command.** A `repomatic lint-*` command. Returns exit code 0 on pass, non-zero on failure.
2. **Workflow job.** A `lint-*` job in `lint.yaml` (not `autofix.yaml`), unless the check only makes sense at a lifecycle moment: `lint-deps` lives in `_release-build.yaml`, where it gates the wheel build. No PR creation — lints gate merges via status checks.
3. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.

**Optional properties:**

- **CLI command.** A CLI wrapper is only required when the lint runs custom Python logic (e.g., `lint-repo`). Lints that invoke a standard external tool (`mypy`, `yamllint`, `actionlint`, `zizmor`, `gitleaks`, etc.) may call the tool directly from the workflow without a `repomatic lint-*` wrapper.

**Invariants:**

- Read-only. No file writes, no PRs, no side effects beyond exit code and stdout/stderr output. A markdown report written with `--output`, for a PR body to render, is not a file write in this sense: it is the exit code spelled out.
- Never in `autofix.yaml`: a lint has nothing to commit. `lint.yaml` is the default home, and the release lane is the one documented alternative.

## Pack job contract

Every `pack-*` operation assembles a distributable artifact set for a release (like `pack-plugin`, which zips the Claude Code plugin, or `pack-binaries`, which materializes the versionless binary aliases and prints the upload list). A pack operation **writes no repository file**: its output is a build artifact or a list consumed by a later step, so it needs no PR branch and no PR body template.

**Required properties:**

1. **CLI command.** A `repomatic pack-*` command. Everything beyond trivial wiring lives here rather than in workflow YAML, so the naming convention has one Python definition instead of a shell loop per call site.
2. **Workflow step.** A step (or job) in the release lane calling that command. `pack-*` has no `autofix.yaml` job: there is nothing to commit.
3. **Documentation.** Job or step description in `docs/workflows.md`. Changelog entry.
4. **Tests.** Unit coverage for the assembly logic, including its idempotency.

**Invariants:**

- Idempotent: re-running overwrites the same artifacts with the same bytes. This is load-bearing on a re-run of a release job, where a partially uploaded asset set must converge rather than duplicate.
- Writes only under a build or distribution directory, never into tracked repository content.
- Only the CLI command and the job or step ID share the `pack-<noun>` name. Unlike a `sync-*` or `format-*` operation, there is no matching PR branch or `.github/pr-templates/` entry to keep in step.

```{note}
Pick `pack-` over `sync-` when the operation's output leaves the repository. `sync-binaries` records the release binaries catalog *into* the repository and therefore commits; `pack-binaries` stages the same binaries *for upload* and commits nothing. Same nouns, different verbs, because the destination differs.
```

## Scan job contract

Every `scan-*` operation submits release artifacts to an external analysis service (like `scan-virustotal`) and records the results in the repository. The submission is the operation's point (seeding AV vendor databases); the recorded results are the durable trace of what the service reported.

**Required properties:**

1. **CLI command.** A `repomatic scan-*` command that performs the submission and writes the result records (like `scan-virustotal --records`).
2. **Workflow job.** A `scan-*` job in the release engine (`_release-engine.yaml`), running after `publish-release` so the released assets exist. Gated on the service's API key secret: no key, no scan.
3. **Config toggle for the recording.** The in-repo recording must be disableable via `[tool.repomatic]`: `binaries.sync = false` skips the catalog regeneration and commit steps while the scan itself still runs. Because the commit is performed by a separate `git-commit-push` step, the toggle is exposed as a metadata output (kept out of `SUBCOMMAND_CONFIG_FIELDS`) and checked in the step `if:` conditions, in addition to the usual CLI check inside the paired `sync-*` command.
4. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.
5. **Tests.** Default and custom value assertions in `test_repomatic_config_defaults` and `test_repomatic_config_custom_values`.

**Invariants:**

- Idempotent: re-running a scan upserts result records (no duplicate snapshots), and regenerating the catalog is convergent.
- The recording only ever touches generated data files (scan history JSON, catalog CSV, generated page), never user-authored content.

### Release-lane direct commits

```{important}
`scan-virustotal` records its results with a direct push to the default branch (via `repomatic git-commit-push`), not a pull request. This is the only file-modifying operation exempt from the PR convention. Disable it with `[tool.repomatic] binaries.sync = false`.
```

A post-release recording job gets nothing from a PR:

- The data is not reviewable: it records facts about an already-published, immutable release, fetched from external APIs (VirusTotal, the GitHub Releases API). Rejecting or editing the diff cannot change the release; it can only make the record wrong or missing.
- The binaries page must be fresh at release time: the push (with `REPOMATIC_PAT` configured) triggers the docs deploy, so download links and VirusTotal verdicts are live exactly when users and downstream distributors (Chocolatey, Scoop) fetch the new binaries. A PR would leave the page stale until someone merges it.
- The job is the tail of the release pipeline: ending a release on an open PR means every release needs a follow-up merge, or per-repo auto-merge wiring (branch protection, required checks, a PAT that triggers CI) that can block indefinitely. That adds failure modes for zero review value.

The push is engineered for a busy default branch: `git-commit-push` is idempotent, rebases and retries on rejection, and multi-release runs are serialized with `max-parallel: 1`. Repositories that do not want automated pushes on their default branch set `[tool.repomatic] binaries.sync = false`: binaries are still scanned, nothing is committed.

## Sample job contract

Every `sample-*` operation reads a metric off an external API and appends it to a history committed in the repository. It is the mirror image of a `scan-*`: that one submits an artifact and records what came back, this one submits nothing and records what was already there.

The property that separates it from a `sync-*` is that it never converges. A sync regenerates a file its external source could rebuild from scratch, so losing the file costs a re-run. A sample records a reading the source will not remember: once GitHub has served today's star count, nothing can re-serve it tomorrow. The store is the only place that history exists.

**Required properties:**

1. **CLI command.** A `repomatic sample-*` command that loads config, checks its toggle, and exits cleanly (`ctx.exit(0)`) when disabled or when nothing is configured to sample.
2. **Config toggle, opt-in.** A `sync: bool = False` field on the operation's nested config (`[tool.repomatic.metrics] sync`). Opt-in rather than opt-out, unlike a `sync-*`: an accumulating store is a commitment a maintainer makes deliberately, and most repositories track nothing. The same key gates the workflow component, so a repository that never enables it is never handed the file.
3. **Workflow job.** A job in a schedule-only workflow, never triggered on push: sampling the same value twice in one day writes the same point, so a per-push run costs API calls and produces nothing. It commits directly (see below).
4. **Documentation.** Config reference in `docs/configuration.md`. Job description with its "Skipped if" clause in `docs/workflows.md`. Changelog entry.
5. **Store format.** One CSV, through {mod}`repomatic.tabular`, one row per reading. Never JSON: see `claude.md` § Naming conventions rule 8.
6. **Tests.** A conformance test over the committed store: known provenances, no duplicate key, sorted, no date in the future, and no attribute holding two rows. The store is written unattended by a scheduled job, so nothing else would catch a malformed append.

**Invariants:**

- **Retention is a property of the metric, not of the caller.** A counter accrues every dated reading; an attribute keeps one row, restamped only when its value moves. One registry entry decides which, and the store's writer is the only code that has to know.
- **Idempotent within a period, additive across periods.** Re-running on the same day overwrites that day's own reading. Tomorrow's run adds one, and that is the operation working, not a defect in its idempotency.
- **A weaker provenance never overwrites a stronger one.** A history mixing methodologies records which one each point came from, and a one-off backfill run against an already-populated store must degrade nothing (see {data}`repomatic.metrics.SOURCE_RANK`).
- **A failed reading costs its own row, never the run.** One unreachable forge must not blank every metric collected beside it, and a stale figure carrying its own date beats a hole.
- **Rendering is a pure function of the store.** Anything drawn from the history is stamped with the newest reading rather than with the run date, so a pass that found nothing new rewrites no committed file.

### Sampling commits directly

```{important}
`sample-metrics` commits its store with a direct push to the default branch (via `repomatic git-commit-push`), not a pull request. Disable it with `[tool.repomatic] metrics.sync = false`.
```

The reasoning of [§ Release-lane direct commits](#release-lane-direct-commits) applies unchanged, and the first bullet applies with more force. The diff records what an external API answered at a moment that has passed: rejecting or editing it cannot change the reading, only make the history wrong or lose it. A weekly pull request appending machine-read counts would also be a weekly pull request nobody reads, and one left unmerged stalls the accrual silently, which is the failure mode the whole operation exists to prevent.

The push is engineered for a busy default branch the same way: `git-commit-push` is idempotent and rebases on rejection. It runs with `--all-changes` rather than a path list, because the store and the charts live wherever the repository's configuration puts them and the workflow cannot know: the runner starts from a pristine checkout and the sampling steps are its only writers, so everything that changed is exactly the job's own output.

## PR body template conventions

PR body templates in `repomatic/templates/` are the downstream user's primary window into what an automated operation did and why. Each template should help users understand, verify, and customize the operation.

**Frontmatter:**

1. **`title`.** The PR title, and the commit-message fallback.
2. **`docs`.** A deep link to the job's section of the hosted workflows reference. The rendered body surfaces it as the leading `Documentation` entry of the collapsible `Workflow metadata` block, so the body carries no standalone description section.
3. **`footer: false`.** The metadata block already appends the attribution footer once; every template opts out of a second copy.
4. **`args`.** The placeholder names the title and body interpolate (like `$diff_table`), each supplied at the call site by `--template-arg` or by a dedicated flag such as `--version` and `--part`.
5. **`labels`.** The labels `pr-sync` puts on the pull request it opens.
6. **`draft`.** Opens the pull request as a draft. Omitted means ready for review.

**Body elements** (include what applies, with `##` section headings):

1. **Configuration section.** For operations driven by `[tool.repomatic]`, a `## ⚙️ Configuration` section listing the relevant options as bullets deep-linking into the hosted [configuration reference](https://repomatic.net/configuration). Sync and update templates lead with it, after their `$diff_table` when they take one.
2. **Customization tip.** For format and fix operations, a `> [!TIP]` block naming the `[tool.X]` `pyproject.toml` section and/or native config file as the way to override defaults, linked to the tool's own configuration reference.

**Example** (format job):

```markdown
---
title: Format X
docs: https://repomatic.net/workflows#format-x-format-x
footer: false
---

> [!TIP]
> Customize formatting rules via [`[tool.X]`](https://example.com/configuration/)
> in your `pyproject.toml`, or via a native `x.toml` file.
```

### Repository-local templates

A repository with a PR-opening job of its own ships the body as a file and passes it to `repomatic pr-sync --template-file`, instead of adding a template upstream. Those files follow the conventions above, plus three of their own:

1. **Location: `.github/pr-templates/`.** `.github/` already namespaces by subdirectory (`ISSUE_TEMPLATE/`, `workflows/`, `actions/`), and a dedicated one leaves each basename free to carry the operation name. A flat `.github/pr-{name}.md` needs the `pr-` prefix only to disambiguate, and lands beside GitHub's own `pull_request_template.md`, an unrelated human-facing file.
2. **Basename: the job ID.** Which is also the PR branch, per [§ Naming conventions for automated operations](https://github.com/kdeldycke/repomatic/blob/main/claude.md#naming-conventions-for-automated-operations). One exception: a template parametrized with `--template-arg` can serve several jobs, and is then named for what it renders rather than for any one of them (`update-package-spec.md`, feeding a job per packaging channel).
3. **No `docs` field.** It deep-links the hosted workflows reference, which documents upstream jobs only.

Write `footer: false` as a bare boolean, and never leave it out. Both `false` and the quoted `'false'` opt out, but an absent field, `'False'` and every other value do not, and the failure is silent: the body carries the attribution footer twice.

`repomatic lint-repo` checks the location, the frontmatter, and that every referenced path exists.
