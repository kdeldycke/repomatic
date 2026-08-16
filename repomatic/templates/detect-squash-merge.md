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

The release process requires the freeze and unfreeze commits to land as **separate commits** via "Rebase and merge". A squash merge combines them into one, preventing the tagging pipeline from identifying the freeze commit.

Existing safeguards prevented the release from being published:

- No git tag was created.
- No PyPI package was published.
- No GitHub release was created.

The merged changes are the net effect of freeze + unfreeze, which leaves `main` in a valid state for the next development cycle. The skipped version appears in the changelog but was never published.

## Recovery

No immediate action is required. To release:

1. Make any pending changes (or wait for the next one).
2. Let `prepare-release` create a new release PR for the next version.
3. Merge it with **"Rebase and merge"**.

Supersedes \$pr_ref.
