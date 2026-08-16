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
sphinx.builder = "dirhtml"
sphinx.deploy = "cloudflare-pages"
uv-lock.sync = false

dependency-graph.output = "./docs/assets/dependencies.mmd"
dependency-graph.all-groups = true
dependency-graph.all-extras = true
dependency-graph.no-groups = []
dependency-graph.no-extras = []
dependency-graph.level = 0

metrics.sync = true
metrics.store = "./docs/assets/metrics.csv"

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

nuitka.dev-targets = ["linux-arm64", "windows-x64"]
nuitka.enabled = false
nuitka.entry-points = ["mpm"]
nuitka.nofollow-imports = []
nuitka.unstable-targets = ["linux-arm64", "windows-arm64"]

workflow.sync = false
workflow.source-paths = ["extra_platforms"]
workflow.extra-paths = ["install.sh", "dotfiles/**"]
workflow.ignore-paths = ["uv.lock"]

[tool.repomatic.labels.file-rules]
"📚 docs" = ["docs/**", "!docs/generated/**"]

[tool.repomatic.labels.content-rules]
"🛡️ security" = ["CVE", "vulnerability"]

[tool.repomatic.workflow.paths]
"tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
```

```{click:config} repomatic
from repomatic.cli import repomatic
```

### Ephemeral components

Most components write files a repository is meant to commit. The `labels` component does not: `labels.toml` is an input that `sync-labels` regenerates from this configuration right before handing it to labelmaker, so a copy sitting in the working tree is never the one that gets used.

The file is therefore staged only when the component is named explicitly:

```shell-session
$ repomatic init labels
```

A bare `repomatic init` leaves it out, and listing `labels` in `include` does not change that: the override is refused with a warning rather than silently honored. Everything downstream repositories customize (`labels.extra`, `labels.file-rules`, `labels.content-rules`, `labels.extra-files`) is read straight from `pyproject.toml`, so no generated label file needs committing. A `.github/labeller-*.yaml` left over from the era when the labeller ran as a GitHub Action is dead weight: nothing reads it any more, delete it.

### Diverging from a managed file

Every file repomatic manages is a generated output, not a starting template. `sync-repomatic`, `sync-gitignore` and their siblings rebuild these files from `[tool.repomatic]` on each run and open a pull request for the difference, so an edit made straight to the file survives exactly until the next sync. The revert arrives as an ordinary-looking sync pull request, which is what makes it easy to miss: the diff is undoing a deliberate local decision and reads like routine maintenance.

`sync-gitignore` is the exception, because there the loss is silent and total rather than a visible diff line: it refuses to write when a rule on disk is absent from what it generated, listing the rules at stake and exiting non-zero. Move them into `gitignore.extra-content` to keep them, or pass `--drop-orphans` to confirm they should go. Comments and ordering are not compared, so a reformatted file is not mistaken for a rewritten one.

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

When no knob covers the change, reach for `exclude` only if you understand what it means, because it is not an opt-out from syncing: it declares that the repository has no such file. The `sync-repomatic` job runs `repomatic init --delete-unmodified --delete-excluded`, so the first sync after the entry lands opens a pull request **deleting** the file rather than leaving the local version alone. Excluding a workflow to protect your edits to it removes your CI instead.

Keeping a customized file therefore means keeping it out of the managed set entirely, by giving it a name repomatic does not own:

```toml
# The repository has no upstream tests.yaml: its own check lives unmanaged in
# workflows/install.yaml, which repomatic never generates and never deletes.
exclude = ["workflows/tests.yaml"]
```

The rename is what preserves the file; the `exclude` entry merely stops repomatic from recreating the managed one beside it. A repository that wants to freeze workflows under their existing names has the blunter `workflow.sync = false`, which returns before any workflow is written and leaves every file on disk untouched, at the cost of ending upstream fixes for all of them, not just the customized one.

Prefer a knob whenever one exists. Reach for a rename when a file's purpose genuinely diverges from the template, and for `workflow.sync = false` when a repository has taken over its workflows wholesale.

Workflow triggers deserve particular care, because a reverted trigger restores itself. GitHub builds a `pull_request` run from the [merge commit](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) rather than from either branch alone, so removing a `pull_request:` trigger on the default branch takes effect immediately for open pull requests: their merge commits pick up the new file and stop firing. The sync pull request restoring that trigger is the exception, since it is the one merge commit where the trigger still exists, and it re-enables the workflow for itself. A repository that removes a trigger and then watches a single pull request keep running it is usually looking at the sync that puts it back, and excluding the file settles both halves at once.

### Labeller rules

`repomatic apply-labels` evaluates both families on every issue and pull request opened. Each is a table mapping a label to the patterns that apply it: `labels.content-rules` against the thread's title and body, `labels.file-rules` against the paths a pull request changes. Any one pattern matching applies the label.

```toml
[tool.repomatic.labels.content-rules]
"📦 manager: apk" = ["apk", "alpine", "alpine linux"]
"🐛 bug" = []

[tool.repomatic.labels.file-rules]
"📦 manager: apk" = ["managers/apk.*", "tests/*apk*"]
```

A content pattern is a plain keyword by default: matched case-insensitively and anchored on word boundaries, so `Alpine` matches and `prefix` does not trip a `fix` rule. Wrap a pattern in slashes (`/traceback|stack trace/i`) to pass a regex through instead, with the `i`, `m` and `s` flags honored. File patterns are `minimatch`-dialect globs (`**` crosses directories, `{a,b}` expands, a leading dot needs no special casing), and a `!`-prefixed glob subtracts from the label's other globs the way a `.gitignore` line would.

Your entry for a label **replaces** the bundled default entry for that label, an empty list disables it (as `"🐛 bug" = []` above), and labels the defaults do not mention are added. Untouched defaults keep flowing from upstream releases. The default rule sets ship in {data}`repomatic.labels.DEFAULT_CONTENT_RULES` and {data}`repomatic.labels.DEFAULT_FILE_RULES`, and every rule must name a label the repository actually defines: see `labels.extra` above for declaring new ones.

Tune rules for precision, not recall. The labeller pre-labels for the maintainer's first pass and never replaces it: a missing label costs one manual click, while a wrong one is noise on every thread that trips it. Never key a rule off a token the project prints in its own output, or a user pasting a trace sets every such label at once.

### Forge sampling

`sample-metrics` records what forges say about a set of repositories, into one CSV committed here. It is opt-in, and `metrics.sync` is also what decides whether `repomatic init` hands the repository the `metrics.yaml` workflow at all.

Every reading is one row: which repository, which metric, on which date, what it said, and where the figure came from. How long a row is kept is a property of the metric rather than of the caller. A **counter** like the star count accrues, so its curve can be charted. An **attribute** like the date of a project's newest commit keeps a single row, restamped only when the value moves, since nothing reads it chronologically and a hundred subjects sampled weekly would otherwise pile up thousands of rows a year.

```toml
[tool.repomatic.metrics]
store = "./docs/assets/metrics.csv"
sync = true

# A bare owner/name is GitHub; anything else is a full URL on whichever forge
# hosts it.
[tool.repomatic.metrics.subjects]
apricot = "apricot-org/apricot"
papaya = "https://gitlab.com/papaya/papaya"

# A project that reopened under a new repository: its forerunner is drawn
# dashed, in the successor's hue, and stops where the successor begins.
[tool.repomatic.metrics.predecessors]
papaya = "old-owner/papaya"

# A self-hosted instance is never guessed from its name.
[tool.repomatic.metrics.forges]
"gitlab.example.org" = "gitlab"

# A mapping rather than a list: the reason is the point.
[tool.repomatic.metrics.skip]
carrot = "Ships in a distribution package with no public repository."

[[tool.repomatic.metrics.charts]]
output = "./docs/assets/star-history.svg"

[[tool.repomatic.metrics.charts]]
mode = "relative"
output = "./docs/assets/star-history-by-age.svg"

[[tool.repomatic.metrics.charts]]
only = ["apricot"]
output = "./docs/assets/star-history-apricot.svg"
```

A chart plots one metric, `stars` unless `metric` names another, and only a metric the store accrues can be charted. `mode` is `absolute` (one shared calendar) or `relative` (every curve measured from its own origin, which compares trajectories rather than dates). Hues come from a twelve-slot palette assigned in draw order; pin one with `[tool.repomatic.metrics.colors]` when it must survive a reordering.

Two collectors are GitHub-only and skip every other forge with a note. An **exact reconstruction** rebuilds a star curve from the timestamp of every star a repository still holds, which works wherever the token administers it, so that curve is complete from day one rather than starting on the day sampling did. An **archive backfill** (`--backfill-wayback`) mines contemporaneous counts from archived `github.com` pages, for a repository nobody administers. Both are one-offs the scheduled job never runs.

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

`flavor.agent` drives where assets land: leave `skills.location`, `subagents.location`, `agent.location` and `settings.location` unset and each follows the agent's own layout, while setting one explicitly overrides it for that asset alone. Defaults are static and never auto-detected: deriving them from the running agent would make a repository's effective configuration depend on which tool last invoked repomatic, and `repomatic metadata` would stop being reproducible.

`agent.location` is the one worth setting by hand, because the file it names is the only asset a repository is likely to already own under a name of its own choosing. It defaults to the selected agent's own filename (`./claude.md` for Claude Code), and pointing it at the cross-agent `AGENTS.md`, or anywhere outside the root, is what lets `repomatic init agent` sync a document that already exists rather than creating a second one beside it:

```toml
[tool.repomatic]
include = ["agent"]
agent.location = "./dotfiles/.agents/AGENTS.md"
```

```{caution}
`agent.location` and `subagents.location` differ by one character and name different things: the first an instructions document, the second a directory of subagent definitions. Setting the wrong one is silent, since neither is validated against the other.
```

### Repository scope

A bare `repomatic init` writes only what a repository can actually use. Three traits, all read from the repository itself, decide what that means:

| Trait          | Detected from                                        | Gates                                                                   |
| :------------- | :--------------------------------------------------- | :---------------------------------------------------------------------- |
| Awesome list   | A repository name starting with `awesome-`           | `awesome-template`, the awesome-only skills, `lychee`                   |
| Python project | A PEP 621 `[project]` table that validates           | `debug.yaml`, dependency locking, the test matrix                       |
| Distributable  | A Python project without `[tool.uv] package = false` | `changelog.md`, `changelog.yaml`, `release.yaml`, `publish-pypi-action` |

The last two come apart for a **uv virtual project**: a repository that declares `[project]` purely to carry dependencies and opts out of being built with `[tool.uv] package = false`. Blogs, docs sites and dotfiles repositories managed with uv all look like this. They keep everything a Python project needs (a `uv.lock` to sync, coverage config, the test matrix) and skip the release lane, which has nothing to publish or tag.

A `pyproject.toml` carrying only `[tool.*]` sections is not a Python project at all, so it gets neither group.

Scope is a default, not a rule. Naming a component on the command line, or listing it in `include`, materializes it regardless:

```shell-session
$ repomatic init changelog
```

### Adopting a tool config

Tool configs are the one group a bare `repomatic init` never introduces. `[tool.typos]`, `[tool.ruff]`, `[tool.pytest]` and their siblings land only when named:

```shell-session
$ repomatic init typos
```

Adoption is one-way. Once the section exists, a bare `init` picks it back up on every run, so the `sync-repomatic` job keeps it aligned with the bundled template from then on: new canonical rules arrive, local additions survive. The section is a managed file like any other after that, which makes it subject to [§ Diverging from a managed file](#diverging-from-a-managed-file): the sync rebuilds it from the template and grafts local content back, so hand-written comments inside it do not survive.

Only the configs repomatic keeps syncing behave this way: `typos`, `uv` and `bumpversion` through this adoption path, plus `lychee` on awesome-list repos, where it lands by default rather than through explicit naming. The rest (`ruff`, `pytest`, `coverage`, `mypy`, `mdformat`) are starting points the repository owns outright after the first write, and `init` never revisits them.

## `[tool.X]` bridge and tool runner

`repomatic run` also bridges the gap for tools that can't read `pyproject.toml` natively: write your config in `[tool.<name>]` and repomatic translates it to the tool's native format at invocation time. See the [tool runner](tool-runner.md) page for the full list of supported tools, config resolution precedence, binary caching, and a tutorial.

## `repomatic.config` API

```{eval-rst}
.. automodule:: repomatic.config
   :members:
   :undoc-members:
   :show-inheritance:
```
