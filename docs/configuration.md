# {octicon}`sliders` Configuration

repomatic reads two kinds of `pyproject.toml` configuration. Its own settings live in `[tool.repomatic]`, documented below. The third-party tools it runs are configured through their own standard `[tool.*]` sections (`[tool.ruff]`, `[tool.mypy]`, `[tool.typos]`, `[tool.nuitka]`, and so on); repomatic discovers and resolves these through its [tool runner](tool-runner.md), where they are documented.

## `[tool.repomatic]` configuration

Downstream projects can customize workflow behavior by adding a `[tool.repomatic]` section in their `pyproject.toml`. These options control the defaults for the corresponding [CLI commands](cli.md).

The `[tool.repomatic]` section is powered by [Click Extra's `pyproject.toml` configuration](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml). Click Extra handles [CWD-first discovery](https://kdeldycke.github.io/click-extra/config.html#cwd-first-discovery) (walking up to the VCS root), [key normalization](https://kdeldycke.github.io/click-extra/config.html#key-normalization) (kebab-case to snake_case), and [typed dataclass schemas](https://kdeldycke.github.io/click-extra/config.html#typed-configuration-schema) (nested sub-tables, opaque dict fields, strict validation).

```toml
[tool.repomatic]
pypi-package-history = ["old-name", "older-name"]

awesome-template.sync = false
binaries.sync = false
bumpversion.sync = false
cache.max-age = 14
dep-sources.sync = false
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
# Claude Code
.claude/
'''

exclude = ["skills", "workflows/debug.yaml", "zizmor"]

flavor.agent = "claude_code"
flavor.ci = "github_ci"

labels.extra-files = ["https://example.com/my-labels.toml"]

nuitka.enabled = false
nuitka.entry-points = ["mpm"]
nuitka.nofollow-imports = []
nuitka.unstable-targets = ["linux-arm64", "windows-arm64"]

workflow.sync = false
workflow.source-paths = ["extra_platforms"]
workflow.extra-paths = ["install.sh", "dotfiles/**"]
workflow.ignore-paths = ["uv.lock"]

[[tool.repomatic.labels.file-rules]]
label = "📚 docs"
any-glob-to-any-file = ["docs/**"]

[[tool.repomatic.labels.content-rules]]
label = "🛡️ security"
patterns = ['/\bCVE\b|\bvulnerability\b/i']

[tool.repomatic.workflow.paths]
"tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
```

```{click:config} repomatic
from repomatic.cli import repomatic
```

### Ephemeral components

Most components write files a repository is meant to commit. The `labels` component does not: `labels.toml` and the two labeller YAMLs under `.github/` are inputs that `sync-labels` and the two labeller jobs each regenerate from this configuration right before reading, so a copy sitting in the working tree is never the one that gets used.

Those files are therefore staged only when the component is named explicitly:

```shell-session
$ repomatic init labels
```

which is how the labeller jobs put a config where `actions/labeler` and `github/issue-labeler` can read it from the checkout. A bare `repomatic init` leaves them out, and listing `labels` in `include` does not change that: the override is refused with a warning rather than silently honored. Everything downstream repositories customize (`labels.extra`, `labels.file-rules`, `labels.content-rules`, `labels.extra-files`) is read straight from `pyproject.toml` by those commands, so no generated label file needs committing.

A repository that committed them before will see them reported as excluded files still on disk. Clear them with:

```shell-session
$ repomatic init --delete-excluded
```

### Diverging from a managed file

Every file repomatic manages is a generated output, not a starting template. `sync-repomatic`, `sync-gitignore` and their siblings rebuild these files from `[tool.repomatic]` on each run and open a pull request for the difference, so an edit made straight to the file survives exactly until the next sync. The revert arrives as an ordinary-looking sync pull request, which is what makes it easy to miss: nothing warns that the diff is undoing a deliberate local decision.

There are two ways to keep a divergence, and picking the wrong one is why the edit keeps coming back.

When the file already exposes a knob for what you want to change, set the knob. `.gitignore` is the common case: everything past the gitignore.io block is `gitignore.extra-content`, so a pattern appended by hand to the generated file is dropped on the next sync, while the same pattern in `pyproject.toml` is emitted every time. Mind that the option **replaces** its default rather than extending it, so the entries repomatic ships have to be repeated alongside the new ones or they disappear too:

```toml
gitignore.extra-content = '''
# Claude Code local files.
.claude/scheduled_tasks.lock
.claude/settings.local.json
**/.claude/.cc-writes/

# Sphinx linkcheck output.
docs/_linkcheck/
'''
```

When no knob covers the change, take the file out of repomatic's hands with a qualified `exclude` entry. A repository whose `tests.yaml` runs something other than the upstream Python matrix, and therefore needs its own triggers and `timeout-minutes`, has no option to reach for:

```toml
exclude = ["workflows/tests.yaml"]
```

This is deliberately all-or-nothing. There is no per-section preservation, so an excluded file stops receiving upstream fixes along with the unwanted overwrites, and its content becomes the repository's own responsibility. Prefer a knob whenever one exists, and reserve `exclude` for files whose purpose genuinely diverges from the template.

Workflow triggers deserve particular care, because a reverted trigger restores itself. GitHub builds a `pull_request` run from the [merge commit](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) rather than from either branch alone, so removing a `pull_request:` trigger on the default branch takes effect immediately for open pull requests: their merge commits pick up the new file and stop firing. The sync pull request restoring that trigger is the exception, since it is the one merge commit where the trigger still exists, and it re-enables the workflow for itself. A repository that removes a trigger and then watches a single pull request keep running it is usually looking at the sync that puts it back, and excluding the file settles both halves at once.

### Labeller rules

`labels.file-rules` match a pull request's changed paths through `actions/labeler`, which OR-joins repeated globs for the same label. `labels.content-rules` match issue and pull request prose through `github/issue-labeler`, which does the opposite: it **AND-joins** a label's `patterns`, so a list of bare keywords fires only when every one of them shows up in the same issue. Give each label a single pattern that ORs its keywords into one alternation, written in the `/…/i` form so it matches case-insensitively, and anchored with `\b` so `fix` does not fire on `prefix`. `sync-labels` warns when a label carries more than one pattern.

### Flavors

`[tool.repomatic.flavor]` declares which ecosystems a repository targets, giving every present and future ecosystem decision one place to live instead of a new flag per feature.

| Key            | Default       | Meaning                                                      |
| :------------- | :------------ | :----------------------------------------------------------- |
| `flavor.agent` | `claude_code` | AI coding agent whose asset layout skills and agents follow. |
| `flavor.ci`    | `github_ci`   | CI system the bundled workflows target.                      |

Values are trait IDs borrowed from [extra-platforms](https://github.com/kdeldycke/extra-platforms), which already models both AI agents and CI systems, so repomatic inherits its vocabulary and detection helpers instead of maintaining a parallel enum. Hyphens are normalized, so `claude-code` and `claude_code` are the same thing.

Each value is checked twice, and the two failures read differently on purpose. A value outside the upstream vocabulary is a typo:

```text
Unknown [tool.repomatic] flavor.ci = 'mango'. Expected an extra-platforms trait ID: azure_pipelines, bamboo, …
```

while a real ecosystem repomatic has not implemented says so plainly:

```text
Unsupported [tool.repomatic] flavor.ci = 'gitlab_ci'. repomatic targets: github_ci.
```

`flavor.agent` drives where assets land: leave `skills.location` and `agents.location` unset and they follow the agent's own layout, while setting either explicitly overrides it. Defaults are static and never auto-detected: deriving them from the running agent would make a repository's effective configuration depend on which tool last invoked repomatic, and `repomatic metadata` would stop being reproducible.

### Repository scope

A bare `repomatic init` writes only what a repository can actually use. Three traits, all read from the repository itself, decide what that means:

| Trait          | Detected from                                        | Gates                                                                   |
| :------------- | :--------------------------------------------------- | :---------------------------------------------------------------------- |
| Awesome list   | A repository name starting with `awesome-`           | `awesome-template`, the awesome-only skills, `lychee`                   |
| Python project | A PEP 621 `[project]` table that validates           | `codecov`, `debug.yaml`, dependency locking                             |
| Distributable  | A Python project without `[tool.uv] package = false` | `changelog.md`, `changelog.yaml`, `release.yaml`, `publish-pypi-action` |

The last two come apart for a **uv virtual project**: a repository that declares `[project]` purely to carry dependencies and opts out of being built with `[tool.uv] package = false`. Blogs, docs sites and dotfiles repositories managed with uv all look like this. They keep everything a Python project needs (a `uv.lock` to sync, coverage config, the test matrix) and skip the release lane, which has nothing to publish or tag.

A `pyproject.toml` carrying only `[tool.*]` sections is not a Python project at all, so it gets neither group.

Scope is a default, not a rule. Naming a component on the command line, or listing it in `include`, materializes it regardless:

```shell-session
$ repomatic init changelog
```

## `[tool.X]` bridge and tool runner

`repomatic run` also bridges the gap for tools that can't read `pyproject.toml` natively: write your config in `[tool.<name>]` and repomatic translates it to the tool's native format at invocation time. See the [tool runner](tool-runner.md) page for the full list of supported tools, config resolution precedence, binary caching, and a tutorial.

## `repomatic.config` API

```{eval-rst}
.. automodule:: repomatic.config
   :members:
   :undoc-members:
   :show-inheritance:
```
