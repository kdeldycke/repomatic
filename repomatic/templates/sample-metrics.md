---
title: Sample forge metrics
docs: https://repomatic.net/workflows#sample-forge-metrics-sample-metrics
footer: false
labels: [🤖 ci]
---

## 📈 Readings

Each scheduled run appends to this branch, so this pull request carries every reading taken since it was opened. Merging it moves them onto the default branch and the next run starts a fresh one.

An open pull request does not stop the readings: they keep accruing here, and only the charts published from the default branch lag behind.

## ⚙️ Configuration

Relevant [`[tool.repomatic]`](https://repomatic.net/configuration) options:

- [`metrics.charts`](https://repomatic.net/configuration#metrics-charts)
- [`metrics.store`](https://repomatic.net/configuration#metrics-store)
- [`metrics.subjects`](https://repomatic.net/configuration#metrics-subjects)
- [`metrics.sync`](https://repomatic.net/configuration#metrics-sync)
