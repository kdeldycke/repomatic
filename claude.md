# Development guide

## Downstream repositories

This repository is the **canonical reference** for conventions. Repos using the `repomatic` CLI and its [`[tool.repomatic]` configuration](https://kdeldycke.github.io/repomatic/configuration.html) should mirror the patterns defined here for code style, documentation, testing, and design.

**Contributing upstream:** Propose improvements to the `repomatic` CLI, configuration, reusable workflows, or this file via PR or issue at [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic/issues).
**Upstream runtime dependency boundary:** The only runtime dependency on upstream is reusable workflow `uses:` calls (e.g., `kdeldycke/repomatic/.github/workflows/autofix.yaml@vX.Y.Z`), pinned to a git tag. Other references (PR body links, footer attribution) are informational. Do not introduce new runtime dependencies (Renovate shareable presets, remote config extends, API calls) — they create unversioned coupling where upstream breaks cascade to all downstream repos.

**Self-contained `claude.md`:** This file deploys as-is to downstream repos via `repomatic init`, so it must stand on its own: do not rely on user-level `~/.claude/CLAUDE.md` or other external instruction files. Every rule Claude needs must be inline here. When in doubt, restate.

## Documentation requirements

### Keeping `claude.md` lean

`claude.md` must contain only conventions, policies, rationale, and non-obvious rules Claude cannot discover by reading the codebase. Actively remove:

- **Structural inventories**: project trees, module tables, workflow lists. Discoverable via `Glob`/`Read`.
- **Code examples that duplicate source files**: YAML snippets copied from workflows, patterns visible in every module. Reference the source instead.
- **General programming knowledge**: standard idioms, well-known library usage, tool descriptions derivable from imports.
- **Implementation details readable from code**: what a function does, what a concurrency block looks like. Only the *rationale* for non-obvious choices belongs here.

### Changelog and docs updates

Always update documentation when making changes:

- **`changelog.md`**: One bullet per user-facing change, describing **what** changed (features, fixes, behavior changes), not **how** it was built or **why**. See [§ Changelog entry length](#changelog-entry-length); rationale belongs in `docs/`, code comments, the commit message, or the PR body, never the changelog.
- **`docs/`**: When this repo has a `docs/` tree, update the relevant page when adding or modifying workflow jobs, CLI commands, or configuration options.

**Order within a release section:** lead with `**Breaking:**` entries, then new features, then other changes, then bug fixes, then docs and tests. A reader scans for breaking changes first, so they belong at the top of the block.

#### Changelog entry length

A changelog entry is a **release note**, not a commit message or PR description. Its reader is scanning to decide two things: does this change affect me, and must I do anything? Write the shortest bullet that answers both, then stop.

- **One sentence by default**, roughly 10-25 words. Add a second short sentence only to flag a breaking change or state a migration step. A bullet past ~40 words is a smell: it is either smuggling in implementation detail (cut it) or covering two changes (split it into two bullets).
- **Keep the user-facing surface:** the public name (CLI command, option, config key, exported function or class), what the change does for the user, plus the required migration when the change breaks something. Lead with the change, not the mechanism.
- **Cut what the user cannot see or act on**, and move it where it belongs:
  - *Mechanism* (the internal module, file, function, or workflow job that implements it; the call sequence; the data structures): commit message, PR body, or code comment.
  - *Rationale* (why this approach, which edge case it covers, what trade-off was made): code or docstring comment, or `docs/`.
  - *Archaeology* (dependency floors chased mid-cycle, the bug's root cause, CI evaluation-order trivia, upstream-issue status): commit message or PR body. See also the "Do not mention" list below.
- **Name, don't narrate.** "Add `--cooldown` to skip packages newer than a given age" beats three sentences naming the environment variable each backend uses to enforce it.

The mechanical layer backs this up: `lint-changelog` warns (without failing) on any unreleased bullet over `[tool.repomatic] changelog.bullet-word-threshold` words. Released sections are immutable, so it leaves them alone.

Domain-neutral example. The verbose form crams a PR description into one bullet:

> *Add a `--forecast` option to the `weather` command that fetches the 7-day outlook from the bundled `weather._provider` module, parses the provider JSON, and falls back to the cached `~/.weather/last.json` when the network is unreachable; a non-200 response raises `ForecastError` so the CLI exits cleanly instead of printing a partial table. Requires `requests >= 2.0`.*

The release note is one line:

> *Add `weather --forecast` for a 7-day outlook, cached for offline use.*

The provider module, the JSON parsing, the error class, and the `requests` floor belong in the commit and PR, not the changelog.

**Do not mention in the changelog:**

- **Mechanical test updates following a behavior change.** Adjusting fixtures, snapshots, parametrize cases, or assertions to match a bumped dependency or renamed symbol is implicit. Only mention *structural* test work: a new harness, new fixture mechanism, switching `unittest.TestCase` to functions, parametrizing a whole module.
- **Short-shelf-life workarounds.** `tool.uv.exclude-newer-package` cooldown bypasses, dev pins for transient upstream bugs, `xfail` markers, commented-out lines: reverted within days, only add noise. Drop unless load-bearing beyond a release cycle.
- **Upstream issue commentary.** Cross-references to upstream tickets with prose about their state (`pallets/click#2771` open / closed / not planned, "the upstream conversation is in…", "mirrors the upstream fix in…", "Click does not ship an equivalent"). The status rots in days, the prose duplicates what `git blame` and the linked thread already show. A bare upstream link is acceptable when the entry is a direct backport (`fix … from upstream PR x/y#NNN`); anything longer belongs in a code comment, docstring, or PR body. Strip the prose during `consolidate`.

### Documentation sync (upstream maintainers)

```{note}
Applies only when developing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` through its reusable workflows can skip this section.
```

When working inside `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Documentation sync](https://kdeldycke.github.io/repomatic/upstream-development.html#documentation-sync) for the canonical list of documentation artifacts that must stay in sync with the package source code (PAT permissions, workflow job descriptions, version references, auto-generated tables).

### Knowledge placement

Each piece of knowledge has one canonical home, chosen by audience. Other locations get a brief pointer ("See `module.py` for rationale.").

| Audience              | Home                      | Content                                                                 |
| :-------------------- | :------------------------ | :---------------------------------------------------------------------- |
| GitHub visitors       | `readme.md`               | Landing page: pitch, quick start, links to docs.                        |
| End users             | `docs/`                   | Installation, configuration, dependencies, workflows, security, skills. |
| Setup walkthroughs    | `setup-guide.md` issue    | Step-by-step setup with deep links to repo settings pages.              |
| Developers            | Python docstrings         | Design decisions, trade-offs, "why" explanations.                       |
| Workflow maintainers  | YAML comments             | Brief "what" + pointer to Python code for "why."                        |
| Bug reporters         | `.github/ISSUE_TEMPLATE/` | Reproduction steps, version commands.                                   |
| Contributors / Claude | `claude.md`               | Conventions, policies, non-obvious rules.                               |

**YAML → Python distillation:** When workflow YAML files contain lengthy "why" explanations, migrate the rationale to Python module, class, or constant docstrings (using MyST admonitions like ```` ```{note} ```` and ```` ```{warning} ````). Trim the YAML comment to a one-line "what" plus a pointer: `# See {package}/{module}.py for rationale.`

### Documenting code decisions

Document design decisions, trade-offs, and non-obvious implementation choices directly in the code using MyST docstring admonitions (```` ```{warning} ````, ```` ```{note} ````, ```` ```{caution} ````), inline comments, and module-level docstrings for constants that need context.

### Example data

Example data everywhere (docs, docstrings, comments, workflows, fixtures) must be domain-neutral: cities, weather, fruits, animals, recipes. Do not reference the project, software engineering concepts, or package metadata. The reader should understand the example without knowing what the project is.

## File naming conventions

### Extensions: prefer long form

Use the longest, most explicit file extension available. For YAML, that means `.yaml` (not `.yml`). Apply the same principle to all extensions (e.g., `.html` not `.htm`, `.jpeg` not `.jpg`).

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

- **PyPI** (not ~~PyPi~~) — the Python Package Index. The "I" is capitalized because it stands for "Index". See [PyPI trademark guidelines](https://pypi.org/trademarks/).
- **GitHub** (not ~~Github~~)
- **GitHub Actions** (not ~~Github Actions~~ or ~~GitHub actions~~)
- **JavaScript** (not ~~Javascript~~)
- **TypeScript** (not ~~Typescript~~)
- **macOS** (not ~~MacOS~~ or ~~macos~~)
- **iOS** (not ~~IOS~~ or ~~ios~~)

<!-- typos:on -->

### Version formatting

The version string is always bare (e.g., `1.2.3`). The `v` prefix is a **tag namespace** — it only appears when the reference is to a git tag or something derived from a tag (action ref, comparison URL, commit message). This aligns with PEP 440, PyPI, and semver conventions.

**Rules:**

1. **No `v` prefix on package versions.** Anywhere the version identifies the *package* (PyPI, changelog heading, CLI output, `pyproject.toml`), use the bare version: `1.2.3`.
2. **`v` prefix on tag references.** Anywhere the version identifies a *git tag* (comparison URLs, action refs, commit messages, PR titles), use `v1.2.3`.
3. **Always backtick-escape versions in prose.** Both `v1.2.3` (tag) and `1.2.3` (package) are identifiers, not natural language. Wrap them in single backticks: `` `v1.2.3` ``, `` `1.2.3` ``.
4. **Development versions** follow PEP 440: `1.2.3.dev0` with optional `+{short_sha}` local identifier.

### GitHub cross-references in commit messages and PRs

Never write `#N` (a literal `#` followed by a number) in commit messages, PR titles, or PR bodies unless N is an actual issue/PR number in the target repo. GitHub auto-links every `#N` token, so positional refs like `test #1` or `tests #14 and #15` render as misleading cross-references to unrelated tickets. Use plain numbers (`test 1`, `tests 14 and 15`), backtick-quote the identifier when it names a slot (`` test `1` ``, `` item `14` ``), or rephrase (`the first test`, `the fourteenth case`).

### Comments and docstrings

- All comments in Python files must end with a period.
- Docstrings use MyST markdown (single-backtick inline code, `[text](url)` links, `` {role}`target` `` cross-references, ```` ```{directive} ```` admonitions); the `repomatic.myst_docstrings` Sphinx extension converts to reST at build time. For Sphinx operational detail (extension load order, admonition fence style, conversion lifecycle, `convert-to-myst` command, page-roster conventions, `conf.py` hygiene), see `.claude/agents/sphinx-docs.md`.
- **No Google-style docstring sections** (`Args:`, `Returns:`, `Raises:`, `Attributes:`, `Yields:`). Use reST field lists: `:param name:`, `:return:`, `:raises ExceptionType:`. This project does not use `sphinx.ext.napoleon`.
- Documentation in `./docs/` uses MyST markdown where possible.
- Keep lines within 88 characters in Python files (ruff default). Markdown files have no line-length limit — do not hard-wrap prose; let the renderer handle wrapping.
- Titles in markdown use sentence case.
- **Heading anchors:** Use the natural auto-generated anchor for cross-references. Add explicit MyST anchors (`(my-anchor)=`) only when the natural anchor is unavailable (duplicate headings, non-heading targets).
- **Parameter and return documentation:** Use reST field lists (`:param name:`, `:return:`, not `:returns:`). Markers pass through unchanged; content inside is MyST-converted (inline code, `{role}` references, links). Continuation lines indent to align with the description text above.
- **Dataclass field docs:** Document fields with attribute docstrings (string literal immediately after the field), not `:param:` entries in the class docstring. The class docstring is for the class purpose only.
- **CLI help text:** Click renders docstrings as plain text in `--help`. Avoid MyST markup in Click command docstrings — use plain text for command names, option names, paths.

### `__init__.py` files

Keep `__init__.py` minimal: easy to overlook, so avoid logic, constants, or re-exports. Acceptable: license headers, package docstrings, `from __future__ import annotations`, and `__version__` (standard root-package convention). Anything else belongs in a named module.

### Imports

- Import from the root package (`from {package} import cli`), not submodules when possible.
- Place imports at the top of the file, unless avoiding circular imports. **Never use local imports inside functions** — move them to the module level. Local imports hide dependencies, bypass ruff's import sorting, and obscure what a module depends on.
- **Version-dependent imports** (e.g., `tomllib` fallback for Python 3.10) go **after all normal imports** but **before the `TYPE_CHECKING` block**. This lets ruff freely sort the normal imports above without interference.

### `TYPE_CHECKING` block

Place a module-level `TYPE_CHECKING` block after all imports (including version-dependent conditional imports). Use `TYPE_CHECKING = False` (not `from typing import TYPE_CHECKING`) to avoid importing `typing` at runtime. See existing modules for the canonical pattern.

**Only add `TYPE_CHECKING = False` when there is a corresponding `if TYPE_CHECKING:` block.** If all type-checking imports are removed, remove the assignment too — a bare assignment with no consumer is dead code.

### Modern `typing` practices

Use modern equivalents from `collections.abc` and built-in types instead of `typing` imports. Use `X | Y` instead of `Union` and `X | None` instead of `Optional`. New modules should include `from __future__ import annotations` ([PEP 563](https://peps.python.org/pep-0563/)).

### Minimal inline type annotations

Omit type annotations on locals, loop variables, and assignments when mypy can infer the type from the right-hand side. Add one only when mypy errors: empty collections needing an element type (`items: list[Package] = []`), ambiguous `None` initializations, or unions mypy can't narrow. Function signatures are unaffected: always annotate parameters and return types.

### Python 3.10 compatibility

Project supports Python 3.10+. Unavailable syntax: multi-line f-string expressions (3.12+; split into concatenated strings), exception groups / `except*` (3.11+), `Self` type hint (3.11+; use `from typing_extensions import Self`).

### YAML workflows

For single-line commands, use plain inline `run:`. For multi-line, use the folded block scalar (`>`) which joins lines with spaces — no backslash continuations needed. Use literal block scalar (`|`) only when preserved newlines are required (multi-statement scripts, heredocs).

YAML lines may run up to 120 characters (`yamllint.yaml` sets `line-length: max: 120`): don't carry over Python's 88-character limit or reflexively wrap comments at 80. The same limit governs generated downstream workflows, so codegen-source comments (like `release.yaml`'s `publish-pypi` job) should fill to 120 too.

Jobs run on `ubuntu-slim` (a lean GitHub-hosted image) by default; generated downstream workflows inherit it. Don't reach for `ubuntu-latest` speculatively: move a job to a fuller image only when failure proves a needed tool is missing (`shfmt`, for one, is absent on slim).

### Naming conventions for automated operations

CLI commands, workflow job IDs, PR branch names, and PR body template names must all agree on the same verb prefix, keeping the conventions learnable and grepable across all four dimensions.

| Prefix     | Semantics                                         | Source of truth      | Idempotent? | Examples                                          |
| :--------- | :------------------------------------------------ | :------------------- | :---------- | :------------------------------------------------ |
| `sync-X`   | Regenerate from a canonical or external source.   | Template, API, repo  | Yes         | `sync-gitignore`, `sync-mailmap`, `sync-uv-lock`  |
| `update-X` | Compute from project state.                       | Lockfile, git log    | Yes         | `update-deps-graph`, `update-checksums`           |
| `format-X` | Rewrite to enforce canonical style.               | Formatter rules      | Yes         | `format-json`, `format-markdown`, `format-python` |
| `fix-X`    | Correct content (auto-fix).                       | Linter/checker rules | Yes         | `fix-typos`                                       |
| `lint-X`   | Check content without modifying it.               | Linter rules         | Yes         | `lint-changelog`                                  |
| `scan-X`   | Submit artifacts to an external analysis service. | External API         | Yes         | `scan-virustotal`                                 |

**Rules:**

1. **Pick the verb that matches the data source.** External template/API/canonical reference: `sync`. Local project state (lockfiles, git history, source code): `update`. Reformatting existing content: `format`.
2. **Name the specific tool or file, not a generic category.** The noun must identify a concrete tool, file, or resource (`sync-zizmor`, `sync-gitignore`). Avoid abstract groupings like `sync-linter-configs`. If a second tool joins a category, create a separate operation.
3. **All four dimensions must agree.** When adding a file-modifying operation, the CLI command, workflow job ID, PR branch name, and PR body template file name must all use the same `verb-noun` identifier (e.g., `sync-gitignore` everywhere). For read-only operations (`lint-*`), only the CLI command and workflow job ID apply.
4. **Function names follow the CLI name.** The Python function uses the underscore equivalent (`sync_gitignore` for `sync-gitignore`). On collision with an imported module, use the Click `name=` parameter (`@repomatic.command(name="update-deps-graph")` on function `deps_graph`) or append `_cmd` (`sync_uv_lock_cmd` to avoid colliding with `from .renovate import sync_uv_lock`).

### Automated operation contracts

Every automated operation follows the [naming conventions](#naming-conventions-for-automated-operations) and is [idempotent](#idempotency-by-default). For the detailed checklists of required properties, invariants, and optional elements for each operation type (sync, update, format/fix, lint, PR body templates), see [`docs/operation-contracts.md`](https://kdeldycke.github.io/repomatic/operation-contracts.html).

### Ordering conventions

Keep definitions sorted for readability and to minimize merge conflicts:

- **Workflow jobs**: Ordered by execution dependency (upstream jobs first), then alphabetically within the same level.
- **Python module-level constants and variables**: Alphabetically, unless a logical grouping or dependency order applies. Place hard-coded domain constants (like `NOT_ON_PYPI_ADMONITION`, `SKIP_BRANCHES`) at the top of the file, right after imports: they encode domain assertions and business rules, so surfacing them early shows readers the module's assumptions up front.
- **YAML configuration keys**: Alphabetically within each mapping level.
- **Documentation lists and tables**: Alphabetically, unless a logical order (e.g., chronological in changelog) takes precedence.

### Named constants

Do not inline named constants during refactors. If a constant has a name and a docstring, it exists for readability and grep-ability — preserve both. When moving code between modules, carry the constant with it rather than replacing it with a literal.

### Single source of truth for defaults

Every configurable default must live in exactly one place: the canonical config dataclass field default (in `kdeldycke/repomatic`, the `Config` dataclass in `repomatic/config.py`). All code derives it from the source (class-level default for static contexts, instance value at runtime) rather than repeating the literal: registry entries, CLI option fallbacks, function parameter defaults, module-level paths. When adding a default, grep for the literal value; if it appears elsewhere, point those occurrences at the canonical source. A duplicated literal is a sync failure waiting to happen.

Beyond code, a config field also surfaces in serialized command output (a non-string default needs format-safe encoding there) and in test fixtures that enumerate the config surface, so a change reaches past the dataclass: run the full test suite after adding or removing a field, not just the tests for the module you touched.

## Release checklist (upstream maintainers)

```{note}
Applies only when releasing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` follow their own release process.
```

When releasing `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Release checklist](https://kdeldycke.github.io/repomatic/upstream-development.html#release-checklist) for the complete list and links to the workflow design rationale.

## Testing guidelines

- Use `@pytest.mark.parametrize` when testing the same logic for multiple inputs. Prefer parametrize over copy-pasted test functions that differ only in their data — it deduplicates test logic, improves readability, and makes adding new cases trivial.

- Keep test logic simple with straightforward asserts.

- Tests should be sorted logically and alphabetically where applicable.

- Test coverage is tracked with `pytest-cov` and reported to Codecov.

- Do not use classes for grouping tests. Write test functions as top-level module functions. Use classes only when they provide shared fixtures, setup/teardown methods, or class-level state.

- **`@pytest.mark.once` for run-once tests.** Downstream repos can define a custom `once` marker (in `[tool.pytest].markers`) to tag tests that need to run once, not across the full CI matrix: CLI entry point invocability, plugin registration, package metadata checks. The main matrix filters them with `pytest -m "not once"`; a dedicated `once-tests` job runs them on a single runner, saving CI minutes.

- **CI-only pytest flags belong in workflow steps, not `[tool.pytest].addopts`.** Flags like `--cov-report=xml`, `--junitxml=junit.xml`, and `--override-ini=junit_family=legacy` produce CI-only artifacts; in `addopts` they pollute local runs. Keep `addopts` for flags that apply everywhere (`--cov`, `--cov-report=term`, `--durations`, `--numprocesses`); pass CI-specific flags in the workflow `run:` step.

- **Coverage configuration belongs in `[tool.coverage]`.** Use the `[tool.coverage]` section in `pyproject.toml` for `run.branch`, `run.source`, and `report.precision` instead of `--cov-branch` and similar in `addopts`. `addopts` should only contain `--cov` (to activate the plugin) and `--cov-report=term` (local feedback).

- **Write conformance tests when fixing a class of bugs.** When a bug represents a *category* (not a one-off), add a generic test that locks in the invariant for the whole category. Iterate over every member of the relevant set (registry entries, generator functions, exported symbols, data files, sorted lists) and assert the property uniformly via `@pytest.mark.parametrize` or a loop. This deters regressions in sibling code paths without inline checks in production code. Applies when the bug stems from a shared convention (sort order, naming pattern, format invariant, cross-reference integrity) and the invariant is checkable from the codebase alone (no fixtures or mocks). Model: `tests/test_readme.py::test_docs_generator_matches_in_tree_state` (every `docs_update.py` generator is a fixed point under `mdformat`). Shape: enumerate the population, assert on each, fail naming the violator.

- **Pass `encoding="UTF-8"` to `subprocess.run(..., text=True)` when output may contain non-ASCII bytes** (emoji in workflow `name:`, accented author names, translated strings). `text=True` alone decodes with the platform default (`cp1252` on Windows), so such output raises `UnicodeDecodeError` only in Windows CI while passing on macOS and Linux. Test helpers shelling out to `git show`/`git cat-file` are the usual offenders; production `read_text`/`write_text` calls already set it.

## Agent conventions

This repository uses two Claude Code agents defined in `.claude/agents/`. Definitions should be lean — if a rule belongs in `CLAUDE.md`, put it here and reference it from the agent file. Do not duplicate.

**Agents must be self-contained for downstream portability.** Agents deploy downstream via `repomatic init agents` as standalone files in `.claude/agents/`; Claude auto-invokes them from their `description:` frontmatter. As with skills, all knowledge must be inline or reference `claude.md` sections, not upstream `docs/` URLs or upstream-only paths. When mining session history, default to local `claude.md` updates; file an upstream proposal only when the pattern is generic across repos.

### Source of truth hierarchy

`CLAUDE.md` defines the rules. The codebase and GitHub (issues, PRs, CI logs) are what you measure against those rules. When they disagree, fix the code to match the rules. If the rules are wrong, fix `CLAUDE.md`.

### Common maintenance pitfalls

Patterns that recur across sessions — watch for these proactively:

- **Documentation drift** is the most frequent issue. Version references and workflow job descriptions in `docs/` go stale after every release or refactor. Always verify docs against actual output after changes.
- **CI debugging starts from the URL.** When a workflow fails, fetch the run logs first (`gh run view --log-failed`); don't guess. When the user points to a specific failure, diagnose that exact error, not adjacent or speculative ones.
- **Type-checking divergence.** Code that passes `mypy` locally may fail in CI where `--python-version 3.10` is used. Always consider the minimum supported Python version.
- **Trace to root cause before coding a fix.** When a bug surfaces, audit its scope across the codebase before writing the patch. If the same pattern appears in multiple places, the fix belongs at the shared layer. If only one call site is affected, check whether the data is on the wrong code path before adding logic to handle it where it lands.
- **Simplify before adding.** When asked to improve something, first ask whether existing code or tools already cover the case. Remove dead code and unused abstractions before introducing new ones.
- **Angle-bracket placeholders in bash code blocks.** The `mdformat-shfmt` plugin runs `shfmt` on fenced ```` ```bash ``` ```` blocks. `shfmt` parses `<foo>` as input redirection and `>` as output redirection, then moves them to the end. Use curly braces (`{foo}`) for placeholders in bash examples to avoid mangling.
- **Route through existing infrastructure, don't bypass it.** Before writing a new helper or merge function, check whether the codebase already handles the operation. A bug from data on the wrong code path is better fixed by routing it to the right path than by duplicating logic at the wrong one. If a file is handled by a generic copier when it should go through a structured merge, move it to the correct registry rather than adding special-case merge code at the call site.
- **Generator/formatter ping-pong is recurrent.** Any code that writes a checked-in Markdown file competes with `format-markdown` for the canonical layout. After touching such code, run the generator, then `repomatic run mdformat -- {file}`, then the generator again, confirming `git diff` stays empty across all three states. If it changes, align the generator with what mdformat produces. Grep for the same pattern in sibling generators and mirror the check in `tests/`.
- **Removing a bundled asset leaves downstream orphans.** Dropping a skill, agent, or workflow from `COMPONENTS` stops shipping it, but copies already materialized in downstream repos are invisible to stale-file detection. Add a `RemovedAsset` tombstone to `REMOVED_ASSETS` in `repomatic/registry.py` so `repomatic init` prunes the orphan. Skills and agents are content-gated (list the SHA-256 of every content shipped across the asset's lifetime; recipe in that tuple's docstring); workflows are fingerprint-gated (omit `hashes`, rely on the `uses:` line matching `UPSTREAM_REPO_SLUGS`). `test_removed_data_assets_are_tombstoned` and `test_removed_reusable_workflows_are_tombstoned` fail CI if a removed asset has no tombstone. A rename is a drop plus an add: tombstone the old name.

### Agent behavior policy

- **Never post to the web without explicit approval.** Do not create or comment on GitHub issues, PRs, or discussions, or post to any external service, without the user's explicit go-ahead. If approval is blocking, draft the content in a temporary markdown file for review.
- Agents make fixes in the working tree only: never commit, push, or create PRs. Exception: skills that run autonomously (`/babysit-ci`, `/repomatic-ship`) may commit and push, and must include a `Co-Authored-By` trailer for traceability; follow the skill's instructions when they override this rule.
- Prefer mechanical enforcement (tests, autofix jobs, linting checks) over prose rules. If a rule can be checked by code, it should be.
- Agent definitions should reference `CLAUDE.md` sections, not restate them.
- qa-engineer is the gatekeeper for agent definition changes.

### Skills

Skills in `.claude/skills/` are user-invocable only (`disable-model-invocation: true`) by default and follow agent conventions: lean definitions, no duplication with `CLAUDE.md`, reference sections instead of restating rules. Run `repomatic list-skills` to list them. **A skill that another skill invokes programmatically drops `disable-model-invocation`** to become model-invocable: the flag blocks the `Skill` tool, not just auto-triggering. `repomatic-changelog` is unlocked this way so `/repomatic-ship` can run its consolidation during end-of-cycle reconciliation; the caller lists `Skill` and `Agent` in `allowed-tools` and guards for graceful degradation (below).

**Skills must be self-contained for downstream portability.** Skills deploy downstream via `repomatic init skills` as standalone SKILL.md files. Downstream repos have no `docs/` directory and skills typically lack `WebFetch`, so all domain knowledge must be inline in the SKILL.md, not linked to `docs/` pages. Duplication between a skill and a docs page is intentional: `docs/` serves humans, the skill serves Claude at runtime. Add a docs cross-reference but keep the full content inline.

**Cross-references between skills and agents must degrade gracefully.** A "Next steps" line suggesting `/other-skill` is informational: the referenced skill may be excluded here (via `[tool.repomatic] exclude` or scope filtering). A *programmatic* call is the same: a skill invoking another through the `Skill` tool must fall back to a subagent or inline work when the target is excluded, never letting a missing skill abort the caller. Write prose so a missing cross-reference is a no-op, not a blocker.

### Mechanical vs analytical work

The `repomatic` ecosystem has two layers: a **mechanical layer** (CLI commands and CI workflows that deterministically sync, lint, format, and fix files on every push to `main`) and an **analytical layer** (judgment-based tasks requiring context comparison and trade-off analysis).

Skills should focus on the analytical gaps: custom job content analysis, cross-repo pattern comparison, judgment calls on intentional vs stale divergence, and interactive guidance. Do not duplicate what CI already handles mechanically — see [§ Automated operation contracts](#automated-operation-contracts) for what the mechanical layer covers.

## Design principles

### Philosophy

1. Create something that works (to provide business value).
2. Create something that's beautiful (to lower maintenance costs).
3. Work on performance.

### CLI and configuration as primary abstractions

The `repomatic` CLI and its `[tool.repomatic]` configuration in `pyproject.toml` are the project's primary interfaces. Everything else (reusable workflows, templates, label definitions) is a delivery mechanism. Implement new features in the CLI first; workflows call the CLI, not the reverse. Documentation leads with what the CLI does and how to configure it.

### Linting and formatting

[Linting](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-lint-yaml-jobs) and [formatting](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) are automated via GitHub workflows. Developers needn't run these manually but should make a best effort; pushing triggers the workflows, which catch issues and handle the nitpicking.

### Registry types own their query logic

Enums and dataclasses that carry metadata should also carry the methods that interpret it. When callers need to make a decision based on a field (scope, format, config key), the logic belongs on the type, not scattered across call sites.

Existing examples, all keeping logic on the type instead of branching at call sites: `RepoScope.matches(is_awesome, is_python)` (scope applicability), `NativeFormat.serialize(data)` (YAML/TOML/JSON serialization), `ArchiveFormat.tarfile_mode()` (tar open mode), `Component.is_enabled(config)` and `FileEntry.is_enabled(config)` (config-key lookup).

When adding a field to a registry type, ask: will callers branch on this value? If yes, add a method on the type. When fixing duplicated conditionals that all interpret the same field, the fix is a method, not a helper function elsewhere.

### Scope exclusions are defaults, not absolutes

`RepoScope` restrictions and `[tool.repomatic] exclude` entries only apply during bare `repomatic init` (no CLI arguments). Naming a component on the CLI, or listing it in `[tool.repomatic] include`, bypasses both scope and user-config exclusions, letting workflows materialize out-of-scope configs at runtime and users opt into scope-restricted items. Config key exclusions (`config_key` fields) always apply: user's `[tool.repomatic]` config is authoritative for feature flags.

`RepoScope` has three states: `ALL`, `AWESOME_ONLY` (only `awesome-*` repos), `PYTHON_ONLY` (only repos with a PEP 621 `[project].name`, detected via `repomatic.pyproject.is_python_project`). Awesome and Python are mutually exclusive in practice. A `pyproject.toml` declaring only `[tool.*]` tables (a dotfiles repo using `[tool.repomatic]` purely for config) is non-Python.

In the source repo, scope exclusions still remove out-of-scope components from `selected`, but stale-file detection is suppressed so bundled data files are never flagged for deletion.

### Keep logic in Python, not workflow YAML

Push anything beyond trivial wiring out of workflow YAML and into the CLI/library. Rather than duplicating `if:` conditions across workflow steps, compute them once in `repomatic metadata` and reference the result. Rather than hand-maintaining workflow content, generate or reshape it in Python: `repomatic.github.workflow_sync._render_publish_pypi_job` derives each downstream `publish-pypi` job from the canonical `release.yaml` instead of a separately maintained YAML fragment. Python is generic, testable, and under our control; GitHub Actions YAML is platform-specific, brittle, and stringly-typed, so shrinking the GHA-specific surface eases a future migration to another CI platform. A tested generator that fails loudly beats a static YAML artifact that duplicates generatable content and can silently drift.

### Defensive workflow design

GitHub Actions workflows face frequent race conditions, eventual consistency, and partial failures. Prefer a **belt-and-suspenders** approach: multiple independent mechanisms for correctness rather than a single guarantee. If a job depends on external state (tags, published packages, API availability), add a fallback or graceful default, and make operations [idempotent](#idempotency-by-default) where possible so re-runs are safe.

```{note}
Release-specific workflow design rationale for `kdeldycke/repomatic` itself (`workflow_run` checkout pitfall, immutable releases, concurrency strategies, freeze/unfreeze commit structure) lives in `docs/upstream-development.md` § Release checklist and the linked release engineering page. Downstream repos defining their own release flow can borrow these patterns but are not bound by them.
```

### Idempotency by default

Workflows and CLI commands must be safe to re-run: the same command twice with the same inputs produces the same result, with no errors or unwanted side effects (duplicate tags, duplicate PR comments, redundant file modifications).

**In practice:**

- Use `--skip-existing` or equivalent guards when creating resources (tags, releases, published packages).
- Check for existing state before writing (e.g., skip adding an admonition if it's already present, skip creating a PR if one already exists for the branch).
- Prefer upsert semantics over create-only semantics.
- Make file-modifying operations convergent: applying the same transformation to an already-transformed file should be a no-op.

**When idempotency is not achievable**, document the reason in a comment or docstring explaining what side effects occur on re-runs and why they are acceptable or unavoidable.

### Skip and move forward, don't rewrite history

When a release goes wrong — squash merge, broken artifact, bad metadata — prefer **skipping the version and releasing the next one** over reverting commits, force-pushing, or rewriting `main`. A burned version number is cheap; a botched automated recovery is not. This mirrors PyPI's [yank](https://peps.python.org/pep-0592/) model: preserve immutability, signal consumers to upgrade.

When designing new workflow safeguards, default to **detection + notification** rather than **detection + automated fix**. The blast radius of a missed notification is zero; the blast radius of a bad automated fix can be catastrophic.

### Command-line options

Always prefer long-form options over short-form for readability in workflow files and scripts (e.g., `--output` not `-o`, `--verbose` not `-v`).

### CLI commands that accept a `--lockfile` or similar path

When a CLI command accepts a path to a project file (`--lockfile path/to/uv.lock`), any subprocess needing the project context (`uv lock`, `uv audit`) must run with `cwd=path.parent`. Otherwise it resolves against the caller's working directory, not the target project.

### CLI output conventions

CLI commands that produce structured output should separate terminal display from file output:

- **Terminal:** Use `ctx.find_root().print_table(rows, headers)` which respects the global `--table-format` option (github, json, csv, etc.).
- **File output (`--output`):** Write markdown for PR bodies and CI consumption. Use `--output-format` to control transport encoding (e.g., `github-actions` for `$GITHUB_OUTPUT` heredoc wrapping) rather than detecting environment variables implicitly.
- **Boolean feature flags** (e.g., `--release-notes`) should use the `--flag/--no-flag` pattern so both directions are explicitly invocable from workflows.

### Prefer `uv` over `pip` in documentation

Documentation and install pages must use `uv` as the default package installer. When showing how to install the package, use `uv tool install` (for CLI tools) or `uv pip install` (for libraries/extras). Alternative installers (`pip`, `pipx`, etc.) may appear as secondary options in tab sets or dedicated sections, but `uv` must be the primary/default command shown.

### uv flags in CI workflows

When invoking `uv` and `uvx` commands in GitHub Actions workflows:

- **`--no-progress`** on all CI commands (uv-level flag, placed before the subcommand). Progress bars render poorly in CI logs.
- **`--frozen`** on `uv run` commands (run-level flag, placed after `run`). Lockfile should be immutable in CI.
- **Flag placement:** `uv --no-progress run --frozen -- command` (not `uv run --no-progress`).
- **Exceptions:** Omit `--frozen` for `uvx` with pinned versions, `uv tool install`, CLI invocability tests, and local development examples.
- **Prefer explicit flags over environment variables** (`UV_NO_PROGRESS`, `UV_FROZEN`). Flags are self-documenting, visible in logs, avoid conflicts (e.g., `UV_FROZEN` vs `--locked`), and align with the long-form option principle.
- **Per-group `requires-python` in `[tool.uv]`:** Downstream repos whose docs or other dependency groups need newer Python can restrict a group with `dependency-groups.docs = { requires-python = ">= 3.14" }`, preventing uv from installing incompatible dependencies on older Python.
