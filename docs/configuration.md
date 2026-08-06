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
patterns = ["(CVE|vulnerability)"]

[tool.repomatic.workflow.paths]
"tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
```

```{click:config} repomatic
from repomatic.cli import repomatic
```

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

## `[tool.X]` bridge and tool runner

`repomatic run` also bridges the gap for tools that can't read `pyproject.toml` natively: write your config in `[tool.<name>]` and repomatic translates it to the tool's native format at invocation time. See the [tool runner](tool-runner.md) page for the full list of supported tools, config resolution precedence, binary caching, and a tutorial.

## `repomatic.config` API

```{eval-rst}
.. automodule:: repomatic.config
   :members:
   :undoc-members:
   :show-inheritance:
```
