---
args: [diff_table]
title: Sync dependency sources
docs: https://kdeldycke.github.io/repomatic/workflows.html#sync-dep-sources-updater
footer: false
---

\$diff_table

> [!NOTE]
> If checks fail on this PR, the project relies on branch commits newer than the adopted release: bump the dependency's `.dev` version floor past it (the next run then retracts this swap automatically) instead of merging.

## Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`dep-sources.sync`](https://kdeldycke.github.io/repomatic/configuration.html#dep-sources-sync)
