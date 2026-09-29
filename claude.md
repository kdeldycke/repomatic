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

## Repository-specific addenda to the generic conventions

The generic conventions these rules add to are maintained in the maintainer's home configuration and load into every session on that machine. What follows is the part of them that is specific to this repository.

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

### click_extra is both a dependency and a release consumer

click_extra is both a runtime dependency and the framework whose release pipeline runs the *pinned* repomatic, so a click_extra change to a symbol repomatic imports can break the pinned repomatic from inside click_extra's own release. Two rules: (1) import only click_extra's public API, never an underscore-prefixed name (enforced by `tests/test_imports.py`, whose docstring carries the full rationale); (2) when such a change touches an API repomatic uses, release the fixed repomatic and bump click_extra's pin *before* releasing click_extra, since both run the pinned tag.

### Cooldown: consuming repomatic from the lockfile

This is why workflows on `main` run the CLI as `uv --no-progress run --frozen -- repomatic`, from the lockfile, rather than resolving it fresh: see {data}`repomatic.release.prepare_release.LOCAL_CLI_INVOCATION`. Beyond the stronger guarantee, an index resolution can be made *unsatisfiable* by the cooldown while a lockfile cannot: raising a dependency floor onto a release younger than the window leaves `uvx` with no version to pick and nowhere to record an exemption, since it reads neither `uv.lock` nor *any* project configuration: neither `[tool.uv] exclude-newer-package` in `pyproject.toml` nor a `uv.toml` sitting beside it, and uv exposes no environment variable for a per-package bypass. See [`docs/dependencies.md` § `exclude-newer-package` cooldown overrides](https://repomatic.net/dependencies#exclude-newer-package-cooldown-overrides) for what that leaves reachable.

Running from the lockfile insulates this repository from that, which creates its own hazard: **a floor inside the window is now invisible here and breaks only the people installing the release** (downstream repos running a frozen workflow's `uvx 'repomatic==X.Y.Z'`, and `uvx repomatic` users). `tests/test_dep_sources.py` is what catches it, so treat that test failing as "this release is not shippable yet", not as a local annoyance to wait out.

Prefer a binary from the tool registry when one exists: it is the only path that carries all three at once. When adding a tool that repomatic shells out to, register it and reach it through {func}`repomatic.tooling.tool_runner.ensure_binary` rather than `$PATH`, which carries none of the three.

### Documented exemptions this repository claims

Three installs deliberately bypass the window. The first two are per-package and never widen to the rest of the tree; the third is a whole job, and says why it has to be.

- **The upstream toolkit's own pin.** `repomatic` runs from a pin that moves in lockstep with the `uses:` refs pointing at it, so a release must be installable the minute it is published or every downstream repo breaks until the window elapses. The release freeze emits an `--exclude-newer-package` escape hatch beside the pin it writes.
- **A security fix still inside the window.** `audit --fix` reaches a CVE fix through an `exclude-newer-package` entry rather than lifting `exclude-newer` for everything.
- **The `test-package-install` job.** Its subject *is* the freshly published artifact, so a cooldown would make the question it exists to answer unanswerable. Scoping the opt-out to one job is what keeps it honest: it holds no secrets, inherits `permissions: {}`, and only runs `--version` on a throwaway runner.

### Where the window comes from

`[tool.repomatic] minimum-release-age` (default `1 week`) is the single source of truth. Never hard-code a duration next to an install command: read it from config, or from the `npm_min_release_age_days` output `repomatic show-metadata` derives from it.

Two files carry the duration as a literal instead, and both are pinned back to that source by a conformance test rather than trusted:

- **Every workflow**, because YAML cannot read Python: each sets `UV_EXCLUDE_NEWER` and `NPM_CONFIG_MIN_RELEASE_AGE` in a **workflow-level `env:` block**, rendered by `cooldown_env_block()` and asserted verbatim by `tests/test_workflows.py`. Job-level `env:` would let the value come from the `metadata` job, but it cannot cover the bootstrap: `metadata` resolves packages before any other job's output exists, and a workflow-level `env:` block cannot reference `needs`. The literal covers every job, including that bootstrap and any step added later by someone who never read this section.
- **`[tool.uv] exclude-newer`**, in this repo's `pyproject.toml` and in the bundled `repomatic/data/uv.toml`, because uv reads its own config and knows nothing of `[tool.repomatic]`. `tests/test_uv.py` asserts both equal `minimum-release-age`. They must not merely be *close*: a lock window wider than the install window resolves versions those installs then refuse, leaving a package pinned in `uv.lock` that CI cannot install.

That makes the cooldown the one place an environment variable beats an explicit flag, inverting the generic preference for explicit uv flags over environment variables: a flag only protects the command someone remembered to write it on, and the commands that most need protecting are the ones nobody thought about.

A command that resolves against a checked-in lockfile is the exception that needs the flag *back*. `uv lock` and `uv sync` are governed by the project's own `[tool.uv] exclude-newer`, and an ambient `UV_EXCLUDE_NEWER` silently overrides it, so CI would lock to a different window than a developer running the same command. `sync-uv-lock` therefore passes `--exclude-newer` explicitly, sourced from `[tool.uv]`: a CLI flag outranks the environment.

### Per-package cooldown exemptions are command-line only

uv takes a per-package cooldown exemption as a command-line flag only, and that is what forces every per-package bypass in this repository onto a command line ({data}`repomatic.release.prepare_release.SELF_PIN_COOLDOWN_EXEMPTION`) rather than into one central declaration. Verified against uv `0.12.3`: under an ambient `UV_EXCLUDE_NEWER`, a `uvx` resolution fails byte-identically whether the exemption sits in `[tool.uv]`, in an adjacent `uv.toml`, or nowhere at all. The one knob that does reach it is `--config-file` / `UV_CONFIG_FILE`, rejected here because it **replaces** discovered configuration instead of merging with it: setting it for a whole CI environment silently drops `required-version`, `exclude-newer`, `dependency-groups` and `build-backend` from every *other* uv command in that environment, so making it safe means maintaining a complete mirror of `[tool.uv]` where a newly added key nobody remembers to mirror fails silently. Upstream: the `UV_EXCLUDE_NEWER_PACKAGE` ask is [astral-sh/uv#20995](https://github.com/astral-sh/uv/issues/20995), glob exemptions are [astral-sh/uv#20788](https://github.com/astral-sh/uv/issues/20788), and pin-based bypasses are [astral-sh/uv#19864](https://github.com/astral-sh/uv/issues/19864) with [astral-sh/uv#18921](https://github.com/astral-sh/uv/pull/18921).

### Shippable dependency sources

Nothing in this repository can feel a stray `[tool.uv.sources]` override, because every workflow here installs from `uv.lock` and resolves straight through it: `repomatic lint-deps` is the only thing that sees one, and it blocks everything off-index, `[dependency-groups]` included. Adding a rule to it means adding it to {func}`repomatic.deps.dep_sources.scan_pyproject` or {func}`~repomatic.deps.dep_sources.scan_lock`, never to a call site.

The gate runs in four places, and the release lane's copy is the backstop, not the mechanism: by the time it fires the freeze commit is already on `main` and the recovery is to burn the version per the generic skip-and-move-forward rule. The layer that prevents that is the `prepare-release` PR banner, which is regenerated on every push. See [`docs/dependencies.md` § Shippable sources](https://repomatic.net/dependencies#shippable-sources) for the rules and the failure classes.

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

### A published release freezes what is missing from it

Publishing flips [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) on, locking the asset list along with the tag. A binary the matrix never produced is then not a gap to fill later: it is a permanent property of that version. `v6.30.0` shipped without `windows-arm64`, `v7.5.0` without either Windows build, and `v7.7.0` without any binary at all. None of the three can be repaired, only superseded.

**Shipping short is the intended behavior, not a failure to prevent.** `publish-release` publishes through a partial matrix on purpose: a release carrying five platforms beats one held hostage by the sixth, and the recovery is the next release, exactly as the generic skip-and-move-forward rule prescribes for every other release mishap. A fast cycle makes a burned platform cheap. So never hold a release, or sit on a draft, waiting for a red build cell: fix the cause and let the next version carry it.

A short ship does leave three artifacts still advertising binaries that are not there: the version's changelog section, the GitHub release body, and `docs/install.md`. Repairing them is a post-publication procedure rather than a rule, so it lives in the `repomatic-ship` skill (§ Repairing a short ship) with the rest of the release lane.

### Pin uv: enforcement internals

CI pins the exact uv through `with: version:` on every `astral-sh/setup-uv` step, walked forward by `sync-workflow-pins` like any other pinned literal.

`tests/test_workflows.py` fails on a `setup-uv` step without the input, or on two steps naming different versions.

`uv.lock` stays stable across minors because `sync-uv-lock` discards a re-lock that only re-spells equivalent environment markers (see `sync_uv_lock` in `repomatic/deps/uv.py`). repomatic manages this: `repomatic init uv` writes both policy pins (`required-version`, `exclude-newer`) from the bundled `uv.toml`, and `sync-uv-lock` re-applies them while leaving every other `[tool.uv]` key untouched.

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

**Rules:**

1. **Pick the verb that matches the data source.** External template/API/canonical reference: `sync`. Local project state (lockfiles, git history, source): `update`. Reformatting: `format`.
2. **Name the specific tool or file, not a generic category** (`sync-zizmor`, not `sync-linter-configs`). A second tool in a category gets its own operation.
3. **All four dimensions must agree.** A file-modifying operation uses one `verb-noun` for CLI command, workflow job ID, PR branch, and PR body template (`sync-gitignore` everywhere). Operations that write no repository file use only the CLI command and job ID: `lint-*`, which reads, `pack-*`, which emits a build artifact, and the bare-noun issue trackers of rule 9. Two exceptions: `sync-binaries` has no PR branch or template of its own because it runs inside the `scan-virustotal` job and appends to that job's pull request, disableable via `[tool.repomatic] binaries.sync`, see [§ Scanning accumulates in one pull request](https://repomatic.net/operation-contracts#scanning-accumulates-in-one-pull-request); and `fix-awesome-toc` runs as a step of `format-markdown` because it corrects what that job just wrote and the two would otherwise fight across separate PRs, see [§ Fix steps inside another job](https://repomatic.net/operation-contracts#fix-steps-inside-another-job). A job local to this repository (one with no upstream template to name on `pr-body --template`) keeps the same identity: its template is `.github/pr-templates/{job-id}.md`, passed with `--template-file`, and `repomatic lint-repo` flags one placed elsewhere. See [§ Repository-local templates](https://repomatic.net/operation-contracts#repository-local-templates).
4. **Function names follow the CLI name** (`sync_gitignore` for `sync-gitignore`). On collision with an imported module, use the Click `name=` parameter (`@repomatic.command(name="update-dep-graph")` on `dep_graph`) or append `_cmd` (`sync_uv_lock_cmd`).
5. **A read-only command may expose mutation via `--fix`.** When a query command (like `audit`) gains a `--fix` autofix mode, the autofix operation keeps its own `fix-X` job ID, PR branch, and template and invokes `<command> --fix` (`fix-vulnerable-deps` runs `audit --fix`). That command name is then exempt from rule 3: it is a general-purpose query command (like `metadata`), not the operation's namesake.
6. **`dep` when attributive, `deps` when the object.** A "dep" prefix modifying another noun stays singular, following English compound-noun convention (`dep-graph`, `dep-sources`, `dep_report.py`); "deps" appears only where the dependencies are themselves the object of the verb (`sync-deps`, `vulnerable-deps`).
7. **A sample accrues where a sync converges.** `sync-X` regenerates a file the external source could rebuild from scratch, so losing it costs a re-run. `sample-X` records a reading the source will not remember: the store is the only place that history exists, and a lost one is gone. That is what settles the two properties the table cannot state. Idempotency holds *within* a period only, since re-running on the same day overwrites its own point while tomorrow's run adds one nothing upstream could reproduce. And the recording accumulates in one long-lived pull request that every run appends to, never a per-run one and never a direct push. A per-run pull request is unreviewable for the reason a post-release recording is: rejecting the diff cannot change what the API answered, only lose it, and one left unmerged would stall the accrual. One accumulating pull request escapes both, because each run restores the store from the branch before appending: leaving it open costs freshness and never a reading. See [§ Sample job contract](https://repomatic.net/operation-contracts#sample-job-contract). All four of rule 3's dimensions therefore apply, PR branch and template included.
8. **A dataset the repository commits is CSV, not JSON.** Every one of them is a flat table, and CSV wins on all four counts that matter for a file under version control: a record is one line rather than seven, so a scheduled append is a readable diff; the file is about half the size; MyST's `csv-table` renders it directly and GitHub serves it through a searchable grid viewer; and nothing in the autofix lane touches CSV, where a committed JSON file has to be serialized in Biome's exact style or `format-json` rewrites it right back. Reach for JSON only when a record genuinely nests. {mod}`repomatic.tabular` is the one place that reads and writes them.
9. **An operation whose output is a GitHub issue takes a bare noun, no verb prefix** (`setup-guide`). It writes no repository file: it upserts one issue through {func}`~repomatic.github.issue.manage_issue_lifecycle`, which opens, updates, closes and reopens it, matched on the exact title. Every such command shares that one mechanism, so a verb would name the mechanism rather than the subject and they would all collapse onto the same prefix; the noun instead names what the issue tracks, keeping the command and the issue title the same phrase. Body fragments live in `repomatic/templates/{name}[-{fragment}].md` and render through {func}`~repomatic.github.pr_body.render_template`: that renderer is shared with PR bodies, but these are issue bodies, so `pr-body --template` never names one and rule 3's PR-branch and PR-template dimensions do not apply. Every issue carries {data}`~repomatic.github.issue.BOT_ISSUE_LABEL`. `broken-links` predates this convention and sits outside its population: its job is `check-broken-links` rather than the bare noun, and its issue carries a topical label (`📚 documentation`, or `🩹 fix link` on an awesome list) instead of the bot one. That last sentence is therefore a claim about the rule-9 family, not about every issue the CLI opens.

### Automated operation contracts

Every automated operation follows the [naming conventions](#naming-conventions-for-automated-operations) and is idempotent. For the detailed checklists of required properties, invariants, and optional elements for each operation type (sync, update, format/fix, lint, pack, scan, PR body templates), see [`docs/operation-contracts.md`](https://repomatic.net/operation-contracts).

### Labeller rules are precision-first conveniences

The issue and PR labeller (content keyword rules, file glob rules) pre-labels a freshly filed issue or PR to save the maintainer a first pass. It never replaces their review and classification, and nothing downstream treats its labels as complete or authoritative. Tune it for **precision, not recall**: a missing label costs one manual click; a wrong label is noise on every item that trips it. Encode a rule only when the signal is unambiguous, and none when it is not.

- **Content rules** match issue/PR prose: key them off terms that unambiguously name the subject (a distro, language, ecosystem or brand) and that the tool never prints in its own output. Never key off a token the tool emits for *every* item it handles, like an ID, sub-command or status-table entry a CLI lists for all its back-ends: a user who pastes such a trace makes every one of those labels fire at once.
- **File rules** match a PR's changed paths: key them off a path owned by exactly one label. A glob broad enough to catch unrelated changes is worse than none.

Both rule families match in-process (`repomatic/labels.py`) rather than through the retired `github/issue-labeler` and `actions/labeler` actions, and a label's pattern list is **OR-joined**: any one pattern matching earns the label. A bare content pattern is a keyword, matched case-insensitively and anchored on each edge that is itself a word character, so `fix` does not fire inside `prefix`. Wrap a pattern in slashes (`/body/flags`) to pass a regex through verbatim instead, case-sensitive unless a flag says otherwise. A label's file globs are evaluated as one set, so a `!`-negated entry subtracts from its siblings the way a `.gitignore` line would (`["docs/**", "!docs/generated/**"]`), with no separate exclude rule to write.

### Agents

This repository uses three Claude Code agents in `.claude/agents/`. Definitions stay lean: if a rule belongs in `CLAUDE.md`, put it there and reference it. Do not duplicate.

**Agents must be self-contained for downstream portability.** Agents deploy downstream via `repomatic init subagents` as standalone files; Claude auto-invokes them from their `description:` frontmatter. All knowledge must be inline or reference `claude.md` sections, not upstream `docs/` URLs or upstream-only paths. When mining session history, default to local `claude.md` updates; file an upstream proposal only when the pattern is generic across repos.

- Agent definitions reference `CLAUDE.md` sections, not restate them.
- qa-engineer is the gatekeeper for agent definition changes.

### Skills

Skills in `.claude/skills/` follow agent conventions: lean, no duplication with `CLAUDE.md`, reference sections instead of restating rules. Run `repomatic list-skills` to list them.

**A skill is a plain folder of static files, copied verbatim.** `repomatic init skills` places the folder at its destination and does nothing else: no rendering, no per-target variants, no flavor flags. Optional `scripts/`, `references/` and `assets/` subdirectories travel with it. Anything that would otherwise vary per destination belongs in the skill body as prose, never in a code path.

**Frontmatter carries [Agent Skills spec](https://agentskills.io/specification) fields, plus `argument-hint`.** That single deviation is settled; every other Claude Code extension stays out. Notably there is no `model:` (the recommended model rides in the spec's `compatibility` field) and no `disable-model-invocation:`, so **every skill is model-invocable by design**: skills exist to augment the parent agent, and what they may actually do is gated by the permission layer, not by frontmatter. `tests/test_skills.py` enforces this, so argue a new field there before adding it to a skill.

**Skills must be self-contained for downstream portability.** Skills deploy downstream via `repomatic init skills` as standalone folders; downstream repos have no `docs/` and skills typically lack `WebFetch`, so all domain knowledge must be inline or in the skill's own `references/`. Duplication between a skill and a docs page is intentional: `docs/` serves humans, the skill serves Claude at runtime.

**Cross-references between skills and agents must degrade gracefully.** A "Next steps" line suggesting `/other-skill` is informational; a *programmatic* call is the same: a skill invoking another through the `Skill` tool must fall back to a subagent or inline work when the target is excluded (via `[tool.repomatic] exclude` or scope filtering), never letting a missing skill abort the caller. Write prose so a missing cross-reference is a no-op, not a blocker.

### Mechanical vs analytical work

The `repomatic` ecosystem has a **mechanical layer** (CLI commands and CI workflows that deterministically sync, lint, format, and fix files on every push to `main`) and an **analytical layer** (judgment-based tasks needing context comparison and trade-offs). Skills focus on the analytical gaps (custom job content analysis, cross-repo pattern comparison, judgment on intentional vs stale divergence); don't duplicate what CI handles mechanically: see [§ Automated operation contracts](#automated-operation-contracts).

### Common maintenance pitfalls

- **Generator/formatter ping-pong is recurrent.** Any code that writes a checked-in Markdown file competes with `format-markdown` for the canonical layout. After touching such code, run the generator, then `repomatic run mdformat -- {file}`, then the generator again, confirming `git diff` stays empty across all three states; if not, align the generator with mdformat. Grep for the pattern in sibling generators and mirror the check in `tests/`. Checked-in JSON has the same trap with `format-json`, in a worse form. The indent is whatever the target repository's Biome config asks for, so a generator hardcoding one fights `format-json` forever wherever the repository is configured the other way. Read it from that repository instead: `_biome_json_indent` in `repomatic/tooling/plugin.py` resolves it, and `render_plugin_settings` writes with it.
- **`repomatic run {tool} --check` is unreliable for tools with a post-process fixup.** A few tools (currently `mdformat`) get a Python post-processing pass that only runs in write mode, so `--check` can report drift the write path would reconcile (false positive) or pass on files it would still rewrite (false negative). To verify or gate formatting, run the write path and inspect `git diff`, never `--check`. The write path is also the default where you might expect a report: `repomatic run typos -- {file}` fixes misspellings in place and prints nothing, so a clean file and a corrected one produce the same empty output. A hand-reconstructed invocation misses the bundled config as well as the post-process: mdformat's sets `number = true` and `validate = false`, so a bare `uvx mdformat` renumbers every ordered list to `1.` and rewrites the `mdformat-toc` block, and that churn reads as your own edit. Copy the config out of `repomatic/data/` first, or run the write path on a scratch copy and diff against it.
- **Removing a bundled asset leaves downstream orphans.** Dropping a skill, agent, or workflow from `COMPONENTS` stops shipping it, but copies already in downstream repos are invisible to stale-file detection. Add a `RemovedAsset` tombstone to `REMOVED_ASSETS` in `repomatic/registry.py` so `repomatic init` prunes the orphan (the `RemovedAsset` docstring has the content- vs fingerprint-gating recipe); a CI test fails otherwise. A rename is a drop plus an add: tombstone the old name.
