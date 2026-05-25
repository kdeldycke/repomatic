---
name: repomatic-release-prep
description: Reconcile the changelog, code, and docs to the net release state, recommend the version bump, then hand off to commit, CI, and merge. Run before merging the release PR.
model: opus
disable-model-invocation: true
allowed-tools: Bash, Read, Grep, Glob, Skill, Agent
---

## Context

!`grep -m1 'version' pyproject.toml 2>/dev/null`
!`head -8 changelog.md 2>/dev/null`
!`git tag --sort=-v:refname | head -3 2>/dev/null`
!`git log --oneline -25 2>/dev/null`
!`git status --short 2>/dev/null`
!`[ -f repomatic/__init__.py ] && echo "CANONICAL_REPO" || echo "DOWNSTREAM"`

## Instructions

You prepare a release by reconciling the working tree to its **net state since the last tag**, then handing off to the pipeline that performs the actual release. You do the analytical work only.

The release is push-driven, and the mechanical steps are already automated: the `prepare-release` job in `changelog.yaml` runs `repomatic release-prep` on push to `main` to build the freeze and unfreeze commits and open the release PR. **Do not run `release-prep` yourself** — running it locally previews a freeze the user must not commit (it marks the changelog "released", and on the canonical repo rewrites every workflow action ref). Your job is to make `main` clean enough that the auto-generated release PR is correct.

### Target workflow

1. Run this skill: the reconciliation sweep, validation, and version recommendation below.
2. Commit the result and push to `main`. CI rebuilds the release PR (freeze + unfreeze commits).
3. Run `/babysit-ci` until `main` is green: it also catches Nuitka binary-build breakage before the release hits it.
4. Merge the release PR with **"Rebase and merge"** (never squash).

You cannot commit, push, or merge (`CLAUDE.md` § Agent behavior policy). Finish the sweep, then hand steps 2-4 back to the user with that exact sequence.

### Determine invocation method

- If the context shows `CANONICAL_REPO`, use `uv run repomatic`.
- Otherwise, use `uvx -- repomatic`.

### 1. Reconciliation sweep

A release materializes the **net state since the last tag**, not the path taken to reach it. After a long cycle (features reworked, dependencies pinned then unpinned, APIs renamed), the changelog, code, and docs all drift toward describing the journey. Reconcile all three against the actual diff from the last tag to `HEAD`, in order:

1. **Changelog** — invoke `/repomatic-changelog consolidate` through the `Skill` tool. It collapses superseded values and drops changes reverted within the cycle. **Degrade gracefully:** if `/repomatic-changelog` is excluded in this repo, spawn an `Agent` that applies the same end-state principle, or consolidate inline. A missing skill is a fallback path, not a blocker.
2. **Code** — spawn an `Agent` for a simplification pass (`CLAUDE.md` § Common maintenance pitfalls, "Simplify before adding"). Remove scaffolding left by reverted or superseded work: abandoned workarounds, dead branches, WIP comments, draft notes that never shipped.
3. **Docs** — spawn an `Agent` to verify docs against current behavior, not the journey (`CLAUDE.md` § Common maintenance pitfalls, "Documentation drift"). Version references, CLI output, and removed or renamed features go stale every cycle.

A change introduced and then reverted before release is a no-op for users: no changelog entry, no scaffolding in the code, no mention in the docs. This skill holds no `Edit`/`Write` of its own — the changelog skill and the agents do the editing.

### 2. Validate

Run `<cmd> lint-changelog` and report the result. A `⚠ X.Y.Z: not found on PyPI` warning for the still-unreleased version is expected and not a blocker.

### 3. Recommend the version part

Read the consolidated unreleased section and recommend the bump, so the user merges the matching version-increment PR (or lets the default patch stand) before releasing:

- A `**Breaking:**` entry, or any removed or renamed public API → **major**.
- A new feature, command, or config key → **minor**.
- Only fixes, dependency bumps, and internal changes → **patch**.

State the recommendation and the single strongest reason. A patch release needs no action (the unfreeze commit bumps the patch by default); major or minor means merging the corresponding `major-version-increment` / `minor-version-increment` bump PR first.

### 4. Present for review

Show `git diff` of `changelog.md` plus a one-line summary of the code and docs changes the agents made. Consolidation drops and merges entries: surface what changed so the user can catch an over-eager drop (a real failure mode) before it ships.

### 5. Hand off

End by listing the steps you cannot perform:

1. Commit the sweep and push to `main` — this regenerates the release PR.
2. Run `/babysit-ci` in autofix mode until `main` is green.
3. Merge the release PR with **"Rebase and merge"**, never squash.

### Why "Rebase and merge", never squash

The release PR carries exactly **two commits**: a **freeze commit** (`[changelog] Release vX.Y.Z`) that finalizes the changelog date and comparison URL, removes the unreleased warning, and pins workflow action refs and CLI invocations to the release version; and an **unfreeze commit** (`[changelog] Post-release bump`) that reverts those to `@main` and local source, adds a fresh unreleased section, and bumps the patch version. The auto-tagging job tags only the freeze commit, located by its message — squashing collapses both into one and breaks tagging. A `detect-squash-merge` safeguard opens an issue and fails the workflow when a squash is detected.

### What a complete release looks like

After the merge, the pipeline produces all of the following; if any is missing, the release is incomplete:

- **Git tag** (`vX.Y.Z`) on the freeze commit.
- **GitHub release** with notes matching the `changelog.md` entry.
- **Binaries** for all 6 platform/architecture combinations (linux-arm64, linux-x64, macos-arm64, macos-x64, windows-arm64, windows-x64), when the project builds them.
- **PyPI package** at the matching version.
- **`changelog.md`** entry with the release date and comparison URL finalized.
