---
args: [proposal]
title: Sync runner images
docs: https://repomatic.net/workflows#sync-runner-images-sync-runner-images
labels: [🤖 ci]
---

A runner image this repository uses has changed in GitHub's [available-images table](https://github.com/actions/runner-images#available-images). This pull request makes the mechanical changes. Review the CI run and decide whether to keep them.

\$proposal

**A 🔴 retirement moves jobs to a released image.** A released successor always wins over a preview. The **Passed over** column names any newer preview not taken: take it instead if you accept the preview's risks.

**A 🆕 probe adds a `continue-on-error` cell to the test matrix.** It cannot fail the build. It exercises the new image early, while any break can still be reported upstream.

> [!IMPORTANT]
> Read the CI run before you merge. Check `repomatic job-timings` for the image's cost in whole-job wall-clock time, not just whether it passes.

To decline a proposal permanently, add its label to `pyproject.toml`. Closing this pull request does not work: the next run brings the proposal back.

```toml
[tool.repomatic.sync-runner-images]
ignore = ["the-label"]
```
