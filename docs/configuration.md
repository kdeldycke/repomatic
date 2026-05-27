# {octicon}`gear` Configuration

repomatic reads two kinds of `pyproject.toml` configuration. Its own settings live in `[tool.repomatic]`, documented below. The third-party tools it runs are configured through their own standard `[tool.*]` sections (`[tool.ruff]`, `[tool.mypy]`, `[tool.typos]`, `[tool.nuitka]`, and so on); repomatic discovers and resolves these through its [tool runner](tool-runner.md), where they are documented.

## `[tool.repomatic]` configuration

Downstream projects can customize workflow behavior by adding a `[tool.repomatic]` section in their `pyproject.toml`. These options control the defaults for the corresponding [CLI commands](cli.md).

The `[tool.repomatic]` section is powered by [Click Extra's `pyproject.toml` configuration](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml). Click Extra handles [CWD-first discovery](https://kdeldycke.github.io/click-extra/config.html#cwd-first-discovery) (walking up to the VCS root), [key normalization](https://kdeldycke.github.io/click-extra/config.html#key-normalization) (kebab-case to snake_case), and [typed dataclass schemas](https://kdeldycke.github.io/click-extra/config.html#typed-configuration-schema) (nested sub-tables, opaque dict fields, strict validation).

```toml
[tool.repomatic]
pypi-package-history = ["old-name", "older-name"]

awesome-template.sync = false
bumpversion.sync = false
cache.max-age = 14
dev-release.sync = false
gitignore.sync = false
labels.sync = false
mailmap.sync = false
setup-guide = false
uv-lock.sync = false

dependency-graph.output = "./docs/assets/dependencies.mmd"
dependency-graph.all-groups = true
dependency-graph.all-extras = true
dependency-graph.no-groups = []
dependency-graph.no-extras = []
dependency-graph.level = 0

gitignore.location = "./.gitignore"
gitignore.extra-categories = ["terraform", "go"]
gitignore.extra-content = '''
junit.xml

# Claude Code
.claude/
'''

exclude = ["skills", "workflows/debug.yaml", "zizmor"]

labels.extra-files = ["https://example.com/my-labels.toml"]
labels.extra-file-rules = "docs:\n  - docs/**"
labels.extra-content-rules = "security:\n  - '(CVE|vulnerability)'"

nuitka.enabled = false
nuitka.entry-points = ["mpm"]
nuitka.unstable-targets = ["linux-arm64", "windows-arm64"]

test-plan.file = "./tests/cli-test-plan.yaml"
test-plan.timeout = 120
test-plan.inline = "- args: --version"

workflow.sync = false
workflow.source-paths = ["extra_platforms"]
workflow.extra-paths = ["install.sh", "dotfiles/**"]
workflow.ignore-paths = ["uv.lock"]

[tool.repomatic.workflow.paths]
"tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
```

<!-- config-reference-start -->

| Option                                                        | Description                                                                      | Default                             |
| :------------------------------------------------------------ | :------------------------------------------------------------------------------- | :---------------------------------- |
| [`agents.location`](#agents-location)                         | Directory prefix for Claude Code agent files, relative to the repository root.   | `"./.claude/agents/"`               |
| [`awesome-template.sync`](#awesome-template-sync)             | Whether awesome-template sync is enabled for this project.                       | `true`                              |
| [`bumpversion.sync`](#bumpversion-sync)                       | Whether bumpversion config sync is enabled for this project.                     | `true`                              |
| [`cache.dir`](#cache-dir)                                     | Override the binary cache directory path.                                        | `""`                                |
| [`cache.github-release-ttl`](#cache-github-release-ttl)       | Freshness TTL for cached single-release bodies (seconds).                        | `604800`                            |
| [`cache.github-releases-ttl`](#cache-github-releases-ttl)     | Freshness TTL for cached all-releases responses (seconds).                       | `86400`                             |
| [`cache.max-age`](#cache-max-age)                             | Auto-purge cached entries older than this many days.                             | `30`                                |
| [`cache.pypi-ttl`](#cache-pypi-ttl)                           | Freshness TTL for cached PyPI metadata (seconds).                                | `86400`                             |
| [`changelog.location`](#changelog-location)                   | File path of the changelog, relative to the root of the repository.              | `"./changelog.md"`                  |
| [`dependency-graph.all-extras`](#dependency-graph-all-extras) | Whether to include all optional extras in the graph.                             | `true`                              |
| [`dependency-graph.all-groups`](#dependency-graph-all-groups) | Whether to include all dependency groups in the graph.                           | `true`                              |
| [`dependency-graph.level`](#dependency-graph-level)           | Maximum depth of the dependency graph.                                           | *(none)*                            |
| [`dependency-graph.no-extras`](#dependency-graph-no-extras)   | Optional extras to exclude from the graph.                                       | `[]`                                |
| [`dependency-graph.no-groups`](#dependency-graph-no-groups)   | Dependency groups to exclude from the graph.                                     | `[]`                                |
| [`dependency-graph.output`](#dependency-graph-output)         | Path where the dependency graph Mermaid diagram should be written.               | `"./docs/assets/dependencies.mmd"`  |
| [`dev-release.sync`](#dev-release-sync)                       | Whether dev pre-release sync is enabled for this project.                        | `true`                              |
| [`docs.apidoc-exclude`](#docs-apidoc-exclude)                 | Glob patterns for modules to exclude from `sphinx-apidoc`.                       | `[]`                                |
| [`docs.apidoc-extra-args`](#docs-apidoc-extra-args)           | Extra arguments appended to the `sphinx-apidoc` invocation.                      | `[]`                                |
| [`docs.update-script`](#docs-update-script)                   | Path to a Python script run after `sphinx-apidoc` to generate dynamic content.   | `"./docs/docs_update.py"`           |
| [`exclude`](#exclude)                                         | Additional components and files to exclude from repomatic operations.            | `[]`                                |
| [`gitignore.extra-categories`](#gitignore-extra-categories)   | Additional gitignore template categories to fetch from gitignore.io.             | `[]`                                |
| [`gitignore.extra-content`](#gitignore-extra-content)         | Additional content to append at the end of the generated `.gitignore` file.      | *(see example)*                     |
| [`gitignore.location`](#gitignore-location)                   | File path of the `.gitignore` to update, relative to the root of the repository. | `"./.gitignore"`                    |
| [`gitignore.sync`](#gitignore-sync)                           | Whether `.gitignore` sync is enabled for this project.                           | `true`                              |
| [`include`](#include)                                         | Components and files to force-include, overriding default exclusions.            | `[]`                                |
| [`labels.extra-content-rules`](#labels-extra-content-rules)   | Additional YAML rules appended to the content-based labeller configuration.      | `""`                                |
| [`labels.extra-file-rules`](#labels-extra-file-rules)         | Additional YAML rules appended to the file-based labeller configuration.         | `""`                                |
| [`labels.extra-files`](#labels-extra-files)                   | URLs of additional label definition files (JSON, JSON5, TOML, or YAML).          | `[]`                                |
| [`labels.sync`](#labels-sync)                                 | Whether label sync is enabled for this project.                                  | `true`                              |
| [`mailmap.sync`](#mailmap-sync)                               | Whether `.mailmap` sync is enabled for this project.                             | `true`                              |
| [`notification.unsubscribe`](#notification-unsubscribe)       | Whether the unsubscribe-threads workflow is enabled.                             | `false`                             |
| [`nuitka.enabled`](#nuitka-enabled)                           | Whether Nuitka binary compilation is enabled for this project.                   | `true`                              |
| [`nuitka.entry-points`](#nuitka-entry-points)                 | Which `[project.scripts]` entry points produce Nuitka binaries.                  | `[]`                                |
| [`nuitka.unstable-targets`](#nuitka-unstable-targets)         | Nuitka build targets allowed to fail without blocking the release.               | `[]`                                |
| [`pypi-package-history`](#pypi-package-history)               | Former PyPI package names for projects that were renamed.                        | `[]`                                |
| [`setup-guide`](#setup-guide)                                 | Whether the setup guide issue is enabled for this project.                       | `true`                              |
| [`skills.location`](#skills-location)                         | Directory prefix for Claude Code skill files, relative to the repository root.   | `"./.claude/skills/"`               |
| [`test-matrix.exclude`](#test-matrix-exclude)                 | Extra exclude rules applied to both full and PR test matrices.                   | `[]`                                |
| [`test-matrix.include`](#test-matrix-include)                 | Extra include directives applied to both full and PR test matrices.              | `[]`                                |
| [`test-matrix.remove`](#test-matrix-remove)                   | Per-axis value removals applied to both full and PR test matrices.               | {}                                  |
| [`test-matrix.replace`](#test-matrix-replace)                 | Per-axis value replacements applied to both full and PR test matrices.           | {}                                  |
| [`test-matrix.unstable`](#test-matrix-unstable)               | Full-matrix-only combinations to flag continue-on-error in CI.                   | `[]`                                |
| [`test-matrix.variations`](#test-matrix-variations)           | Extra matrix dimension values added to the full test matrix only.                | {}                                  |
| [`test-plan.file`](#test-plan-file)                           | Path to the YAML test plan file for binary testing.                              | `"./tests/cli-test-plan.yaml"`      |
| [`test-plan.inline`](#test-plan-inline)                       | Inline YAML test plan for binaries.                                              | *(none)*                            |
| [`test-plan.timeout`](#test-plan-timeout)                     | Timeout in seconds for each binary test.                                         | *(none)*                            |
| [`uv-lock.sync`](#uv-lock-sync)                               | Whether `uv.lock` sync is enabled for this project.                              | `true`                              |
| [`vulnerable-deps.sources`](#vulnerable-deps-sources)         | Advisory databases to consult for known vulnerabilities.                         | `['uv-audit', 'github-advisories']` |
| [`vulnerable-deps.sync`](#vulnerable-deps-sync)               | Whether the `fix-vulnerable-deps` job is enabled for this project.               | `true`                              |
| [`workflow.extra-paths`](#workflow-extra-paths)               | Literal entries to append to every workflow's `paths:` filter.                   | `[]`                                |
| [`workflow.ignore-paths`](#workflow-ignore-paths)             | Literal entries to strip from every workflow's `paths:` filter.                  | `[]`                                |
| [`workflow.paths`](#workflow-paths)                           | Per-workflow override of the `paths:` filter, keyed by filename.                 | {}                                  |
| [`workflow.source-paths`](#workflow-source-paths)             | Source code directory names for workflow trigger `paths:` filters.               | *(none)*                            |
| [`workflow.sync`](#workflow-sync)                             | Whether workflow sync is enabled for this project.                               | `true`                              |

### `agents.location`

Directory prefix for Claude Code agent files, relative to the repository root.

**Type:** `str` | **Default:** `"./.claude/agents/"`

Agent files are written as `{agents_location}/{agent-id}.md`.
Useful for repositories where `.claude/` is not at the root (like
dotfiles repos that store configs under a subdirectory).

**Example:**

```toml
[tool.repomatic]
agents.location = "./.claude/agents/"
```

### `awesome-template.sync`

Whether awesome-template sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Repositories whose name starts with `awesome-` get their boilerplate synced
from files bundled in `repomatic`. Set to `false` to opt out.

**Example:**

```toml
[tool.repomatic]
awesome-template.sync = true
```

### `bumpversion.sync`

Whether bumpversion config sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own `[tool.bumpversion]` section and do not want
the autofix job to overwrite it can set this to `false`.

**Example:**

```toml
[tool.repomatic]
bumpversion.sync = true
```

### `cache.dir`

Override the binary cache directory path.

**Type:** `str` | **Default:** `""`

When empty (the default), the cache uses the platform convention:
`~/Library/Caches/repomatic` on macOS, `$XDG_CACHE_HOME/repomatic`
or `~/.cache/repomatic` on Linux, `%LOCALAPPDATA%\repomatic\Cache`
on Windows. The `REPOMATIC_CACHE_DIR` environment variable takes
precedence over this setting.

**Example:**

```toml
[tool.repomatic]
cache.dir = ""
```

### `cache.github-release-ttl`

Freshness TTL for cached single-release bodies (seconds).

**Type:** `int` | **Default:** `604800`

GitHub release bodies are immutable once published, so a long TTL (7 days)
is safe. Set to `0` to disable caching for single-release lookups.

**Example:**

```toml
[tool.repomatic]
cache.github-release-ttl = 604800
```

### `cache.github-releases-ttl`

Freshness TTL for cached all-releases responses (seconds).

**Type:** `int` | **Default:** `86400`

New releases can appear at any time, so a shorter TTL (24 hours) balances
freshness with API savings.

**Example:**

```toml
[tool.repomatic]
cache.github-releases-ttl = 86400
```

### `cache.max-age`

Auto-purge cached entries older than this many days.

**Type:** `int` | **Default:** `30`

Set to `0` to disable auto-purge. The `REPOMATIC_CACHE_MAX_AGE`
environment variable takes precedence over this setting.

**Example:**

```toml
[tool.repomatic]
cache.max-age = 30
```

### `cache.pypi-ttl`

Freshness TTL for cached PyPI metadata (seconds).

**Type:** `int` | **Default:** `86400`

PyPI metadata changes when new versions are published. A 24-hour TTL
avoids redundant API calls while keeping data reasonably current.

**Example:**

```toml
[tool.repomatic]
cache.pypi-ttl = 86400
```

### `changelog.location`

File path of the changelog, relative to the root of the repository.

**Type:** `str` | **Default:** `"./changelog.md"`

**Example:**

```toml
[tool.repomatic]
changelog.location = "./changelog.md"
```

### `dependency-graph.all-extras`

Whether to include all optional extras in the graph.

**Type:** `bool` | **Default:** `true`

When `True`, the `update-deps-graph` command behaves as if
`--all-extras` was passed.

**Example:**

```toml
[tool.repomatic]
dependency-graph.all-extras = true
```

### `dependency-graph.all-groups`

Whether to include all dependency groups in the graph.

**Type:** `bool` | **Default:** `true`

When `True`, the `update-deps-graph` command behaves as if
`--all-groups` was passed. Projects that want to exclude development
dependency groups (docs, test, typing) from their published graph can
set this to `false`.

**Example:**

```toml
[tool.repomatic]
dependency-graph.all-groups = true
```

### `dependency-graph.level`

Maximum depth of the dependency graph.

**Type:** `int` | **Default:** *(none)*

`None` means unlimited. `1` = primary deps only, `2` = primary +
their deps, etc. Equivalent to `--level`.

### `dependency-graph.no-extras`

Optional extras to exclude from the graph.

**Type:** `list[str]` | **Default:** `[]`

Equivalent to passing `--no-extra` for each entry. Takes precedence
over `dependency-graph.all-extras`.

**Example:**

```toml
[tool.repomatic]
dependency-graph.no-extras = []
```

### `dependency-graph.no-groups`

Dependency groups to exclude from the graph.

**Type:** `list[str]` | **Default:** `[]`

Equivalent to passing `--no-group` for each entry. Takes precedence
over `dependency-graph.all-groups`.

**Example:**

```toml
[tool.repomatic]
dependency-graph.no-groups = []
```

### `dependency-graph.output`

Path where the dependency graph Mermaid diagram should be written.

**Type:** `str` | **Default:** `"./docs/assets/dependencies.mmd"`

The dependency graph visualizes the project's dependency tree in Mermaid format.

**Example:**

```toml
[tool.repomatic]
dependency-graph.output = "./docs/assets/dependencies.mmd"
```

### `dev-release.sync`

Whether dev pre-release sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that do not want a rolling draft pre-release maintained on
GitHub can set this to `false`.

**Example:**

```toml
[tool.repomatic]
dev-release.sync = true
```

### `docs.apidoc-exclude`

Glob patterns for modules to exclude from `sphinx-apidoc`.

**Type:** `list[str]` | **Default:** `[]`

Passed as positional exclude arguments after the source directory
(e.g., `["setup.py", "tests"]`).

**Example:**

```toml
[tool.repomatic]
docs.apidoc-exclude = []
```

### `docs.apidoc-extra-args`

Extra arguments appended to the `sphinx-apidoc` invocation.

**Type:** `list[str]` | **Default:** `[]`

The base flags `--no-toc --module-first` are always applied.
Use this for project-specific options (e.g., `["--implicit-namespaces"]`).

**Example:**

```toml
[tool.repomatic]
docs.apidoc-extra-args = []
```

### `docs.update-script`

Path to a Python script run after `sphinx-apidoc` to generate dynamic content.

**Type:** `str` | **Default:** `"./docs/docs_update.py"`

Resolved relative to the repository root. Must reside under the `docs/`
directory for security. Set to an empty string to disable.

**Example:**

```toml
[tool.repomatic]
docs.update-script = "./docs/docs_update.py"
```

### `exclude`

Additional components and files to exclude from repomatic operations.

**Type:** `list[str]` | **Default:** `[]`

Additive to the default exclusions (`labels`, `skills`). Bare names
exclude an entire component (e.g., `"workflows"`). Qualified
`component/identifier` entries exclude a specific file within a component
(e.g., `"workflows/debug.yaml"`, `"skills/repomatic-audit"`,
`"labels/labeller-content-based.yaml"`).

Affects `repomatic init`, `workflow sync`, and `workflow create`.
Explicit CLI positional arguments override this list.

**Example:**

```toml
[tool.repomatic]
exclude = []
```

### `gitignore.extra-categories`

Additional gitignore template categories to fetch from gitignore.io.

**Type:** `list[str]` | **Default:** `[]`

List of template names (e.g., `["Python", "Node", "Terraform"]`) to combine
with the generated `.gitignore` content.

**Example:**

```toml
[tool.repomatic]
gitignore.extra-categories = []
```

### `gitignore.extra-content`

Additional content to append at the end of the generated `.gitignore` file.

**Type:** `str` | **Default:** *(see example)*

**Example:**

```toml
[tool.repomatic]
gitignore.extra-content = '''
junit.xml

# Claude Code local files.
.claude/scheduled_tasks.lock
.claude/settings.local.json
'''
```

### `gitignore.location`

File path of the `.gitignore` to update, relative to the root of the repository.

**Type:** `str` | **Default:** `"./.gitignore"`

**Example:**

```toml
[tool.repomatic]
gitignore.location = "./.gitignore"
```

### `gitignore.sync`

Whether `.gitignore` sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own `.gitignore` and do not want the autofix job
to overwrite it can set this to `false`.

**Example:**

```toml
[tool.repomatic]
gitignore.sync = true
```

### `include`

Components and files to force-include, overriding default exclusions.

**Type:** `list[str]` | **Default:** `[]`

Use this to opt into components that are excluded by default (`labels`,
`skills`). Each entry is subtracted from the effective exclude set
(defaults + user `exclude`) and bypasses `RepoScope` filtering, so
scope-restricted components (like awesome-only skills or Python-only
`publish-pypi-action`) are included regardless of repository type.
Qualified entries (`component/file`) implicitly select the parent
component. Same syntax as `exclude`.

**Example:**

```toml
[tool.repomatic]
include = []
```

### `labels.extra-content-rules`

Additional YAML rules appended to the content-based labeller configuration.

**Type:** `str` | **Default:** `""`

Appended to the bundled `labeller-content-based.yaml` during export.

**Example:**

```toml
[tool.repomatic]
labels.extra-content-rules = ""
```

### `labels.extra-file-rules`

Additional YAML rules appended to the file-based labeller configuration.

**Type:** `str` | **Default:** `""`

Appended to the bundled `labeller-file-based.yaml` during export.

**Example:**

```toml
[tool.repomatic]
labels.extra-file-rules = ""
```

### `labels.extra-files`

URLs of additional label definition files (JSON, JSON5, TOML, or YAML).

**Type:** `list[str]` | **Default:** `[]`

Each URL is downloaded and applied separately by `labelmaker`.

**Example:**

```toml
[tool.repomatic]
labels.extra-files = []
```

### `labels.sync`

Whether label sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own repository labels and do not want the
labels workflow to overwrite them can set this to `false`.

**Example:**

```toml
[tool.repomatic]
labels.sync = true
```

### `mailmap.sync`

Whether `.mailmap` sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own `.mailmap` and do not want the autofix job
to overwrite it can set this to `false`.

**Example:**

```toml
[tool.repomatic]
mailmap.sync = true
```

### `notification.unsubscribe`

Whether the unsubscribe-threads workflow is enabled.

**Type:** `bool` | **Default:** `false`

Notifications are per-user across all repos. Enable on the single repo where
you want scheduled cleanup of closed notification threads. Requires a classic
PAT with `notifications` scope stored as `REPOMATIC_NOTIFICATIONS_PAT`.

**Example:**

```toml
[tool.repomatic]
notification.unsubscribe = false
```

### `nuitka.enabled`

Whether Nuitka binary compilation is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects with `[project.scripts]` entries that are not intended to produce
standalone binaries (e.g., libraries with convenience CLI wrappers) can set this
to `false` to opt out of Nuitka compilation.

**Example:**

```toml
[tool.repomatic]
nuitka.enabled = true
```

### `nuitka.entry-points`

Which `[project.scripts]` entry points produce Nuitka binaries.

**Type:** `list[str]` | **Default:** `[]`

List of CLI IDs (e.g., `["mpm"]`) to compile. When empty (the default),
deduplicates by callable target: keeps the first entry point for each
unique `module:callable` pair. This avoids building duplicate binaries
when a project declares alias entry points (like both `mpm` and
`meta-package-manager` pointing to the same function).

**Example:**

```toml
[tool.repomatic]
nuitka.entry-points = []
```

### `nuitka.unstable-targets`

Nuitka build targets allowed to fail without blocking the release.

**Type:** `list[str]` | **Default:** `[]`

List of target names (e.g., `["linux-arm64", "windows-x64"]`) that are marked as
unstable. Jobs for these targets will be allowed to fail without preventing the
release workflow from succeeding.

**Example:**

```toml
[tool.repomatic]
nuitka.unstable-targets = []
```

### `pypi-package-history`

Former PyPI package names for projects that were renamed.

**Type:** `list[str]` | **Default:** `[]`

When a project changes its PyPI name, older versions remain published under
the previous name. List former names here so `lint-changelog` can fetch
release metadata from all names and generate correct PyPI URLs.

**Example:**

```toml
[tool.repomatic]
pypi-package-history = []
```

### `setup-guide`

Whether the setup guide issue is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that do not need `REPOMATIC_PAT` or manage their
own PAT setup can set this to `false` to suppress the setup guide issue.

**Example:**

```toml
[tool.repomatic]
setup-guide = true
```

### `skills.location`

Directory prefix for Claude Code skill files, relative to the repository root.

**Type:** `str` | **Default:** `"./.claude/skills/"`

Skill files are written as `{skills_location}/{skill-id}/SKILL.md`.
Useful for repositories where `.claude/` is not at the root (like
dotfiles repos that store configs under a subdirectory).

**Example:**

```toml
[tool.repomatic]
skills.location = "./.claude/skills/"
```

### `test-matrix.exclude`

Extra exclude rules applied to both full and PR test matrices.

**Type:** `list[dict[str, str]]` | **Default:** `[]`

Each entry is a dict of GitHub Actions matrix keys (like
`{"os": "windows-11-arm"}`) that removes matching combinations.
Additive to the upstream default excludes.

**Example:**

```toml
[tool.repomatic]
test-matrix.exclude = []
```

### `test-matrix.include`

Extra include directives applied to both full and PR test matrices.

**Type:** `list[dict[str, str]]` | **Default:** `[]`

Each entry is a dict of GitHub Actions matrix keys that adds or augments
matrix combinations. Additive to the upstream default includes.

Because includes apply to both matrices, a directive whose keys are not PR
base axes is risky. In the PR matrix only `os` and `python-version` are base
axes, so a key like `click-version` (injected by another include) has
nothing to match and GitHub's expansion adds the directive to every PR job,
overwriting it. To flag a value continue-on-error, prefer `unstable` over an
`include` carrying `state: unstable`.

**Example:**

```toml
[tool.repomatic]
test-matrix.include = []
```

### `test-matrix.remove`

Per-axis value removals applied to both full and PR test matrices.

**Type:** `dict[str, list[str]]` | **Default:** {}

Outer key is the variation/axis ID (e.g., `os`, `python-version`).
Inner list contains values to drop from that axis. Applied after
replacements but before excludes, includes, and variations.

### `test-matrix.replace`

Per-axis value replacements applied to both full and PR test matrices.

**Type:** `dict[str, dict[str, str]]` | **Default:** {}

Outer key is the variation/axis ID (e.g., `os`, `python-version`).
Inner dict maps old values to new values. Applied before removals,
excludes, includes, and variations.

### `test-matrix.unstable`

Full-matrix-only combinations to flag continue-on-error in CI.

**Type:** `list[dict[str, str]]` | **Default:** `[]`

Each entry is a dict of GitHub Actions matrix keys (like
`{"click-version": "main"}`). Every full-matrix combination matching an
entry gets a `state: unstable` value, which `tests.yaml` reads to set
`continue-on-error`. Like `variations`, this applies to the full matrix
only; the PR matrix stays a curated stable set.

Prefer this over an `include` entry carrying `state: unstable`. `include`
applies to both matrices, and in the PR matrix a key like `click-version`
is not a base axis (another `include` injects it), so GitHub's expansion
would add the directive to every PR job and overwrite it. `unstable` only
touches the full matrix, sidestepping that hijack.

**Example:**

```toml
[tool.repomatic]
test-matrix.unstable = []
```

### `test-matrix.variations`

Extra matrix dimension values added to the full test matrix only.

**Type:** `dict[str, list[str]]` | **Default:** {}

Each key is a dimension ID (e.g., `os`, `click-version`) and its value
is a list of additional entries. For existing dimensions, values are merged
with the upstream defaults. For new dimension IDs, a new axis is created.
Only affects the full matrix; the PR matrix stays a curated reduced set.

### `test-plan.file`

Path to the YAML test plan file for binary testing.

**Type:** `str` | **Default:** `"./tests/cli-test-plan.yaml"`

The test plan file defines a list of test cases to run against compiled binaries.
Each test case specifies command-line arguments and expected output patterns.

**Example:**

```toml
[tool.repomatic]
test-plan.file = "./tests/cli-test-plan.yaml"
```

### `test-plan.inline`

Inline YAML test plan for binaries.

**Type:** `str` | **Default:** *(none)*

Alternative to `test_plan_file`. Allows specifying the test plan directly in
`pyproject.toml` instead of a separate file.

### `test-plan.timeout`

Timeout in seconds for each binary test.

**Type:** `int` | **Default:** *(none)*

If set, each test command will be terminated after this duration. `None` means no
timeout (tests can run indefinitely).

### `uv-lock.sync`

Whether `uv.lock` sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own lock file strategy and do not want the
`sync-uv-lock` job to run `uv lock --upgrade` can set this to `false`.

**Example:**

```toml
[tool.repomatic]
uv-lock.sync = true
```

### `vulnerable-deps.sources`

Advisory databases to consult for known vulnerabilities.

**Type:** `list[str]` | **Default:** `['uv-audit', 'github-advisories']`

Recognized values:

- `"uv-audit"`: PyPA Advisory Database via `uv audit` (works locally
  and in CI without a GitHub token).
- `"github-advisories"`: GitHub Advisory Database via the
  repository's Dependabot alerts (CI-only, requires a token with
  `Dependabot alerts: Read-only`).

Sources are unioned and deduplicated per package by advisory identity:
entries sharing an `advisory_id` or a cross-referenced CVE/GHSA/PYSEC
alias are merged. Repositories that distrust GHSA — or have no
Dependabot alerts enabled — can opt out with `sources = ["uv-audit"]`.

**Example:**

```toml
[tool.repomatic]
vulnerable-deps.sources = ["uv-audit", "github-advisories"]
```

### `vulnerable-deps.sync`

Whether the `fix-vulnerable-deps` job is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own vulnerability remediation flow can set
this to `false` to skip the autofix job.

**Example:**

```toml
[tool.repomatic]
vulnerable-deps.sync = true
```

### `workflow.extra-paths`

Literal entries to append to every workflow's `paths:` filter.

**Type:** `list[str]` | **Default:** `[]`

Applies to thin-caller and header-only sync. Useful for repo-specific
files that should re-trigger CI but are not detected by the canonical
`paths:` filter (e.g., `install.sh`, `dotfiles/**`).

Per-workflow overrides in `paths` ignore this list: when an entry exists
for a given filename, that entry is treated as the complete list.

**Example:**

```toml
[tool.repomatic]
workflow.extra-paths = []
```

### `workflow.ignore-paths`

Literal entries to strip from every workflow's `paths:` filter.

**Type:** `list[str]` | **Default:** `[]`

Useful for canonical entries that don't exist downstream (e.g.,
`tests/**`, `uv.lock` in repos with no Python tests or lockfile).
Match is by exact string equality. Applies before `extra_paths`.

Per-workflow overrides in `paths` ignore this list.

**Example:**

```toml
[tool.repomatic]
workflow.ignore-paths = []
```

### `workflow.paths`

Per-workflow override of the `paths:` filter, keyed by filename.

**Type:** `dict[str, list[str]]` | **Default:** {}

When a workflow filename appears here, its `paths:` blocks (in `push`,
`pull_request`, etc.) are replaced wholesale with the listed entries.
`source_paths`, `extra_paths`, and `ignore_paths` do **not** apply when
a per-workflow override is set: the list is treated as authoritative.

Override only takes effect on triggers that already have a `paths:`
filter in the canonical workflow. Workflows without `paths:` upstream
keep their unrestricted trigger semantics.

Example:

```toml
[tool.repomatic.workflow.paths]
"tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
```

### `workflow.source-paths`

Source code directory names for workflow trigger `paths:` filters.

**Type:** `list[str]` | **Default:** *(none)*

When set, thin-caller and header-only workflows include `paths:` filters
using these directory names (as `name/**` globs) alongside universal paths
like `pyproject.toml` and `uv.lock`.

When `None` (default), source paths are auto-derived from
`[project.name]` in `pyproject.toml` by replacing hyphens with
underscores — the universal Python convention. For example,
`name = "extra-platforms"` automatically uses `["extra_platforms"]`.

### `workflow.sync`

Whether workflow sync is enabled for this project.

**Type:** `bool` | **Default:** `true`

Projects that manage their own workflow files and do not want the autofix job
to sync thin callers or headers can set this to `false`.

**Example:**

```toml
[tool.repomatic]
workflow.sync = true
```

<!-- config-reference-end -->

## `[tool.X]` bridge and tool runner

`repomatic run` also bridges the gap for tools that can't read `pyproject.toml` natively: write your config in `[tool.<name>]` and repomatic translates it to the tool's native format at invocation time. See the [tool runner](tool-runner.md) page for the full list of supported tools, config resolution precedence, binary caching, and a tutorial.
