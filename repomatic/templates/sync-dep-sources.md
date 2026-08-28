---
args: [diff_table]
title: Sync dependency sources
docs: https://repomatic.net/workflows#sync-dep-sources-updater
footer: false
labels: [🔗 dependencies]
---

\$diff_table

> [!NOTE]
> If checks fail on this PR, the project relies on branch commits newer than the adopted release. Do not merge: bump the dependency's `.dev` version floor past them instead. The next run then retracts this swap automatically.

## ⚙️ Configuration

Relevant [`[tool.repomatic]`](https://repomatic.net/configuration) options:

- [`dep-sources.sync`](https://repomatic.net/configuration#dep-sources-sync)
