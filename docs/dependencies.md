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
3. **Security fixes are a valid floor bump reason.** A CVE or advisory in an older version justifies raising the floor even when the API is unchanged:
   ```toml
   # requests 2.32.0 fixes CVE-2024-35195 (session credential leak on redirects).
   "requests>=2.32",
   ```
4. **Python version support is not a valid reason to bump a floor.** The dependency resolver already picks the right version via `requires-python` metadata. If `boltons>=20` works and boltons 25 merely adds Python 3.13 support, keep `>=20`. **Exception:** when a dependency *drops* a Python version your project still supports (or your project drops one, aligning minimum `requires-python`), that alignment is a valid floor bump:
   ```toml
   # boltons 25.0.0 dropped Python 3.9, matching our requires-python >= 3.10.
   "boltons>=25",
   ```
5. **Use conditional markers for Python-version-gated deps.** Example: `"tomli>=2; python_version<'3.11'"`. When a dep has a version marker, the floor rationale must make sense for the Python versions where the dep is actually installed.
6. **Alphabetical order** within the list.

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

- "First version we used" or "first version when we last changed the requirement": the floor is an artifact of when the dep was added or last bumped by Renovate/Dependabot.
- "First version to support Python 3.X": unless it documents a `requires-python` drop alignment or a concrete build failure, this is not a valid floor reason.
- **The `~= -> >=` conversion pipeline:** a common inflation path where (a) dep is added as `~=X.Y` (latest at the time), (b) Renovate bumps to `~=X.Z`, (c) a bulk "relax requirements" commit converts all `~=` to `>=`. Each step inflates the floor without API validation.

## `exclude-newer-package` cooldown overrides

The `[tool.uv]` section may contain `exclude-newer-package` entries that exempt specific packages from the global [`exclude-newer`](https://docs.astral.sh/uv/reference/settings/#exclude-newer) cooldown window. Each entry is one of two kinds:

- **A fixed date** (like `"2026-06-15"`): a freeze that holds the package at the version available on that date, used when a needed release is still inside the cooldown window. `sync-uv-lock` writes these automatically (the day after the held version shipped) and prunes each one once its held version ages past `exclude-newer`, returning the package to the normal cooldown. These are self-expiring and rarely need manual attention.
- **A `"0 day"` span**: a permanent exemption, for packages with no PyPI release to age against (git or path sources, like a project depending on itself). These never expire on their own.

For each `exclude-newer-package` entry, check:

1. **Is the package still a dependency?** If removed from `[project].dependencies` and all `[dependency-groups]`, the entry is dead weight.
2. **Is a `"0 day"` span still justified?** A permanent span fits an in-repo (git or path) package. A `"0 day"` span on an external PyPI package is suspect: it tracks the latest release forever and never ages out, so it should be a freeze date instead (which `sync-uv-lock` materializes on its next run).
3. **Is a freeze date stuck?** A freeze date older than the `exclude-newer` window should already have been pruned. A lingering one usually means the package left the dependency tree or the sync job has not run since it aged.

## Floor bumps to adopt new APIs

A floor bump is justified when a newer version of an existing dependency provides an API that **replaces hand-rolled code** in the project. A valid simplification bump must:

1. **Replace existing code**, not add new features. The goal is less code, not more capability.
2. **Be a net reduction** in complexity. Swapping a one-line comprehension for a library call is not a win.
3. **Use the public API** of the dependency. Private/undocumented attributes do not count.
4. **Update the floor comment** to reference the new API and the code it replaces.
