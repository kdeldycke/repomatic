# `repomatic`

[![Last release](https://img.shields.io/pypi/v/repomatic.svg)](https://pypi.org/project/repomatic/)
[![Python versions](https://img.shields.io/pypi/pyversions/repomatic.svg)](https://pypi.org/project/repomatic/)
[![Downloads](https://static.pepy.tech/badge/repomatic/month)](https://pepy.tech/projects/repomatic)
[![Unittests status](https://github.com/kdeldycke/repomatic/actions/workflows/tests.yaml/badge.svg?branch=main)](https://github.com/kdeldycke/repomatic/actions/workflows/tests.yaml?query=branch%3Amain)
[![Coverage status](https://codecov.io/gh/kdeldycke/repomatic/branch/main/graph/badge.svg)](https://app.codecov.io/gh/kdeldycke/repomatic)

A Python CLI and [`pyproject.toml` configuration](#toolrepomatic-configuration) that let you **release Python packages multiple times a day with only 2-clicks**. Designed for `uv`-based Python projects, but usable for other projects too. The CLI operates through [reusable GitHub Actions workflows](#reusable-workflows) as its CI delivery mechanism.

[**Maintainer-in-the-loop**](#maintainer-in-the-loop): nothing is done behind your back. A PR or issue is created every time a change is proposed or action is needed.

Automates:

- Version bumping
- Changelog management
- Formatting autofix for: Python, Markdown, JSON, typos
- Linting: Python types with `mypy`, YAML, `zsh`, GitHub Actions, workflow security, URLS & redirects, Awesome lists, secrets
- Compiling of Python binaries for Linux / macOS / Windows on `x86_64` & `arm64`
- Building of Python packages and upload to PyPI
- Produce attestations
- Git version tagging and GitHub release creation
- Synchronization of: `uv.lock`, `.gitignore`, `.mailmap` and Mermaid dependency graph
- Auto-locking of inactive closed issues
- Static image optimization
- Sphinx documentation building & deployment, and `autodoc` updates
- Label management, with file-based and content-based rules
- Awesome list template synchronization
- Address [GitHub Actions limitations](#github-actions-limitations)

## Quick start

```shell-session
$ cd my-project
$ uvx -- repomatic init
$ git add . && git commit -m "Bootstrap reusable workflows" && git push
```

That's it. The workflows will start running and guide you through any remaining setup (like [creating a `WORKFLOW_UPDATE_GITHUB_PAT` secret](#permissions-and-token)) via issues and PRs in your repository.

Run `repomatic init --help` to see available components and options.

## `repomatic` CLI

### Try it

Thanks to `uv`, you can run it in one command, without installation or venv:

```shell-session
$ uvx -- repomatic
Usage: repomatic [OPTIONS] COMMAND [ARGS]...

Options:
  --time / --no-time    Measure and print elapsed execution time.  [default:
                        no-time]
  --color, --ansi / --no-color, --no-ansi
                        Strip out all colors and all ANSI codes from output.
                        [default: color]
  --config CONFIG_PATH  Location of the configuration file. Supports local path
                        with glob patterns or remote URL.  [default:
                        ~/Library/Application
                        Support/repomatic/*.toml|*.yaml|*.yml|*.json|*.ini]
  --no-config           Ignore all configuration files and only use command
                        line parameters and environment variables.
  --show-params         Show all CLI parameters, their provenance, defaults and
                        value, then exit.
  --table-format [aligned|asciidoc|csv|csv-excel|csv-excel-tab|csv-unix|double-grid|double-outline|fancy-grid|fancy-outline|github|grid|heavy-grid|heavy-outline|html|jira|latex|latex-booktabs|latex-longtable|latex-raw|mediawiki|mixed-grid|mixed-outline|moinmoin|orgtbl|outline|pipe|plain|presto|pretty|psql|rounded-grid|rounded-outline|rst|simple|simple-grid|simple-outline|textile|tsv|unsafehtml|vertical|youtrack]
                        Rendering style of tables.  [default: rounded-outline]
  --verbosity LEVEL     Either CRITICAL, ERROR, WARNING, INFO, DEBUG.
                        [default: WARNING]
  -v, --verbose         Increase the default WARNING verbosity by one level for
                        each additional repetition of the option.  [default: 0]
  --version             Show the version and exit.
  -h, --help            Show this message and exit.

Commands:
  broken-links       Manage broken links issue lifecycle
  changelog          Maintain a Markdown-formatted changelog
  check-renovate     Check Renovate migration prerequisites
  git-tag              Create and push a Git tag
  init                 Bootstrap a repository to use reusable workflows
  lint-changelog       Check changelog dates against release dates
  lint-repo            Run repository consistency checks
  metadata             Output project metadata
  pr-body              Generate PR body with workflow metadata
  release-prep         Prepare files for a release
  setup-guide          Manage setup guide issue lifecycle
  sponsor-label        Label issues/PRs from GitHub sponsors
  sync-bumpversion     Sync bumpversion config from bundled template
  sync-gitignore       Sync .gitignore from gitignore.io templates
  sync-dev-release     Sync rolling dev pre-release on GitHub
  sync-github-releases Sync GitHub release notes from changelog
  sync-linter-configs  Sync linter config files from bundled definitions
  sync-mailmap         Sync Git's .mailmap file with missing contributors
  sync-renovate        Sync Renovate config from canonical reference
  sync-skills          Sync Claude Code skills from bundled definitions
  sync-uv-lock         Re-lock and revert if only timestamp noise changed
  test-plan            Run a test plan from a file against a binary
  update-checksums     Update SHA-256 checksums for binary downloads
  update-deps-graph    Generate dependency graph from uv lockfile
  verify-binary        Verify binary architecture using exiftool
  version-check      Check if a version bump is allowed
  workflow           Manage downstream workflow caller files
```

```shell-session
$ uvx -- repomatic --version
repomatic, version 5.9.1
```

That's the best way to get started with `repomatic` and experiment with it.

### Executables

To ease deployment, standalone executables of `repomatic`'s latest version are available as direct downloads for several platforms and architectures:

| Platform    | `arm64`                                                                                                                               | `x86_64`                                                                                                                          |
| :---------- | ------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| **Linux**   | [Download `repomatic-linux-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-linux-arm64.bin)     | [Download `repomatic-linux-x64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-linux-x64.bin)     |
| **macOS**   | [Download `repomatic-macos-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-macos-arm64.bin)     | [Download `repomatic-macos-x64.bin`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-macos-x64.bin)     |
| **Windows** | [Download `repomatic-windows-arm64.exe`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-windows-arm64.exe) | [Download `repomatic-windows-x64.exe`](https://github.com/kdeldycke/repomatic/releases/latest/download/repomatic-windows-x64.exe) |

That way you have a chance to try it out without installing Python or `uv`. Or embed it in your CI/CD pipelines running on minimal images. Or run it on old platforms without worrying about dependency hell.

## `[tool.repomatic]` configuration

Downstream projects can customize workflow behavior by adding a `[tool.repomatic]` section in their `pyproject.toml`:

```toml
[tool.repomatic]
nuitka = false
nuitka-extra-args = [
  "--include-data-files=my_pkg/data/*.json=my_pkg/data/",
]
unstable-targets = ["linux-arm64", "windows-arm64"]
test-plan-file = "./tests/cli-test-plan.yaml"
timeout = 120
test-plan = "- args: --version"
gitignore-location = "./.gitignore"
gitignore-extra-categories = ["terraform", "go"]
gitignore-extra-content = '''
junit.xml

# Claude Code
.claude/
'''
dependency-graph-output = "./docs/assets/dependencies.mmd"
dependency-graph-all-groups = true
dependency-graph-all-extras = true
dependency-graph-no-groups = []
dependency-graph-no-extras = []
dependency-graph-level = 0
extra-label-files = ["https://example.com/my-labels.toml"]
extra-file-rules = "docs:\n  - docs/**"
extra-content-rules = "security:\n  - '(CVE|vulnerability)'"
pypi-package-history = ["old-name", "older-name"]
renovate-sync = false
workflow-sync = false
workflow-sync-exclude = ["debug.yaml", "autolock.yaml"]
init-exclude = ["linters", "skills"]
```

| Option                        | Type      | Default                                           | Description                                                                                                                                                                                                  |
| :---------------------------- | :-------- | :------------------------------------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `nuitka`                      | bool      | `true`                                            | Enable [Nuitka binary compilation](#githubworkflowsreleaseyaml-jobs). Set to `false` for projects with `[project.scripts]` that don't need binaries.                                                         |
| `nuitka-extra-args`           | list[str] | `[]`                                              | Extra Nuitka CLI arguments for binary compilation (e.g., `--include-data-files`, `--include-package-data`). Passed via the build matrix.                                                                     |
| `unstable-targets`            | list[str] | `[]`                                              | Nuitka build targets allowed to fail without blocking the release (e.g., `["linux-arm64"]`).                                                                                                                 |
| `test-plan-file`              | str       | `"./tests/cli-test-plan.yaml"`                    | Path to the YAML test plan file for binary testing. Read directly by `test-plan` subcommand; CLI args override.                                                                                              |
| `timeout`                     | int       | *(none)*                                          | Timeout in seconds for each binary test. Read directly by `test-plan` subcommand; CLI `--timeout` overrides.                                                                                                 |
| `test-plan`                   | str       | *(none)*                                          | Inline YAML test plan for binary testing. Read directly by `test-plan` subcommand; CLI `--plan-file`/`--plan-envvar` override.                                                                               |
| `gitignore-location`          | str       | `"./.gitignore"`                                  | File path of the `.gitignore` to update.                                                                                                                                                                     |
| `gitignore-extra-categories`  | list[str] | `[]`                                              | Additional categories to add to the `.gitignore` file (e.g., `["terraform", "go"]`).                                                                                                                         |
| `gitignore-extra-content`     | str       | See [example above](#toolrepomatic-configuration) | Additional content to append to the generated `.gitignore`. Supports TOML multi-line literal strings (`'''...'''`).                                                                                          |
| `dependency-graph-output`     | str       | `"./docs/assets/dependencies.mmd"`                | Location of the generated dependency graph file. Read directly by `update-deps-graph` subcommand; CLI `--output` overrides.                                                                                  |
| `dependency-graph-all-groups` | bool      | `true`                                            | Include all dependency groups in the graph. Set to `false` to exclude development groups (docs, test, typing). CLI `--all-groups` overrides.                                                                 |
| `dependency-graph-all-extras` | bool      | `true`                                            | Include all optional extras in the graph. CLI `--all-extras` overrides.                                                                                                                                      |
| `dependency-graph-no-groups`  | list[str] | `[]`                                              | Dependency groups to exclude from the graph. Equivalent to `--no-group` for each entry. Takes precedence over `dependency-graph-all-groups`.                                                                 |
| `dependency-graph-no-extras`  | list[str] | `[]`                                              | Optional extras to exclude from the graph. Equivalent to `--no-extra` for each entry. Takes precedence over `dependency-graph-all-extras`.                                                                   |
| `dependency-graph-level`      | int       | *(none)*                                          | Maximum depth of the dependency graph. `1` = primary deps only, `2` = primary + their deps, etc. CLI `--level` overrides.                                                                                    |
| `extra-label-files`           | list[str] | `[]`                                              | URLs of additional label definition files (JSON, JSON5, TOML, or YAML) downloaded and applied by `labelmaker`.                                                                                               |
| `extra-file-rules`            | str       | `""`                                              | Additional YAML rules appended to the bundled file-based labeller configuration.                                                                                                                             |
| `extra-content-rules`         | str       | `""`                                              | Additional YAML rules appended to the bundled content-based labeller configuration.                                                                                                                          |
| `pypi-package-history`        | list[str] | `[]`                                              | Former PyPI package names for renamed projects. `lint-changelog` fetches releases from each name and generates correct PyPI URLs per version.                                                                |
| `renovate-sync`               | bool      | `true`                                            | Enable Renovate config sync. Set to `false` to skip `sync-renovate` in the autofix workflow.                                                                                                                 |
| `workflow-sync`               | bool      | `true`                                            | Enable workflow sync. Set to `false` to skip `workflow create` and `workflow sync` when no explicit filenames are given.                                                                                     |
| `init-exclude`                | list[str] | `["labels", "linters", "skills"]`                 | Component names to exclude from `repomatic init` default selection. Defaults to components whose files are regenerated by workflows at execution time. Explicit CLI positional arguments override this list. |
| `workflow-sync-exclude`       | list[str] | `[]`                                              | Workflow filenames to exclude from init/sync/create (e.g., `["debug.yaml"]`). Explicit CLI positional arguments override this list.                                                                          |

> [!TIP]
> The workflows also invoke tools that read their own `[tool.*]` sections from your `pyproject.toml`. You can customize their behavior in your project without forking or patching the workflows:
>
> | Tool                                                                  | Section                     | Customizes                          |
> | :-------------------------------------------------------------------- | :-------------------------- | :---------------------------------- |
> | [bump-my-version](https://callowayproject.github.io/bump-my-version/) | `[tool.bumpversion]`        | Version bump patterns and files     |
> | [coverage.py](https://coverage.readthedocs.io/en/latest/config.html)  | `[tool.coverage.*]`         | Code coverage reporting             |
> | [mypy](https://mypy.readthedocs.io/en/stable/config_file.html)        | `[tool.mypy]`               | Static type checking                |
> | [pytest](https://docs.pytest.org/en/stable/reference/customize.html)  | `[tool.pytest.ini_options]` | Test runner options                 |
> | [ruff](https://docs.astral.sh/ruff/configuration/)                    | `[tool.ruff]`               | Linting and formatting rules        |
> | [typos](https://github.com/crate-ci/typos)                            | `[tool.typos]`              | Spell-checking exceptions           |
> | [uv](https://docs.astral.sh/uv/reference/settings/)                   | `[tool.uv]`                 | Package resolution and build config |
>
> See [click-extra's inventory of `pyproject.toml`-aware tools](https://kdeldycke.github.io/click-extra/config.html#pyproject-toml) for a broader list.

## Reusable workflows

The `repomatic` CLI operates in CI through [reusable GitHub Actions workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows). You configure behavior via [`[tool.repomatic]`](#toolrepomatic-configuration) in `pyproject.toml`; the workflows are the execution layer.

### Example usage

The fastest way to adopt these workflows is with `repomatic init` (see [Quick start](#quick-start)). It generates all the thin-caller workflow files for you.

If you prefer to set up a single workflow manually, create a `.github/workflows/lint.yaml` file [using the `uses` syntax](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#calling-a-reusable-workflow):

```yaml
name: Lint
on:
  push:
  pull_request:

jobs:
  lint:
    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@v5.9.1
```

> [!IMPORTANT]
> [Concurrency is already configured](#concurrency-and-cancellation) in the reusable workflows—you don't need to re-specify it in your calling workflow.

### GitHub Actions limitations

GitHub Actions has several design limitations that the workflows work around:

| Limitation                                                  | Status             | Addressed by                                                                                                                                                      |
| :---------------------------------------------------------- | :----------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No conditional step groups                                  | ✅ Addressed       | [`project-metadata` job](#what-is-this-project-metadata-job) + [`repomatic metadata`](#repomatic-cli)                                                             |
| Workflow inputs only accept strings                         | ✅ Addressed       | String parsing in [`repomatic`](#repomatic-cli)                                                                                                                   |
| Matrix outputs not cumulative                               | ✅ Addressed       | [`project-metadata`](#what-is-this-project-metadata-job) pre-computes matrices                                                                                    |
| `cancel-in-progress` evaluated on new run, not old          | ✅ Addressed       | [SHA-based concurrency groups](#concurrency-and-cancellation) in [`release.yaml`](#githubworkflowsreleaseyaml-jobs)                                               |
| Cross-event concurrency cancellation                        | ✅ Addressed       | [`event_name` in `changelog.yaml` concurrency group](#concurrency-and-cancellation)                                                                               |
| PR close doesn't cancel runs                                | ✅ Addressed       | [`cancel-runs.yaml`](#githubworkflowscancel-runsyaml-jobs)                                                                                                        |
| `GITHUB_TOKEN` can't modify workflow files                  | ✅ Addressed       | [`WORKFLOW_UPDATE_GITHUB_PAT` fine-grained PAT](#permissions-and-token)                                                                                           |
| Tag pushes from Actions don't trigger workflows             | ✅ Addressed       | [Custom PAT](#permissions-and-token) for tag operations                                                                                                           |
| Default input values not propagated across events           | ✅ Addressed       | Manual defaults in `env:` section                                                                                                                                 |
| `head_commit` only has latest commit in multi-commit pushes | ✅ Addressed       | [`repomatic metadata`](#what-is-this-project-metadata-job) extracts full commit range                                                                             |
| `actions/checkout` uses merge commit for PRs                | ✅ Addressed       | Explicit `ref: github.event.pull_request.head.sha`                                                                                                                |
| Multiline output encoding fragile                           | ✅ Addressed       | Random delimiters in `repomatic/github.py`                                                                                                                        |
| Branch deletion doesn't cancel runs                         | ❌ Not addressed   | Same root cause as PR close; partially mitigated by [`cancel-runs.yaml`](#githubworkflowscancel-runsyaml-jobs) since branch deletion typically follows PR closure |
| No native way to depend on all matrix jobs completing       | ❌ Not addressed   | GitHub limitation; use `needs:` with a summary job as workaround                                                                                                  |
| `actionlint` false positives for runtime env vars           | 🚫 Not addressable | Linter limitation, not GitHub's                                                                                                                                   |

### 🪄 [`.github/workflows/autofix.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/autofix.yaml)

*Setup* — guide new users through initial configuration:

- 📖 **Setup guide** (`setup-guide`)

  - Detects missing `WORKFLOW_UPDATE_GITHUB_PAT` secret and opens an issue with step-by-step setup instructions
  - Automatically closes the issue once the secret is configured
  - **Skip**: upstream `kdeldycke/repomatic` repo, `workflow_call` events

*Formatters* — rewrite files to enforce canonical style:

- 🐍 **Format Python** (`format-python`)

  - Auto-formats Python code using [`autopep8`](https://github.com/hhatto/autopep8) and [`ruff`](https://github.com/astral-sh/ruff)
  - **Requires**:
    - Python files (`**/*.{py,pyi,pyw,pyx,ipynb}`) in the repository, or
    - documentation files (`**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx,rst,tex}`)

- 📐 **Format `pyproject.toml`** (`format-pyproject`)

  - Auto-formats `pyproject.toml` using [`pyproject-fmt`](https://github.com/tox-dev/pyproject-fmt)
  - **Requires**:
    - Python package with a `pyproject.toml` file

- ✍️ **Format Markdown** (`format-markdown`)

  - Auto-formats Markdown files using [`mdformat`](https://github.com/hukkin/mdformat)
  - **Requires**:
    - Markdown files (`**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx}`) in the repository

- 🔧 **Format JSON** (`format-json`)

  - Auto-formats JSON, JSONC, and JSON5 files using [Biome](https://github.com/biomejs/biome)
  - **Requires**:
    - JSON files (`**/*.{json,jsonc,json5}`, `**/.code-workspace`, `!**/package-lock.json`) in the repository

*Fixers* — correct or improve existing content in-place:

- ✏️ **Fix typos** (`fix-typos`)

  - Automatically fixes typos in the codebase using [`typos`](https://github.com/crate-ci/typos)

- 📋 **Fix changelog** (`fix-changelog`)

  - Checks and fixes changelog dates, availability admonitions, and orphaned versions using [`repomatic lint-changelog --fix`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/changelog.py)

- 🖼️ **Optimize images** (`optimize-images`)

  - Losslessly compresses PNG and JPEG images using [`repomatic optimize-images`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/images.py) with `oxipng` and `jpegoptim`
  - **Requires**:
    - Image files (`**/*.{jpeg,jpg,png,webp,avif}`) in the repository

*Syncers* — regenerate files from external sources or project state:

- 🙈 **Sync `.gitignore`** (`sync-gitignore`)

  - Regenerates `.gitignore` from [gitignore.io](https://github.com/toptal/gitignore.io) templates using [`repomatic sync-gitignore`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/cli.py)
  - **Requires**:
    - A `.gitignore` file in the repository

- 🔄 **Sync bumpversion config** (`sync-bumpversion`)

  - Syncs the `[tool.bumpversion]` configuration in `pyproject.toml` using [`repomatic sync-bumpversion`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/cli.py)
  - **Skipped if**:
    - `[tool.bumpversion]` section already exists in `pyproject.toml`

- 🔄 **Sync linter configs** (`sync-linter-configs`)

  - Syncs linter configuration files (`zizmor.yaml`) with the canonical references from [`repomatic`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/zizmor.yaml)

- 🔄 **Sync `renovate.json5`** (`sync-renovate`)

  - Syncs the local `renovate.json5` with the canonical reference from [`repomatic`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/init_project.py), stripping repo-specific settings (`customManagers`, `assignees`)
  - **Skipped if**:
    - Repository is [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic) itself (the upstream source)
    - No `renovate.json5` file in the repository root
    - `renovate-sync = false` in `[tool.repomatic]`

- 🪢 **Sync workflows** (`sync-workflows`)

  - Syncs [workflows from the upstream `kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic/tree/main/.github/workflows) repository using [`repomatic workflow sync`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/github/workflow_sync.py)
  - **Skipped if**:
    - Repository is [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic) itself (the upstream source)

- 📬 **Sync `.mailmap`** (`sync-mailmap`)

  - Keeps `.mailmap` file up to date with contributors using [`repomatic sync-mailmap`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/mailmap.py)
  - **Requires**:
    - A `.mailmap` file in the repository root

- 🕸️ **Update dependency graph** (`update-deps-graph`)

  - Generates a Mermaid dependency graph of the Python project using [`repomatic update-deps-graph`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/deps_graph.py)
  - **Requires**:
    - Python package with a `uv.lock` file

- 📚 **Update docs** (`update-docs`)

  - Regenerates Sphinx autodoc files using [`sphinx-apidoc`](https://github.com/sphinx-doc/sphinx)
  - Runs `docs/docs_update.py` if present to generate dynamic content (tables, diagrams, Sphinx directives)
  - **Requires**:
    - Python package with a `pyproject.toml` file
    - `docs` dependency group
    - Sphinx autodoc enabled (checks for `sphinx.ext.autodoc` in `docs/conf.py`)

- 🌟 **Sync awesome template** (`sync-awesome-template`)

  - Syncs awesome list projects from the [`awesome-template`](https://github.com/kdeldycke/awesome-template) repository using [`actions-template-sync`](https://github.com/AndreasAugustin/actions-template-sync)
  - **Requires**:
    - Repository name starts with `awesome-`
    - Repository is not [`awesome-template`](https://github.com/kdeldycke/awesome-template) itself

### 🔒 [`.github/workflows/autolock.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/autolock.yaml)

- 🔒 **Lock inactive threads** (`lock`)

  - Automatically locks closed issues and PRs after 90 days of inactivity using [`lock-threads`](https://github.com/dessant/lock-threads)

### 🩺 [`.github/workflows/debug.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/debug.yaml)

- 🩺 **Dump context** (`dump-context`)

  - Dumps GitHub Actions context and runner environment info across all build targets using [`ghaction-dump-context`](https://github.com/crazy-max/ghaction-dump-context)
  - Useful for debugging runner differences and CI environment issues
  - **Runs on**:
    - Push to `main` (only when `debug.yaml` itself changes)
    - Monthly schedule
    - Manual dispatch
    - `workflow_call` from downstream repositories

### ✂️ [`.github/workflows/cancel-runs.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/cancel-runs.yaml)

- ✂️ **Cancel PR runs** (`cancel-runs`)

  - Cancels all in-progress and queued workflow runs for a PR's branch when the PR is closed
  - Prevents wasted CI resources from long-running jobs (e.g. Nuitka binary builds) that continue after a PR is closed
  - GitHub Actions does not natively cancel runs on PR close — the `concurrency` mechanism only triggers cancellation when a *new* run enters the same group

### 🆙 [`.github/workflows/changelog.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/changelog.yaml)

- 🆙 **Bump versions** (`bump-versions`)

  - Creates PRs for minor and major version bumps using [`bump-my-version`](https://github.com/callowayproject/bump-my-version)
  - Syncs `uv.lock` to include the new version in the same commit
  - Uses commit message parsing as fallback when tags aren't available yet
  - **Requires**:
    - `bump-my-version` configuration in `pyproject.toml`
    - A `changelog.md` file
  - **Runs on**:
    - Schedule (daily at 6:00 UTC)
    - Manual dispatch
    - After `release.yaml` workflow completes successfully (via `workflow_run` trigger, to ensure tags exist before checking bump eligibility). Checks out the latest `main` HEAD, not the triggering workflow's commit.

- 🎬 **Prepare release** (`prepare-release`)

  - Creates a release PR with two commits: a **freeze commit** that freezes everything to the release version, and an **unfreeze commit** that reverts to development references and bumps the patch version
  - Uses [`bump-my-version`](https://github.com/callowayproject/bump-my-version) and [`repomatic changelog`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/changelog.py)
  - Must be merged with "Rebase and merge" (not squash) — the auto-tagging job needs both commits separate
  - **Requires**:
    - `bump-my-version` configuration in `pyproject.toml`
    - A `changelog.md` file
  - **Runs on**:
    - Push to `main` (when `changelog.md`, `pyproject.toml`, or workflow files change)
    - Manual dispatch
    - `workflow_call` from downstream repositories

### 📚 [`.github/workflows/docs.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/docs.yaml)

These jobs require a `docs` [dependency group](https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-groups) in `pyproject.toml` so they can determine the right Sphinx version to install and its dependencies:

```toml
[dependency-groups]
docs = [
    "furo",
    "myst-parser",
    "sphinx",
    …
]
```

- 📖 **Deploy Sphinx doc** (`deploy-docs`)

  - Builds Sphinx-based documentation and publishes it to GitHub Pages using [`sphinx`](https://github.com/sphinx-doc/sphinx) and [`gh-pages`](https://github.com/peaceiris/actions-gh-pages)
  - **Requires**:
    - Python package with a `pyproject.toml` file
    - `docs` dependency group
    - Sphinx configuration file at `docs/conf.py`

- 🔗 **Sphinx linkcheck** (`check-sphinx-links`)

  - Runs Sphinx's built-in [`linkcheck`](https://www.sphinx-doc.org/en/master/usage/builders/index.html#sphinx.builders.linkcheck.CheckExternalLinksBuilder) builder to detect broken auto-generated links (intersphinx, autodoc, type annotations) that Lychee cannot see
  - Creates/updates issues for broken documentation links found
  - **Requires**:
    - Python package with a `pyproject.toml` file
    - `docs` dependency group
    - Sphinx configuration file at `docs/conf.py`
  - **Skipped for**:
    - Pull requests
    - `prepare-release` branch
    - Post-release version bump commits

- 💔 **Check broken links** (`check-broken-links`)

  - Checks for broken links in documentation using [`lychee`](https://github.com/lycheeverse/lychee)
  - Creates/updates issues for broken links found
  - **Requires**:
    - Documentation files (`**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx,rst,tex}`) in the repository
  - **Skipped for**:
    - All PRs (only runs on push to main)
    - `prepare-release` branch
    - Post-release bump commits

### 🏷️ [`.github/workflows/labels.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/labels.yaml)

- 🔄 **Sync labels** (`sync-labels`)

  - Synchronizes repository labels using [`labelmaker`](https://github.com/jwodder/labelmaker)
  - Uses [`labels.toml`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/labels.toml) with multiple profiles:
    - `default` profile applied to all repositories
    - `awesome` profile additionally applied to `awesome-*` repositories

- 📁 **File-based PR labeller** (`file-labeller`)

  - Automatically labels PRs based on changed file paths using [`labeler`](https://github.com/actions/labeler)
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

- 📝 **Content-based labeller** (`content-labeller`)

  - Automatically labels issues and PRs based on title and body content using [`issue-labeler`](https://github.com/github/issue-labeler)
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

- 💝 **Tag sponsors** (`sponsor-labeller`)

  - Adds a `💖 sponsors` label to issues and PRs from sponsors using the GitHub GraphQL API
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

### 🧹 [`.github/workflows/lint.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/lint.yaml)

- 🏠 **Lint repository metadata** (`lint-repo`)

  - Validates repository metadata (package name, Sphinx docs, project description) using [`repomatic lint-repo`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/cli.py). Reads `pyproject.toml` directly.
  - **Requires**:
    - Python package (with a `pyproject.toml` file)

- 🔤 **Lint types** (`lint-types`)

  - Type-checks Python code using [`mypy`](https://github.com/python/mypy)
  - **Requires**:
    - Python files (`**/*.{py,pyi,pyw,pyx,ipynb}`) in the repository
  - **Skipped for**:
    - `prepare-release` branch

- 📄 **Lint YAML** (`lint-yaml`)

  - Lints YAML files using [`yamllint`](https://github.com/adrienverge/yamllint)
  - **Requires**:
    - YAML files (`**/*.{yaml,yml}`) in the repository
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

- 🐚 **Lint Zsh** (`lint-zsh`)

  - Syntax-checks Zsh scripts using `zsh --no-exec`
  - **Requires**:
    - Zsh files (`**/*.zsh`) in the repository
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

- ⚡ **Lint GitHub Actions** (`lint-github-actions`)

  - Lints workflow files using [`actionlint`](https://github.com/rhysd/actionlint) and [`shellcheck`](https://github.com/koalaman/shellcheck)
  - **Requires**:
    - Workflow files (`.github/workflows/**/*.{yaml,yml}`) in the repository
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

- 🔒 **Lint workflow security** (`lint-workflow-security`)

  - Audits workflow files for security issues using [`zizmor`](https://github.com/zizmorcore/zizmor) (template injection, excessive permissions, supply chain risks, etc.)
  - **Requires**:
    - Workflow files (`.github/workflows/**/*.{yaml,yml}`) in the repository
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

- 🌟 **Lint Awesome list** (`lint-awesome`)

  - Lints awesome lists using [`awesome-lint`](https://github.com/sindresorhus/awesome-lint)
  - **Requires**:
    - Repository name starts with `awesome-`
    - Repository is not [`awesome-template`](https://github.com/kdeldycke/awesome-template) itself
  - **Skipped for**:
    - `prepare-release` branch

- 🔐 **Lint secrets** (`lint-secrets`)

  - Scans for leaked secrets using [`gitleaks`](https://github.com/gitleaks/gitleaks)
  - **Skipped for**:
    - `prepare-release` branch
    - Bot-created PRs

### 🚀 [`.github/workflows/release.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/release.yaml)

[Release Engineering is a full-time job, and full of edge-cases](https://web.archive.org/web/20250126113318/https://blog.axo.dev/2023/02/cargo-dist) that nobody wants to deal with. This workflow automates most of it for Python projects.

**Cross-platform binaries** — Targets 6 platform/architecture combinations (Linux/macOS/Windows × `x86_64`/`arm64`). Unstable targets use `continue-on-error` so builds don't fail on experimental platforms. Job names are prefixed with ✅ (stable, must pass) or ⁉️ (unstable, allowed to fail) for quick visual triage in the GitHub Actions UI.

- 🧯 **Detect squash merge** (`detect-squash-merge`)

  - Detects squash-merged release PRs, opens a GitHub issue to notify the maintainer, and fails the workflow
  - The release is effectively skipped: `create-tag` only matches commits with the `[changelog] Release v` prefix, so no tag, PyPI publish, or GitHub release is created from a squash merge
  - The net effect of squashing freeze + unfreeze leaves `main` in a valid state for the next development cycle; the maintainer just releases the next version when ready
  - **Runs on**:
    - Push to `main` only

- 📦 **Build package** (`build-package`)

  - Builds Python wheel and sdist packages using [`uv build`](https://github.com/astral-sh/uv)
  - **Requires**:
    - Python package with a `pyproject.toml` file

- ✅ **Compile binaries** (`compile-binaries`)

  - Compiles standalone binaries using [`Nuitka`](https://github.com/Nuitka/Nuitka) for Linux/macOS/Windows on `x64`/`arm64`
  - On release pushes, each binary generates an attestation and uploads itself to the GitHub release as its build completes
  - **Requires**:
    - Python package with [CLI entry points](https://docs.astral.sh/uv/concepts/projects/config/#entry-points) defined in `pyproject.toml`
  - **Skipped if** `[tool.repomatic] nuitka = false` is set in `pyproject.toml` (for projects with CLI entry points that don't need standalone binaries)
  - **Skipped for** branches that don't affect code:
    - `format-json` (JSON formatting)
    - `format-markdown` (documentation formatting)
    - `optimize-images` (image optimization)
    - `sync-gitignore` (`.gitignore` sync)
    - `sync-mailmap` (`.mailmap` sync)
    - `update-deps-graph` (dependency graph docs)

- ✅ **Test binaries** (`test-binaries`)

  - Runs test plans against compiled binaries using [`repomatic test-plan`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/test_plan.py)
  - **Requires**:
    - Compiled binaries from `compile-binaries` job
    - Test plan file (default: `./tests/cli-test-plan.yaml`)
  - **Skipped for**:
    - Same branches as `compile-binaries`

- 📌 **Create tag** (`create-tag`)

  - Creates a Git tag for the release version
  - **Requires**:
    - Push to `main` branch
    - Release commits matrix from [`repomatic metadata`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/metadata.py)

- 🐍 **Publish to PyPI** (`publish-pypi`)

  - Uploads packages to PyPI with attestations using [`uv publish`](https://github.com/astral-sh/uv)
  - **Requires**:
    - `PYPI_TOKEN` secret
    - Built packages from `build-package` job

- 🐙 **Create release draft** (`create-release`)

  - Creates a GitHub release **draft** with the Python package attached using [`action-gh-release`](https://github.com/softprops/action-gh-release)
  - Binaries are attached independently by each `compile-binaries` matrix entry as they complete (uploading to drafts is allowed)
  - **Requires**:
    - Successful `create-tag` job

- 🎉 **Publish release** (`publish-release`)

  - Publishes the draft GitHub release after all assets have been uploaded
  - Supports [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases): once published, tags and assets are locked
  - Uses `always()` so it runs even when `compile-binaries` is skipped (non-binary projects) or partially fails (unstable platforms)
  - **Requires**:
    - Successful `create-release` job (draft must exist)

- 🔄 **Sync dev pre-release** (`sync-dev-release`)

  - Maintains a rolling dev pre-release on GitHub that mirrors the unreleased changelog section
  - Downloads binaries and Python packages from build jobs and attaches them to the dev release
  - The dev tag (e.g. `v6.1.1.dev0`) is force-updated to point to the latest `main` commit
  - Automatically cleaned up when a real release is created
  - **Runs on**: Non-release pushes to `main` only
  - **Requires**:
    - `build-package` and `compile-binaries` jobs (uses `always()` for resilience)

### 🆕 [`.github/workflows/renovate.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/renovate.yaml)

- 🔄 **Sync bundled config** (`sync-bundled-config`)

  - Keeps the bundled `repomatic/data/renovate.json5` in sync with the root `renovate.json5`
  - **Only runs in**:
    - The `kdeldycke/repomatic` repository

- 🚚 **Migrate to Renovate** (`migrate-to-renovate`)

  - Automatically migrates from Dependabot to Renovate by creating a PR that:
    - Exports `renovate.json5` configuration file (if missing)
    - Removes `.github/dependabot.yaml` or `.github/dependabot.yml` (if present)
  - PR body includes a prerequisites status table showing:
    - What this PR fixes (config file creation, Dependabot removal)
    - What needs manual action (security updates settings, token permissions)
    - Links to relevant settings pages for easy access
  - Uses [`peter-evans/create-pull-request`](https://github.com/peter-evans/create-pull-request) for consistent PR creation
  - **Skipped if**:
    - No changes needed (`renovate.json5` already exists and no Dependabot config is present)

- 🆕 **Renovate** (`renovate`)

  - Validates prerequisites before running (fails if not met):
    - `renovate.json5` configuration exists
    - No Dependabot config file present
    - Dependabot security updates disabled
  - Runs self-hosted [Renovate](https://github.com/renovatebot/renovate) to update dependencies
  - Creates PRs for outdated dependencies with stabilization periods
  - Handles security vulnerabilities via `vulnerabilityAlerts`
  - **Requires**:
    - `WORKFLOW_UPDATE_GITHUB_PAT` secret with Dependabot alerts permission

- ⛓️ **Sync `uv.lock`** (`sync-uv-lock`)

  - Runs `uv lock --upgrade` to update transitive dependencies to their latest allowed versions using [`repomatic sync-uv-lock`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/renovate.py)
  - Only creates a PR when the lock file contains real dependency changes (timestamp-only noise is detected and skipped)
  - Replaces Renovate's `lockFileMaintenance`, which cannot reliably revert noise-only changes
  - **Requires**:
    - Python package with a `pyproject.toml` file

### 🔬 [`.github/workflows/tests.yaml` jobs](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/tests.yaml)

- 🔬 **Run tests** (`tests`)

  - Runs the test suite across a matrix of OS (Linux/macOS/Windows × `x86_64`/`arm64`) and Python versions (`3.10`, `3.14`, `3.14t`, `3.15`, `3.15t`)
  - Verifies CLI launchability via `uvx`, `uv run`, and module invocation
  - Runs `pytest` with coverage reporting to Codecov
  - Runs self-tests against the CLI test plan
  - Job names prefixed with **✅** (stable) or **⁉️** (unstable, e.g., unreleased Python versions)

- 🖥️ **Validate architecture** (`validate-arch`)

  - Checks that the detected CPU architecture matches what the runner image advertises
  - Ensures runners are not silently using emulation (e.g., x86_64 on aarch64)
  - **Requires**:
    - Build targets from `project-metadata` job

### 🧬 What is this `project-metadata` job?

Most jobs in this repository depend on a shared parent job called `project-metadata`. It runs first to extract contextual information, reconcile and combine it, and expose it for downstream jobs to consume.

This expands the capabilities of GitHub Actions, since it allows to:

- Share complex data across jobs (like build matrix)
- Remove limitations of conditional jobs
- Allow for runner introspection
- Fix quirks (like missing environment variables, events/commits mismatch, merge commits, etc.)

This job relies on the [`repomatic metadata` command](https://github.com/kdeldycke/repomatic/blob/main/repomatic/metadata.py) to gather data from multiple sources:

- **Git**: current branch, latest tag, commit messages, changed files
- **GitHub**: event type, actor, PR labels
- **Environment**: OS, architecture
- **`pyproject.toml`**: project name, version, entry points

> [!IMPORTANT]
> This flexibility comes at the cost of:
>
> - Making the whole workflow a bit more computationally intensive
> - Introducing a small delay at the beginning of the run
> - Preventing child jobs to run in parallel before its completion
>
> But is worth it given how [GitHub Actions can be frustrating](https://nesbitt.io/2025/12/06/github-actions-package-manager.html).

## How does it work?

### `uv` everywhere

All Python dependencies and CLIs are installed via [`uv`](https://github.com/astral-sh/uv) for speed and reproducibility.

### Smart job skipping

Jobs are guarded by conditions to skip unnecessary steps: file type detection (only lint Python if `.py` files exist), branch filtering (`prepare-release` skipped for most linting), and bot detection.

### Maintainer-in-the-loop

Workflows never commit directly or act silently. Every proposed change creates a PR; every action needed opens an issue. You review and decide — nothing lands without your approval.

### Configurable with sensible defaults

Downstream projects customize behavior via [`[tool.repomatic]`](#toolrepomatic-configuration) in `pyproject.toml`. Workflows also accept `inputs` for fine-tuning, but the configuration file is the primary interface.

### Idempotent operations

Safe to re-run: tag creation skips if already exists, version bumps have eligibility checks, PRs update existing branches.

### Graceful degradation

Fallback tokens (`secrets.WORKFLOW_UPDATE_GITHUB_PAT || secrets.GITHUB_TOKEN`) and `continue-on-error` for unstable targets. Job names use emoji prefixes for at-a-glance status: **✅** for stable jobs that must pass, **⁉️** for unstable jobs (e.g., experimental Python versions, unreleased platforms) that are expected to fail and won't block the workflow.

### Dogfooding

This repository uses these workflows for itself.

### Dependency strategy

All dependencies are pinned to specific versions for stability, reproducibility, and security.

#### Pinning mechanisms

| Mechanism                   | What it pins                | How it's updated  |
| :-------------------------- | :-------------------------- | :---------------- |
| `uv.lock`                   | Project dependencies        | Renovate PRs      |
| Hard-coded versions in YAML | GitHub Actions, npm, Python | Renovate PRs      |
| `uv --exclude-newer` option | Transitive dependencies     | Time-based window |
| Tagged workflow URLs        | Remote workflow references  | Release process   |
| `--from . repomatic`        | CLI from local source       | Release freeze    |

#### Hard-coded versions in workflows

GitHub Actions and npm packages are pinned directly in YAML files:

```yaml
  - uses: actions/checkout@v6.0.1        # Pinned action
  - run: npm install eslint@9.39.1       # Pinned npm package
```

Renovate's `github-actions` manager handles action updates, and a [custom regex manager](https://github.com/kdeldycke/repomatic/blob/main/renovate.json5) handles npm packages pinned inline in workflow files.

#### Renovate cooldowns

To avoid update fatigue, and [mitigate supply chain attacks](https://blog.yossarian.net/2025/11/21/We-should-all-be-using-dependency-cooldowns), [`renovate.json5`](https://github.com/kdeldycke/repomatic/blob/main/renovate.json5) uses stabilization periods (with prime numbers to stagger updates).

This ensures major updates get more scrutiny while patches flow through faster.

#### `uv.lock` and `--exclude-newer`

The `uv.lock` file pins all project dependencies, and Renovate keeps it in sync.

The `--exclude-newer` flag ignores packages released in the last 7 days, providing a buffer against freshly-published broken releases.

#### Tagged workflow URLs

Workflows in this repository are **self-referential**. The [`prepare-release`](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/changelog.yaml) job's freeze commit rewrites workflow URL references from `main` to the release tag, ensuring released versions reference immutable URLs. The unfreeze commit reverts them back to `main` for development.

### Permissions and token

Several workflows need a `WORKFLOW_UPDATE_GITHUB_PAT` secret to create PRs that modify files in `.github/workflows/` and to trigger downstream workflows. Without it, those jobs silently fall back to the default `GITHUB_TOKEN`, which lacks the required permissions.

After your first push, the [`setup-guide` job](#githubworkflowsautofixyaml-jobs) automatically opens an issue with [step-by-step instructions](https://github.com/kdeldycke/repomatic/blob/main/repomatic/templates/setup-guide.md) to create and configure the token.

### Concurrency and cancellation

All workflows use a `concurrency` directive to prevent redundant runs and save CI resources. When a new commit is pushed, any in-progress workflow runs for the same branch or PR are automatically cancelled.

Workflows are grouped by:

- **Pull requests**: `{workflow-name}-{pr-number}` — Multiple commits to the same PR cancel previous runs
- **Branch pushes**: `{workflow-name}-{branch-ref}` — Multiple pushes to the same branch cancel previous runs

`release.yaml` uses a stronger protection: release commits get a **unique concurrency group** based on the commit SHA, so they can never be cancelled. This ensures tagging, PyPI publishing, and GitHub release creation complete successfully.

Additionally, [`cancel-runs.yaml`](#githubworkflowscancel-runsyaml-jobs) actively cancels in-progress and queued runs when a PR is closed. This complements passive concurrency groups, which only trigger cancellation when a *new* run enters the same group — closing a PR doesn't produce such an event.

> [!TIP]
> For implementation details on how concurrency groups are computed and why `release.yaml` needs special handling, see [`claude.md` § Concurrency implementation](claude.md#concurrency-implementation).

## Claude Code integration

This repository includes [Claude Code skills](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/skills) that wrap `repomatic` CLI commands as slash commands. Downstream repositories can install them with:

```shell-session
$ uvx -- repomatic init skills
```

To keep skills in sync with the latest version:

```shell-session
$ uvx -- repomatic sync-skills
```

To list all available skills with descriptions:

```shell-session
$ uvx -- repomatic list-skills
```

### Available skills

| Phase       | Skill                  | Description                                              |
| :---------- | :--------------------- | :------------------------------------------------------- |
| Setup       | `/repomatic-init`      | Bootstrap a repository with reusable workflows           |
| Setup       | `/repomatic-sync`      | Sync workflow caller files with upstream                 |
| Development | `/repomatic-deps`      | Generate and analyze dependency graphs from uv lockfiles |
| Development | `/repomatic-metadata`  | Extract and explain project metadata                     |
| Quality     | `/repomatic-lint`      | Lint workflows and repository metadata                   |
| Quality     | `/repomatic-test`      | Run and write YAML test plans for compiled binaries      |
| Release     | `/repomatic-changelog` | Draft, validate, and fix changelog entries               |
| Release     | `/repomatic-release`   | Pre-checks, release preparation, and post-release steps  |

### Recommended workflow

The typical lifecycle for maintaining a downstream repository follows this sequence. Each skill suggests next steps after completing, creating a guided flow:

1. `/repomatic-init` — One-time setup: bootstrap workflows, labels, and configs
2. `/repomatic-sync` — Periodic: pull latest upstream workflow changes
3. `/repomatic-lint` — Before merging: validate workflows and metadata
4. `/repomatic-deps` — As needed: visualize the dependency tree
5. `/repomatic-changelog` — Before release: draft and validate changelog entries
6. `/repomatic-release` — Release time: pre-flight checks and release preparation

<details>
<summary>Walkthrough: setup to first release</summary>

```text
# In Claude Code, bootstrap your repository
/repomatic-init

# After making changes, sync with latest upstream workflows
/repomatic-sync

# Validate everything
/repomatic-lint all

# Add changelog entries for your changes
/repomatic-changelog add

# Validate the changelog
/repomatic-changelog check

# Pre-flight checks before release
/repomatic-release check

# Prepare the release PR
/repomatic-release prep
```

</details>

## Migrating from `kdeldycke/workflows`

> [!WARNING]
> This section is for users of the former `gha-utils` package / `kdeldycke/workflows` repository.

This project was previously published as [`gha-utils`](https://pypi.org/project/gha-utils/) on PyPI and hosted at `kdeldycke/workflows` on GitHub. It was [renamed to `repomatic` in `6.0.1`](https://github.com/kdeldycke/repomatic/blob/main/changelog.md#601-2026-02-24).

Running `uvx -- repomatic init workflows --overwrite` in an existing downstream repository regenerates all workflow files to point at `kdeldycke/repomatic`, but several things require manual attention:

1. **Rename the config section** in `pyproject.toml`: `[tool.gha-utils]` → `[tool.repomatic]`. The `init` command does not migrate this automatically.

2. **Use `--overwrite`**. Without it, `init` skips existing workflow files — so old thin-callers referencing `kdeldycke/workflows` are left untouched. The `workflow sync` command also won't recognize them, because it only matches `uses:` references to `kdeldycke/repomatic`.

3. **Remove old Claude Code skills**. The old `/gha-*` skill directories (`.claude/skills/gha-init/`, `.claude/skills/gha-changelog/`, etc.) are not cleaned up by `init`. Delete them manually:

   ```shell-session
   $ rm -rf .claude/skills/gha-*/
   ```

4. **Update CLI references**. Replace `gha-utils` / `uvx gha-utils` with `repomatic` / `uvx repomatic` in any scripts, Makefiles, or CI steps outside of the managed workflow files.

5. **Review `tests.yaml`**. This is the only non-reusable workflow — it contains project-specific logic. If it references `kdeldycke/workflows` or `gha-utils` in comments or `uses:` lines, update those manually.

### Migration checklist

```shell-session
$ cd my-project

# 1. Rename config section in pyproject.toml (if present).
$ sed -i.bak 's/\[tool\.gha-utils\]/[tool.repomatic]/' pyproject.toml && rm pyproject.toml.bak

# 2. Regenerate workflow files.
$ uvx -- repomatic init workflows --overwrite

# 3. Remove old skill directories.
$ rm -rf .claude/skills/gha-*/

# 4. Commit and push.
$ git add . && git commit -m "Migrate from gha-utils to repomatic" && git push
```

After pushing, the [autofix workflow](#githubworkflowsautofixyaml-jobs) handles ongoing sync (workflows, Renovate config, skills, linter configs).

## Used in

Check these projects to get real-life examples of usage and inspiration:

- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-falsehood?label=%E2%AD%90&style=flat-square) [Awesome Falsehood](https://github.com/kdeldycke/awesome-falsehood) - Falsehoods Programmers Believe in.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-engineering-team-management?label=%E2%AD%90&style=flat-square) [Awesome Engineering Team Management](https://github.com/kdeldycke/awesome-engineering-team-management) - How to transition from software development to engineering management.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-iam?label=%E2%AD%90&style=flat-square) [Awesome IAM](https://github.com/kdeldycke/awesome-iam) - Identity and Access Management knowledge for cloud platforms.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-billing?label=%E2%AD%90&style=flat-square) [Awesome Billing](https://github.com/kdeldycke/awesome-billing) - Billing & Payments knowledge for cloud platforms.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/meta-package-manager?label=%E2%AD%90&style=flat-square) [Meta Package Manager](https://github.com/kdeldycke/meta-package-manager) - A unifying CLI for multiple package managers.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/mail-deduplicate?label=%E2%AD%90&style=flat-square) [Mail Deduplicate](https://github.com/kdeldycke/mail-deduplicate) - A CLI to deduplicate similar emails.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/dotfiles?label=%E2%AD%90&style=flat-square) [dotfiles](https://github.com/kdeldycke/dotfiles) - macOS dotfiles for Python developers.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/click-extra?label=%E2%AD%90&style=flat-square) [Click Extra](https://github.com/kdeldycke/click-extra) - Extra colorization and configuration loading for Click.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/repomatic?label=%E2%AD%90&style=flat-square) [repomatic](https://github.com/kdeldycke/repomatic) - Itself. Eat your own dog-food.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/extra-platforms?label=%E2%AD%90&style=flat-square) [Extra Platforms](https://github.com/kdeldycke/extra-platforms) - Detect platforms and group them by family.

Feel free to send a PR to add your project in this list if you are relying on these scripts.

## Development

See [`claude.md`](claude.md) for development commands, code style, testing guidelines, and design principles.
