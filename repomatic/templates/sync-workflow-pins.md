---
args: [diff_table]
title: Sync workflow pins
footer: false
---

### Description

Bumps npm and PyPI version literals embedded in workflow YAML (`npm install pkg@x`, `uvx '<pkg>==x'`) to their latest releases that have cleared the stabilization cooldown. See the [`sync-workflow-pins` job documentation](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) for details.

\$diff_table

### Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`minimum-release-age`](https://kdeldycke.github.io/repomatic/configuration.html#minimum-release-age)
- [`workflow-pins.sync`](https://kdeldycke.github.io/repomatic/configuration.html#workflow-pins-sync)
