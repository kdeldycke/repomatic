# {octicon}`command-palette` CLI

> [!TIP]
> Each `[tool.repomatic]` config option maps to CLI behavior. See the [configuration reference](configuration.md) for project-level defaults.

```{click:tree} repomatic
from repomatic.cli.main import repomatic
```

## Command hierarchy

```{eval-rst}
.. autoclasstree:: repomatic.cli.main repomatic.cli.ci repomatic.cli.deps repomatic.cli.repo
   :strict:
```

The [`repomatic.cli.main`](repomatic.cli.main.md) module holds the `repomatic` group and the table that names every command. Each command's signature and docstring is on the page of its domain module, under the [`repomatic.cli`](repomatic.cli.md) package.
