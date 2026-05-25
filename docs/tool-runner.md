# {octicon}`tools` Tool runner

`repomatic run` is a unified entry point for running external linters, formatters, and security scanners. It installs each tool at a pinned version, resolves configuration through a strict precedence chain, and invokes the tool: no manual setup, no dotfile sprawl.

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

<!-- tool-summary-start -->

| Tool                                                                  | Version  | Type        | Config discovery                                             |
| :-------------------------------------------------------------------- | :------- | :---------- | :----------------------------------------------------------- |
| [actionlint](https://github.com/rhysd/actionlint)                     | `1.7.12` | Binary      | `.github/actionlint.yaml`                                    |
| [autopep8](https://github.com/hhatto/autopep8)                        | `2.3.2`  | PyPI        | CLI flags only                                               |
| [Biome](https://github.com/biomejs/biome)                             | `2.4.15` | Binary      | `biome.json`, `biome.jsonc`                                  |
| [bump-my-version](https://github.com/callowayproject/bump-my-version) | `1.2.7`  | PyPI        | `[tool.bump-my-version]` in `pyproject.toml`                 |
| [Gitleaks](https://github.com/gitleaks/gitleaks)                      | `8.30.1` | Binary      | `.gitleaks.toml`, `.github/gitleaks.toml`                    |
| [labelmaker](https://github.com/jwodder/labelmaker)                   | `0.6.4`  | Binary      | CLI flags only                                               |
| [Lychee](https://github.com/lycheeverse/lychee)                       | `0.24.2` | Binary      | `lychee.toml`, `[tool.lychee]` in `pyproject.toml`           |
| [mdformat](https://github.com/hukkin/mdformat)                        | `1.0.0`  | PyPI        | `.mdformat.toml`, `[tool.mdformat]` in `pyproject.toml`      |
| [mypy](https://github.com/python/mypy)                                | `1.19.1` | PyPI (venv) | `[tool.mypy]` in `pyproject.toml`                            |
| [Nuitka](https://github.com/Nuitka/Nuitka)                            | `4.1`    | PyPI (venv) | `[tool.nuitka]` in `pyproject.toml`                          |
| [pyproject-fmt](https://github.com/tox-dev/pyproject-fmt)             | `2.16.2` | PyPI        | `[tool.pyproject-fmt]` in `pyproject.toml`                   |
| [Ruff](https://github.com/astral-sh/ruff)                             | `0.15.5` | PyPI        | `ruff.toml`, `.ruff.toml`, `[tool.ruff]` in `pyproject.toml` |
| [shfmt](https://github.com/mvdan/sh)                                  | `3.13.1` | Binary      | `.editorconfig`                                              |
| [typos](https://github.com/crate-ci/typos)                            | `1.46.1` | Binary      | `[tool.typos]` in `pyproject.toml`                           |
| [yamllint](https://github.com/adrienverge/yamllint)                   | `1.38.0` | PyPI        | `.yamllint.yaml`, `.yamllint.yml`, `.yamllint`               |
| [zizmor](https://github.com/zizmorcore/zizmor)                        | `1.23.0` | PyPI        | `zizmor.yaml`                                                |

<!-- tool-summary-end -->

- **Binary**: downloaded as platform-specific executables from GitHub Releases.
- **PyPI**: installed via `uvx`.
- **PyPI (venv)**: run inside the project virtualenv via `uv run` because they need to import project code.

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
| [biome](https://biomejs.dev)                                                        | JSON/JS formatting and linting      | [`[tool.biome]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)                 | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → JSON                         |
| [bump-my-version](https://callowayproject.github.io/bump-my-version/)               | Version bump patterns and files     | [`[tool.bumpversion]`](https://callowayproject.github.io/bump-my-version/reference/configuration/)   | Native                                                                                                                |
| [coverage.py](https://coverage.readthedocs.io/en/latest/config.html)                | Code coverage reporting             | [`[tool.coverage.*]`](https://coverage.readthedocs.io/en/latest/config.html#configuration-reference) | Native                                                                                                                |
| [gitleaks](https://github.com/gitleaks/gitleaks)                                    | Secret detection rules              | [`[tool.gitleaks]`](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml)              | [repomatic bridge](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) → TOML                         |
| [lychee](https://lychee.cli.rs)                                                     | Link checking rules                 | [`[tool.lychee]`](https://lychee.cli.rs/guides/config/)                                              | Native                                                                                                                |
| [mdformat](https://mdformat.readthedocs.io/en/stable/users/configuration_file.html) | Markdown formatting options         | [`[tool.mdformat]`](https://mdformat.readthedocs.io/en/stable/users/configuration_file.html)         | Native (via [`mdformat-pyproject`](https://github.com/csala/mdformat-pyproject))                                      |
| [mypy](https://mypy.readthedocs.io/en/stable/config_file.html)                      | Static type checking                | [`[tool.mypy]`](https://mypy.readthedocs.io/en/stable/config_file.html#using-a-pyproject-toml-file)  | Native                                                                                                                |
| [nuitka](https://github.com/Nuitka/Nuitka)                                          | Standalone binary compilation       | [`[tool.nuitka]`](https://nuitka.net/doc/user-manual.html)                                           | [repomatic bridge](#nuitka) → CLI flags ([native support: Nuitka#2136](https://github.com/Nuitka/Nuitka/issues/2136)) |
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

Tools with bundled defaults: mdformat, ruff, yamllint, zizmor.

### Level 4: bare invocation

If none of the above applies (no config file, no `[tool.X]`, no bundled default), the tool runs with its own built-in defaults. Tools like autopep8 and pyproject-fmt work this way: all behavior is controlled through CLI flags.

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

### Cleaning up redundant configs

If you previously ran `repomatic init` and have a native config file that is identical to the bundled default, `repomatic init --clean-redundant-configs` removes it:

```shell-session
$ repomatic init --clean-redundant-configs
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

## Passing extra arguments

Everything after `--` is forwarded to the tool:

```shell-session
$ repomatic run ruff -- check --fix .
$ repomatic run zizmor -- --offline .github/workflows/
$ repomatic run biome -- format --write src/
```

For tools with subcommands (ruff, biome, gitleaks), the subcommand goes after `--` as the first argument.

## Tool details

<!-- tool-reference-start -->

### [actionlint](https://github.com/rhysd/actionlint)

**Installed version:** `1.7.12`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config files:** `.github/actionlint.yaml`

**Default flags:** `-color`

[Source](https://github.com/rhysd/actionlint) | [Config reference](https://github.com/rhysd/actionlint/blob/main/docs/config.md) | [CLI usage](https://github.com/rhysd/actionlint/blob/main/docs/usage.md)

<!-- actionlint-manual-start -->

<!-- actionlint-manual-end -->

### [autopep8](https://github.com/hhatto/autopep8)

**Installed version:** `2.3.2`

**Installation method:** PyPI, installed via `uvx`

**Config:** CLI flags only

**Default flags:** `--recursive` `--in-place` `--max-line-length` `88` `--select` `E501`

[Source](https://github.com/hhatto/autopep8) | [CLI usage](https://pypi.org/project/autopep8/)

<!-- autopep8-manual-start -->

<!-- autopep8-manual-end -->

### [Biome](https://github.com/biomejs/biome)

**Installed version:** `2.4.15`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config files:** `biome.json`, `biome.jsonc`

**`[tool.biome]` bridge:** repomatic translates to JSON and passes via `--config-path`.

[Source](https://github.com/biomejs/biome) | [Config reference](https://biomejs.dev/reference/configuration/) | [CLI usage](https://biomejs.dev/reference/cli/)

<!-- biome-manual-start -->

<!-- biome-manual-end -->

### [bump-my-version](https://github.com/callowayproject/bump-my-version)

**Installed version:** `1.2.7`

**Installation method:** PyPI, installed via `uvx`

**Config:** `[tool.bump-my-version]` in `pyproject.toml` (native)

[Source](https://github.com/callowayproject/bump-my-version) | [Config reference](https://callowayproject.github.io/bump-my-version/reference/configuration/) | [CLI usage](https://callowayproject.github.io/bump-my-version/reference/cli/)

<!-- bump-my-version-manual-start -->

<!-- bump-my-version-manual-end -->

### [Gitleaks](https://github.com/gitleaks/gitleaks)

**Installed version:** `8.30.1`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config files:** `.gitleaks.toml`, `.github/gitleaks.toml`

**`[tool.gitleaks]` bridge:** repomatic translates to TOML and passes via `--config`.

[Source](https://github.com/gitleaks/gitleaks) | [Config reference](https://github.com/gitleaks/gitleaks#configuration) | [CLI usage](https://github.com/gitleaks/gitleaks#usage)

<!-- gitleaks-manual-start -->

<!-- gitleaks-manual-end -->

### [labelmaker](https://github.com/jwodder/labelmaker)

**Installed version:** `0.6.4`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config:** CLI flags only

[Source](https://github.com/jwodder/labelmaker) | [CLI usage](https://github.com/jwodder/labelmaker)

<!-- labelmaker-manual-start -->

<!-- labelmaker-manual-end -->

### [Lychee](https://github.com/lycheeverse/lychee)

**Installed version:** `0.24.2`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config files:** `lychee.toml` and `[tool.lychee]` in `pyproject.toml` (native)

[Source](https://github.com/lycheeverse/lychee) | [Config reference](https://lychee.cli.rs/guides/config/) | [CLI usage](https://lychee.cli.rs/guides/cli/)

<!-- lychee-manual-start -->

<!-- lychee-manual-end -->

### [mdformat](https://github.com/hukkin/mdformat)

**Installed version:** `1.0.0`

**Installation method:** PyPI, installed via `uvx`

**Config files:** `.mdformat.toml` and `[tool.mdformat]` in `pyproject.toml` (native)

**Default flags:** `--strict-front-matter`

**Bundled default:** [`mdformat.toml`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/mdformat.toml)

**Plugins:**

- `mdformat_admon`
- `mdformat-config`
- `mdformat_deflist`
- `mdformat_footnote`
- `mdformat-front-matters`
- `mdformat-gfm`
- `mdformat_gfm_alerts`
- `mdformat_myst`
- `mdformat-pelican`
- `mdformat_pyproject`
- `mdformat-recover-urls`
- `mdformat-ruff`
- `mdformat-shfmt`
- `mdformat_simple_breaks`
- `mdformat-toc`
- `mdformat-web`
- `ruff`

[Source](https://github.com/hukkin/mdformat) | [Config reference](https://mdformat.readthedocs.io/en/stable/users/configuration_file.html) | [CLI usage](https://mdformat.readthedocs.io/en/stable/users/installation_and_usage.html)

<!-- mdformat-manual-start -->

<!-- mdformat-manual-end -->

### [mypy](https://github.com/python/mypy)

**Installed version:** `1.19.1`

**Installation method:** PyPI, runs in project virtualenv via `uv run`

**Config:** `[tool.mypy]` in `pyproject.toml` (native)

**Default flags:** `--color-output`

[Source](https://github.com/python/mypy) | [Config reference](https://mypy.readthedocs.io/en/stable/config_file.html) | [CLI usage](https://mypy.readthedocs.io/en/stable/command_line.html)

<!-- mypy-manual-start -->

<!-- mypy-manual-end -->

### [Nuitka](https://github.com/Nuitka/Nuitka)

**Installed version:** `4.1`

**Installation method:** PyPI, runs in project virtualenv via `uv run`

**Config:** `[tool.nuitka]` in `pyproject.toml` (translated to CLI flags)

**Default flags:** `--mode=onefile` `--assume-yes-for-downloads`

[Source](https://github.com/Nuitka/Nuitka) | [Config reference](https://nuitka.net/doc/user-manual.html) | [CLI usage](https://nuitka.net/doc/user-manual.html)

<!-- nuitka-manual-start -->

**Try it:**

```shell-session
$ repomatic run nuitka -- my_app/__main__.py
```

**Minimal `[tool.nuitka]`:**

```toml
[tool.nuitka]
onefile = true
output-dir = "build"
```

repomatic reads every key from `[tool.nuitka]` and forwards it as a CLI flag: `true` becomes a bare `--flag`, a string or number becomes `--key=value`, and a list repeats the flag once per item. Nuitka does not read `[tool.nuitka]` natively yet ([Nuitka#2136](https://github.com/Nuitka/Nuitka/issues/2136)); repomatic's bridge fills the gap until it does.

<!-- nuitka-manual-end -->

### [pyproject-fmt](https://github.com/tox-dev/pyproject-fmt)

**Installed version:** `2.16.2`

**Installation method:** PyPI, installed via `uvx`

**Config:** `[tool.pyproject-fmt]` in `pyproject.toml` (native)

**Default flags:** `--expand-tables` `project.entry-points,project.optional-dependencies,project.urls,project.scripts`

[Source](https://github.com/tox-dev/pyproject-fmt) | [Config reference](https://pyproject-fmt.readthedocs.io/en/latest/) | [CLI usage](https://pyproject-fmt.readthedocs.io/en/latest/)

<!-- pyproject-fmt-manual-start -->

<!-- pyproject-fmt-manual-end -->

### [Ruff](https://github.com/astral-sh/ruff)

**Installed version:** `0.15.5`

**Installation method:** PyPI, installed via `uvx`

**Config files:** `ruff.toml`, `.ruff.toml` and `[tool.ruff]` in `pyproject.toml` (native)

**Bundled default:** [`ruff.toml`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/ruff.toml)

[Source](https://github.com/astral-sh/ruff) | [Config reference](https://docs.astral.sh/ruff/configuration/) | [CLI usage](https://docs.astral.sh/ruff/configuration/#command-line-interface)

<!-- ruff-manual-start -->

<!-- ruff-manual-end -->

### [shfmt](https://github.com/mvdan/sh)

**Installed version:** `3.13.1`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config files:** `.editorconfig`

**Default flags:** `--write`

[Source](https://github.com/mvdan/sh) | [Config reference](https://github.com/mvdan/sh/blob/master/cmd/shfmt/shfmt.1.scd) | [CLI usage](https://github.com/mvdan/sh#shfmt)

<!-- shfmt-manual-start -->

<!-- shfmt-manual-end -->

### [typos](https://github.com/crate-ci/typos)

**Installed version:** `1.46.1`

**Installation method:** Binary (downloaded from GitHub Releases)

**Config:** `[tool.typos]` in `pyproject.toml` (native)

**Default flags:** `--write-changes`

[Source](https://github.com/crate-ci/typos) | [Config reference](https://github.com/crate-ci/typos/blob/master/docs/reference.md) | [CLI usage](https://github.com/crate-ci/typos/blob/master/docs/reference.md)

<!-- typos-manual-start -->

<!-- typos-manual-end -->

### [yamllint](https://github.com/adrienverge/yamllint)

**Installed version:** `1.38.0`

**Installation method:** PyPI, installed via `uvx`

**Config files:** `.yamllint.yaml`, `.yamllint.yml`, `.yamllint`

**`[tool.yamllint]` bridge:** repomatic translates to YAML and passes via `--config-file`.

**Default flags:** `--strict`

**CI flags:** `--format` `github`

**Bundled default:** [`yamllint.yaml`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/yamllint.yaml)

[Source](https://github.com/adrienverge/yamllint) | [Config reference](https://yamllint.readthedocs.io/en/stable/configuration.html) | [CLI usage](https://yamllint.readthedocs.io/en/stable/quickstart.html)

<!-- yamllint-manual-start -->

<!-- yamllint-manual-end -->

### [zizmor](https://github.com/zizmorcore/zizmor)

**Installed version:** `1.23.0`

**Installation method:** PyPI, installed via `uvx`

**Config files:** `zizmor.yaml`

**`[tool.zizmor]` bridge:** repomatic translates to YAML and passes via `--config`.

**Default flags:** `--offline`

**CI flags:** `--format` `github`

**Bundled default:** [`zizmor.yaml`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/zizmor.yaml)

[Source](https://github.com/zizmorcore/zizmor) | [Config reference](https://docs.zizmor.sh/configuration/) | [CLI usage](https://docs.zizmor.sh/usage/)

<!-- zizmor-manual-start -->

<!-- zizmor-manual-end -->

## Comparison

| Tool                                |                                                                                                     Stars |                                                      Last release                                                       |                                                      Last commit                                                      |                                                                                                                 Commits |                                                      Dependencies                                                      |                                                  Language                                                  |                                                    License                                                    |
| :---------------------------------- | --------------------------------------------------------------------------------------------------------: | :---------------------------------------------------------------------------------------------------------------------: | :-------------------------------------------------------------------------------------------------------------------: | ----------------------------------------------------------------------------------------------------------------------: | :--------------------------------------------------------------------------------------------------------------------: | :--------------------------------------------------------------------------------------------------------: | :-----------------------------------------------------------------------------------------------------------: |
| [actionlint](#actionlint)           |                ![Stars](https://img.shields.io/github/stars/rhysd/actionlint?label=%20&style=flat-square) |        ![Last release](https://img.shields.io/github/release-date/rhysd/actionlint?label=%20&style=flat-square)         |        ![Last commit](https://img.shields.io/github/last-commit/rhysd/actionlint?label=%20&style=flat-square)         |                ![Commits](https://img.shields.io/github/commit-activity/m/rhysd/actionlint?label=%20&style=flat-square) |        ![Dependencies](https://img.shields.io/librariesio/github/rhysd/actionlint?label=%20&style=flat-square)         |        ![Language](https://img.shields.io/github/languages/top/rhysd/actionlint?style=flat-square)         |        ![License](https://img.shields.io/github/license/rhysd/actionlint?label=%20&style=flat-square)         |
| [autopep8](#autopep8)               |                 ![Stars](https://img.shields.io/github/stars/hhatto/autopep8?label=%20&style=flat-square) |         ![Last release](https://img.shields.io/github/release-date/hhatto/autopep8?label=%20&style=flat-square)         |         ![Last commit](https://img.shields.io/github/last-commit/hhatto/autopep8?label=%20&style=flat-square)         |                 ![Commits](https://img.shields.io/github/commit-activity/m/hhatto/autopep8?label=%20&style=flat-square) |         ![Dependencies](https://img.shields.io/librariesio/github/hhatto/autopep8?label=%20&style=flat-square)         |         ![Language](https://img.shields.io/github/languages/top/hhatto/autopep8?style=flat-square)         |         ![License](https://img.shields.io/github/license/hhatto/autopep8?label=%20&style=flat-square)         |
| [Biome](#biome)                     |                   ![Stars](https://img.shields.io/github/stars/biomejs/biome?label=%20&style=flat-square) |          ![Last release](https://img.shields.io/github/release-date/biomejs/biome?label=%20&style=flat-square)          |          ![Last commit](https://img.shields.io/github/last-commit/biomejs/biome?label=%20&style=flat-square)          |                   ![Commits](https://img.shields.io/github/commit-activity/m/biomejs/biome?label=%20&style=flat-square) |          ![Dependencies](https://img.shields.io/librariesio/github/biomejs/biome?label=%20&style=flat-square)          |          ![Language](https://img.shields.io/github/languages/top/biomejs/biome?style=flat-square)          |          ![License](https://img.shields.io/github/license/biomejs/biome?label=%20&style=flat-square)          |
| [bump-my-version](#bump-my-version) | ![Stars](https://img.shields.io/github/stars/callowayproject/bump-my-version?label=%20&style=flat-square) | ![Last release](https://img.shields.io/github/release-date/callowayproject/bump-my-version?label=%20&style=flat-square) | ![Last commit](https://img.shields.io/github/last-commit/callowayproject/bump-my-version?label=%20&style=flat-square) | ![Commits](https://img.shields.io/github/commit-activity/m/callowayproject/bump-my-version?label=%20&style=flat-square) | ![Dependencies](https://img.shields.io/librariesio/github/callowayproject/bump-my-version?label=%20&style=flat-square) | ![Language](https://img.shields.io/github/languages/top/callowayproject/bump-my-version?style=flat-square) | ![License](https://img.shields.io/github/license/callowayproject/bump-my-version?label=%20&style=flat-square) |
| [Gitleaks](#gitleaks)               |               ![Stars](https://img.shields.io/github/stars/gitleaks/gitleaks?label=%20&style=flat-square) |        ![Last release](https://img.shields.io/github/release-date/gitleaks/gitleaks?label=%20&style=flat-square)        |        ![Last commit](https://img.shields.io/github/last-commit/gitleaks/gitleaks?label=%20&style=flat-square)        |               ![Commits](https://img.shields.io/github/commit-activity/m/gitleaks/gitleaks?label=%20&style=flat-square) |        ![Dependencies](https://img.shields.io/librariesio/github/gitleaks/gitleaks?label=%20&style=flat-square)        |        ![Language](https://img.shields.io/github/languages/top/gitleaks/gitleaks?style=flat-square)        |        ![License](https://img.shields.io/github/license/gitleaks/gitleaks?label=%20&style=flat-square)        |
| [labelmaker](#labelmaker)           |              ![Stars](https://img.shields.io/github/stars/jwodder/labelmaker?label=%20&style=flat-square) |       ![Last release](https://img.shields.io/github/release-date/jwodder/labelmaker?label=%20&style=flat-square)        |       ![Last commit](https://img.shields.io/github/last-commit/jwodder/labelmaker?label=%20&style=flat-square)        |              ![Commits](https://img.shields.io/github/commit-activity/m/jwodder/labelmaker?label=%20&style=flat-square) |       ![Dependencies](https://img.shields.io/librariesio/github/jwodder/labelmaker?label=%20&style=flat-square)        |       ![Language](https://img.shields.io/github/languages/top/jwodder/labelmaker?style=flat-square)        |       ![License](https://img.shields.io/github/license/jwodder/labelmaker?label=%20&style=flat-square)        |
| [Lychee](#lychee)                   |              ![Stars](https://img.shields.io/github/stars/lycheeverse/lychee?label=%20&style=flat-square) |       ![Last release](https://img.shields.io/github/release-date/lycheeverse/lychee?label=%20&style=flat-square)        |       ![Last commit](https://img.shields.io/github/last-commit/lycheeverse/lychee?label=%20&style=flat-square)        |              ![Commits](https://img.shields.io/github/commit-activity/m/lycheeverse/lychee?label=%20&style=flat-square) |       ![Dependencies](https://img.shields.io/librariesio/github/lycheeverse/lychee?label=%20&style=flat-square)        |       ![Language](https://img.shields.io/github/languages/top/lycheeverse/lychee?style=flat-square)        |       ![License](https://img.shields.io/github/license/lycheeverse/lychee?label=%20&style=flat-square)        |
| [mdformat](#mdformat)               |                 ![Stars](https://img.shields.io/github/stars/hukkin/mdformat?label=%20&style=flat-square) |                   ![Last release](https://img.shields.io/pypi/v/mdformat?label=%20&style=flat-square)                   |         ![Last commit](https://img.shields.io/github/last-commit/hukkin/mdformat?label=%20&style=flat-square)         |                 ![Commits](https://img.shields.io/github/commit-activity/m/hukkin/mdformat?label=%20&style=flat-square) |         ![Dependencies](https://img.shields.io/librariesio/github/hukkin/mdformat?label=%20&style=flat-square)         |         ![Language](https://img.shields.io/github/languages/top/hukkin/mdformat?style=flat-square)         |         ![License](https://img.shields.io/github/license/hukkin/mdformat?label=%20&style=flat-square)         |
| [mypy](#mypy)                       |                     ![Stars](https://img.shields.io/github/stars/python/mypy?label=%20&style=flat-square) |                     ![Last release](https://img.shields.io/pypi/v/mypy?label=%20&style=flat-square)                     |           ![Last commit](https://img.shields.io/github/last-commit/python/mypy?label=%20&style=flat-square)           |                     ![Commits](https://img.shields.io/github/commit-activity/m/python/mypy?label=%20&style=flat-square) |           ![Dependencies](https://img.shields.io/librariesio/github/python/mypy?label=%20&style=flat-square)           |           ![Language](https://img.shields.io/github/languages/top/python/mypy?style=flat-square)           |           ![License](https://img.shields.io/github/license/python/mypy?label=%20&style=flat-square)           |
| [Nuitka](#nuitka)                   |                   ![Stars](https://img.shields.io/github/stars/Nuitka/Nuitka?label=%20&style=flat-square) |          ![Last release](https://img.shields.io/github/release-date/Nuitka/Nuitka?label=%20&style=flat-square)          |          ![Last commit](https://img.shields.io/github/last-commit/Nuitka/Nuitka?label=%20&style=flat-square)          |                   ![Commits](https://img.shields.io/github/commit-activity/m/Nuitka/Nuitka?label=%20&style=flat-square) |          ![Dependencies](https://img.shields.io/librariesio/github/Nuitka/Nuitka?label=%20&style=flat-square)          |          ![Language](https://img.shields.io/github/languages/top/Nuitka/Nuitka?style=flat-square)          |          ![License](https://img.shields.io/github/license/Nuitka/Nuitka?label=%20&style=flat-square)          |
| [pyproject-fmt](#pyproject-fmt)     |           ![Stars](https://img.shields.io/github/stars/tox-dev/pyproject-fmt?label=%20&style=flat-square) |      ![Last release](https://img.shields.io/github/release-date/tox-dev/pyproject-fmt?label=%20&style=flat-square)      |      ![Last commit](https://img.shields.io/github/last-commit/tox-dev/pyproject-fmt?label=%20&style=flat-square)      |           ![Commits](https://img.shields.io/github/commit-activity/m/tox-dev/pyproject-fmt?label=%20&style=flat-square) |      ![Dependencies](https://img.shields.io/librariesio/github/tox-dev/pyproject-fmt?label=%20&style=flat-square)      |      ![Language](https://img.shields.io/github/languages/top/tox-dev/pyproject-fmt?style=flat-square)      |      ![License](https://img.shields.io/github/license/tox-dev/pyproject-fmt?label=%20&style=flat-square)      |
| [Ruff](#ruff)                       |                  ![Stars](https://img.shields.io/github/stars/astral-sh/ruff?label=%20&style=flat-square) |         ![Last release](https://img.shields.io/github/release-date/astral-sh/ruff?label=%20&style=flat-square)          |         ![Last commit](https://img.shields.io/github/last-commit/astral-sh/ruff?label=%20&style=flat-square)          |                  ![Commits](https://img.shields.io/github/commit-activity/m/astral-sh/ruff?label=%20&style=flat-square) |         ![Dependencies](https://img.shields.io/librariesio/github/astral-sh/ruff?label=%20&style=flat-square)          |         ![Language](https://img.shields.io/github/languages/top/astral-sh/ruff?style=flat-square)          |         ![License](https://img.shields.io/github/license/astral-sh/ruff?label=%20&style=flat-square)          |
| [shfmt](#shfmt)                     |                        ![Stars](https://img.shields.io/github/stars/mvdan/sh?label=%20&style=flat-square) |            ![Last release](https://img.shields.io/github/release-date/mvdan/sh?label=%20&style=flat-square)             |            ![Last commit](https://img.shields.io/github/last-commit/mvdan/sh?label=%20&style=flat-square)             |                        ![Commits](https://img.shields.io/github/commit-activity/m/mvdan/sh?label=%20&style=flat-square) |            ![Dependencies](https://img.shields.io/librariesio/github/mvdan/sh?label=%20&style=flat-square)             |            ![Language](https://img.shields.io/github/languages/top/mvdan/sh?style=flat-square)             |            ![License](https://img.shields.io/github/license/mvdan/sh?label=%20&style=flat-square)             |
| [typos](#typos)                     |                  ![Stars](https://img.shields.io/github/stars/crate-ci/typos?label=%20&style=flat-square) |         ![Last release](https://img.shields.io/github/release-date/crate-ci/typos?label=%20&style=flat-square)          |         ![Last commit](https://img.shields.io/github/last-commit/crate-ci/typos?label=%20&style=flat-square)          |                  ![Commits](https://img.shields.io/github/commit-activity/m/crate-ci/typos?label=%20&style=flat-square) |         ![Dependencies](https://img.shields.io/librariesio/github/crate-ci/typos?label=%20&style=flat-square)          |         ![Language](https://img.shields.io/github/languages/top/crate-ci/typos?style=flat-square)          |         ![License](https://img.shields.io/github/license/crate-ci/typos?label=%20&style=flat-square)          |
| [yamllint](#yamllint)               |            ![Stars](https://img.shields.io/github/stars/adrienverge/yamllint?label=%20&style=flat-square) |      ![Last release](https://img.shields.io/github/release-date/adrienverge/yamllint?label=%20&style=flat-square)       |      ![Last commit](https://img.shields.io/github/last-commit/adrienverge/yamllint?label=%20&style=flat-square)       |            ![Commits](https://img.shields.io/github/commit-activity/m/adrienverge/yamllint?label=%20&style=flat-square) |      ![Dependencies](https://img.shields.io/librariesio/github/adrienverge/yamllint?label=%20&style=flat-square)       |      ![Language](https://img.shields.io/github/languages/top/adrienverge/yamllint?style=flat-square)       |      ![License](https://img.shields.io/github/license/adrienverge/yamllint?label=%20&style=flat-square)       |
| [zizmor](#zizmor)                   |               ![Stars](https://img.shields.io/github/stars/zizmorcore/zizmor?label=%20&style=flat-square) |        ![Last release](https://img.shields.io/github/release-date/zizmorcore/zizmor?label=%20&style=flat-square)        |        ![Last commit](https://img.shields.io/github/last-commit/zizmorcore/zizmor?label=%20&style=flat-square)        |               ![Commits](https://img.shields.io/github/commit-activity/m/zizmorcore/zizmor?label=%20&style=flat-square) |        ![Dependencies](https://img.shields.io/librariesio/github/zizmorcore/zizmor?label=%20&style=flat-square)        |        ![Language](https://img.shields.io/github/languages/top/zizmorcore/zizmor?style=flat-square)        |        ![License](https://img.shields.io/github/license/zizmorcore/zizmor?label=%20&style=flat-square)        |

<!-- tool-reference-end -->
