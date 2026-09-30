# Development guide

Project-specific guidance for developing `repomatic` itself. The generic coding conventions load from the maintainer's machine configuration and are deliberately not carried here: this file holds only what is specific to this repository.

## Downstream repositories

This repository is the **canonical reference** for conventions. Repos using the `repomatic` CLI and its [`[tool.repomatic]` configuration](https://repomatic.net/configuration) should mirror the patterns here for code style, documentation, testing, and design.

**Contributing upstream:** Propose improvements to the `repomatic` CLI, configuration, reusable workflows, or this file via PR or issue at [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic/issues).
**Upstream runtime dependency boundary:** The only runtime dependency on upstream is reusable workflow `uses:` calls (like `kdeldycke/repomatic/.github/workflows/autofix.yaml@vX.Y.Z`), pinned to a git tag. Other references (PR body links, footer attribution) are informational. Do not introduce new runtime dependencies (Renovate shareable presets, remote config extends, API calls): they create unversioned coupling where upstream breaks cascade to all downstream repos.

### Trying an unreleased fix from downstream

A fix landed on upstream `main` reaches a repository running a released `repomatic` only at the next release. To validate it ahead of that, run the CLI from the git pin, carrying the cooldown on the dependency tree `uvx` resolves beside it, and from the downstream checkout so `[tool.repomatic]` and repository-name discovery see the right project:

```shell-session
$ uvx --no-progress --exclude-newer '{minimum-release-age}' --from 'git+https://github.com/kdeldycke/repomatic@{commit-sha}' repomatic {command}
```

The pin is a testing tool, not a deployment: workflow `uses:` refs and `uvx 'repomatic==X.Y.Z'` pins move only through a release.

## Repository-specific addenda to the generic conventions

The generic conventions these rules add to are maintained in the maintainer's home configuration and load into every session on that machine. What follows is the part of them that is specific to this repository.

### Documentation sync (upstream maintainers)

```{note}
Applies only when developing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` through its reusable workflows can skip this section.
```

When working inside `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Documentation sync](https://repomatic.net/upstream-development#documentation-sync) for the canonical list of documentation artifacts that must stay in sync with the package source code (PAT permissions, workflow job descriptions, version references, auto-generated tables).

### Changelog: what counts as breaking here

`repomatic` is invoked rather than imported, so `**Breaking:**` here covers the surfaces a user touches: CLI commands and options, `[tool.repomatic]` config keys, reusable-workflow inputs, job names and outcomes, `repomatic show-metadata` output keys, and bundled assets.

### CLI and configuration as primary abstractions

The `repomatic` CLI and its `[tool.repomatic]` configuration are the project's primary interfaces; everything else (workflows, templates, labels) is a delivery mechanism. Implement features in the CLI first; workflows call the CLI, not the reverse. Documentation leads with the CLI and its configuration.

### Registry types own their query logic

Enums and dataclasses that carry metadata should also carry the methods that interpret it. When callers decide based on a field (scope, format, config key), the logic belongs on the type, not scattered across call sites (`RepoScope.matches(...)`, `NativeFormat.serialize(...)`, `Component.is_enabled(config)`). When adding a field, ask: will callers branch on this value? If yes, add a method. When fixing duplicated conditionals that interpret the same field, the fix is a method, not a helper elsewhere.

### Scope exclusions are defaults, not absolutes

`RepoScope` restrictions and `[tool.repomatic] exclude` entries apply only during bare `repomatic init` (no CLI arguments): naming a component on the CLI, or listing it in `[tool.repomatic] include`, bypasses both, letting workflows materialize out-of-scope configs and users opt into scope-restricted items. Config key exclusions (`config_key` fields) always apply: the user's `[tool.repomatic]` config is authoritative for feature flags.

{class}`repomatic.registry.RepoScope` holds the four states, and the rule that picks between the two Python scopes.

### click_extra is both a dependency and a release consumer

click_extra is both a runtime dependency and the framework whose release pipeline runs the *pinned* repomatic, so a click_extra change to a symbol repomatic imports can break the pinned repomatic from inside click_extra's own release. Two rules: (1) import only click_extra's public API, never an underscore-prefixed name (enforced by `tests/test_imports.py`, whose docstring carries the full rationale); (2) when such a change touches an API repomatic uses, release the fixed repomatic and bump click_extra's pin *before* releasing click_extra, since both run the pinned tag.

### Cooldown: consuming repomatic from the lockfile

Workflows on `main` run the CLI from the lockfile rather than resolving it fresh: {data}`repomatic.release.prepare_release.LOCAL_CLI_INVOCATION` says why. See [`docs/dependencies.md` § `exclude-newer-package` cooldown overrides](https://repomatic.net/dependencies#exclude-newer-package-cooldown-overrides) for what an index resolution leaves reachable.

Running from the lockfile creates its own hazard: **a floor inside the window is now invisible here and breaks only the people installing the release** (downstream repos running a frozen workflow's `uvx 'repomatic==X.Y.Z'`, and `uvx repomatic` users). `tests/test_dep_sources.py` is what catches it, so treat that test failing as "this release is not shippable yet", not as a local annoyance to wait out.

When adding a tool that repomatic shells out to, register it and reach it through {func}`repomatic.tooling.tool_runner.ensure_binary` rather than `$PATH`: a registry binary carries the cooldown, the pin and the checksum at once, and `$PATH` carries none of the three.

### Documented exemptions this repository claims

Three installs deliberately bypass the window. The first two are per-package and never widen to the rest of the tree; the third is a whole job, and says why it has to be.

- **The upstream toolkit's own pin.** `repomatic` runs from a pin that moves in lockstep with the `uses:` refs pointing at it, so a release must be installable the minute it is published or every downstream repo breaks until the window elapses. The release freeze emits an `--exclude-newer-package` escape hatch beside the pin it writes.
- **A security fix still inside the window.** `audit --fix` reaches a CVE fix through an `exclude-newer-package` entry rather than lifting `exclude-newer` for everything.
- **The `test-package-install` job.** Its subject *is* the freshly published artifact, so a cooldown would make the question it exists to answer unanswerable. Scoping the opt-out to one job is what keeps it honest: it holds no secrets, inherits `permissions: {}`, and only runs `--version` on a throwaway runner.

### Commit messages: the `[changelog]` prefix invariant

Only `[changelog] …` qualifies as a parsed bracket prefix here, and it is an invariant, not a convention: {data}`repomatic.git_ops.CHANGELOG_COMMIT_PREFIX` states it, and `tests/test_workflows.py::test_changelog_prefix_is_the_machinery_invariant` holds the prefix set, the gates, and the emitting template together.

### Directives are written in Simplified Technical English

The generic conventions bind every text artifact to ASD-STE100. Only part of that is mechanically checkable, so only that part is enforced: `tests/test_claude_assets.py` holds every bundled skill and agent to a 25-word ceiling on each *directive*, and leaves the rationale after it alone. Its docstrings say what counts as a directive, why the ceiling binds each sentence of a `description` and never the whole field, and why the approved-word dictionary stays out of scope.

A directive over the ceiling is nearly always two rules sharing a bullet, or a rule with its exception folded in. Split it; do not raise the number.

### Defaults: the `Config` dataclass surface

Every configurable default lives in exactly one place: the `Config` dataclass in `repomatic/config.py`; all code derives it from there rather than repeating the literal.

A config field also surfaces in serialized command output (a non-string default needs format-safe encoding) and in test fixtures enumerating the config surface: run the full test suite after adding or removing a field, not just the module's own tests.

### Test suite: hermeticity boundaries

The suite is hermetic against the host's own `repomatic` configuration, and not against this repository's `[tool.repomatic]`: the `_isolate_user_config` fixture in `tests/conftest.py` says where the isolation stops. Pass an explicit `Config()` in any test asserting on default behaviour, and treat a test that breaks when you flip a `[tool.repomatic]` key as coupled rather than as a real regression.

### Release-specific design rationale

```{note}
Release-specific design rationale for `kdeldycke/repomatic` (the `workflow_run` checkout pitfall, immutable releases, concurrency, freeze/unfreeze structure) lives in `docs/upstream-development.md` § Release checklist. Downstream repos with their own release flow can borrow it but aren't bound by it.
```

### Pin uv: enforcement internals

`tests/test_workflows.py` fails on a `setup-uv` step without `with: version:`, or on two steps naming different versions. {func}`repomatic.deps.uv.sync_uv_lock` says why `uv.lock` stays stable across uv minors.

`repomatic init uv` writes both policy pins (`required-version`, `exclude-newer`) from the bundled `uv.toml`, and `sync-uv-lock` re-applies them while leaving every other `[tool.uv]` key untouched.

### Naming conventions for automated operations

CLI commands, workflow job IDs, PR branch names, and PR body template names must share the same verb prefix, keeping the conventions learnable and grepable.

| Prefix     | Semantics                                          | Source of truth      | Idempotent? | Examples                                          |
| :--------- | :------------------------------------------------- | :------------------- | :---------- | :------------------------------------------------ |
| `sync-X`   | Regenerate from a canonical or external source.    | Template, API, repo  | Yes         | `sync-gitignore`, `sync-mailmap`, `sync-uv-lock`  |
| `update-X` | Compute from project state.                        | Lockfile, git log    | Yes         | `update-dep-graph`, `update-checksums`            |
| `format-X` | Rewrite to enforce canonical style.                | Formatter rules      | Yes         | `format-json`, `format-markdown`, `format-python` |
| `fix-X`    | Correct content (auto-fix).                        | Linter/checker rules | Yes         | `fix-typos`                                       |
| `lint-X`   | Check content without modifying it.                | Linter rules         | Yes         | `lint-changelog`                                  |
| `pack-X`   | Assemble a distributable artifact set for release. | Repository tree      | Yes         | `pack-binaries`, `pack-plugin`                    |
| `sample-X` | Record an external reading into a local history.   | External API         | Per period  | `sample-metrics`                                  |
| `scan-X`   | Submit artifacts to an external analysis service.  | External API         | Yes         | `scan-virustotal`                                 |
| `{noun}`   | Maintain a GitHub issue tracking a repo condition. | GitHub API, settings | Yes         | `setup-guide`                                     |

Nine rules settle the edge cases: read [`docs/operation-contracts.md` § Naming rules](https://repomatic.net/operation-contracts#naming-rules) before adding or renaming an operation.

### Automated operation contracts

Every automated operation follows the [naming conventions](#naming-conventions-for-automated-operations) and is idempotent. For the detailed checklists of required properties, invariants, and optional elements for each operation type (sync, update, format/fix, lint, pack, scan, PR body templates), see [`docs/operation-contracts.md`](https://repomatic.net/operation-contracts).

### Upstream findings land here

The generic rule lands a finding that belongs to `repomatic` as uncommitted edits in a sibling `../repomatic` checkout. Inside `kdeldycke/repomatic` there is no `../repomatic`, so "propose it upstream" collapses into "fix it here" and stopping before the commit only defers the fix to a later cycle while the diff collects conflicts against whatever the machinery rewrites meanwhile. Implement it, verify it with whichever checks the change touches, and commit it like any other work. A release is when this pays off rather than a reason to hold it back: `prepare-release` regenerates the release pull request on every push to the default branch, replaying the freeze onto the new head, so a fix landed before the merge ships in *that* release instead of the next. Keep the pass bounded to what the session actually surfaced, though: anything needing more than a contained edit, or touching code the release itself depends on, stays an uncommitted diff plus a note, exactly as it would downstream.

### Bundled agents and skills

This repository uses three Claude Code agents in `.claude/agents/`, and `qa-engineer` is the gatekeeper for changes to their definitions. Definitions stay lean, and each states its rules inline.

**Agents must be self-contained for downstream portability.** Agents deploy downstream via `repomatic init subagents` as standalone files; Claude auto-invokes them from their `description:` frontmatter. All knowledge must be inline: a downstream repository receives no copy of this `claude.md`, of the upstream `docs/` or of any upstream-only path, so a pointer to one of them dangles there. When mining session history, default to local `claude.md` updates; file an upstream proposal only when the pattern is generic across repos.

**Skills are self-contained the same way.** `repomatic init skills` deploys each one as a standalone folder into repositories that have no `docs/` tree, and skills typically lack `WebFetch`, so a skill keeps its domain knowledge inline or in its own `references/`. Duplication between a skill and a docs page is intentional: `docs/` serves humans, the skill serves Claude at runtime.

### Mechanical vs analytical work

The `repomatic` ecosystem has a **mechanical layer** (CLI commands and CI workflows that deterministically sync, lint, format, and fix files on every push to `main`) and an **analytical layer** (judgment-based tasks needing context comparison and trade-offs). Skills focus on the analytical gaps (custom job content analysis, cross-repo pattern comparison, judgment on intentional vs stale divergence); don't duplicate what CI handles mechanically: see [§ Automated operation contracts](#automated-operation-contracts).
