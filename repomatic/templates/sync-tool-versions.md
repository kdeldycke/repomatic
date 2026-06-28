---
args: [diff_table]
title: Sync tool versions
footer: false
---

### Description

Bumps the tools in the [`repomatic run`](https://kdeldycke.github.io/repomatic/tool-runner.html) registry to their latest releases that have cleared the stabilization cooldown (GitHub releases for binary tools, PyPI otherwise), refreshing binary checksums in the same pass. See the [`sync-tool-versions` job documentation](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) for details.

\$diff_table

### Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`minimum-release-age`](https://kdeldycke.github.io/repomatic/configuration.html#minimum-release-age)
- [`tool-versions.sync`](https://kdeldycke.github.io/repomatic/configuration.html#tool-versions-sync)
