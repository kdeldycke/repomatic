<p align="center">
  <img src="https://raw.githubusercontent.com/kdeldycke/repomatic/main/docs/assets/logo-banner.svg" alt="repomatic">
</p>

[![Last release](https://img.shields.io/pypi/v/repomatic.svg)](https://pypi.org/project/repomatic/)
[![Python versions](https://img.shields.io/pypi/pyversions/repomatic.svg)](https://pypi.org/project/repomatic/)
[![Downloads](https://static.pepy.tech/badge/repomatic/month)](https://pepy.tech/projects/repomatic)
[![Unittests status](https://img.shields.io/github/actions/workflow/status/kdeldycke/repomatic/tests.yaml?branch=main&label=%F0%9F%94%AC%20Tests)](https://github.com/kdeldycke/repomatic/actions/workflows/tests.yaml?query=branch%3Amain)
[![Documentation status](https://img.shields.io/github/actions/workflow/status/kdeldycke/repomatic/docs.yaml?branch=main&label=%F0%9F%93%9A%20Docs)](https://github.com/kdeldycke/repomatic/actions/workflows/docs.yaml?query=branch%3Amain)

A Python CLI and `pyproject.toml` configuration that let you **release Python packages multiple times a day with only 2-clicks**. Designed for `uv`-based Python projects, but usable for other projects too. Every step is a CLI command: reusable GitHub Actions workflows are only the trigger, so the same automation runs on your machine.

**Maintainer-in-the-loop**: nothing is done behind your back. Every change repomatic proposes arrives as a PR you review; every action it needs from you opens an issue.

## What it automates

- Version bumping, git tagging, and GitHub release creation
- Changelog management
- Python package building and PyPI publishing with supply chain attestations
- Cross-platform binary compilation (Linux / macOS / Windows, x86_64 / arm64)
- Formatting autofix for Python, Markdown, JSON, Shell, and typos
- Linting: Python types with mypy, YAML, GitHub Actions, workflow security, URLs, secrets, and Awesome lists
- Synchronization of `uv.lock`, GitHub Action pins, workflow version literals, and `repomatic run` tool versions with configurable stabilization cooldowns
- Synchronization of `.gitignore`, `.mailmap`, and Mermaid dependency graph
- Label management with file-based and content-based rules
- Inactive issue locking
- Static image optimization
- Sphinx documentation building, deployment, and autodoc updates
- Awesome list template synchronization

## Why repomatic

repomatic does for the release process what ruff did for linting and uv did for packaging: replace a stack of single-purpose tools with one.

- [23 third-party GitHub Actions replaced](https://repomatic.net/security#third-party-action-minimization) by internal CLI commands and SHA-256-verified binary downloads, keeping the supply chain attack surface minimal
- [10 Python linters and formatters](https://repomatic.net/security#ruff-consolidation) (pylint, pydocstyle, pycln, pyupgrade, isort, black, docformatter, mdformat-black, blacken-docs, mdformat-ruff) consolidated into ruff
- [5 packaging and install tools](https://repomatic.net/security#uv-consolidation) (poetry, build, twine, check-wheel-contents, pip-audit) consolidated into uv
- All `uses:` references [pinned to full commit SHAs](https://repomatic.net/security#supply-chain-security) with stabilization windows before adopting new versions, managed entirely by self-hosted sync jobs
- [SLSA provenance attestations](https://repomatic.net/security#supply-chain-security) on every release artifact (wheels and compiled binaries)
- [VirusTotal scanning](https://repomatic.net/security#av-false-positive-submissions) of compiled binaries to seed AV vendor databases and reduce false positives
- [Trusted Publishing](https://repomatic.net/workflows#github-workflows-release-yaml-jobs) for PyPI uploads: no long-lived tokens stored as secrets
- [Immutable releases](https://repomatic.net/security#supply-chain-security) enforced via GitHub's tag protection and release locking
- Workflow security linting with [`zizmor`](https://repomatic.net/workflows#github-workflows-lint-yaml-jobs) on every push to catch dangerous triggers and excessive permissions
- Credential scanning with [`gitleaks`](https://repomatic.net/workflows#github-workflows-lint-yaml-jobs) to prevent secret leakage
- Single [`pyproject.toml` configuration](https://repomatic.net/configuration): no extra dotfiles, no JSON configs, no YAML presets to maintain
- The CLI itself [ships as standalone binaries](https://repomatic.net/install#executables) (Linux / macOS / Windows, x86_64 / arm64): no Python environment needed to run it
- [15+ code quality tools](https://repomatic.net/tool-runner) (ruff, mypy, biome, typos, mdformat, shfmt, yamllint, actionlint, lychee, oxipng, pyproject-fmt, labelmaker, gitleaks, zizmor) managed through one `repomatic run <tool>` interface with automatic installation and platform-specific binary caching

## Quick start

```shell-session
$ cd my-project
$ uvx -- repomatic init
$ git add .
$ git commit -m "Add repomatic"
$ git push
```

Works for new and existing repositories. Managed files are always regenerated to the latest version; `changelog.md` is never overwritten. Push, and the workflows guide you through remaining setup via issues and PRs.

The workflows only call CLI commands, so every automated step can also be run and previewed locally:

```shell-session
$ uvx -- repomatic sync-deps --dry-run
$ uvx -- repomatic run ruff -- check
```

See `repomatic init --help` for available components and options.

## Documentation

See the **[full documentation](https://repomatic.net/)** for:

- [Installation methods and executables](https://repomatic.net/install)
- [`[tool.repomatic]` configuration reference](https://repomatic.net/configuration)
- [CLI parameters](https://repomatic.net/cli)
- [Reusable workflow reference](https://repomatic.net/workflows) (all 15 workflows with job descriptions)
- [Security practices and token setup](https://repomatic.net/security)
- [Claude Code skills](https://repomatic.net/skills) and [agents](https://repomatic.net/agents), also installable as a [Claude Code plugin](https://repomatic.net/plugin)
- [API reference](https://repomatic.net/repomatic)
- [Project history](https://repomatic.net/history)

## Used in

Check these projects to get real-life examples of usage and inspiration:

- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-falsehood?label=%E2%AD%90&style=flat-square) [Awesome Falsehood](https://github.com/kdeldycke/awesome-falsehood) - Falsehoods Programmers Believe in.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-engineering-team-management?label=%E2%AD%90&style=flat-square) [Awesome Engineering Team Management](https://github.com/kdeldycke/awesome-engineering-team-management) - How to transition from software development to engineering management.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-iam?label=%E2%AD%90&style=flat-square) [Awesome IAM](https://github.com/kdeldycke/awesome-iam) - Identity and Access Management knowledge for cloud platforms.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/awesome-billing?label=%E2%AD%90&style=flat-square) [Awesome Billing](https://github.com/kdeldycke/awesome-billing) - Billing & Payments knowledge for cloud platforms.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/meta-package-manager?label=%E2%AD%90&style=flat-square) [Meta Package Manager](https://github.com/kdeldycke/meta-package-manager) - A unifying CLI for multiple package managers.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/mail-deduplicate?label=%E2%AD%90&style=flat-square) [Mail Deduplicate](https://github.com/kdeldycke/mail-deduplicate) - A CLI to deduplicate similar emails.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/dotfiles?label=%E2%AD%90&style=flat-square) [dotfiles](https://github.com/kdeldycke/dotfiles) - macOS dotfiles for Python developers.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/click-extra?label=%E2%AD%90&style=flat-square) [Click Extra](https://github.com/kdeldycke/click-extra) - Drop-in replacement for Click to make user-friendly and colorful CLI.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/repomatic?label=%E2%AD%90&style=flat-square) [repomatic](https://github.com/kdeldycke/repomatic) - Itself. Eat your own dog-food.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/plumage?label=%E2%AD%90&style=flat-square) [Plumage](https://github.com/kdeldycke/plumage) - Clean and tidy theme for Pelican, the static site generator.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/kevin-deldycke-blog?label=%E2%AD%90&style=flat-square) [Kevin Deldycke's blog](https://github.com/kdeldycke/kevin-deldycke-blog) - My personal blog, based on Pelican.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/extra-platforms?label=%E2%AD%90&style=flat-square) [Extra Platforms](https://github.com/kdeldycke/extra-platforms) - Detect architectures, platforms, shells, terminals, CI systems and agents, grouped by family.
- ![GitHub stars](https://img.shields.io/github/stars/kdeldycke/kdeldycke?label=%E2%AD%90&style=flat-square) [GitHub profile](https://github.com/kdeldycke/kdeldycke) - My GitHub profile page and short bio.

Send a PR to add your project if you use repomatic.
