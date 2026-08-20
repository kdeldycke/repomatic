# {octicon}`tools` Tool runner

`repomatic run` is a unified entry point for running external linters, formatters, and security scanners. It installs each tool at a pinned version, resolves configuration through a strict precedence chain, and invokes the tool: no manual setup, no dotfile sprawl.

## Why not run the tools directly?

Installing a tool and running `yamllint .` yourself is fine for one tool on one machine. Once a project leans on a dozen, the same three chores repeat for each, and `repomatic run` takes care of all of them:

- Configuration stays in `pyproject.toml`, one reviewed file rather than a dotfile per tool. Even tools that can't read `pyproject.toml` themselves get their `[tool.X]` table translated to a temporary native config at run time, following the [precedence chain](#config-resolution) below.
- Installation is automatic: binaries come from GitHub Releases and are checksum-verified, PyPI tools run through `uvx`, tools that import your code (mypy, Nuitka) run inside the project virtualenv, and npm tools install from the npm registry (Node.js required).
- Versions are pinned and re-verified on each use, so a check behaves the same on your laptop and in CI instead of drifting with whatever each machine happens to have installed.

```{todo}
Drop the `[tool.X]` translation layer for each tool that grows native `pyproject.toml` support, as lychee already has. The standing upstream requests, all still unshipped:
[actionlint#623](https://github.com/rhysd/actionlint/issues/623),
[biome#9239](https://github.com/biomejs/biome/discussions/9239),
[gitleaks#2066](https://github.com/gitleaks/gitleaks/issues/2066),
[Nuitka#3909](https://github.com/Nuitka/Nuitka/issues/3909),
[zizmor#322](https://github.com/orgs/zizmorcore/discussions/322#discussioncomment-15919620).
The same request for shfmt ([sh#1268](https://github.com/mvdan/sh/issues/1268)) was declined.
```

## Quick start

Run a tool against your project:

```shell-session
$ repomatic run yamllint -- .
```

The `--` separates repomatic's own options from the arguments forwarded to the tool. Everything after `--` is passed through verbatim.

List all managed tools and their resolved config source:

```{click:source}
:hide-source:
from repomatic.cli import repomatic
```

```{click:run}
invoke(repomatic, args=['run', '--list'])
```

## Available tools

```{python:render}
from repomatic.tool_registry import tool_summary

print(tool_summary())
```

- **Binary**: downloaded as platform-specific executables from GitHub Releases.
- **PyPI**: installed via `uvx`.
- **PyPI (venv)**: run inside the project virtualenv via `uv run` because they need to import project code.
- **npm**: installed from the npm registry into a throwaway prefix and run via `node_modules/.bin`. The one backend that needs a foreign runtime (Node.js and npm on `PATH`); integrity is npm's own per-tarball verification, with no repomatic-pinned checksum.

## Config resolution

When `repomatic run <tool>` is invoked, configuration is resolved through a 4-level precedence chain. The first match wins: no merging across levels.

```mermaid
flowchart TD
    run([repomatic run TOOL]) --> l1{native config file?}
    l1 -->|yes| u1[Level 1. Use the in-repo config file]
    l1 -->|no| l2{tool.X in pyproject.toml?}
    l2 -->|yes| u2[Level 2. Use tool.X, translate if needed]
    l2 -->|no| l3{bundled default?}
    l3 -->|yes| u3[Level 3. repomatic bundled baseline]
    l3 -->|no| u4[Level 4. Bare invocation, tool defaults]
```

> [!TIP]
> Run `repomatic --verbosity INFO run <tool>` to see which config level was selected and the exact command line being executed. This is useful for debugging unexpected behavior. For full detail (config file contents, environment, caching), use `--verbosity DEBUG`.

### Level 1: native config file

If the tool's own config file exists in the repo (like `ruff.toml` or `.yamllint.yaml`), repomatic defers to it entirely. Your repo stays in control.

```shell-session
$ ls ruff.toml
ruff.toml
$ repomatic run ruff -- check .
# Uses ruff.toml directly — repomatic does nothing special.
```

### Level 2: `[tool.X]` in `pyproject.toml`

If no native config file is found but your `pyproject.toml` has a `[tool.<name>]` section, repomatic uses it. For tools that read `pyproject.toml` natively (ruff, mypy, bump-my-version, etc.), this just works. For tools that don't, repomatic translates the section into the tool's native format and passes it via a temporary config file.

```{note}
When the tool's native format is also TOML (like gitleaks), the translation keeps the comments from your `[tool.X]` section and only drops the `[tool.X]` prefix. Translations to another format (YAML, JSON) carry the values only: a TOML comment has no equivalent to map onto.
```

```toml
# pyproject.toml
[tool.yamllint.rules.line-length]
max = 120

[tool.yamllint.rules.truthy]
check-keys = false
```

```shell-session
$ repomatic run yamllint -- .
# Translates [tool.yamllint] to YAML, passes via --config-file.
```

All tools that support `[tool.X]` sections in `pyproject.toml`, whether natively or via repomatic's translation bridge:

| Tool                                                                                | Customizes                          | Section                                                                                              | Support                                                                                                               |
| :---------------------------------------------------------------------------------- | :---------------------------------- | :--------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------- |
| [actionlint](https://github.com/rhysd/actionlint)                                   | Workflow linting rules              | [`[tool.actionlint]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)            | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → YAML                         |
| [autopep8](https://github.com/hhatto/autopep8)                                      | Python code formatting              | [`[tool.autopep8]`](https://pypi.org/project/autopep8/)                                              | Native                                                                                                                |
| [biome](https://biomejs.dev)                                                        | JSON/JS formatting and linting      | [`[tool.biome]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)                 | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → JSON                         |
| [bump-my-version](https://callowayproject.github.io/bump-my-version/)               | Version bump patterns and files     | [`[tool.bumpversion]`](https://callowayproject.github.io/bump-my-version/reference/configuration/)   | Native                                                                                                                |
| [coverage.py](https://coverage.readthedocs.io/en/latest/config.html)                | Code coverage reporting             | [`[tool.coverage.*]`](https://coverage.readthedocs.io/en/latest/config.html#configuration-reference) | Native                                                                                                                |
| [gitleaks](https://github.com/gitleaks/gitleaks)                                    | Secret detection rules              | [`[tool.gitleaks]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)              | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → TOML                         |
| [lychee](https://lychee.cli.rs)                                                     | Link checking rules                 | [`[tool.lychee]`](https://lychee.cli.rs/guides/config/)                                              | Native                                                                                                                |
| [mdformat](https://mdformat.readthedocs.io/en/stable/users/configuration_file.html) | Markdown formatting options         | [`[tool.mdformat]`](https://mdformat.readthedocs.io/en/stable/users/configuration_file.html)         | Native (via [`mdformat-pyproject`](https://github.com/csala/mdformat-pyproject))                                      |
| [mypy](https://mypy.readthedocs.io/en/stable/config_file.html)                      | Static type checking                | [`[tool.mypy]`](https://mypy.readthedocs.io/en/stable/config_file.html#using-a-pyproject-toml-file)  | Native                                                                                                                |
| [nuitka](https://github.com/Nuitka/Nuitka)                                          | Standalone binary compilation       | [`[tool.nuitka]`](https://nuitka.net/doc/user-manual.html)                                           | [repomatic bridge](#nuitka) → CLI flags ([native support: Nuitka#3909](https://github.com/Nuitka/Nuitka/issues/3909)) |
| [pyproject-fmt](https://pyproject-fmt.readthedocs.io/en/latest/)                    | `pyproject.toml` formatting         | [`[tool.pyproject-fmt]`](https://pyproject-fmt.readthedocs.io/en/latest/)                            | Native                                                                                                                |
| [pytest](https://docs.pytest.org/en/stable/reference/customize.html)                | Test runner options                 | [`[tool.pytest]`](https://docs.pytest.org/en/stable/reference/customize.html#pyproject-toml)         | Native                                                                                                                |
| [ruff](https://docs.astral.sh/ruff/configuration/)                                  | Linting and formatting rules        | [`[tool.ruff]`](https://docs.astral.sh/ruff/configuration/#configuring-ruff)                         | Native                                                                                                                |
| [typos](https://github.com/crate-ci/typos)                                          | Spell-checking exceptions           | [`[tool.typos]`](https://github.com/crate-ci/typos/blob/master/docs/reference.md)                    | Native                                                                                                                |
| [uv](https://docs.astral.sh/uv/reference/settings/)                                 | Package resolution and build config | [`[tool.uv]`](https://docs.astral.sh/uv/reference/settings/)                                         | Native                                                                                                                |
| [yamllint](https://yamllint.readthedocs.io)                                         | YAML linting rules                  | [`[tool.yamllint]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)              | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → YAML                         |
| [zizmor](https://docs.zizmor.sh)                                                    | Workflow security scanning          | [`[tool.zizmor]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)                | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → YAML                         |

See [Click Extra's inventory of `pyproject.toml`-aware tools](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) for a broader list.

### Level 3: bundled default

If the repo has no config at all, repomatic falls back to its own bundled defaults (stored in `repomatic/data/`). These provide sensible baseline rules so that tools produce useful results even without any project-specific configuration.

Tools with bundled defaults: actionlint, mdformat, ruff, yamllint, zizmor.

### Level 4: bare invocation

If none of the above applies (no config file, no `[tool.X]`, no bundled default), the tool runs with its own built-in defaults. Tools like autopep8 work this way: all behavior is controlled through CLI flags.

### Checking the active config source

To see which precedence level is active for each tool in your repo:

```{click:run}
invoke(repomatic, args=['run', '--list'])
```

The "Config source" column shows whether the tool is using a native config file (level 1), `[tool.X]` (level 2), a bundled default (level 3), or bare invocation (level 4).

## Tutorial: adding yamllint to your project

This walkthrough covers a common scenario: running yamllint on a project that has no YAML linting configured.

### Step 1: run with defaults

With no config file and no `[tool.yamllint]` section in `pyproject.toml`, repomatic uses its bundled default:

```shell-session
$ repomatic run yamllint -- .
```

The bundled config enforces strict YAML rules. If that produces too many warnings, customize it.

### Step 2: customize via `pyproject.toml`

Instead of creating a `.yamllint.yaml` file, add a section to your `pyproject.toml`:

```toml
[tool.yamllint.rules.line-length]
max = 120

[tool.yamllint.rules.truthy]
check-keys = false
```

Now `repomatic run yamllint -- .` translates this to YAML, passes it via `--config-file`, and cleans up the temporary file afterward.

### Step 3: graduate to a native config file

If your yamllint config grows complex, create a `.yamllint.yaml` directly. Once that file exists, repomatic defers to it (level 1 takes precedence) and the `[tool.yamllint]` section in `pyproject.toml` is ignored.

### Cleaning up unmodified configs

If you previously ran `repomatic init` and have a native config file that is identical to the bundled default, `repomatic init --delete-unmodified` removes it:

```shell-session
$ repomatic init --delete-unmodified
```

## Overriding tool versions

To test a newer version of a tool before the registry is updated:

```shell-session
$ repomatic run shfmt --version 3.14.0 --skip-checksum -- .
```

`--skip-checksum` is required because the registry only stores checksums for the pinned version. For binary tools, `--checksum` lets you provide the correct SHA-256 for the new version instead of skipping verification entirely:

```shell-session
$ repomatic run shfmt --version 3.14.0 --checksum abc123... -- .
```

## Binary caching

`repomatic run` downloads platform-specific binaries (actionlint, biome, gitleaks, labelmaker, lychee, etc.) from GitHub Releases. To avoid re-downloading on every invocation, binaries are cached under a platform-appropriate user cache directory:

| Platform | Default cache path                                  |
| :------- | :-------------------------------------------------- |
| Linux    | `$XDG_CACHE_HOME/repomatic` or `~/.cache/repomatic` |
| macOS    | `~/Library/Caches/repomatic`                        |
| Windows  | `%LOCALAPPDATA%\repomatic\Cache`                    |

Cached binaries are re-verified against their registry SHA-256 checksum on every use. Entries older than 30 days are auto-purged.

Both settings are configurable via `[tool.repomatic]` (see [`cache.dir`](configuration.md#cache-dir) and [`cache.max-age`](configuration.md#cache-max-age)) or environment variables. The env var takes precedence over the config.

| Environment variable      | Config key                                        | Default               | Description                                                 |
| :------------------------ | :------------------------------------------------ | :-------------------- | :---------------------------------------------------------- |
| `REPOMATIC_CACHE_DIR`     | [`cache.dir`](configuration.md#cache-dir)         | *(platform-specific)* | Override the cache directory path.                          |
| `REPOMATIC_CACHE_MAX_AGE` | [`cache.max-age`](configuration.md#cache-max-age) | `30`                  | Auto-purge entries older than this many days. `0` disables. |

Cache management commands:

```shell-session
$ repomatic cache show
$ repomatic cache clean
$ repomatic cache clean --tool ruff --max-age 7
$ repomatic cache path
```

Use `--no-cache` on `repomatic run` to bypass the cache entirely.

## Running with no arguments

Some tools declare the arguments and target files CI would pass on their behalf, so `repomatic run <tool>` with nothing after it runs the same invocation:

```shell-session
$ repomatic run yamllint
$ repomatic run mdformat
```

The first resolves to `repomatic run yamllint -- .`; the second walks every Markdown file in the repository and formats each one in turn, matching what the `format-markdown` job runs. A tool with no declared defaults runs bare, exactly as before.

```{note}
This only fires on a **bare** invocation. Passing any argument after `--`, even one that overlaps with the tool's defaults, hands control to you entirely: nothing is injected on top of it. Splicing repomatic's defaults into a caller-driven command could otherwise build something like `biome format … check .`, which is not a command anyone meant to run.
```

When a tool's defaults are file-driven and the repository holds no matching file, the tool is skipped rather than invoked with no path: a formatter handed zero paths does not no-op, it walks the entire tree in write mode.

## Passing extra arguments

Everything after `--` is forwarded to the tool:

```shell-session
$ repomatic run ruff -- check --fix .
$ repomatic run zizmor -- --offline .github/workflows/
$ repomatic run biome -- format --write src/
```

For tools with subcommands (ruff, biome, gitleaks), the subcommand goes after `--` as the first argument.

## Verifying without writing

`--verify` reports which targets a tool would rewrite, without touching the working tree:

```shell-session
$ repomatic run mdformat --verify -- changelog.md
```

It runs the write path against throwaway copies of the targets and diffs the results, rather than trusting the tool's own `--check` or `--dry-run` mode. That distinction matters for a tool whose check mode relies on a repomatic post-processing step that only runs after an actual write: for those, `--check` can report drift the write path would reconcile, or miss drift the write path would introduce. `--verify` is the authoritative answer either way.

With no arguments after the tool name, `--verify` resolves the same defaults a bare `repomatic run <tool>` would, so `repomatic run mdformat --verify` checks every Markdown file in the repository.

## Tool details

```{python:render}
from repomatic.tool_registry import tool_reference

print(tool_reference())
```

## `repomatic.tool_runner` API

```{eval-rst}
.. automodule:: repomatic.tool_runner
   :members:
   :undoc-members:
   :show-inheritance:
```
