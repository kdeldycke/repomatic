# {octicon}`tasklist` Automated operation contracts

> Referenced from `claude.md` [§ Automated operation contracts](https://github.com/kdeldycke/repomatic/blob/main/claude.md#automated-operation-contracts). This file contains the detailed checklists for each operation type.

## Sync job contract

Every `sync-*` operation modifies or overwrites user-controlled files or resources. Users must retain full control: each sync operation must be individually disableable via `[tool.repomatic]`.

**Required properties** (checklist for adding or auditing a sync job):

1. **Config toggle.** A `*_sync: bool = True` field in the `Config` dataclass. Dotted sub-key in `[tool.repomatic]` (e.g., `gitignore.sync = false`). Alphabetically sorted among existing sync fields.
2. **CLI command.** A `repomatic sync-*` command that loads config, checks the toggle, and exits cleanly (`ctx.exit(0)`) when disabled. Uses `@pass_context` to receive `ctx`.
3. **Toggle enforcement.** For CLI-based syncs: the toggle field goes in `SUBCOMMAND_CONFIG_FIELDS` (checked in the CLI, not exposed as metadata). For workflow-only syncs (no CLI command): the toggle is exposed as a metadata output and checked in the job's `if:` condition.
4. **Workflow job.** A `sync-*` job in the appropriate workflow file (usually `autofix.yaml`, but lifecycle-specific syncs may live elsewhere — e.g., `sync-dev-release` in `_release-engine.yaml`, `sync-labels` in `labels.yaml`). Requires: metadata `needs:` when applicable, prerequisite `if:` conditions, PR creation via `peter-evans/create-pull-request` (branch name = job ID, body from `repomatic pr-body --template sync-*`). Exception: syncs targeting API resources (e.g., labels) rather than repo files apply changes directly.
5. **Documentation.** Config table row and TOML example in `docs/configuration.md`. Job description with "Skipped if" clause in `docs/workflows.md`. Changelog entry.
6. **Tests.** Default and custom value assertions in `test_repomatic_config_defaults` and `test_repomatic_config_custom_values`.

**Invariants:**

- A disabled toggle must produce **zero side effects**: no file writes, no API calls, no PRs.

## Update job contract

Every `update-*` operation computes derived artifacts from project state (lockfiles, git history, source code). Unlike sync operations, these generate computed output rather than overwriting user-authored content.

**Required properties:**

1. **CLI command.** A `repomatic update-*` command.
2. **Workflow job.** An `update-*` job in the appropriate workflow file with PR creation via `peter-evans/create-pull-request` (branch name = job ID, body from `repomatic pr-body --template update-*`).
3. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.

**Optional properties:**

- **CLI command.** A CLI wrapper is only required when the update runs custom repomatic Python logic (e.g., `update-deps-graph`). Updates that invoke external tools or standalone scripts (e.g., `sphinx-apidoc`) may call them directly from the workflow without a `repomatic update-*` wrapper.
- **Config toggle.** Add a `*_update: bool = True` toggle only when the generated output involves files the user may want to manage independently. If added, follow the sync toggle pattern (Config field, `SUBCOMMAND_CONFIG_FIELDS`, tests).
- **Config parameters.** Output paths, filtering options, or depth limits belong as Config fields (e.g., `dependency-graph.output`, `dependency-graph.level`). These configure behavior without enabling/disabling the operation.

## Format and fix job contract

Every `format-*` and `fix-*` operation rewrites files using a pinned external tool. `format-*` enforces canonical style (semantics-preserving); `fix-*` corrects content errors such as typos (semantics-altering). The naming convention table in `CLAUDE.md` § Naming conventions for automated operations defines when to use each prefix.

**Required properties:**

1. **CLI command.** A `repomatic format-*` or `repomatic fix-*` command that wraps a pinned external tool (e.g., ruff, mdformat, jq, typos).
2. **Workflow job.** A job in the appropriate workflow file (usually `autofix.yaml`) with PR creation via `peter-evans/create-pull-request` (branch name = job ID, body from `repomatic pr-body --template verb-noun`).
3. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.

**Invariants:**

- No config toggle. Format jobs gate on metadata file-detection outputs (e.g., `python_files`, `markdown_files`, `json_files`) making them self-skipping when irrelevant. Fix jobs may run unconditionally when the tool applies to all file types.
- The external tool version must be pinned in the CLI command for reproducibility.

## Lint job contract

Every `lint-*` operation checks content without modifying it. Lint operations are **read-only**.

**Required properties:**

1. **CLI command.** A `repomatic lint-*` command. Returns exit code 0 on pass, non-zero on failure.
2. **Workflow job.** A `lint-*` job in `lint.yaml` (not `autofix.yaml`). No PR creation — lints gate merges via status checks.
3. **Documentation.** Job description in `docs/workflows.md`. Changelog entry.

**Optional properties:**

- **CLI command.** A CLI wrapper is only required when the lint runs custom Python logic (e.g., `lint-repo`). Lints that invoke a standard external tool (`mypy`, `yamllint`, `actionlint`, `zizmor`, `gitleaks`, etc.) may call the tool directly from the workflow without a `repomatic lint-*` wrapper.

**Invariants:**

- Read-only. No file writes, no PRs, no side effects beyond exit code and stdout/stderr output.
- Lives in `lint.yaml`, not `autofix.yaml`.

## PR body template conventions

PR body templates in `repomatic/templates/` are the downstream user's primary window into what an automated operation did and why. Each template should help users understand, verify, and customize the operation.

**Required elements:**

1. **Description.** What the job does, linking to the tool's homepage and the job documentation in `docs/workflows.md`.
2. **Bundled defaults link.** When the operation uses a bundled default config from `repomatic/data/`, link to it so users can inspect the exact settings applied. Use the `blob/main` URL (e.g., `https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/ruff.toml`).
3. **Customization tip.** A `> [!TIP]` block pointing users to the tool's own configuration documentation, mentioning the `[tool.X]` `pyproject.toml` section and/or native config file as the way to override defaults. Link to the tool's configuration reference (not just the homepage).

**Example** (format job with bundled default):

```markdown
Auto-formats X files with [tool](https://example.com). When no `[tool.X]`
section or `x.toml` is present, [repomatic's bundled defaults](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/x.toml)
are applied at runtime. See the [`format-x` job documentation](...) for details.

> [!TIP]
> Customize formatting rules via [`[tool.X]`](https://example.com/configuration/)
> in your `pyproject.toml`, or via a native `x.toml` file.
```
