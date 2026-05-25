# Development guide

## Downstream repositories

This repository is the **canonical reference** for conventions. Repos using the `repomatic` CLI and its [`[tool.repomatic]` configuration](https://kdeldycke.github.io/repomatic/configuration.html) should mirror the patterns defined here for code style, documentation, testing, and design.

**Contributing upstream:** Propose improvements to the `repomatic` CLI, configuration, reusable workflows, or this file via PR or issue at [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic/issues).
**Upstream runtime dependency boundary:** The only runtime dependency on the upstream repo is reusable workflow `uses:` calls (e.g., `kdeldycke/repomatic/.github/workflows/autofix.yaml@vX.Y.Z`), version-pinned to a git tag. All other references (PR body links, footer attribution) are informational. Do not introduce new runtime dependencies (Renovate shareable presets, remote config extends, API calls) — they create unversioned coupling where an upstream break cascades to all downstream repos.

**Self-contained `claude.md`:** This file is deployed as-is to downstream repos via `repomatic init`. It must stand on its own: do not rely on the presence of any user-level `~/.claude/CLAUDE.md` or other external instruction file. Every rule Claude needs to follow when working in this repo (or a downstream repo) must be inline here. When in doubt, restate.

## Documentation requirements

### Keeping `claude.md` lean

`claude.md` must contain only conventions, policies, rationale, and non-obvious rules that Claude cannot discover by reading the codebase. Actively remove:

- **Structural inventories** — project trees, module tables, workflow lists. Claude can discover these via `Glob`/`Read`.
- **Code examples that duplicate source files** — YAML snippets copied from workflows, Python patterns visible in every module. Reference the source file instead.
- **General programming knowledge** — standard Python idioms, well-known library usage, tool descriptions derivable from imports.
- **Implementation details readable from code** — what a function does, what a workflow's concurrency block looks like. Only the *rationale* for non-obvious choices belongs here.

### Changelog and docs updates

Always update documentation when making changes:

- **`changelog.md`**: Add a bullet point describing **what** changed (new features, bug fixes, behavior changes), not **why**. Keep entries concise and actionable. Justifications and rationale belong in documentation (`docs/`, code comments), not in the changelog.
- **`docs/`**: When this repo has a `docs/` tree, update the relevant page when adding or modifying workflow jobs, CLI commands, or configuration options.

**Do not mention in the changelog:**

- **Test updates that follow mechanically from a behavior change.** Adjusting fixtures, snapshots, parametrize cases, or assertions to match a bumped dependency or a renamed symbol is implicit in the underlying change: don't list it. Only mention test work when it is *structural*: a new test harness, a new fixture mechanism, switching from `unittest.TestCase` to functions, introducing parametrization across a whole module, etc.
- **Temporary workarounds with a short shelf life.** `tool.uv.exclude-newer-package` cooldown bypasses, pinned dev versions to dodge a transient upstream bug, `xfail` markers waiting for a fix, commented-out lines: these typically get reverted within days and only add noise to the permanent record. Drop them. If a workaround is load-bearing for more than a release cycle, that's the moment to add an entry.

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

Example data everywhere (documentation, docstrings, comments, workflows, test fixtures) must be domain-neutral: cities, weather, fruits, animals, recipes, or similar real-world subjects. Do not reference the project itself, software engineering concepts, package metadata, or any project-internal details. The reader should understand the example without knowing what the project is.

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

GitHub silently ignores these unless they use the exact name shown. Workflows (`.github/workflows/*.yaml`) and action metadata (`action.yaml`) support both `.yml` and `.yaml` — use `.yaml`.

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

### Comments and docstrings

- All comments in Python files must end with a period.
- Docstrings use MyST markdown (single-backtick inline code, `[text](url)` links, `` {role}`target` `` cross-references, ```` ```{directive} ```` admonitions). The `repomatic.myst_docstrings` Sphinx extension converts them to reST at build time. For Sphinx-specific operational detail (extension load order, `mdformat`-friendly admonition fence style, conversion lifecycle, `convert-to-myst` migration command, page-roster conventions, `conf.py` hygiene), see `.claude/agents/sphinx-docs.md`.
- **No Google-style docstring sections** (`Args:`, `Returns:`, `Raises:`, `Attributes:`, `Yields:`). Use reST field lists: `:param name:`, `:return:`, `:raises ExceptionType:`. This project does not use `sphinx.ext.napoleon`.
- Documentation in `./docs/` uses MyST markdown where possible.
- Keep lines within 88 characters in Python files (ruff default). Markdown files have no line-length limit — do not hard-wrap prose; let the renderer handle wrapping.
- Titles in markdown use sentence case.
- **Heading anchors:** Use the natural auto-generated anchor for cross-references. Add explicit MyST anchors (`(my-anchor)=`) only when the natural anchor is unavailable (duplicate headings, non-heading targets).
- **Parameter and return documentation:** Use reST field lists (`:param name:`, `:return:`). The markers pass through unchanged, but content inside is MyST-converted (inline code, `{role}` references, links all work). Use `:return:`, not `:returns:`. Continuation lines are indented to align with the description text above.
- **Dataclass field docs:** Document fields with attribute docstrings (string literal immediately after the field), not `:param:` entries in the class docstring. The class docstring is for the class purpose only.
- **CLI help text:** Click renders docstrings as plain text in `--help`. Avoid MyST markup in Click command docstrings — use plain text for command names, option names, paths.

### `__init__.py` files

Keep `__init__.py` files minimal. They are easy to overlook when scanning a codebase, so avoid placing logic, constants, or re-exports in them. Acceptable content: license headers, package docstrings, `from __future__ import annotations`, and `__version__` (standard Python convention for the root package). Anything else belongs in a named module.

### Imports

- Import from the root package (`from {package} import cli`), not submodules when possible.
- Place imports at the top of the file, unless avoiding circular imports. **Never use local imports inside functions** — move them to the module level. Local imports hide dependencies, bypass ruff's import sorting, and make it harder to see what a module depends on.
- **Version-dependent imports** (e.g., `tomllib` fallback for Python 3.10) should be placed **after all normal imports** but **before the `TYPE_CHECKING` block**. This allows ruff to freely sort and organize the normal imports above without interference.

### `TYPE_CHECKING` block

Place a module-level `TYPE_CHECKING` block after all imports (including version-dependent conditional imports). Use `TYPE_CHECKING = False` (not `from typing import TYPE_CHECKING`) to avoid importing `typing` at runtime. See existing modules for the canonical pattern.

**Only add `TYPE_CHECKING = False` when there is a corresponding `if TYPE_CHECKING:` block.** If all type-checking imports are removed, remove the `TYPE_CHECKING = False` assignment too — a bare assignment with no consumer is dead code.

### Modern `typing` practices

Use modern equivalents from `collections.abc` and built-in types instead of `typing` imports. Use `X | Y` instead of `Union` and `X | None` instead of `Optional`. New modules should include `from __future__ import annotations` ([PEP 563](https://peps.python.org/pep-0563/)).

### Minimal inline type annotations

Omit type annotations on local variables, loop variables, and assignments when mypy can infer the type from the right-hand side. Add an explicit annotation only when mypy reports an error — e.g., empty collections needing a specific element type (`items: list[Package] = []`), `None` initializations where the intended type is ambiguous, or narrowing a union mypy cannot resolve. Function signatures are unaffected — always annotate parameters and return types.

### Python 3.10 compatibility

This project supports Python 3.10+. Unavailable syntax: multi-line f-string expressions (3.12+; split into concatenated strings instead), exception groups / `except*` (3.11+), `Self` type hint (3.11+; use `from typing_extensions import Self`).

### YAML workflows

For single-line commands, use plain inline `run:`. For multi-line, use the folded block scalar (`>`) which joins lines with spaces — no backslash continuations needed. Use literal block scalar (`|`) only when preserved newlines are required (multi-statement scripts, heredocs).

### Naming conventions for automated operations

CLI commands, workflow job IDs, PR branch names, and PR body template names must all agree on the same verb prefix. This consistency makes the conventions learnable and grepable across all four dimensions.

| Prefix     | Semantics                                         | Source of truth      | Idempotent? | Examples                                          |
| :--------- | :------------------------------------------------ | :------------------- | :---------- | :------------------------------------------------ |
| `sync-X`   | Regenerate from a canonical or external source.   | Template, API, repo  | Yes         | `sync-gitignore`, `sync-mailmap`, `sync-uv-lock`  |
| `update-X` | Compute from project state.                       | Lockfile, git log    | Yes         | `update-deps-graph`, `update-checksums`           |
| `format-X` | Rewrite to enforce canonical style.               | Formatter rules      | Yes         | `format-json`, `format-markdown`, `format-python` |
| `fix-X`    | Correct content (auto-fix).                       | Linter/checker rules | Yes         | `fix-typos`                                       |
| `lint-X`   | Check content without modifying it.               | Linter rules         | Yes         | `lint-changelog`                                  |
| `scan-X`   | Submit artifacts to an external analysis service. | External API         | Yes         | `scan-virustotal`                                 |

**Rules:**

1. **Pick the verb that matches the data source.** External template/API/canonical reference → `sync`. Local project state (lockfiles, git history, source code) → `update`. Reformatting existing content → `format`.
2. **Name the specific tool or file, not a generic category.** The noun must identify a concrete tool, file, or resource (`sync-zizmor`, `sync-gitignore`). Avoid abstract groupings like `sync-linter-configs`. If a second tool joins a category, create a separate operation.
3. **All four dimensions must agree.** When adding a file-modifying operation, the CLI command, workflow job ID, PR branch name, and PR body template file name must all use the same `verb-noun` identifier (e.g., `sync-gitignore` everywhere). For read-only operations (`lint-*`), only the CLI command and workflow job ID apply.
4. **Function names follow the CLI name.** The Python function uses the underscore equivalent (e.g., `sync_gitignore` for `sync-gitignore`). Exception: when the function name would collide with an imported module, use the Click `name=` parameter (e.g., `@repomatic.command(name="update-deps-graph")` on a function named `deps_graph`) or append a `_cmd` suffix (e.g., `sync_uv_lock_cmd` to avoid collision with `from .renovate import sync_uv_lock`).

### Automated operation contracts

Every automated operation follows the [naming conventions](#naming-conventions-for-automated-operations) and is [idempotent](#idempotency-by-default). For the detailed checklists of required properties, invariants, and optional elements for each operation type (sync, update, format/fix, lint, PR body templates), see [`docs/operation-contracts.md`](https://kdeldycke.github.io/repomatic/operation-contracts.html).

### Ordering conventions

Keep definitions sorted for readability and to minimize merge conflicts:

- **Workflow jobs**: Ordered by execution dependency (upstream jobs first), then alphabetically within the same dependency level.
- **Python module-level constants and variables**: Alphabetically, unless there is a logical grouping or dependency order. Hard-coded domain constants (e.g., `NOT_ON_PYPI_ADMONITION`, `SKIP_BRANCHES`) should be placed at the top of the file, immediately after imports. These constants encode domain assertions and business rules — surfacing them early gives readers an immediate sense of the assumptions the module operates under.
- **YAML configuration keys**: Alphabetically within each mapping level.
- **Documentation lists and tables**: Alphabetically, unless a logical order (e.g., chronological in changelog) takes precedence.

### Named constants

Do not inline named constants during refactors. If a constant has a name and a docstring, it exists for readability and grep-ability — preserve both. When moving code between modules, carry the constant with it rather than replacing it with a literal.

### Single source of truth for defaults

Every configurable default value must be defined in exactly one place: the canonical config dataclass (or equivalent settings holder) field default. All code that needs that value must derive it from the source (the class-level default for static contexts, or the instance value for runtime) rather than repeating the same literal. This applies to registry entries, CLI option fallbacks, function parameter defaults, and module-level path constructions. In `kdeldycke/repomatic`, the canonical holder is the `Config` dataclass in `repomatic/config.py`.

When adding a new default, grep the codebase for the literal value. If it already appears elsewhere, replace those occurrences with a reference to the canonical source. A duplicated literal is a sync failure waiting to happen.

## Release checklist (upstream maintainers)

```{note}
Applies only when releasing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` follow their own release process.
```

When releasing `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Release checklist](https://kdeldycke.github.io/repomatic/upstream-development.html#release-checklist) for the complete list and links to the workflow design rationale.

## Testing guidelines

- Use `@pytest.mark.parametrize` when testing the same logic for multiple inputs. Prefer parametrize over copy-pasted test functions that differ only in their data — it deduplicates test logic, improves readability, and makes it trivial to add new cases.

- Keep test logic simple with straightforward asserts.

- Tests should be sorted logically and alphabetically where applicable.

- Test coverage is tracked with `pytest-cov` and reported to Codecov.

- Do not use classes for grouping tests. Write test functions as top-level module functions. Only use test classes when they provide shared fixtures, setup/teardown methods, or class-level state.

- **`@pytest.mark.once` for run-once tests.** Downstream repos can define a custom `once` marker (in `[tool.pytest].markers`) to tag tests that only need to run once — not across the full CI matrix. Typical candidates: CLI entry point invocability, plugin registration, package metadata checks. The main test matrix filters them out with `pytest -m "not once"`, while a dedicated `once-tests` job runs them on a single runner. This avoids wasting CI minutes on redundant cross-platform runs.

- **CI-only pytest flags belong in workflow steps, not `[tool.pytest].addopts`.** Flags like `--cov-report=xml`, `--junitxml=junit.xml`, and `--override-ini=junit_family=legacy` produce artifacts only needed in CI. Placing them in `addopts` pollutes local test runs with `junit.xml` files and XML coverage reports. Keep `addopts` for flags that apply everywhere (`--cov`, `--cov-report=term`, `--durations`, `--numprocesses`). Pass CI-specific flags in the workflow `run:` step.

- **Coverage configuration belongs in `[tool.coverage]`.** Use the `[tool.coverage]` section in `pyproject.toml` for `run.branch`, `run.source`, and `report.precision` instead of `--cov=<source>`, `--cov-branch`, and `--cov-precision` flags in `addopts`. This keeps coverage configuration canonical and `addopts` clean. The pytest `addopts` should only contain `--cov` (to activate the plugin) and `--cov-report=term` (for local feedback).

- **Write conformance tests when fixing a class of bugs.** When you encounter a bug that represents a *category* (not a one-off), add a generic test that locks in the invariant for the whole category, not just the single occurrence. The test should iterate over every member of the relevant set (registry entries, generator functions, exported symbols, data files, sorted lists) and assert the property uniformly via `@pytest.mark.parametrize` or a loop. This deters regressions in adjacent code paths and informs future maintainers and agents of the convention without adding inline checks to production code. Heuristics for when a test belongs in this category:

  - The bug stems from a shared convention (sort order, naming pattern, format invariant, cross-reference integrity, ordering relative to a canonical list).
  - The fix touches one site but the same mistake could be made at any sibling site.
  - The invariant is checkable purely from the codebase (no fixtures or mocks needed).

  Model: `tests/test_readme.py::test_docs_generator_matches_in_tree_state` (every `docs_update.py` generator is a fixed point under `mdformat`). The common shape: enumerate the population, assert the invariant on each, fail with a message that names the violator.

## Agent conventions

This repository uses two Claude Code agents defined in `.claude/agents/`. Their definitions should be lean — if a rule belongs in `CLAUDE.md`, put it here and reference it from the agent file. Do not duplicate.

**Agents must be self-contained for downstream portability.** Agents are deployed to downstream repos via `repomatic init agents` as standalone files in `.claude/agents/`. Claude auto-invokes them based on their `description:` frontmatter. The same self-containment rule that applies to skills applies here: all knowledge an agent needs must be inline or reference `claude.md` sections, not upstream `docs/` URLs or upstream-only paths. When mining session history or distilling patterns, default to local `claude.md` updates; file an upstream proposal only when the pattern is generic across repos.

### Source of truth hierarchy

`CLAUDE.md` defines the rules. The codebase and GitHub (issues, PRs, CI logs) are what you measure against those rules. When they disagree, fix the code to match the rules. If the rules are wrong, fix `CLAUDE.md`.

### Common maintenance pitfalls

Patterns that recur across sessions — watch for these proactively:

- **Documentation drift** is the most frequent issue. Version references and workflow job descriptions in `docs/` go stale after every release or refactor. Always verify docs against actual output after changes.
- **CI debugging starts from the URL.** When a workflow fails, fetch the run logs first (`gh run view --log-failed`). Do not guess at the cause. When the user points to a specific failure, diagnose that exact error — do not wander into adjacent or speculative issues (e.g., analyzing Python 3.15 compatibility warnings when the user asked about mypy errors).
- **Type-checking divergence.** Code that passes `mypy` locally may fail in CI where `--python-version 3.10` is used. Always consider the minimum supported Python version.
- **Trace to root cause before coding a fix.** When a bug surfaces, audit its scope across the codebase before writing the patch. If the same pattern appears in multiple places, the fix belongs at the shared layer. If only one call site is affected, check whether the data is on the wrong code path before adding logic to handle it where it lands.
- **Simplify before adding.** When asked to improve something, first ask whether existing code or tools already cover the case. Remove dead code and unused abstractions before introducing new ones.
- **Angle-bracket placeholders in bash code blocks.** The `mdformat-shfmt` plugin runs `shfmt` on fenced ```` ```bash ``` ```` blocks. `shfmt` parses `<foo>` as shell input redirection (`< foo`) and `>` as output redirection, then moves them to the end of the command. Use curly braces (`{foo}`) for placeholders in bash examples to avoid mangling.
- **Route through existing infrastructure, don't bypass it.** Before writing a new helper or merge function, check whether the codebase already has a mechanism for the same operation. A bug caused by data taking the wrong code path is better fixed by routing data to the right path than by duplicating logic at the wrong one. If a file or config entry is handled by a generic copier when it should go through a structured merge, move it to the correct registry or component type rather than adding special-case merge code at the call site.
- **Generator/formatter ping-pong is recurrent.** Any code that writes a Markdown file checked into the repo competes with `format-markdown` for the canonical layout. After touching such code, run the generator, then `repomatic run mdformat -- <file>`, then the generator again, and confirm `git diff` is empty across all three states. If the file changes between steps, align the generator with what mdformat produces — not the other way around. When fixing one, grep for the same pattern in sibling generators and mirror the check in `tests/`.
- **Removing a bundled asset leaves downstream orphans.** Dropping a skill, agent, or workflow from the `COMPONENTS` registry stops shipping it, but copies already materialized in downstream repos are invisible to stale-file detection (it only inspects assets still in the registry). When you drop a bundled asset, add a `RemovedAsset` tombstone to `REMOVED_ASSETS` in `repomatic/registry.py` — with the SHA-256 of every content it shipped across its released lifetime, computed via the recipe in that tuple's docstring — so `repomatic init` prunes the orphan on the next run. The `test_removed_data_assets_are_tombstoned` test fails in CI if a removed skill/agent has no tombstone. A rename is a drop plus an add: tombstone the old name.

### Agent behavior policy

- **Never post to the web without explicit approval.** Do not create or comment on GitHub issues, PRs, or discussions, and do not post to any external service, without the user's explicit go-ahead. If approval is blocking, draft the content in a temporary markdown file and present it for review.
- Agents make fixes in the working tree only. Never commit, push, or create PRs. Exception: skills that run autonomously (e.g., `/babysit-ci` and `/repomatic-ship`) may commit and push, and must include a `Co-Authored-By` trailer for traceability; follow the skill's instructions when they explicitly override this rule.
- Prefer mechanical enforcement (tests, autofix jobs, linting checks) over prose rules. If a rule can be checked by code, it should be.
- Agent definitions should reference `CLAUDE.md` sections, not restate them.
- qa-engineer is the gatekeeper for agent definition changes.

### Skills

Skills in `.claude/skills/` are user-invocable only (`disable-model-invocation: true`) by default and follow agent conventions: lean definitions, no duplication with `CLAUDE.md`, reference sections instead of restating rules. Run `repomatic list-skills` to see all skills with descriptions. **A skill that another skill invokes programmatically drops `disable-model-invocation`** so it is model-invocable: the flag blocks the `Skill` tool, not just auto-triggering. `repomatic-changelog` is unlocked this way so `/repomatic-ship` can run its consolidation during the end-of-cycle reconciliation sweep; the caller lists `Skill` and `Agent` in `allowed-tools` and guards for graceful degradation (next paragraph).

**Skills must be self-contained for downstream portability.** Skills deploy to downstream repos via `repomatic init skills` as standalone SKILL.md files. Downstream repos have no `docs/` directory and skills typically lack `WebFetch`. All domain knowledge a skill needs must be inline in the SKILL.md: do not replace inline content with links to `docs/` pages. When the same knowledge appears in both a skill and a docs page, the duplication is intentional — `docs/` serves human readers, the skill serves Claude at runtime. Add a docs cross-reference but keep the full content inline.

**Cross-references between skills and agents must degrade gracefully.** A "Next steps" line suggesting `/other-skill` is informational: the referenced skill may be excluded in this repo (via `[tool.repomatic] exclude` or scope filtering). The same holds for a *programmatic* call: a skill that invokes another through the `Skill` tool must fall back to a subagent or inline work when the target is excluded, never letting a missing skill abort the caller. Likewise, an agent mentioning its teammate must remain useful when invoked alone. Write skill and agent prose so a missing cross-reference is a no-op, not a blocker.

### Mechanical vs analytical work

The `repomatic` ecosystem has two layers: a **mechanical layer** (CLI commands and CI workflows that deterministically sync, lint, format, and fix files on every push to `main`) and an **analytical layer** (judgment-based tasks requiring context comparison and trade-off analysis).

Skills should focus on the analytical gaps: custom job content analysis, cross-repo pattern comparison, judgment calls on intentional vs stale divergence, and interactive guidance. Do not duplicate what CI already handles mechanically — see [§ Automated operation contracts](#automated-operation-contracts) for what the mechanical layer covers.

## Design principles

### Philosophy

1. Create something that works (to provide business value).
2. Create something that's beautiful (to lower maintenance costs).
3. Work on performance.

### CLI and configuration as primary abstractions

The `repomatic` CLI and its `[tool.repomatic]` configuration in `pyproject.toml` are the project's primary interfaces. Everything else — reusable workflows, templates, label definitions — is a delivery mechanism. New features should be implemented in the CLI first; workflows should call the CLI, not the other way around. Documentation should lead with what the CLI does and how to configure it.

### Linting and formatting

[Linting](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-lint-yaml-jobs) and [formatting](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) are automated via GitHub workflows. Developers don't need to run these manually during development, but are still expected to do best effort. Push your changes and the workflows will catch any issues and perform the nitpicking.

### Registry types own their query logic

Enums and dataclasses that carry metadata should also carry the methods that interpret it. When callers need to make a decision based on a field (scope, format, config key), the logic belongs on the type, not scattered across call sites.

Existing examples:

- `RepoScope.matches(is_awesome, is_python)` encapsulates scope applicability instead of `is_awesome and scope == AWESOME_ONLY or is_python and scope == PYTHON_ONLY or ...` repeated at every check.
- `NativeFormat.serialize(data)` encapsulates format-specific serialization (YAML/TOML/JSON) instead of an if/elif/elif chain.
- `ArchiveFormat.tarfile_mode()` encapsulates the tar open mode instead of an inline ternary.
- `Component.is_enabled(config)` and `FileEntry.is_enabled(config)` encapsulate config key lookup instead of `_config_flag(config, X.config_key, X.config_default)`.

When adding a new field to a registry type, ask: will callers branch on this value? If yes, add a method on the type. When fixing duplicated conditionals, check whether they are all interpreting the same field: if so, the fix is a method, not a helper function elsewhere.

### Scope exclusions are defaults, not absolutes

`RepoScope` restrictions and `[tool.repomatic] exclude` entries only apply during bare `repomatic init` (no CLI arguments). Explicitly naming a component on the CLI, or listing it in `[tool.repomatic] include`, bypasses both scope and user-config exclusions. This lets workflows materialize out-of-scope configs at runtime and lets users opt into scope-restricted items via config. Config key exclusions (`config_key` fields) always apply regardless: the user's `[tool.repomatic]` config is authoritative for feature flags.

`RepoScope` has three states: `ALL`, `AWESOME_ONLY` (only `awesome-*` repos), `PYTHON_ONLY` (only repos with a PEP 621 `[project].name` in `pyproject.toml`, detected via `repomatic.pyproject.is_python_project`). Awesome and Python are mutually exclusive in practice. A `pyproject.toml` that only declares `[tool.*]` tables (a dotfiles repo using `[tool.repomatic]` purely for configuration) is treated as non-Python.

In the source repo, scope exclusions still remove out-of-scope components from `selected`, but stale-file detection is suppressed so bundled data files are never flagged for deletion.

### Metadata-driven workflow conditions

Rather than duplicating `if:` conditions on every workflow step, augment the `repomatic metadata` subcommand to compute the condition once and reference it from workflow steps. Python code in `repomatic` is simpler to maintain, test, and debug than complex GitHub Actions workflow logic.

### Defensive workflow design

GitHub Actions workflows run in an environment where race conditions, eventual consistency, and partial failures are common. Prefer a **belt-and-suspenders** approach: use multiple independent mechanisms to ensure correctness rather than relying on a single guarantee. If a job depends on external state (tags, published packages, API availability), add a fallback or a graceful default. When possible, make operations [idempotent](#idempotency-by-default) so re-runs are safe.

```{note}
Release-specific workflow design rationale for the `kdeldycke/repomatic` package itself (`workflow_run` checkout pitfall, immutable releases, concurrency strategies, freeze/unfreeze commit structure) lives in `docs/upstream-development.md` § Release checklist and the linked release engineering page. Downstream repos defining their own release flow can borrow these patterns but are not bound by them.
```

### Idempotency by default

Workflows and CLI commands must be safe to re-run. Running the same command or workflow twice with the same inputs should produce the same result without errors or unwanted side effects (e.g., duplicate tags, duplicate PR comments, redundant file modifications).

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

When a CLI command accepts a path to a project file (e.g., `--lockfile path/to/uv.lock`), any subprocess that needs the project context (like `uv lock`, `uv audit`) must run with `cwd=path.parent`. Otherwise the subprocess resolves against the caller's working directory, not the target project.

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
- **Per-group `requires-python` in `[tool.uv]`:** Downstream repos whose docs or other dependency groups require newer Python features can restrict specific groups with `dependency-groups.docs = { requires-python = ">= 3.14" }`. This prevents uv from installing incompatible dependencies when running on older Python versions.
