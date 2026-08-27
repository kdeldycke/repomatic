# Development guide

Project-specific guidance for developing `repomatic` itself. The generic coding conventions load from the maintainer's machine configuration and are deliberately not carried here: this file holds only what is specific to this repository. It used to be the source document the retired `agent` component projected into consuming repositories; those sections now live with their owner.

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

## A release ships only released dependencies

Consuming a dependency from a git branch, a fork, a local path or a private index is the right move mid-cycle, and `[tool.uv.sources]` exists for it. Releasing while one is in place is not: source overrides never reach the published metadata, so the wheel builds and uploads exactly as it would have, and only the *install* fails. Nothing in this repository can feel it, because every workflow here installs from `uv.lock` and resolves straight through the override.

`repomatic lint-deps` is the gate, and it blocks everything off-index, `[dependency-groups]` included. Do not weaken a finding to a warning to get a release out; the two legitimate moves are to land the swap (`sync-dep-sources` automates the git-branch-plus-`.dev`-floor idiom on its own) or to name the package in `[tool.repomatic] lint-deps.allow` with the reason it is safe. Adding a rule here means adding it to {func}`repomatic.dep_sources.scan_pyproject` or {func}`~repomatic.dep_sources.scan_lock`, never to a call site.

The gate runs in four places, and the release lane's copy is the backstop, not the mechanism: by the time it fires the freeze commit is already on `main` and the recovery is to burn the version per [§ Skip and move forward](#skip-and-move-forward-dont-rewrite-history). The layer that prevents that is the `prepare-release` PR banner, which is regenerated on every push. See [`docs/dependencies.md` § Shippable sources](https://repomatic.net/dependencies#shippable-sources) for the rules and the failure classes.

### Changelog and docs updates

Always update documentation when making changes:

- **`changelog.md`**: One bullet per user-facing change, describing **what** changed (features, fixes, behavior changes), not **how** it was built or **why**. See [§ Changelog entry length](#changelog-entry-length); rationale belongs in `docs/`, code comments, the commit message, or the PR body, never the changelog.
- **`docs/`**: When this repo has a `docs/` tree, update the relevant page when adding or modifying workflow jobs, CLI commands, or configuration options.

**Never ship an empty release section.** When a cycle's net diff since the last tag is entirely mechanical — a dependency-pin bump, a generated-file regen, a docs-only or CI-only fix — and nothing qualifies as user-facing, add one bullet naming what actually moved rather than leaving the section blank under a heading that still gets tagged and published. A blank section reads as unexplained or broken to anyone scanning release notes for that version. Assert the absence of functional impact only when it is verified (a green, behavior-preserving cycle); otherwise just name the mechanical change: "Sync CI tooling, workflow pins and dependency floors with the latest repomatic release."

**Order within a release section:** `**Breaking:**` entries first, then `**Deprecated:**` entries, then new features, other changes, bug fixes and docs (a reader scans for breaking changes first).

Use `**Breaking:**` when a surface the reader actually consumes is gone and their code, invocation, config or workflow must change to keep working. Use `**Deprecated:**` when the old surface still resolves but emits a `DeprecationWarning` and is scheduled for removal in a named future release: name that release in the entry, so the reader knows their deadline.

**Who consumes the project decides what counts as breaking.** A project consumed as a library breaks real code when a Python symbol is renamed, moved or dropped, so that symbol earns a bullet. A project that is invoked rather than imported does not: its importable surface is an implementation detail, and a bullet announcing a dropped enum or a regrouped module only alarms readers who could never have called it. `repomatic` is the second kind, so `**Breaking:**` here covers the surfaces a user touches: CLI commands and options, `[tool.repomatic]` config keys, reusable-workflow inputs and job names, `repomatic metadata` output keys, and bundled assets.

To back a `**Deprecated:**` change in code, keep the old name importable for one cycle instead of deleting it: resolve it through an alias registry in a PEP 562 module `__getattr__` hook that emits the `DeprecationWarning` and redirects to the replacement, then remove it in the release the entry named. Reserve a hard `**Breaking:**` removal for a surface that genuinely cannot keep working. The `_deprecated.py` modules in `click-extra` and `extra-platforms` are reference implementations.

#### Changelog entry length

A changelog entry is a **release note**, not a commit message or PR description. The reader scans to decide: does this affect me, and must I do anything? Write the shortest bullet that answers both.

- **One sentence by default**, ~10-25 words. Add a second sentence only to flag a breaking change or migration step. A bullet past ~40 words is a smell: it smuggles in implementation detail (cut it) or covers two changes (split it).
- **Keep the user-facing surface:** the public name (CLI command, option, config key, or exported function/class where the project is imported), what it does for the user, plus the migration when it breaks something. Lead with the change, not the mechanism.
- **Cut what the user cannot see or act on**, and move it: *mechanism* (the module/function/job implementing it) to the commit, PR, or code comment; *rationale* (why this approach, which edge case) to a code/docstring comment or `docs/`; *archaeology* (dependency floors chased mid-cycle, root cause, CI trivia) to the commit or PR.
- **Name, don't narrate.** "Add `--cooldown` to skip packages newer than a given age" beats three sentences naming the environment variable each backend uses.

`lint-changelog` warns (without failing) on any unreleased bullet over `[tool.repomatic] changelog.bullet-word-threshold` words. Released sections are immutable.

**Do not mention in the changelog:**

- **Internal refactors behind an unchanged user surface**, in a project nobody imports. A module regrouped, a helper moved between modules, an enum merged or dropped, a signature reworked: none of it reaches a user whose CLI commands, config keys, workflow inputs and metadata keys all still resolve. Omit the bullet rather than demoting it out of `**Breaking:**`; the commit and PR already record the move.
- **Test work of any kind.** Fixtures, snapshots, parametrize cases and assertions adjusted to match a change, and equally the structural work: a new harness or fixture mechanism, switching `unittest.TestCase` to functions, parametrizing a whole module. None of it reaches a user, and `git log` already records it for contributors.
- **Short-shelf-life workarounds.** `tool.uv.exclude-newer-package` cooldown bypasses, dev pins for transient upstream bugs, `xfail` markers, commented-out lines: reverted within days. Drop unless load-bearing beyond a release cycle.
- **Upstream issue commentary.** Prose about a ticket's state (open/closed/not planned, "mirrors the upstream fix in…"). It rots in days and duplicates what `git blame` and the linked thread show. A bare upstream link is fine for a direct backport (`fix … from upstream PR x/y#NNN`); anything longer belongs in a code comment, docstring, or PR. Strip the prose during `consolidate`.

### Documentation sync (upstream maintainers)

```{note}
Applies only when developing the `kdeldycke/repomatic` package itself. Repos that use `repomatic` through its reusable workflows can skip this section.
```

When working inside `kdeldycke/repomatic`, see [`docs/upstream-development.md` § Documentation sync](https://repomatic.net/upstream-development#documentation-sync) for the canonical list of documentation artifacts that must stay in sync with the package source code (PAT permissions, workflow job descriptions, version references, auto-generated tables).

### CLI and configuration as primary abstractions

The `repomatic` CLI and its `[tool.repomatic]` configuration are the project's primary interfaces; everything else (workflows, templates, labels) is a delivery mechanism. Implement features in the CLI first; workflows call the CLI, not the reverse. Documentation leads with the CLI and its configuration.

### Registry types own their query logic

Enums and dataclasses that carry metadata should also carry the methods that interpret it. When callers decide based on a field (scope, format, config key), the logic belongs on the type, not scattered across call sites (`RepoScope.matches(...)`, `NativeFormat.serialize(...)`, `Component.is_enabled(config)`). When adding a field, ask: will callers branch on this value? If yes, add a method. When fixing duplicated conditionals that interpret the same field, the fix is a method, not a helper elsewhere.

### Scope exclusions are defaults, not absolutes

`RepoScope` restrictions and `[tool.repomatic] exclude` entries apply only during bare `repomatic init` (no CLI arguments): naming a component on the CLI, or listing it in `[tool.repomatic] include`, bypasses both, letting workflows materialize out-of-scope configs and users opt into scope-restricted items. Config key exclusions (`config_key` fields) always apply: the user's `[tool.repomatic]` config is authoritative for feature flags.

`RepoScope` has four states: `ALL`, `AWESOME_ONLY` (only `awesome-*` repos), `PYTHON_ONLY` (only repos with a PEP 621 `[project].name`, via `repomatic.pyproject.is_python_project`), and `PACKAGE_ONLY` (only those that also build a distributable, via `repomatic.pyproject.is_python_package`); a `pyproject.toml` with only `[tool.*]` tables (a dotfiles repo) is non-Python. In the source repo, scope exclusions still remove out-of-scope components from `selected`, but stale-file detection is suppressed so bundled data files are never flagged for deletion.

Pick between the two Python scopes by asking what the entry needs to be useful. A uv virtual project (`[tool.uv] package = false`) declares `[project]` purely to carry dependencies: it locks, tests and reports coverage like any Python repo, but has nothing to publish, tag or write release notes for. Anything in the release lane is `PACKAGE_ONLY`; everything else Python-flavored stays `PYTHON_ONLY`. The workflow layer mirrors the same split: `is_python_project` is the default gate, since `sync-uv-lock` and `sync-dep-sources` apply to a virtual project too, and `is_python_package` narrows a job to what has something to publish or version (`sync-bumpversion` is the one that needs it).

### Skip and move forward, don't rewrite history

When a release goes wrong (squash merge, broken artifact, bad metadata), prefer **skipping the version and releasing the next one** over reverting, force-pushing, or rewriting `main`: a burned version number is cheap, a botched automated recovery is not (this mirrors PyPI's [yank](https://peps.python.org/pep-0592/) model). When designing new safeguards, default to **detection + notification** over **detection + automated fix**: the blast radius of a missed notification is zero; that of a bad automated fix can be catastrophic.

### A published release freezes what is missing from it

Publishing flips [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) on, locking the asset list along with the tag. A binary the matrix never produced is then not a gap to fill later: it is a permanent property of that version. `v6.30.0` shipped without `windows-arm64`, `v7.5.0` without either Windows build, and `v7.7.0` without any binary at all. None of the three can be repaired, only superseded.

**Shipping short is the intended behavior, not a failure to prevent.** `publish-release` publishes through a partial matrix on purpose: a release carrying five platforms beats one held hostage by the sixth, and the recovery is the next release, exactly as [§ Skip and move forward](#skip-and-move-forward-dont-rewrite-history) prescribes for every other release mishap. A fast cycle makes a burned platform cheap. So never hold a release, or sit on a draft, waiting for a red build cell: fix the cause and let the next version carry it.

A short ship does leave three artifacts still advertising binaries that are not there: the version's changelog section, the GitHub release body, and `docs/install.md`. Repairing them is a post-publication procedure rather than a rule, so it lives in the `repomatic-ship` skill (§ Repairing a short ship) with the rest of the release lane.

### click_extra is both a dependency and a release consumer

click_extra is both a runtime dependency and the framework whose release pipeline runs the *pinned* repomatic, so a click_extra change to a symbol repomatic imports can break the pinned repomatic from inside click_extra's own release. Two rules: (1) import only click_extra's public API, never an underscore-prefixed name (enforced by `tests/test_imports.py`, whose docstring carries the full rationale); (2) when such a change touches an API repomatic uses, release the fixed repomatic and bump click_extra's pin *before* releasing click_extra, since both run the pinned tag.

## Repository-specific addenda to the generic conventions

The generic conventions these rules add to are maintained in the maintainer's home configuration and load into every session on that machine. What follows is the part of them that is specific to this repository, kept here when the `agent` component retired on 2026-08-20.

### Cooldown: consuming repomatic from the lockfile

This is why workflows on `main` run the CLI as `uv --no-progress run --frozen -- repomatic`, from the lockfile, rather than resolving it fresh: see {data}`repomatic.prepare_release.LOCAL_CLI_INVOCATION`. Beyond the stronger guarantee, an index resolution can be made *unsatisfiable* by the cooldown while a lockfile cannot: raising a dependency floor onto a release younger than the window leaves `uvx` with no version to pick and nowhere to record an exemption, since it reads neither `uv.lock` nor *any* project configuration: neither `[tool.uv] exclude-newer-package` in `pyproject.toml` nor a `uv.toml` sitting beside it, and uv exposes no environment variable for a per-package bypass. See [`docs/dependencies.md` § `exclude-newer-package` cooldown overrides](https://repomatic.net/dependencies#exclude-newer-package-cooldown-overrides) for what that leaves reachable.

Running from the lockfile insulates this repository from that, which creates its own hazard: **a floor inside the window is now invisible here and breaks only the people installing the release** (downstream repos running a frozen workflow's `uvx 'repomatic==X.Y.Z'`, and `uvx repomatic` users). `tests/test_dep_sources.py` is what catches it, so treat that test failing as "this release is not shippable yet", not as a local annoyance to wait out.

Prefer a binary from the tool registry when one exists: it is the only path that carries all three at once. When adding a tool that repomatic shells out to, register it and reach it through {func}`repomatic.tool_runner.ensure_binary` rather than `$PATH`, which carries none of the three.

### Documented exemptions this repository claims

Three installs deliberately bypass the window. The first two are per-package and never widen to the rest of the tree; the third is a whole job, and says why it has to be.

- **The upstream toolkit's own pin.** `repomatic` runs from a pin that moves in lockstep with the `uses:` refs pointing at it, so a release must be installable the minute it is published or every downstream repo breaks until the window elapses. The release freeze emits an `--exclude-newer-package` escape hatch beside the pin it writes.
- **A security fix still inside the window.** `audit --fix` reaches a CVE fix through an `exclude-newer-package` entry rather than lifting `exclude-newer` for everything.
- **The `test-package-install` job.** Its subject *is* the freshly published artifact, so a cooldown would make the question it exists to answer unanswerable. Scoping the opt-out to one job is what keeps it honest: it holds no secrets, inherits `permissions: {}`, and only runs `--version` on a throwaway runner.

### Commit messages: the `[changelog]` prefix invariant

A `[bracketed]` commit-subject prefix is reserved for a load-bearing mechanism that parses it back.

Only `[changelog] …` qualifies here, and it is an invariant, not a convention: every machine-authored version-machinery commit (release freeze, post-release bump, manual major/minor bumps) starts with {data}`repomatic.git_ops.CHANGELOG_COMMIT_PREFIX`, which lets workflow gates skip machinery pushes on that single prefix instead of enumerating message shapes. `tests/test_workflows.py::test_changelog_prefix_is_the_machinery_invariant` holds the prefix set, the gates, and the emitting template together; the auto-tagging job matches {data}`repomatic.git_ops.RELEASE_COMMIT_PATTERN` within the same family.

### Directives are written in Simplified Technical English

The generic conventions bind every text artifact to ASD-STE100. Only part of that is mechanically checkable, so only that part is enforced: `tests/test_claude_assets.py` holds every bundled skill and agent to a 25-word ceiling on each *directive*, and leaves the rationale after it alone.

The split follows the house bullet style rather than imposing a new one. A rule bullet opens with the rule in bold, then explains itself in plain prose, so the bold span is the directive and everything after it is exempt. A bullet carrying no bold lead is measured only when it opens on a verb in `IMPERATIVE_VERBS`, and then only its first sentence. Enumerating those verbs is what keeps the check under-inclusive: a directive nobody's verb list catches is read as prose and skipped, which is the same trade the drift checks in that module make.

A directive over the ceiling is nearly always two rules sharing a bullet, or a rule with its exception folded in. Split it; do not raise the number.

**The ceiling does not cap a whole `description` field, and should not.** The three longest run past 45 words across four or five short sentences, which is the right shape: the router matches against the field's whole vocabulary, so trimming trigger nouns to hit a total would cost matching accuracy and buy nothing. The Agent Skills spec's 1024-character ceiling is the only total, already held by `tests/test_skills.py`. Note what this leaves uncovered: a description made of short sentences passes however long it grows, so the two worst offenders this rule was written against (56 and 39 words) were caught by review, not by the test.

The approved-word dictionary stays out of scope. It is a controlled ASD specification rather than something to vendor as a data file, and the words it would rule on here (`run`, `sync`, `pin`, `release`) are the ones that must keep matching the code identifiers they name.

### Defaults: the `Config` dataclass surface

Every configurable default lives in exactly one place: the `Config` dataclass in `repomatic/config.py`; all code derives it from there rather than repeating the literal.

A config field also surfaces in serialized command output (a non-string default needs format-safe encoding) and in test fixtures enumerating the config surface: run the full test suite after adding or removing a field, not just the module's own tests.

### Test suite: hermeticity boundaries

- **The suite is hermetic against the host's own `repomatic` configuration.** The default config search derives from `click.get_app_dir`, so any config file in the developer's app folder is discovered by every in-process `CliRunner().invoke(repomatic, ...)`: a local setting can fail a test CI cannot reproduce. The `_isolate_user_config` autouse fixture in `tests/conftest.py` (aliasing click-extra's `isolated_app_dir`) repoints discovery at an empty per-test directory; tests exercising config loading pass an explicit path instead.
- **It is *not* hermetic against this repository's own `[tool.repomatic]`, and nothing makes it so.** Discovery is CWD-first, walking up to the VCS root, so a call that resolves config itself (`run_init(config=None)`, anything reaching `load_repomatic_config()` with no argument) reads the checkout's `pyproject.toml` under pytest exactly as it would in a shell. The autouse fixture above covers the app dir only. This turns *enabling a feature here* into a test failure elsewhere: switching on a component's config gate made `test_init_only_workflows` see a workflow it asserted absent. Pass an explicit `Config()` in any test asserting on default behaviour, and treat a test that breaks when you flip a `[tool.repomatic]` key as coupled rather than as a real regression.

### Release-specific design rationale

```{note}
Release-specific design rationale for `kdeldycke/repomatic` (the `workflow_run` checkout pitfall, immutable releases, concurrency, freeze/unfreeze structure) lives in `docs/upstream-development.md` § Release checklist. Downstream repos with their own release flow can borrow it but aren't bound by it.
```

### Pin uv: enforcement internals

CI pins the exact uv through `with: version:` on every `astral-sh/setup-uv` step, walked forward by `sync-workflow-pins` like any other pinned literal.

`tests/test_workflows.py` fails on a `setup-uv` step without the input, or on two steps naming different versions.

`uv.lock` stays stable across minors because `sync-uv-lock` discards a re-lock that only re-spells equivalent environment markers (see `sync_uv_lock` in `repomatic/uv.py`). repomatic manages this: `repomatic init uv` writes both policy pins (`required-version`, `exclude-newer`) from the bundled `uv.toml`, and `sync-uv-lock` re-applies them while leaving every other `[tool.uv]` key untouched.
