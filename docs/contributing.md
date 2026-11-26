# {octicon}`git-pull-request` Contributing

This page holds the commands and the rules for developing `kdeldycke/repomatic` itself. The rules are rendered from its [`AGENTS.md`](https://github.com/kdeldycke/repomatic/blob/main/AGENTS.md). The section on downstream repositories below says which patterns other repositories can mirror. Maintainers also follow [§ Upstream development](upstream-development.md).

## Development commands

### Run the tests

```shell-session
# Run all tests, with the dependency set CI uses.
$ uv run --frozen --group test -- pytest

# Run a single test file.
$ uv run --frozen --group test -- pytest tests/test_changelog.py
```

### Build the documentation

```shell-session
$ uv run --frozen --all-extras --group docs -- sphinx-build -b html --fail-on-warning docs docs/_build
```

This is the command of the `docs` workflow. The `docs` group includes the `test` group, which the API pages of the test suite need. `--fail-on-warning` matches CI, where `[tool.repomatic] sphinx.fail-on-warning` fails the build on any warning.

### Run the CLI

```shell-session
$ uv run -- repomatic --help
```

## `AGENTS.md` file

```{include} ../AGENTS.md
:start-line: 2
```
