# {octicon}`package-dependencies` Dependency management

This page documents the version specifier conventions and dependency audit procedures used across all `repomatic`-managed repositories. Downstream projects should follow these conventions in their `pyproject.toml` files.

## Version specifier policy

### Runtime dependencies (`[project].dependencies`)

1. **Use `>=` (not `~=` or `==`).** Relaxed lower bounds give packagers freedom to release security hotfixes without waiting for an upstream bump. Upper bounds are [forbidden](https://iscinumpy.dev/post/bound-version-constraints/).
2. **Every version bound needs a comment tying the floor to a concrete code dependency.** The comment goes on the line above the dependency and states which feature, method, or API from that version the project actually uses:
   ```toml
   # wcmatch 10.0 changed globbing semantics; sync_gitignore() relies on
   # the new symlink-aware matching behavior.
   "wcmatch>=10",
   ```
   A good floor comment answers: "if someone installed an older version, what would break and where?"
3. **The comment documents the floor as it stands, in one short paragraph.** Rewrite it on a bump rather than appending to it: see [§ Rewrite a floor comment, don't extend it](#rewrite-a-floor-comment-dont-extend-it).
4. **Security fixes are a valid floor bump reason.** A CVE or advisory in an older version justifies raising the floor even when the API is unchanged:
   ```toml
   # requests 2.32.0 fixes CVE-2024-35195 (session credential leak on redirects).
   "requests>=2.32",
   ```
5. **Python version support is not a valid reason to bump a floor.** The dependency resolver already picks the right version via `requires-python` metadata. If `boltons>=20` works and boltons 25 merely adds Python 3.13 support, keep `>=20`. **Exception:** when a dependency *drops* a Python version your project still supports (or your project drops one, aligning minimum `requires-python`), that alignment is a valid floor bump:
   ```toml
   # boltons 25.0.0 dropped Python 3.9, matching our requires-python >= 3.10.
   "boltons>=25",
   ```
6. **Use conditional markers for Python-version-gated deps.** Example: `"tomli>=2; python_version<'3.11'"`. When a dep has a version marker, the floor rationale must make sense for the Python versions where the dep is actually installed.
7. **Alphabetical order** within the list.

### Development dependencies (`[dependency-groups]`)

1. **Prefer `[dependency-groups]`** (uv standard) over `[project.optional-dependencies]` for test, typing, and docs groups.
2. **`>=` is preferred for dev deps too**, but `~=` is acceptable when stricter pinning reduces CI randomness. If a package also appears in runtime deps, the dev entry must use the same specifier style.
3. **Standard group names:** `test`, `typing`, `docs` (lowercase, alphabetical).
4. **Type stubs** go in the `typing` group with stub-specific versions: `"types-boltons>=25.0.0.20250822"`.
5. **Alphabetical order** within each group.

### General rules

- **No upper bounds** (`<`, `<=`, `!=`, `~=` that implies an upper bound). The only exception is conditional markers like `python_version<'3.11'`.
- **Extras syntax** is fine: `"coverage[toml]>=7.11"`.
- **One dependency per line** for readable diffs. Short groups that fit on one line are acceptable: the `format-json` workflow normalizes layout automatically.

### Rewrite a floor comment, don't extend it

A floor comment documents **the floor as it stands**: what breaks below the version declared on that line, and where this project would notice. It is not a record of how the floor got there.

Left alone, a comment drifts the other way on its own. Each bump appends a paragraph about the newly required version, nothing gets deleted, and after a few years the comment is a private changelog of the dependency: the declared version is in there somewhere, under the floors it replaced. `meta-package-manager`'s `click-extra` entry reached 676 words that way, walking back through eight superseded floors before reaching the dependency itself.

When raising a floor, rewrite the comment around the new version:

- **Keep** what the newly required version buys, concrete enough to verify: the API, the fix, the `requires-python` alignment, and the call site consuming it. A CVE or upstream issue identifier is part of that claim.
- **Delete** the superseded floors. A version no longer declared cannot break anything for someone running the declared one, and `git log -- pyproject.toml` keeps that history for whoever wants it.
- **Move out** what is not about this floor: how the package is used across the codebase belongs in the module using it, and a comparison against an alternative package belongs in an `XXX` pointer to the upstream ticket.

```toml
# Before: three floors deep, and the reader still has to work out what 8.8 does.
# click-extra 8.8.0 is the floor: `format_size` and `prep_path` moved here.
# Earlier floors remain in play: `replace_region` (8.7.1) splices the binaries
# page; `OperationTrail` (8.6.0) renders the resolve trail; `click:config`
# (8.3) backs configuration.md…
"click-extra>=8.8",

# After: the version in force, and what it buys.
# click-extra 8.8 absorbed the utilities repomatic now delegates to instead of
# carrying: `format_size` (image and cache reports), `prep_path`/`is_stdout`
# (CLI `--output`), and `config_table_to_flags` (tool flag translation).
"click-extra>=8.8",
```

### What is checked automatically

`repomatic lint-deps` reports the rules above that are decidable from `pyproject.toml` alone, and `lint.yaml` runs it on every push:

- An upper bound on a runtime dependency, `~=` and `==` included.
- A dependency carrying no version specifier. A second mention selecting an extra of a package already floored elsewhere (`click-extra[sphinx]` beside `click-extra>=8.8`) is not one: repeating the floor there would just be a second number to keep in step.
- A list that is not in alphabetical order. Only the first entry out of place is named, since one misplaced entry makes every later one look wrong too.
- A `types-*` stub outside the `typing` group.
- A floor with no comment above it. A comment above the array's opening line documents the whole array, which is how a run of related entries is usually justified.
- A floor comment running past `[tool.repomatic] lint-deps.comment-word-threshold` words, 40 by default. Set it to `0` to disable the check. A comment above the array is measured too, but only when some entry has nothing of its own and is therefore documented by it: that closes the escape of moving a long comment up one line, while leaving a version-policy preamble alone, which documents no floor and is not what the rule is about.

These are warnings and never affect the exit code: a release is not held for an uncommented floor. Pass `--no-policy` to skip them.

What no parser settles stays a judgment call, and is where a review is worth spending time: whether a floor is *justified* by the APIs the code actually calls, whether its comment has gone stale, and whether a rationale contradicts its conditional marker. See [§ Floor verification](#floor-verification).

## Shippable sources

A dependency is **shippable** when whoever installs the published artifact gets the same code the release was tested against. `repomatic lint-deps` checks that, and the release lane refuses to build a package while any dependency fails it.

The check is offline, reading `pyproject.toml` and `uv.lock`. The lockfile pass is what makes it complete rather than a list of hand-written rules: it records the resolved origin of every package in the tree, so a git dependency pulled in by another git dependency shows up even though no table names it.

### Why a gate rather than a code review

Two failure classes hide behind "I'll remember to swap it back before releasing", and they fail very differently.

**A direct reference is refused at upload.** PyPI rejects a distribution whose `Requires-Dist` carries a PEP 508 direct reference (`mango @ git+https://…`). That detection exists already, it just happens far too late: the tag is created, the GitHub release is published and the binaries are built before `publish-pypi` fails. `repomatic`'s own `5.0.0` shipped this way, carrying `mdformat-pelican @ git+…` in an extra. It is a GitHub release with no PyPI counterpart to this day, and `5.0.1` shipped the same afternoon to "fix publishing to PyPI by removing URL-based dependency on `mdformat-pelican`". The version number is burned permanently.

**A `[tool.uv.sources]` override is not detectable anywhere.** Source overrides never reach published metadata, so this:

```toml
[project]
dependencies = ["mango>=2.1.0.dev0"]

[tool.uv.sources]
mango = { git = "https://github.com/acme/mango", branch = "main" }
```

builds a valid wheel, uploads cleanly, and declares a requirement PyPI cannot satisfy. Every install of that release fails. The repository's own CI stays green throughout, because it installs from `uv.lock` and resolves straight through the override. Nothing short of a gate catches it.

### What blocks

| Source                      | Example                                                                                       |
| :-------------------------- | :-------------------------------------------------------------------------------------------- |
| A git repository            | `{ git = "…", branch = "main" }`, and the `tag` and `rev` forms                               |
| A local path                | `{ path = "../mango" }`, with or without `editable`                                           |
| A direct artifact URL       | `{ url = "https://…/mango-2.1-py3-none-any.whl" }`                                            |
| A workspace member          | `{ workspace = true }`                                                                        |
| An index other than PyPI    | `{ index = "internal" }`, or a `default = true` index pointing elsewhere                      |
| A PEP 508 direct reference  | `mango @ git+https://…`, in any requirement array                                             |
| A floor inside the cooldown | see [§ `exclude-newer-package` cooldown overrides](#exclude-newer-package-cooldown-overrides) |

Everything blocks, including a source on a package declared only in `[dependency-groups]`, which never reaches an installer. `uv.lock` pins a tracked branch to a revision, and once that branch is force-pushed or the fork disappears, the released tag no longer builds for contributors.

`override-dependencies` and `constraint-dependencies` are reported as warnings instead. They name a published version rather than an unreleased artifact, so the result still installs; what they change is the tree the release was tested against.

The report says which of these applies to each finding, because the remedies differ. A git source paired with a `.dev` floor is the managed `sync-dep-sources` idiom described at the end of [§ `exclude-newer-package` cooldown overrides](#exclude-newer-package-cooldown-overrides): the swap PR opens on its own once the awaited release ships, so the fix is to wait rather than to edit anything.

### Where it runs

Four layers, ordered by how cheap the failure is:

1. **Locally**, through the `/repomatic-ship` pre-push gate.

2. **The release PR body**, regenerated on every push to `main`. A blocker replaces the checklist's "This PR is ready to be merged" opener with a `[!CAUTION]` block, one row per offending dependency, each linked to the line that declares it. This is the layer that matters: it reaches the maintainer at the moment they decide to merge, which is why it also carries the countdown below and why the checklist's other alert, the `Squash and merge` safeguard, ranks one level lower: `detect-squash-merge` catches that mistake after the fact, where merging a blocked release has no backstop.

   The `Clears` column says what the reader is waiting on, in the vocabulary the `sync-uv-lock` cooldown tables already use:

   | Countdown                | What lifts the block                                                                                                   |
   | :----------------------- | :--------------------------------------------------------------------------------------------------------------------- |
   | `2026-09-07 (in 3 days)` | The clock. A floor inside the cooldown clears the day its locked release ages out of the window.                       |
   | 🚧 *needs release*       | Upstream. The git track sits inside the managed idiom, so `sync-dep-sources` opens the swap PR once the release ships. |
   | ✋ *needs an edit*       | A maintainer. Nothing watches this one; the `lint-deps` report names the edit.                                         |

3. **The release lane**, as `_release-build.yaml`'s `lint-deps` job. Fatal only on a release commit, so test-driving a git branch mid-cycle stays frictionless. `build-package` depends on it, so a failure skips the wheel build, which leaves `package_built` false and skips `publish-pypi`, and fails the lane, which skips the engine's tag, release and publish jobs with it.

4. **The test suite**, upstream only, as a millisecond-latency copy of the same check.

Layer 3 is a backstop that should never fire. By the time it does, the freeze commit is already on `main` and the recovery is to burn the version per the skip-and-move-forward rule.

### Allowing an exception

Some arrangements are legitimate: a monorepo member published under its own name, a private mirror an internal project genuinely targets. Name each one in `[tool.repomatic]`, mapped to the reason it is safe:

```toml
[tool.repomatic]
lint-deps.allow = { papaya = "monorepo workspace member, published separately" }
```

A mapping rather than a list, because the reason is the point: an exemption without one is indistinguishable from a forgotten development shortcut six months later. The reason renders in the report and in the release PR banner, so an accepted exception stays visible rather than disappearing. Listing a package does not exempt its transitive dependencies, which stay gated on their own.

## Floor verification

Comments and changelogs can lie; the codebase is the source of truth. For each dependency with a weak or suspicious comment, verify the floor against actual usage:

1. **Grep for imports.** Search the source tree for all imports from the package. List the specific APIs used (functions, classes, constants).
2. **Determine the oldest version providing those APIs.** Check changelogs, release notes, or `pip index versions <pkg>` to see what exists on PyPI.
3. **Lower the floor** when it exceeds the oldest compatible version. Prefer conservative minimums (the major version that introduced the API) over aggressive ones. Update both the version specifier and the comment.
4. **Run `uv lock`** after any floor change to verify the lock still resolves.

### Special cases

- **Backport packages** (like `tomli`, `exceptiongroup`) exist solely to provide a stdlib class to older Python versions. Their entire API is the backported class, available in all versions. The floor is typically `>=1` unless a specific bug fix is needed.
- **Conditional deps with stale bug-fix floors.** A dep gated by `python_version<'3.11'` that has a floor set for a bug affecting Python `<3.8.6`: if `requires-python` is `>=3.10`, that bug is irrelevant and the floor can be lowered.
- **pytest plugins** with no special API beyond auto-registration have low effective floors. Set the floor at the major version introducing the current plugin interface, not at the latest release.

### Red flag patterns in floor comments

These comment patterns typically signal a floor set at adoption or auto-bump time, not at an API boundary:

- "First version we used" or "first version when we last changed the requirement": the floor is an artifact of when the dep was added or last bumped by a dependency bot.
- "First version to support Python 3.X": unless it documents a `requires-python` drop alignment or a concrete build failure, this is not a valid floor reason.
- **The `~= -> >=` conversion pipeline:** a common inflation path where (a) dep is added as `~=X.Y` (latest at the time), (b) a dependency bot bumps to `~=X.Z`, (c) a bulk "relax requirements" commit converts all `~=` to `>=`. Each step inflates the floor without API validation.

## `exclude-newer-package` cooldown overrides

The `[tool.uv]` section may contain `exclude-newer-package` entries that exempt specific packages from the global [`exclude-newer`](https://docs.astral.sh/uv/reference/settings/#exclude-newer) cooldown window. Each entry is one of two kinds:

- **A fixed UTC timestamp** (like `"2026-06-16T00:00:00Z"`): a freeze that holds the package at the version available just before that instant, used when a needed release is still inside the cooldown window. `sync-uv-lock` writes these automatically (pinned to the second UTC midnight after the held version shipped) and prunes each one once its held version ages past `exclude-newer`, returning the package to the normal cooldown. The full timestamp is deliberate: uv re-expands a bare `YYYY-MM-DD` date in the locking machine's local timezone, so a bare date serializes to a different `uv.lock` value locally than in CI and churns the file on every run. These are self-expiring and rarely need manual attention: each `sync-uv-lock` PR body tracks them in its `Cooldown bypasses` table, one row per freeze with the held version and a `Held until` expiry. Rows the run rewrote or removed carry a `📌 frozen:` or `🧹 cleared:` label, and a freeze holding an unreleased version (a git or path source) is labelled `🚧 unreleased:` with a *needs release* expiry.
- **A `"0 day"` span**: a permanent exemption, for packages with no PyPI release to age against (git or path sources, like a project depending on itself). These never expire on their own.

These entries only reach commands that read the project's configuration, which means `uv lock` and `uv sync` but **not** `uvx` / `uv tool run`: an isolated tool environment reads no project configuration at all, so an `exclude-newer-package` table cannot relax an ambient `UV_EXCLUDE_NEWER` for it. A `uvx` invocation that must resolve a release still inside the window has to carry `--exclude-newer-package` on its own command line. uv exposes no environment variable for the flag; [astral-sh/uv#20995](https://github.com/astral-sh/uv/issues/20995) is the upstream request to add one. `lint-repo` checks that the workflow-embedded `uvx 'repomatic==X.Y.Z'` self-pin carries this flag whenever its workflow sets a cooldown at all: see [`self-pin-cooldown-exemption`](workflows.md#github-workflows-lint-yaml-jobs).

The exemption also does not extend to what the exempted release pulls in: its own dependencies stay gated, so reaching a fresh version often means naming the newly required transitive packages too.

To deliberately adopt a release that is still inside the cooldown (a feature migration to a just-published version, the counterpart to the security fix that `audit --fix` automates), add a freeze timestamp by hand: set it to the start of the second UTC day after the target version shipped (the same convention the automatic freezes follow), so `uv lock` resolves up to and including that release. Because the cutoff is a whole-day boundary, it holds a window rather than one exact version: a patch published between the target and that boundary (later the same day, or on the next calendar day) is adopted alongside the target on the next lock. That is usually harmless, since the absorbed build is a newer release of the package being deliberately un-cooled-down and the dependency floor still sets the minimum; but to hold the target and reject even a same-day patch, set the cutoff to the exact upload instant plus a second instead of the day boundary. The hand-written entry is then managed like any other: `sync-uv-lock` prunes it automatically once the adopted version ages past `exclude-newer`, so it needs no later cleanup and will not linger as a stale pin.

To run *unreleased* code while waiting for the next release, track the upstream branch with a `[tool.uv.sources]` git override and declare a `.dev` version floor naming the awaited release (like `mango>=2.1.0.dev0`). That pair is a managed idiom: the `sync-dep-sources` updater watches PyPI and, once a stable release satisfying the floor ships, opens a PR that drops the override, tightens the floor to the released version, and freezes the adoption through the cooldown, so the whole excursion ends without manual cleanup. While the wait lasts, the `Cooldown bypasses` table flags the package as `🚧 unreleased:` with a *needs release* expiry.

For each `exclude-newer-package` entry, check:

1. **Is the package still a dependency?** If removed from `[project].dependencies` and all `[dependency-groups]`, the entry is dead weight.
2. **Is a `"0 day"` span still justified?** A permanent span fits an in-repo (git or path) package. A `"0 day"` span on an external PyPI package is suspect: it tracks the latest release forever and never ages out, so it should be a freeze timestamp instead (which `sync-uv-lock` materializes on its next run).
3. **Is a freeze timestamp stuck?** A freeze timestamp older than the `exclude-newer` window should already have been pruned. A lingering one usually means the package left the dependency tree or the sync job has not run since it aged.

## Floor bumps to adopt new APIs

A floor bump is justified when a newer version of an existing dependency provides an API that **replaces hand-rolled code** in the project. A valid simplification bump must:

1. **Replace existing code**, not add new features. The goal is less code, not more capability.
2. **Be a net reduction** in complexity. Swapping a one-line comprehension for a library call is not a win.
3. **Use the public API** of the dependency. Private/undocumented attributes do not count.
4. **Update the floor comment** to reference the new API and the code it replaces.
