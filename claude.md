# Development guide

## Downstream repositories

This repository serves as the **canonical reference** for conventions and best practices. When Claude is used in any repository that uses the `repomatic` CLI and its [`[tool.repomatic]` configuration](readme.md#toolrepomatic-configuration), it should follow the same conventions defined here—including the structure and guidelines of this `claude.md` file itself.

In other words, downstream repositories should mirror the patterns established here for code style, documentation, testing, and design principles.

**Contributing upstream:** If Claude spots inefficiencies, potential improvements, performance bottlenecks, missing features, or opportunities for better adaptability in the `repomatic` CLI, its configuration, the reusable workflows, or this `claude.md` file itself, it should propose these changes upstream via a pull request or issue at [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic/issues). This benefits all downstream repositories.

## Commands

### Testing

```shell-session
# Run all tests with coverage.
$ uv run --group test pytest

# Run a single test file.
$ uv run --group test pytest tests/test_changelog.py

# Run a specific test.
$ uv run --group test pytest tests/test_changelog.py::test_function_name
```

### Type checking

```shell-session
$ uv run --group typing mypy repomatic
```

### Running the CLI

```shell-session
# Run locally during development.
$ uv run repomatic --help

# Try without installation using uvx.
$ uvx -- repomatic --help
```

## Documentation requirements

### Scope of `claude.md` vs `readme.md`

- **`claude.md`**: Contributor and Claude-focused directives—code style, testing guidelines, design principles, and internal development guidance.
- **`readme.md`**: User-facing documentation for the `repomatic` CLI and `[tool.repomatic]` configuration—installation, usage, and the workflows that implement them.

When adding new content, consider whether it benefits end users (`readme.md`) or contributors/Claude working on the codebase (`claude.md`).

### Keeping `claude.md` lean

`claude.md` must contain only conventions, policies, rationale, and non-obvious rules that Claude cannot discover by reading the codebase. Actively remove:

- **Structural inventories** — project trees, module tables, workflow lists. Claude can discover these via `Glob`/`Read`.
- **Code examples that duplicate source files** — YAML snippets copied from workflows, Python patterns visible in every module. Reference the source file instead.
- **General programming knowledge** — standard Python idioms, well-known library usage, tool descriptions derivable from imports.
- **Implementation details readable from code** — what a function does, what a workflow's concurrency block looks like. Only the *rationale* for non-obvious choices belongs here.

### Changelog and readme updates

Always update documentation when making changes:

- **`changelog.md`**: Add a bullet point describing **what** changed (new features, bug fixes, behavior changes), not **why**. Keep entries concise and actionable. Justifications and rationale belong in documentation (`readme.md`, Sphinx docs) or code comments, not in the changelog.
- **`readme.md`**: Update relevant sections when adding/modifying workflow jobs, CLI commands, or configuration options.

### Documentation sync

The following documentation artifacts must stay in sync with the code. When changing any of these, update the others:

- **CLI output in `readme.md`**: The inline `uvx -- repomatic` help block, `--version` output, and development version output must match actual CLI output. Re-run the commands and update the pasted text.
- **Version references in `readme.md`**: The `--version` examples and example workflow `@vX.Y.Z` reference must reflect the latest released version.
- **Workflow job descriptions in `readme.md`**: Each `.github/workflows/*.yaml` workflow section must document all jobs by their actual job ID, with accurate descriptions of what they do, their requirements, and skip conditions.
- **`[tool.repomatic]` configuration table in `readme.md`**: The options table must match what the code actually reads from `pyproject.toml`. Search `repomatic/` for config key references to verify.

### Knowledge placement

Each piece of knowledge has one canonical home, chosen by audience. Other locations get a brief pointer ("See `module.py` for rationale.").

| Audience              | Home                      | Content                                                    |
| :-------------------- | :------------------------ | :--------------------------------------------------------- |
| End users             | `readme.md`               | Installation, configuration, usage.                        |
| Setup walkthroughs    | `setup-guide.md` issue    | Step-by-step setup with deep links to repo settings pages. |
| Developers            | Python docstrings         | Design decisions, trade-offs, "why" explanations.          |
| Workflow maintainers  | YAML comments             | Brief "what" + pointer to Python code for "why."           |
| Bug reporters         | `.github/ISSUE_TEMPLATE/` | Reproduction steps, version commands.                      |
| Contributors / Claude | `CLAUDE.md`               | Conventions, policies, non-obvious rules.                  |

**YAML → Python distillation:** When workflow YAML files contain lengthy "why" explanations, migrate the rationale to Python module, class, or constant docstrings (using reST admonitions like `.. note::` and `.. warning::`). Trim the YAML comment to a one-line "what" plus a pointer: `# See repomatic/module.py for rationale.`

### Documenting code decisions

Document design decisions, trade-offs, and non-obvious implementation choices directly in the code using docstring admonitions (reST `.. warning::`, `.. note::`, `.. caution::`), inline comments, and module-level docstrings for constants that need context.

## Code style

### Terminology and spelling

Use correct capitalization for proper nouns and trademarked names:

- **PyPI** (not ~~PyPi~~) — the Python Package Index. The "I" is capitalized because it stands for "Index". See [PyPI trademark guidelines](https://pypi.org/trademarks/).
- **GitHub** (not ~~Github~~)
- **GitHub Actions** (not ~~Github Actions~~ or ~~GitHub actions~~)
- **JavaScript** (not ~~Javascript~~)
- **TypeScript** (not ~~Typescript~~)
- **macOS** (not ~~MacOS~~ or ~~macos~~)
- **iOS** (not ~~IOS~~ or ~~ios~~)

### Version formatting

The version string is always bare (e.g., `1.2.3`). The `v` prefix is a **tag namespace** — it only appears when the reference is to a git tag or something derived from a tag (action ref, comparison URL, commit message). This aligns with PEP 440, PyPI, and semver conventions.

| Context                                | Format                          | Example                              | Rationale                         |
| :------------------------------------- | :------------------------------ | :----------------------------------- | :-------------------------------- |
| Python `__version__`, `pyproject.toml` | `1.2.3`                         | `version = "5.10.1"`                 | PEP 440 bare version.             |
| Git tags                               | `` `v1.2.3` ``                  | `` `v5.10.1` ``                      | Tag namespace convention.         |
| GitHub comparison URLs                 | `v1.2.3...v1.2.4`               | `compare/v5.10.0...v5.10.1`          | References tags.                  |
| GitHub action/workflow refs            | `` `@v1.2.3` ``                 | `actions/checkout@v6.0.2`            | References tags.                  |
| Commit messages                        | `v1.2.3`                        | `[changelog] Release v5.10.1`        | References the tag being created. |
| CLI `--version` output                 | `1.2.3`                         | `repomatic, version 5.10.1`          | Package version, not a tag.       |
| Changelog headings                     | `` `1.2.3` ``                   | `` ## [`5.10.1` (2026-02-17)] ``     | Package version, code-formatted.  |
| PyPI URLs                              | `1.2.3`                         | `pypi.org/project/repomatic/5.10.1/` | PyPI uses bare versions.          |
| PyPI admonitions                       | `` `1.2.3` ``                   | `` `5.10.1` is available on PyPI ``  | Package version, not a tag.       |
| PR titles                              | `` `v1.2.3` ``                  | `` Release `v5.10.1` ``              | References the tag.               |
| Prose/documentation                    | `` `v1.2.3` `` or `` `1.2.3` `` | Depends on referent                  | Match what is being referenced.   |

**Rules:**

1. **No `v` prefix on package versions.** Anywhere the version identifies the *package* (PyPI, changelog heading, CLI output), use the bare version: `1.2.3`.
2. **`v` prefix on tag references.** Anywhere the version identifies a *git tag* (comparison URLs, action refs, commit messages, PR titles), use `v1.2.3`.
3. **Always backtick-escape versions in prose.** Both `v1.2.3` (tag) and `1.2.3` (package) are identifiers, not natural language. In markdown, wrap them in backticks: `` `v1.2.3` ``, `` `1.2.3` ``. In reST docstrings, use double backticks: ``` ``v1.2.3`` ```.
4. **Development versions** follow PEP 440: `1.2.3.dev0` with optional `+{short_sha}` local identifier.

### Comments and docstrings

- All comments in Python files must end with a period.
- Docstrings use reStructuredText format (vanilla style, not Google/NumPy).
- Documentation in `./docs/` uses MyST markdown format where possible. Fallback to reStructuredText if necessary.
- Keep lines within 88 characters in Python files, including docstrings and comments (ruff default). Markdown files have no line-length limit.
- Titles in markdown use sentence case.

### Imports

- Import from the root package (`from repomatic import cli`), not submodules when possible.
- Place imports at the top of the file, unless avoiding circular imports.
- **Version-dependent imports** (e.g., `tomllib` fallback for Python 3.10) should be placed **after all normal imports** but **before the `TYPE_CHECKING` block**. This allows ruff to freely sort and organize the normal imports above without interference.

### `TYPE_CHECKING` block

Place a module-level `TYPE_CHECKING` block after all imports (including version-dependent conditional imports). Use `TYPE_CHECKING = False` (not `from typing import TYPE_CHECKING`) to avoid importing `typing` at runtime. See existing modules for the canonical pattern.

### Modern `typing` practices

Use modern equivalents from `collections.abc` and built-in types instead of `typing` imports. Use `X | Y` instead of `Union` and `X | None` instead of `Optional`. New modules should include `from __future__ import annotations` ([PEP 563](https://peps.python.org/pep-0563/)).

### Minimal inline type annotations

Omit type annotations on local variables, loop variables, and assignments when mypy can infer the type from the right-hand side. Annotations add visual noise without helping the type checker.

```python
# ✅ Preferred: mypy infers the type.
root_dir = None
name = "default"
items = []

# ❌ Avoid: redundant annotation that mypy already knows.
root_dir: Path | None = None
name: str = "default"
items: list[str] = []
```

**When to annotate:** Add an explicit annotation only when mypy cannot infer the correct type and reports an error — e.g., empty collections that need a specific element type (`items: list[Package] = []`), `None` initializations where the intended type isn't obvious from later usage, or narrowing a union that mypy doesn't resolve on its own.

**Function signatures are unaffected.** Always annotate function parameters and return types — those are part of the public API and cannot be inferred.

### Python 3.10 compatibility

This project supports Python 3.10+. Be aware of syntax features that are **not** available in Python 3.10:

- **Multi-line f-string expressions (Python 3.12+):** You cannot break an f-string after the `{` character and continue the expression on the next line.

  ```python
  # fmt: off
  # ❌ Fails on Python 3.10 (only works in Python 3.12+)
  message = f"value={
      some_long_expression
  }"

  # ✅ Works on Python 3.10+: split into concatenated strings.
  message = (
      "value="
      f"{some_long_expression}"
  )
  # fmt: on
  ```

- **Exception groups and `except*` (Python 3.11+).**

- **`Self` type hint (Python 3.11+):** Use `from typing_extensions import Self` instead.

### YAML workflows

For single-line commands that fit on one line, use plain inline `run:` without any block scalar indicator:

```yaml
# ✅ Preferred for short commands: plain inline.
  - name: Check out repository
    run: git checkout main
```

When a command is too long for a single line, use the folded block scalar (`>`) to split it across multiple lines:

```yaml
# ✅ Preferred for long commands: folded block scalar joins lines with spaces.
  - name: Run linter
    run: >
      uvx --no-progress 'yamllint==1.38.0' --strict --format github
      --config-data "{rules: {line-length: {max: 120}}}" .

# ❌ Avoid: literal block scalar with backslash continuations.
  - name: Run linter
    run: |
      uvx --no-progress 'yamllint==1.38.0' --strict --format github \
        --config-data "{rules: {line-length: {max: 120}}}" .
```

**Why:** The `>` scalar folds newlines into spaces, producing a single command without needing backslash escapes. This is cleaner and avoids issues with trailing whitespace after `\`.

**When to use `|`:** Use literal block scalar (`|`) only when the command requires preserved newlines (e.g., multi-statement scripts, heredocs).

### Naming conventions for automated operations

CLI commands, workflow job IDs, PR branch names, and PR body template names must all agree on the same verb prefix. This consistency makes the conventions learnable and grepable across all four dimensions.

| Prefix     | Semantics                                       | Source of truth      | Idempotent? | Examples                                          |
| :--------- | :---------------------------------------------- | :------------------- | :---------- | :------------------------------------------------ |
| `sync-X`   | Regenerate from a canonical or external source. | Template, API, repo  | Yes         | `sync-gitignore`, `sync-mailmap`, `sync-renovate` |
| `update-X` | Compute from project state.                     | Lockfile, git log    | Yes         | `update-deps-graph`, `update-checksums`           |
| `format-X` | Rewrite to enforce canonical style.             | Formatter rules      | Yes         | `format-json`, `format-markdown`, `format-python` |
| `fix-X`    | Correct content (auto-fix).                     | Linter/checker rules | Yes         | `fix-typos`                                       |
| `lint-X`   | Check content without modifying it.             | Linter rules         | Yes         | `lint-changelog`                                  |

**Rules:**

1. **Pick the verb that matches the data source.** If the operation pulls from an external template, API, or canonical reference, it is a `sync`. If it computes from local project state (lockfiles, git history, source code), it is an `update`. If it reformats existing content, it is a `format`.
2. **All four dimensions must agree.** When adding a new automated operation, the CLI command, workflow job ID, PR branch name, and PR body template file name must all use the same `verb-noun` identifier (e.g., `sync-gitignore` everywhere).
3. **Function names follow the CLI name.** The Python function backing a CLI command uses the underscore equivalent of the CLI name (e.g., `sync_gitignore` for `sync-gitignore`). Exception: when the function name would collide with an imported module, use the Click `name=` parameter to override (e.g., `@repomatic.command(name="update-deps-graph")` on a function named `deps_graph`) or append a `_cmd` suffix (e.g., `sync_uv_lock_cmd` to avoid collision with `from .renovate import sync_uv_lock`).

### Ordering conventions

Keep definitions sorted for readability and to minimize merge conflicts:

- **Workflow jobs**: Ordered by execution dependency (upstream jobs first), then alphabetically within the same dependency level.
- **Python module-level constants and variables**: Alphabetically, unless there is a logical grouping or dependency order. Hard-coded domain constants (e.g., `NOT_ON_PYPI_ADMONITION`, `SKIP_BRANCHES`) should be placed at the top of the file, immediately after imports. These constants encode domain assertions and business rules — surfacing them early gives readers an immediate sense of the assumptions the module operates under.
- **YAML configuration keys**: Alphabetically within each mapping level.
- **Documentation lists and tables**: Alphabetically, unless a logical order (e.g., chronological in changelog) takes precedence.

## Release checklist

A complete release consists of all of the following. If any are missing, the release is incomplete:

- **Git tag** (`vX.Y.Z`) created on the freeze commit
- **GitHub release** with non-empty release notes matching the `changelog.md` entry for that version
- **Binaries attached** to the GitHub release for all 6 platform/architecture combinations (linux-arm64, linux-x64, macos-arm64, macos-x64, windows-arm64, windows-x64)
- **PyPI package** published at the matching version
- **`changelog.md`** entry with the release date and comparison URL finalized

## Testing guidelines

- Use `@pytest.mark.parametrize` when testing the same logic for multiple inputs. Prefer parametrize over copy-pasted test functions that differ only in their data — it deduplicates test logic, improves readability, and makes it trivial to add new cases.
- Keep test logic simple with straightforward asserts.
- Tests should be sorted logically and alphabetically where applicable.
- Test coverage is tracked with `pytest-cov` and reported to Codecov.
- Do not use classes for grouping tests. Write test functions as top-level module functions. Only use test classes when they provide shared fixtures, setup/teardown methods, or class-level state.

## Agent conventions

This repository uses two Claude Code agents defined in `.claude/agents/`. Their definitions should be lean — if a rule belongs in `CLAUDE.md`, put it here and reference it from the agent file. Do not duplicate.

### Source of truth hierarchy

`CLAUDE.md` defines the rules. The codebase and GitHub (issues, PRs, CI logs) are what you measure against those rules. When they disagree, fix the code to match the rules. If the rules are wrong, fix `CLAUDE.md`.

### Common maintenance pitfalls

Patterns that recur across sessions — watch for these proactively:

- **Documentation drift** is the most frequent issue. CLI output, version references, and workflow job descriptions in `readme.md` go stale after every release or refactor. Always verify docs against actual output after changes.
- **CI debugging starts from the URL.** When a workflow fails, fetch the run logs first (`gh run view --log-failed`). Do not guess at the cause.
- **Type-checking divergence.** Code that passes `mypy` locally may fail in CI where `--python-version 3.10` is used. Always consider the minimum supported Python version.
- **Simplify before adding.** When asked to improve something, first ask whether existing code or tools already cover the case. Remove dead code and unused abstractions before introducing new ones.

### Agent behavior policy

- Agents make fixes in the working tree only. Never commit, push, or create PRs.
- Prefer mechanical enforcement (tests, autofix jobs, linting checks) over prose rules. If a rule can be checked by code, it should be.
- Agent definitions should reference `CLAUDE.md` sections, not restate them.
- qa-engineer is the gatekeeper for agent definition changes.

### Skills

Skills in `.claude/skills/` wrap `repomatic` CLI commands as slash commands. They are user-invocable only (`disable-model-invocation: true`) and follow agent conventions: lean definitions, no duplication with `CLAUDE.md`, reference sections instead of restating rules.

Skills are grouped by lifecycle phase. Each skill includes a "Next steps" section that suggests related skills to run next, creating a guided workflow:

- **Setup**: `/repomatic-init`, `/repomatic-sync`
- **Development**: `/repomatic-deps`, `/repomatic-metadata`
- **Quality**: `/repomatic-lint`, `/repomatic-test`
- **Release**: `/repomatic-changelog`, `/repomatic-release`

Run `repomatic list-skills` to see all skills with descriptions.

## Design principles

### Philosophy

1. Create something that works (to provide business value).
2. Create something that's beautiful (to lower maintenance costs).
3. Work on performance.

### CLI and configuration as primary abstractions

The `repomatic` CLI and its `[tool.repomatic]` configuration in `pyproject.toml` are the project's primary interfaces. Everything else — reusable workflows, templates, label definitions — is a delivery mechanism. New features should be implemented in the CLI first; workflows should call the CLI, not the other way around. Documentation should lead with what the CLI does and how to configure it.

### Linting and formatting

[Linting](readme.md#githubworkflowslintyaml-jobs) and [formatting](readme.md#githubworkflowsautofixyaml-jobs) are automated via GitHub workflows. Developers don't need to run these manually during development, but are still expected to do best effort. Push your changes and the workflows will catch any issues and perform the nitpicking.

### Metadata-driven workflow conditions

GitHub Actions lacks conditional step groups—you cannot conditionally skip multiple steps with a single condition. Rather than duplicating `if:` conditions on every step, augment the `repomatic metadata` subcommand to compute the condition once and reference it from workflow steps.

**Why:** Python code in `repomatic` is simpler to maintain, test, and debug than complex GitHub Actions workflow logic. Moving conditional checks into metadata extraction centralizes logic in one place.

Example: Instead of a separate "check" step followed by multiple steps with `if: steps.check.outputs.allowed == 'true'`, add the check to metadata output and reference `steps.metadata.outputs.some_check == 'true'`.

### Defensive workflow design

GitHub Actions workflows run in an environment where race conditions, eventual consistency, and partial failures are common. Prefer a **belt-and-suspenders** approach: use multiple independent mechanisms to ensure correctness rather than relying on a single guarantee.

For example, `changelog.yaml`'s `bump-versions` job needs to know the latest released version. Rather than trusting that git tags are always available:

1. **Belt** — The `workflow_run` trigger ensures the job runs *after* the release workflow completes, so tags exist by then.
2. **Suspenders** — The `is_version_bump_allowed()` function falls back to commit message parsing (`[changelog] Release vX.Y.Z`) when tags aren't found.

Apply the same philosophy elsewhere: avoid single points of failure in workflow logic. If a job depends on external state (tags, published packages, API availability), add a fallback or a graceful default. When possible, make operations [idempotent](#idempotency-by-default) so re-runs are safe.

#### `workflow_run` checkout pitfall

See also: [actions/checkout#504](https://github.com/actions/checkout/issues/504) for context on `actions/checkout`'s default merge commit behavior on pull requests.

When `workflow_run` fires, `github.event.workflow_run.head_sha` points to the commit that *triggered* the upstream workflow — not the latest commit on `main`. If the release cycle added commits after that trigger (freeze + unfreeze), checking out `head_sha` produces a stale tree and the resulting PR will conflict with current `main`.

**Fix:** Use `github.sha` instead, which for `workflow_run` events resolves to the latest commit on the default branch. The `workflow_run` trigger's purpose is *timing* (ensuring tags exist), not pinning to a specific commit. This applies to any job that needs the current state of `main` after an upstream workflow completes.

#### Immutable releases

The release workflow creates a draft, uploads all assets, then publishes. Once published with [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) enabled, tags and assets are locked. Tag names are permanently burned — reinforcing the [skip and move forward](#skip-and-move-forward-dont-rewrite-history) principle. Release notes remain editable for `sync-github-releases`.

### Idempotency by default

Workflows and CLI commands must be safe to re-run. Running the same command or workflow twice with the same inputs should produce the same result without errors or unwanted side effects (e.g., duplicate tags, duplicate PR comments, redundant file modifications).

**In practice:**

- Use `--skip-existing` or equivalent guards when creating resources (tags, releases, published packages).
- Check for existing state before writing (e.g., skip adding an admonition if it's already present, skip creating a PR if one already exists for the branch).
- Prefer upsert semantics over create-only semantics.
- Make file-modifying operations convergent: applying the same transformation to an already-transformed file should be a no-op.

**When idempotency is not achievable**, document the reason in a comment or docstring explaining what side effects occur on re-runs and why they are acceptable or unavoidable.

### Skip and move forward, don't rewrite history

When a release goes wrong — squash merge, broken artifact, bad metadata — prefer **skipping the version and releasing the next one** over reverting commits, force-pushing, or rewriting `main`. A burned version number is cheap; a botched automated recovery is not.

This mirrors how package repositories handle defective releases. PyPI lets maintainers [yank](https://peps.python.org/pep-0592/) a release rather than delete it, preserving immutability while signaling that consumers should upgrade. The same principle applies to our workflow:

- **Don't automate destructive recovery.** Automated reverts, force-pushes, and history rewrites on `main` are high-risk operations that compound the original mistake. The `detect-squash-merge` job creates a notification issue instead of reverting precisely for this reason.
- **Notify and let humans decide.** Open an issue, fail the workflow, and trust the maintainer to choose the right recovery path. A human in the loop is always safer than an automated guess.
- **Version numbers are disposable.** Software skips versions routinely. A changelog entry for an unpublished version is a minor cosmetic issue, not a correctness problem.
- **Existing safeguards are the real protection.** The tagging, publishing, and release jobs are gated on commit message patterns (`[changelog] Release v`). If those gates hold, no broken release escapes — regardless of what landed on `main`.

When designing new workflow safeguards, default to **detection + notification** rather than **detection + automated fix**. The blast radius of a missed notification is zero; the blast radius of a bad automated fix can be catastrophic.

### Concurrency implementation

> [!NOTE]
> For user-facing documentation, see [`readme.md` § Concurrency and cancellation](readme.md#concurrency-and-cancellation).

Workflows use two concurrency strategies depending on whether they perform critical release operations. Read the `concurrency:` block in each workflow file for the exact YAML.

#### `release.yaml` — SHA-based unique groups

`release.yaml` handles tagging, PyPI publishing, and GitHub release creation. These operations must run to completion. Using conditional `cancel-in-progress: false` doesn't work because it's evaluated on the *new* workflow, not the old one. If a regular commit is pushed while a release workflow is running, the new workflow would cancel the release because they share the same concurrency group.

The solution is to give each release run its own unique group using the commit SHA. Both `[changelog] Release` and `[changelog] Post-release` patterns must be matched because when a release is pushed, the event contains **two commits bundled together** and `github.event.head_commit` refers to the most recent one (the post-release bump).

#### Release PR: freeze and unfreeze commits

The `prepare-release` job in `changelog.yaml` creates a PR with exactly **two commits** that must be merged via "Rebase and merge" (never squash):

1. **Freeze commit** (`[changelog] Release vX.Y.Z`) — Freezes everything to the release version: finalizes the changelog date and comparison URL, removes the "unreleased" warning, freezes workflow action references to `@vX.Y.Z`, and freezes CLI invocations to a PyPI version.
2. **Unfreeze commit** (`[changelog] Post-release bump vX.Y.Z → vX.Y.Z`) — Unfreezes for the next development cycle: reverts action references back to `@main`, reverts CLI invocations back to local source (`--from . repomatic`), adds a new unreleased changelog section, and bumps the version to the next patch.

The auto-tagging job in `release.yaml` depends on these being **separate commits** — it uses `release_commits_matrix` to identify and tag only the freeze commit. Squashing would merge both into one, breaking the tagging logic.

**Squash merge safeguard:** The `detect-squash-merge` job in `release.yaml` detects squash merges by checking if the head commit message starts with `` Release `v `` (the PR title pattern) rather than `[changelog] Release v` (the canonical freeze commit pattern). When detected, it opens a GitHub issue assigned to the person who merged, then fails the workflow. The release is effectively skipped — existing safeguards in `create-tag` prevent tagging, publishing, and releasing.

On `main`, workflows use `--from . repomatic` to run the CLI from local source (dogfooding). The freeze commit freezes these to `'repomatic==X.Y.Z'` so tagged releases reference a published package. The unfreeze commit reverts them back for the next development cycle.

#### `changelog.yaml` — event-scoped groups

`changelog.yaml` includes `github.event_name` in its concurrency group to prevent cross-event cancellation. This is required because `changelog.yaml` has both `push` and `workflow_run` triggers. Without `event_name` in the group, the `workflow_run` event (which fires when "Build & release" completes) would cancel the `push` event's `prepare-release` job, but then skip `prepare-release` itself (due to `if: github.event_name != 'workflow_run'`), so `prepare-release` would never run.

### Command-line options

Always prefer long-form options over short-form for readability when invoking commands:

- Use `--output` instead of `-o`.
- Use `--verbose` instead of `-v`.
- Use `--recursive` instead of `-r`.

The `repomatic` CLI defines both short and long-form options for convenience, but workflow files and scripts should use long-form options for clarity.

### uv flags in CI workflows

When invoking `uv` and `uvx` commands in GitHub Actions workflows:

- **`--no-progress`** on all CI commands (uv-level flag, placed before the subcommand). Progress bars render poorly in CI logs.
- **`--frozen`** on `uv run` commands (run-level flag, placed after `run`). Lockfile should be immutable in CI.
- **Flag placement:** `uv --no-progress run --frozen -- command` (not `uv run --no-progress`).
- **Exceptions:** Omit `--frozen` for `uvx` with pinned versions, `uv tool install`, CLI invocability tests, and local development examples.
- **Prefer explicit flags over environment variables** (`UV_NO_PROGRESS`, `UV_FROZEN`). Flags are self-documenting, visible in logs, avoid conflicts (e.g., `UV_FROZEN` vs `--locked`), and align with the long-form option principle.
