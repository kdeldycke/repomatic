# Development guide

## Downstream repositories

This repository is the **canonical reference** for conventions. Repos using the `repomatic` CLI and its [`[tool.repomatic]` configuration](https://kdeldycke.github.io/repomatic/configuration.html) should mirror the patterns here for code style, documentation, testing, and design.

**Contributing upstream:** Propose improvements to the `repomatic` CLI, configuration, reusable workflows, or this file via PR or issue at [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic/issues).
**Upstream runtime dependency boundary:** The only runtime dependency on upstream is reusable workflow `uses:` calls (like `kdeldycke/repomatic/.github/workflows/autofix.yaml@vX.Y.Z`), pinned to a git tag. Other references (PR body links, footer attribution) are informational. Do not introduce new runtime dependencies (Renovate shareable presets, remote config extends, API calls): they create unversioned coupling where upstream breaks cascade to all downstream repos.

**Self-contained `claude.md`:** This file deploys as-is to downstream repos via `repomatic init`, so it must stand on its own: do not rely on user-level `~/.claude/CLAUDE.md` or other external instruction files. Every rule Claude needs must be inline here.

## Documentation requirements

### Keeping `claude.md` lean

`claude.md` must contain only conventions, policies, rationale, and non-obvious rules Claude cannot discover by reading the codebase. Actively remove:

- **Structural inventories**: project trees, module tables, workflow lists. Discoverable via `Glob`/`Read`.
- **Code examples that duplicate source files**: YAML snippets copied from workflows, patterns visible in every module. Reference the source instead.
- **General programming knowledge**: standard idioms, well-known library usage, tool descriptions derivable from imports.
- **Implementation details readable from code**: what a function does. Only the *rationale* for non-obvious choices belongs here.

### Changelog and docs updates

Always update documentation when making changes:

- **`changelog.md`**: One bullet per user-facing change, describing **what** changed (features, fixes, behavior changes), not **how** it was built or **why**. See [§ Changelog entry length](#changelog-entry-length); rationale belongs in `docs/`, code comments, the commit message, or the PR body, never the changelog.
- **`docs/`**: When this repo has a `docs/` tree, update the relevant page when adding or modifying workflow jobs, CLI commands, or configuration options.

**Order within a release section:** `**Breaking:**` entries first, then new features, other changes, bug fixes, docs and tests (a reader scans for breaking changes first).

#### Changelog entry length

A changelog entry is a **release note**, not a commit message or PR description. The reader scans to decide: does this affect me, and must I do anything? Write the shortest bullet that answers both.

- **One sentence by default**, ~10-25 words. Add a second sentence only to flag a breaking change or migration step. A bullet past ~40 words is a smell: it smuggles in implementation detail (cut it) or covers two changes (split it).
- **Keep the user-facing surface:** the public name (CLI command, option, config key, exported function/class), what it does for the user, plus the migration when it breaks something. Lead with the change, not the mechanism.
- **Cut what the user cannot see or act on**, and move it: *mechanism* (the module/function/job implementing it) to the commit, PR, or code comment; *rationale* (why this approach, which edge case) to a code/docstring comment or `docs/`; *archaeology* (dependency floors chased mid-cycle, root cause, CI trivia) to the commit or PR.
- **Name, don't narrate.** "Add `--cooldown` to skip packages newer than a given age" beats three sentences naming the environment variable each backend uses.

`lint-changelog` warns (without failing) on any unreleased bullet over `[tool.repomatic] changelog.bullet-word-threshold` words. Released sections are immutable.

**Do not mention in the changelog:**

- **Mechanical test updates following a behavior change.** Adjusting fixtures, snapshots, parametrize cases, or assertions to match a bumped dependency or renamed symbol is implicit. Only mention *structural* test work: a new harness or fixture mechanism, switching `unittest.TestCase` to functions, parametrizing a whole module.
- **Short-shelf-life workarounds.** `tool.uv.exclude-newer-package` cooldown bypasses, dev pins for transient upstream bugs, `xfail` markers, commented-out lines: reverted within days. Drop unless load-bearing beyond a release cycle.
- **Upstream issue commentary.** Prose about a ticket's state (open/closed/not planned, "mirrors the upstream fix in…"). It rots in days and duplicates what `git blame` and the linked thread show. A bare upstream link is fine for a direct backport (`fix … from upstream PR x/y#NNN`); anything longer belongs in a code comment, docstring, or PR. Strip the prose during `consolidate`.

### Documentation sync (upstream maintainers)

```{note}
Applies only when developing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` through its reusable workflows can skip this section.
```

When working inside `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Documentation sync](https://kdeldycke.github.io/repomatic/upstream-development.html#documentation-sync) for the canonical list of documentation artifacts that must stay in sync with the package source code (PAT permissions, workflow job descriptions, version references, auto-generated tables).

### Knowledge placement

Each piece of knowledge has one canonical home, chosen by audience; other locations get a brief pointer ("See `module.py`.").

| Audience              | Home                      | Content                                                                 |
| :-------------------- | :------------------------ | :---------------------------------------------------------------------- |
| GitHub visitors       | `readme.md`               | Landing page: pitch, quick start, links to docs.                        |
| End users             | `docs/`                   | Installation, configuration, dependencies, workflows, security, skills. |
| Setup walkthroughs    | `setup-guide.md` issue    | Step-by-step setup with deep links to repo settings pages.              |
| Developers            | Python docstrings         | Design decisions, trade-offs, "why" explanations.                       |
| Workflow maintainers  | YAML comments             | Brief "what" + pointer to Python code for "why."                        |
| Bug reporters         | `.github/ISSUE_TEMPLATE/` | Reproduction steps, version commands.                                   |
| Contributors / Claude | `claude.md`               | Conventions, policies, non-obvious rules.                               |

**YAML → Python distillation:** migrate lengthy "why" explanations from workflow YAML to Python module/class/constant docstrings (MyST admonitions like ```` ```{note} ````). Trim the YAML comment to a one-line "what" plus a pointer: `# See {package}/{module}.py for rationale.`

### Documenting code decisions

Document design decisions, trade-offs, and non-obvious choices in the code: MyST docstring admonitions (```` ```{warning} ````, ```` ```{note} ````, ```` ```{caution} ````), inline comments, and module-level docstrings for constants that need context.

### Example data

Example data everywhere (docs, docstrings, comments, workflows, fixtures) must be domain-neutral: cities, weather, fruits, animals, recipes. Do not reference the project, software engineering concepts, or package metadata. The reader should understand the example without knowing what the project is.

## File naming conventions

### Extensions: prefer long form

Use the longest, most explicit file extension. For YAML, `.yaml` (not `.yml`); likewise `.html` not `.htm`, `.jpeg` not `.jpg`.

### Filenames: lowercase

Use lowercase filenames everywhere. Avoid shouting-case names like `FUNDING.YML` or `README.MD`.

### GitHub exceptions

GitHub silently ignores certain files unless they use the exact name it expects. These are the known hard constraints where you **cannot** use `.yaml` or lowercase:

| File                     | Required name                       |
| ------------------------ | ----------------------------------- |
| Issue form templates     | `.github/ISSUE_TEMPLATE/*.yml`      |
| Issue template config    | `.github/ISSUE_TEMPLATE/config.yml` |
| Funding config           | `.github/funding.yml`               |
| Release notes config     | `.github/release.yml`               |
| Issue template directory | `.github/ISSUE_TEMPLATE/`           |
| Code owners              | `CODEOWNERS`                        |

Workflows (`.github/workflows/*.yaml`) and action metadata (`action.yaml`) support both `.yml` and `.yaml`: use `.yaml`.

## Code style

### Terminology and spelling

Use correct capitalization for proper nouns and trademarked names:

<!-- typos:off -->

- **PyPI** (not ~~PyPi~~): the Python Package Index, capitalized "I" for "Index". See [PyPI trademark guidelines](https://pypi.org/trademarks/).
- **GitHub** (not ~~Github~~)
- **GitHub Actions** (not ~~Github Actions~~ or ~~GitHub actions~~)
- **JavaScript** (not ~~Javascript~~)
- **TypeScript** (not ~~Typescript~~)
- **macOS** (not ~~MacOS~~ or ~~macos~~)
- **iOS** (not ~~IOS~~ or ~~ios~~)

<!-- typos:on -->

### Version formatting

The version string is always bare (`1.2.3`). The `v` prefix is a **tag namespace**: it only appears when the reference is to a git tag or something derived from one (action ref, comparison URL, commit message). This aligns with PEP 440, PyPI, and semver.

**Rules:**

1. **No `v` prefix on package versions.** Where the version identifies the *package* (PyPI, changelog heading, CLI output, `pyproject.toml`), use the bare version: `1.2.3`.
2. **`v` prefix on tag references.** Where the version identifies a *git tag* (comparison URLs, action refs, commit messages, PR titles), use `v1.2.3`.
3. **Always backtick-escape versions in prose.** Both `v1.2.3` and `1.2.3` are identifiers: wrap them in single backticks.
4. **Development versions** follow PEP 440: `1.2.3.dev0` with optional `+{short_sha}` local identifier.

### GitHub cross-references in commit messages and PRs

Never write `#N` (a literal `#` followed by a number) in commit messages, PR titles, or PR bodies unless N is an actual issue/PR number in the target repo. GitHub auto-links every `#N`, so positional refs like `test #1` render as misleading cross-references. Use plain numbers (`test 1`, `tests 14 and 15`), backtick-quote a slot identifier (`` test `1` ``), or rephrase (`the first test`).

### Linking to external repositories in Markdown

In Markdown (changelog, `readme.md`, `docs/`, issue and PR bodies), link to another repository using GitHub's reference slug as the link text, not the raw URL:

- Issue or PR: `[owner/repo#N](https://github.com/owner/repo/issues/N)`. Issues and PRs share one number space; pick `/issues/N` or `/pull/N` to match the real type (GitHub redirects either way).
- Commit: `[owner/repo@shortsha](https://github.com/owner/repo/commit/fullsha)`.
- Repository homepage: `[owner/repo](https://github.com/owner/repo)`.

GitHub autolinks the bare `owner/repo#N` form only inside conversations (issues, PRs, commit messages), never in committed files, so the explicit link is what renders the compact slug in a Markdown file. Same-repo references drop the slug: `[#N](…/issues/N)`.

### Comments and docstrings

- All comments in Python files must end with a period.
- Docstrings use MyST markdown (single-backtick inline code, `[text](url)` links, `` {role}`target` `` cross-references, ```` ```{directive} ```` admonitions); `repomatic.myst_docstrings` converts to reST at build time. For Sphinx operational detail (fence style, `convert-to-myst`, page rosters, `conf.py` hygiene), see `.claude/agents/sphinx-docs.md`.
- **No Google-style docstring sections** (`Args:`, `Returns:`, `Raises:`, …; no `sphinx.ext.napoleon`). Use reST field lists: `:param name:`, `:return:` (not `:returns:`), `:raises ExceptionType:`. Markers pass through unchanged; their content is MyST-converted, and continuation lines indent to align with the description above.
- **Dataclass field docs:** attribute docstrings (a string literal immediately after the field), not `:param:` entries; the class docstring is for the class purpose only.
- **CLI help text:** Click renders docstrings as plain text in `--help`, so avoid MyST markup in Click command docstrings.
- Documentation in `./docs/` uses MyST markdown where possible.
- Keep Python lines within 88 characters (ruff default). Markdown has no line-length limit: do not hard-wrap prose.
- Titles in markdown use sentence case.
- **Heading anchors:** use the natural auto-generated anchor for cross-references; add explicit MyST anchors (`(my-anchor)=`) only when the natural one is unavailable (duplicate headings, non-heading targets).

### `__init__.py` files

Keep `__init__.py` minimal (easy to overlook): no logic, constants, or re-exports. Acceptable: license headers, package docstrings, `from __future__ import annotations`, `__version__`. Anything else belongs in a named module.

### Imports

- Import from the root package (`from {package} import cli`), not submodules, when possible.
- Imports go at the top of the file unless avoiding circular imports. **Never use local imports inside functions**: they hide dependencies and bypass ruff's import sorting.
- **Version-dependent imports** (like a `tomllib` fallback for Python 3.10) go after all normal imports but before the `TYPE_CHECKING` block, so ruff can sort the normal imports above.

### `TYPE_CHECKING` block

Place a module-level `TYPE_CHECKING` block after all imports (including version-dependent ones). Use `TYPE_CHECKING = False` (not `from typing import TYPE_CHECKING`) to avoid importing `typing` at runtime. **Only add it when there is a corresponding `if TYPE_CHECKING:` block**: a bare assignment with no consumer is dead code, so if all type-checking imports are removed, remove the assignment too.

### Modern `typing` practices

Use `collections.abc` and built-in types instead of `typing` imports; `X | Y` not `Union`, `X | None` not `Optional`. New modules include `from __future__ import annotations` ([PEP 563](https://peps.python.org/pep-0563/)).

### Minimal inline type annotations

Omit annotations on locals, loop variables, and assignments when mypy can infer from the right-hand side. Add one only when mypy errors (empty collections needing an element type like `items: list[Package] = []`, ambiguous `None` init, unions mypy can't narrow). Always annotate function parameters and return types.

### Python 3.10 compatibility

Project supports Python 3.10+. Unavailable syntax: multi-line f-string expressions (3.12+; split into concatenated strings), exception groups / `except*` (3.11+), `Self` type hint (3.11+; use `from typing_extensions import Self`).

### YAML workflows

Single-line commands: plain inline `run:`. Multi-line: the folded block scalar (`>`), which joins lines with spaces (no backslash continuations); use the literal scalar (`|`) only when preserved newlines are required (multi-statement scripts, heredocs).

YAML lines may run to 120 characters (`yamllint.yaml` sets `line-length: max: 120`): don't carry over Python's 88-char limit. The same limit governs generated downstream workflows, so codegen-source comments (like `release.yaml`'s `publish-pypi` job) should fill to 120 too.

Jobs default to `ubuntu-slim` (a lean image), and downstream workflows inherit it. Don't reach for `ubuntu-latest` speculatively: move a job to a fuller image only when failure proves a tool is missing (`shfmt` is absent on slim).

### Naming conventions for automated operations

CLI commands, workflow job IDs, PR branch names, and PR body template names must share the same verb prefix, keeping the conventions learnable and grepable.

| Prefix     | Semantics                                         | Source of truth      | Idempotent? | Examples                                          |
| :--------- | :------------------------------------------------ | :------------------- | :---------- | :------------------------------------------------ |
| `sync-X`   | Regenerate from a canonical or external source.   | Template, API, repo  | Yes         | `sync-gitignore`, `sync-mailmap`, `sync-uv-lock`  |
| `update-X` | Compute from project state.                       | Lockfile, git log    | Yes         | `update-deps-graph`, `update-checksums`           |
| `format-X` | Rewrite to enforce canonical style.               | Formatter rules      | Yes         | `format-json`, `format-markdown`, `format-python` |
| `fix-X`    | Correct content (auto-fix).                       | Linter/checker rules | Yes         | `fix-typos`                                       |
| `lint-X`   | Check content without modifying it.               | Linter rules         | Yes         | `lint-changelog`                                  |
| `scan-X`   | Submit artifacts to an external analysis service. | External API         | Yes         | `scan-virustotal`                                 |

**Rules:**

1. **Pick the verb that matches the data source.** External template/API/canonical reference: `sync`. Local project state (lockfiles, git history, source): `update`. Reformatting: `format`.
2. **Name the specific tool or file, not a generic category** (`sync-zizmor`, not `sync-linter-configs`). A second tool in a category gets its own operation.
3. **All four dimensions must agree.** A file-modifying operation uses one `verb-noun` for CLI command, workflow job ID, PR branch, and PR body template (`sync-gitignore` everywhere). Read-only operations (`lint-*`) use only the CLI command and job ID.
4. **Function names follow the CLI name** (`sync_gitignore` for `sync-gitignore`). On collision with an imported module, use the Click `name=` parameter (`@repomatic.command(name="update-deps-graph")` on `deps_graph`) or append `_cmd` (`sync_uv_lock_cmd`).
5. **A read-only command may expose mutation via `--fix`.** When a query command (like `audit`) gains a `--fix` autofix mode, the autofix operation keeps its own `fix-X` job ID, PR branch, and template and invokes `<command> --fix` (`fix-vulnerable-deps` runs `audit --fix`). That command name is then exempt from rule 3: it is a general-purpose query command (like `metadata`), not the operation's namesake.

### Automated operation contracts

Every automated operation follows the [naming conventions](#naming-conventions-for-automated-operations) and is [idempotent](#idempotency-by-default). For the detailed checklists of required properties, invariants, and optional elements for each operation type (sync, update, format/fix, lint, PR body templates), see [`docs/operation-contracts.md`](https://kdeldycke.github.io/repomatic/operation-contracts.html).

### Ordering conventions

Keep definitions sorted for readability and to minimize merge conflicts:

- **Workflow jobs**: by execution dependency (upstream jobs first), then alphabetically within the same level.
- **Python module-level constants**: alphabetically, unless a logical or dependency order applies. Place hard-coded domain constants (like `NOT_ON_PYPI_ADMONITION`, `SKIP_BRANCHES`) at the top, right after imports: they encode domain assertions, so surfacing them early shows the module's assumptions.
- **YAML configuration keys**: alphabetically within each mapping level.
- **Documentation lists and tables**: alphabetically, unless a logical order (like chronological in changelog) takes precedence.

### Named constants

Do not inline named constants during refactors: a named, documented constant exists for readability and grep-ability. When moving code between modules, carry the constant with it, don't replace it with a literal.

### Single source of truth for defaults

Every configurable default lives in exactly one place: the canonical config dataclass field default (the `Config` dataclass in `repomatic/config.py`). All code derives it from the source (class-level default for static contexts, instance value at runtime) rather than repeating the literal across registry entries, CLI option fallbacks, parameter defaults, or module-level paths. When adding a default, grep for the literal and point any other occurrence at the source.

A config field also surfaces in serialized command output (a non-string default needs format-safe encoding) and in test fixtures enumerating the config surface: run the full test suite after adding or removing a field, not just the module's own tests.

## Release checklist (upstream maintainers)

```{note}
Applies only when releasing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` follow their own release process.
```

When releasing `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Release checklist](https://kdeldycke.github.io/repomatic/upstream-development.html#release-checklist) for the complete list and links to the workflow design rationale.

## Testing guidelines

- Use `@pytest.mark.parametrize` for the same logic over multiple inputs, rather than copy-pasted test functions differing only in data.
- Keep test logic simple with straightforward asserts.
- Sort tests logically and alphabetically where applicable.
- Coverage is tracked with `pytest-cov` and reported to Codecov.
- No classes for grouping tests; write top-level functions. Use a class only for shared fixtures, setup/teardown, or class-level state.
- **`@pytest.mark.once` for run-once tests.** A custom `once` marker (in `[tool.pytest].markers`) tags tests that run once, not across the full matrix (CLI invocability, plugin registration, metadata checks). The main matrix filters with `pytest -m "not once"`; a dedicated `once-tests` job runs them on one runner.
- **CI-only pytest flags belong in workflow steps, not `[tool.pytest].addopts`.** `--cov-report=xml` produces a CI-only artifact and pollutes local runs if in `addopts`. Keep `addopts` for everywhere-flags (`--cov`, `--cov-report=term`, `--durations`, `--numprocesses`); pass CI-specific flags in the workflow `run:` step.
- **Coverage configuration belongs in `[tool.coverage]`** (`run.branch`, `run.source`, `report.precision`), not `--cov-branch` in `addopts`. `addopts` carries only `--cov` and `--cov-report=term`.
- **Write conformance tests when fixing a class of bugs.** For a bug that is a *category* (not a one-off), add a generic test locking in the invariant: iterate over every member of the set (registry entries, generators, exported symbols, data files) and assert the property uniformly via `@pytest.mark.parametrize` or a loop. Applies when the bug stems from a shared convention checkable from the codebase alone (no fixtures or mocks). Model: `tests/test_readme.py::test_docs_generator_matches_in_tree_state`. Shape: enumerate the population, assert on each, fail naming the violator.
- **Pass `encoding="UTF-8"` to `subprocess.run(..., text=True)` when output may contain non-ASCII bytes** (emoji in workflow `name:`, accented names). `text=True` alone uses the platform default (`cp1252` on Windows), raising `UnicodeDecodeError` only in Windows CI. Test helpers shelling out to `git show`/`git cat-file` are the usual offenders; production `read_text`/`write_text` already set it.

### Choosing test-matrix targets

`repomatic metadata` builds the full and PR test matrices from `[tool.repomatic.test-matrix.*]`. For the config reference, a runner-speed inventory, and a worked example, see [`docs/test-matrix.md`](https://kdeldycke.github.io/repomatic/test-matrix.html). The selection conventions:

- **Cover the shipped config broadly; probe unreleased axes narrowly; smoke-test released flavors.** Released dependencies on stable Python get the full cross-platform spread. Unreleased dependency branches and prerelease Python run on one runner as `continue-on-error` probes (`test-matrix.unstable`), never across platforms. A released free-threaded build (`3.14t`) runs **stable** on a single runner (a `python-version` variation pinned with `exclude`, left out of `unstable`), not as an `unstable` probe.
- **Pin the dependency floor, and any release a workaround targets.** Add the floor of a supported range as an explicit matrix value, plus any mid-range release a shim works around: that is the version that catches the shim regressing.
- **Select runners by measured speed and workload, not architecture** (read your own CI timings; the `docs/test-matrix.md` inventory has the numbers). The parallel `pytest --numprocesses=auto` suite favors `ubuntu-24.04-arm` (the test PR Linux slot), while setup-bound light jobs keep the lean `ubuntu-slim`. Where one fast runner suffices, `ubuntu-24.04-arm` is the default (fastest and cheapest tier; hosted macOS bills ~10x Linux), so `macos-26`/Windows are reserved for the OS coverage only they add. Drop the slower twin of an OS pair via `test-matrix.remove.os`.

## Agent conventions

This repository uses two Claude Code agents in `.claude/agents/`. Definitions stay lean: if a rule belongs in `CLAUDE.md`, put it there and reference it. Do not duplicate.

**Agents must be self-contained for downstream portability.** Agents deploy downstream via `repomatic init agents` as standalone files; Claude auto-invokes them from their `description:` frontmatter. All knowledge must be inline or reference `claude.md` sections, not upstream `docs/` URLs or upstream-only paths. When mining session history, default to local `claude.md` updates; file an upstream proposal only when the pattern is generic across repos.

### Source of truth hierarchy

`CLAUDE.md` defines the rules; the codebase and GitHub (issues, PRs, CI logs) are what you measure against them. When they disagree, fix the code to match; if the rules are wrong, fix `CLAUDE.md`.

### Common maintenance pitfalls

Patterns that recur across sessions, to watch for proactively:

- **Documentation drift** is the most frequent issue: version references and workflow job descriptions in `docs/` go stale after every release or refactor, so verify docs against actual output.
- **CI debugging starts from the URL.** When a workflow fails, fetch the run logs first (`gh run view --log-failed`), don't guess; when the user points to a specific failure, diagnose that exact one.
- **Type-checking divergence.** Code that passes `mypy` locally may fail in CI under `--python-version 3.10`; always check the minimum supported version.
- **Trace to root cause before coding a fix.** Audit a bug's scope before writing the patch. If the same pattern appears in multiple places, fix it at the shared layer; if only one call site is affected, check whether the data is on the wrong code path before handling it where it lands.
- **Simplify before adding.** When improving something, first check whether existing code or tools cover the case; remove dead code and unused abstractions before introducing new ones.
- **Angle-bracket placeholders in bash code blocks.** `mdformat-shfmt` runs `shfmt` on fenced ```` ```bash ``` ```` blocks, and `shfmt` parses `<foo>`/`>foo` as redirection and reorders the command. Use curly braces (`{foo}`) for placeholders in bash examples.
- **Route through existing infrastructure, don't bypass it.** Before writing a new helper or merge function, check whether the codebase already handles the operation. A bug from data on the wrong code path is better fixed by routing it correctly than by duplicating logic at the wrong site: move a misrouted file to the right registry rather than special-casing it at the call site.
- **Generator/formatter ping-pong is recurrent.** Any code that writes a checked-in Markdown file competes with `format-markdown` for the canonical layout. After touching such code, run the generator, then `repomatic run mdformat -- {file}`, then the generator again, confirming `git diff` stays empty across all three states; if not, align the generator with mdformat. Grep for the pattern in sibling generators and mirror the check in `tests/`.
- **`repomatic run {tool} --check` is unreliable for tools with a post-process fixup.** A few tools (currently `mdformat`) get a Python post-processing pass that only runs in write mode, so `--check` can report drift the write path would reconcile (false positive) or pass on files it would still rewrite (false negative). To verify or gate formatting, run the write path and inspect `git diff`, never `--check`.
- **Removing a bundled asset leaves downstream orphans.** Dropping a skill, agent, or workflow from `COMPONENTS` stops shipping it, but copies already in downstream repos are invisible to stale-file detection. Add a `RemovedAsset` tombstone to `REMOVED_ASSETS` in `repomatic/registry.py` so `repomatic init` prunes the orphan (the `RemovedAsset` docstring has the content- vs fingerprint-gating recipe); a CI test fails otherwise. A rename is a drop plus an add: tombstone the old name.

### Agent behavior policy

- **Never post to the web without explicit approval.** Do not create or comment on GitHub issues, PRs, or discussions, or post to any external service, without the user's explicit go-ahead. If approval is blocking, draft the content in a temporary markdown file for review.
- Agents make fixes in the working tree only: never commit, push, or create PRs. Exception: skills that run autonomously (`/babysit-ci`, `/repomatic-ship`) may commit and push, and must include a `Co-Authored-By` trailer; follow the skill's instructions when they override this rule.
- Prefer mechanical enforcement (tests, autofix jobs, lint checks) over prose rules. If a rule can be checked by code, it should be.
- Agent definitions reference `CLAUDE.md` sections, not restate them.
- qa-engineer is the gatekeeper for agent definition changes.

### Skills

Skills in `.claude/skills/` are user-invocable only (`disable-model-invocation: true`) by default and follow agent conventions: lean, no duplication with `CLAUDE.md`, reference sections instead of restating rules. Run `repomatic list-skills` to list them. **A skill another skill invokes programmatically drops `disable-model-invocation`** to become model-invocable (the flag blocks the `Skill` tool, not just auto-triggering): `repomatic-changelog` is unlocked so `/repomatic-ship` can run its consolidation, and the caller lists `Skill` and `Agent` in `allowed-tools` and guards for graceful degradation (below).

**Skills must be self-contained for downstream portability.** Skills deploy downstream via `repomatic init skills` as standalone SKILL.md files; downstream repos have no `docs/` and skills typically lack `WebFetch`, so all domain knowledge must be inline. Duplication between a skill and a docs page is intentional: `docs/` serves humans, the skill serves Claude at runtime.

**Cross-references between skills and agents must degrade gracefully.** A "Next steps" line suggesting `/other-skill` is informational; a *programmatic* call is the same: a skill invoking another through the `Skill` tool must fall back to a subagent or inline work when the target is excluded (via `[tool.repomatic] exclude` or scope filtering), never letting a missing skill abort the caller. Write prose so a missing cross-reference is a no-op, not a blocker.

### Mechanical vs analytical work

The `repomatic` ecosystem has a **mechanical layer** (CLI commands and CI workflows that deterministically sync, lint, format, and fix files on every push to `main`) and an **analytical layer** (judgment-based tasks needing context comparison and trade-offs). Skills focus on the analytical gaps (custom job content analysis, cross-repo pattern comparison, judgment on intentional vs stale divergence); don't duplicate what CI handles mechanically: see [§ Automated operation contracts](#automated-operation-contracts).

## Design principles

### Philosophy

1. Create something that works (to provide business value).
2. Create something that's beautiful (to lower maintenance costs).
3. Work on performance.

### CLI and configuration as primary abstractions

The `repomatic` CLI and its `[tool.repomatic]` configuration are the project's primary interfaces; everything else (workflows, templates, labels) is a delivery mechanism. Implement features in the CLI first; workflows call the CLI, not the reverse. Documentation leads with the CLI and its configuration.

### Linting and formatting

[Linting](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-lint-yaml-jobs) and [formatting](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) are automated via GitHub workflows. Developers needn't run them manually; pushing triggers the workflows, which catch issues and handle the nitpicking.

### Registry types own their query logic

Enums and dataclasses that carry metadata should also carry the methods that interpret it. When callers decide based on a field (scope, format, config key), the logic belongs on the type, not scattered across call sites (`RepoScope.matches(...)`, `NativeFormat.serialize(...)`, `Component.is_enabled(config)`). When adding a field, ask: will callers branch on this value? If yes, add a method. When fixing duplicated conditionals that interpret the same field, the fix is a method, not a helper elsewhere.

### Scope exclusions are defaults, not absolutes

`RepoScope` restrictions and `[tool.repomatic] exclude` entries apply only during bare `repomatic init` (no CLI arguments): naming a component on the CLI, or listing it in `[tool.repomatic] include`, bypasses both, letting workflows materialize out-of-scope configs and users opt into scope-restricted items. Config key exclusions (`config_key` fields) always apply: the user's `[tool.repomatic]` config is authoritative for feature flags.

`RepoScope` has three states: `ALL`, `AWESOME_ONLY` (only `awesome-*` repos), and `PYTHON_ONLY` (only repos with a PEP 621 `[project].name`, via `repomatic.pyproject.is_python_project`); a `pyproject.toml` with only `[tool.*]` tables (a dotfiles repo) is non-Python. In the source repo, scope exclusions still remove out-of-scope components from `selected`, but stale-file detection is suppressed so bundled data files are never flagged for deletion.

### Keep logic in Python, not workflow YAML

Push anything beyond trivial wiring out of workflow YAML into the CLI/library. Rather than duplicating `if:` conditions across steps, compute them once in `repomatic metadata` and reference the result. Rather than hand-maintaining workflow content, generate it in Python (see `repomatic.github.workflow_sync` for the rationale and the `publish-pypi` example): a tested generator that fails loudly beats a static artifact that can silently drift.

### Defensive workflow design

GitHub Actions workflows face race conditions, eventual consistency, and partial failures. Prefer **belt-and-suspenders**: multiple independent correctness mechanisms over a single guarantee. If a job depends on external state (tags, published packages, API availability), add a fallback or graceful default and make operations [idempotent](#idempotency-by-default) so re-runs are safe.

```{note}
Release-specific design rationale for `kdeldycke/repomatic` (the `workflow_run` checkout pitfall, immutable releases, concurrency, freeze/unfreeze structure) lives in `docs/upstream-development.md` § Release checklist. Downstream repos with their own release flow can borrow it but aren't bound by it.
```

### Idempotency by default

Workflows and CLI commands must be safe to re-run: the same command twice with the same inputs produces the same result, with no errant side effects (duplicate tags or PR comments, redundant file writes). Use `--skip-existing` or equivalent guards when creating resources; check for existing state before writing (skip an admonition already present, skip a PR that already exists for the branch); prefer upsert over create-only; make file-modifying operations convergent (re-applying is a no-op).

**When idempotency is not achievable**, document in a comment or docstring what side effects occur on re-runs and why they are acceptable.

### Skip and move forward, don't rewrite history

When a release goes wrong (squash merge, broken artifact, bad metadata), prefer **skipping the version and releasing the next one** over reverting, force-pushing, or rewriting `main`: a burned version number is cheap, a botched automated recovery is not (this mirrors PyPI's [yank](https://peps.python.org/pep-0592/) model). When designing new safeguards, default to **detection + notification** over **detection + automated fix**: the blast radius of a missed notification is zero; that of a bad automated fix can be catastrophic.

### click_extra is both a dependency and a release consumer

click_extra is both a runtime dependency and the framework whose release pipeline runs the *pinned* repomatic, so a click_extra change to a symbol repomatic imports can break the pinned repomatic from inside click_extra's own release. Two rules: (1) import only click_extra's public API, never an underscore-prefixed name (enforced by `tests/test_imports.py`, whose docstring carries the full rationale); (2) when such a change touches an API repomatic uses, release the fixed repomatic and bump click_extra's pin *before* releasing click_extra, since both run the pinned tag.

### Command-line options

Always prefer long-form options over short-form for readability in workflow files and scripts (`--output` not `-o`, `--verbose` not `-v`).

### CLI commands that accept a `--lockfile` or similar path

A CLI command taking a project-file path (`--lockfile path/to/uv.lock`) must run any context-needing subprocess (`uv lock`, `uv audit`) with `cwd=path.parent`, else it resolves against the caller's directory, not the target project.

### CLI output conventions

CLI commands that produce structured output should separate terminal display from file output:

- **Terminal:** use `ctx.find_root().print_table(rows, headers)`, which respects the global `--table-format` option (github, json, csv, etc.).
- **File output (`--output`):** write markdown for PR bodies and CI; use `--output-format` for transport encoding (like `github-actions` for `$GITHUB_OUTPUT` heredoc wrapping), not implicit env-var detection.
- **Boolean feature flags** (like `--release-notes`) should use the `--flag/--no-flag` pattern so both directions are explicitly invocable from workflows.

### Prefer `uv` over `pip` in documentation

Documentation and install pages must use `uv` as the default installer (`uv tool install` for CLI tools, `uv pip install` for libraries/extras). Other installers may appear as secondary options, but `uv` must be primary.

### uv flags in CI workflows

When invoking `uv` and `uvx` commands in GitHub Actions workflows:

- **`--no-progress`** on all CI commands (uv-level flag, before the subcommand): progress bars render poorly in CI logs.
- **`--frozen`** on `uv run` commands (run-level flag, after `run`): the lockfile should be immutable in CI.
- **Flag placement:** `uv --no-progress run --frozen -- command` (not `uv run --no-progress`).
- **Exceptions:** omit `--frozen` for `uvx` with pinned versions, `uv tool install`, CLI invocability tests, and local examples.
- **Prefer explicit flags over environment variables** (`UV_NO_PROGRESS`, `UV_FROZEN`): self-documenting, visible in logs, and free of conflicts (like `UV_FROZEN` vs `--locked`).
- **Per-group `requires-python` in `[tool.uv]`:** a group needing newer Python can be restricted with `dependency-groups.docs = { requires-python = ">= 3.14" }`, so uv won't install incompatible dependencies on older Python.

### Pin uv with `required-version`

Downstream Python repos pin uv in `[tool.uv]` to a bounded range (like `required-version = ">=0.11.23,<0.12"`), not floating, so CI and local development share one resolver (`astral-sh/setup-uv` reads it automatically). Prefer a bounded range over `==`, and bump the ceiling only once a new uv minor has regenerated the lock (`required-version` is a hard gate). repomatic manages this: `repomatic init uv` writes both policy pins (`required-version`, `exclude-newer`) from the bundled `uv.toml`, and `sync-uv-lock` re-applies them while leaving every other `[tool.uv]` key untouched. Pinning also eliminates the re-lock churn documented in `sync_uv_lock` (`repomatic/uv.py`).
