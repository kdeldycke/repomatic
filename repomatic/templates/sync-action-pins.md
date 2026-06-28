---
args: [diff_table]
title: Sync action pins
footer: false
---

### Description

Bumps SHA-pinned GitHub Actions (`uses: owner/repo@<sha> # vX.Y.Z`) to their latest releases that have cleared the stabilization cooldown, resolving each release tag to its commit SHA. See the [`sync-action-pins` job documentation](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) for details.

\$diff_table

### Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`action-pins.sync`](https://kdeldycke.github.io/repomatic/configuration.html#action-pins-sync)
- [`minimum-release-age`](https://kdeldycke.github.io/repomatic/configuration.html#minimum-release-age)
