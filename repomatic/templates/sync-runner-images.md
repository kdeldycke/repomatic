---
args: [proposal]
title: Sync runner images
docs: https://repomatic.net/workflows#sync-runner-images-sync-runner-images
labels: [🤖 ci]
---

A runner image this repository runs has moved in GitHub's [available-images table](https://github.com/actions/runner-images#available-images). This pull request carries the mechanical half of the response; deciding whether to take it is the point of opening it rather than committing it.

\$proposal

**A 🔴 retirement moves jobs onto released ground.** A released successor always wins over a preview: a forced move should not trade a known deadline for an unknown one. Where a newer preview was passed over, it is named under **Passed over** so you can take it instead if the capacity risk is worth the freshness.

**A 🆕 probe changes nothing you depend on.** The image joins the full test matrix as a `continue-on-error` cell, so it cannot fail the build. The point is to start exercising it now: a dependency that breaks on a new image surfaces here months before the image is one you have to be on, while there is still time to report it upstream.

Only a strictly newer *version* is proposed as a probe. A same-version variant carrying a different toolchain is a different image rather than a newer one, and is left alone.

> [!IMPORTANT]
> The CI run on this pull request is the evidence. Read it before merging, and check `repomatic job-timings` for what the image costs in whole-job wall-clock, not just whether it passes.

To decline a proposal permanently, name the label in `pyproject.toml` rather than closing this pull request, which only brings it back on the next push:

```toml
[tool.repomatic.sync-runner-images]
ignore = ["the-label"]
```
