---
args: [pr_ref]
title: 🚨 Squash merge detected — release skipped
docs: https://repomatic.net/workflows#detect-squash-merge-detect-squash-merge
footer: false
---

## Summary

> [!CAUTION]
> The release PR \$pr_ref was squash-merged instead of rebase-merged.

## What happened

The release needs the freeze and unfreeze commits as **separate commits**, landed with "Rebase and merge". A squash merge combines them into one, so the tagging pipeline cannot find the freeze commit.

Existing safeguards stopped the release:

- No git tag was created.
- No PyPI package was published.
- No GitHub release was created.

Freeze and unfreeze cancel out, so `main` stays valid for the next cycle. The skipped version appears in the changelog but was never published.

## Recovery

No action is required. To release:

1. Make any pending changes (or wait for the next one).
2. Let `prepare-release` create a new release PR for the next version.
3. Merge it with **"Rebase and merge"**.

Supersedes \$pr_ref.
