---
title: Record release binaries
docs: https://repomatic.net/workflows#virustotal-scan-scan-virustotal
footer: false
labels: [🤖 ci]
---

## 🛡️ Scan records

Each release appends to this branch, so this pull request carries every binary scanned since it was opened. Merging it moves the records onto the default branch and the next release starts a fresh one.

The detection counts are a trend, not a verdict on any one release: a fresh Nuitka binary no vendor has seen is flagged on sight, and the count falls as vendors process it. What the history is for is watching that trend across releases.

Leaving this open costs nothing but the freshness of the published binaries page. The records keep accruing here, and no scan is lost by waiting.

## ⚙️ Configuration

Relevant [`[tool.repomatic]`](https://repomatic.net/configuration) options:

- [`binaries.sync`](https://repomatic.net/configuration#binaries-sync)
