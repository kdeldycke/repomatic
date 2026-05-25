# {octicon}`dependabot` Claude Code skills

This repository includes [Claude Code skills](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/skills) that bring `repomatic` workflows into Claude Code as slash commands. Downstream repositories can install them with:

```shell-session
$ uvx -- repomatic init skills
```

To install a single skill:

```shell-session
$ uvx -- repomatic init skills/repomatic-topics
```

Selectors use the same `component[/file]` syntax as the `exclude` config option in [`[tool.repomatic]`](configuration.md).

To list all available skills with descriptions:

```{click:run}
from repomatic.cli import repomatic
invoke(repomatic, args=['list-skills'])
```

## Available skills

| Phase       | Skill                                                                                                                      | Description                                                           |
| :---------- | :------------------------------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------- |
| Setup       | [`/repomatic-init`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-init/SKILL.md)               | Bootstrap a repository with reusable workflows                        |
| Development | [`/babysit-ci`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/babysit-ci/SKILL.md)                       | Monitor CI and fix failing tests, lint, and binary builds until green |
| Development | [`/brand-assets`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/brand-assets/SKILL.md)                   | Create and export project logo/banner SVG assets to PNG variants      |
| Development | [`/repomatic-deps`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-deps/SKILL.md)               | Dependency graphs, tree analysis, and declaration audit               |
| Development | [`/repomatic-topics`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-topics/SKILL.md)           | Optimize GitHub topics for discoverability                            |
| Maintenance | [`/awesome-triage`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/awesome-triage/SKILL.md)               | Triage issues and PRs on awesome-list repos (awesome-list only)       |
| Maintenance | [`/file-bug-report`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/file-bug-report/SKILL.md)             | Write a bug report for an upstream project                            |
| Maintenance | [`/repomatic-audit`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-audit/SKILL.md)             | Audit downstream repo alignment with upstream reference               |
| Maintenance | [`/sphinx-docs-sync`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/sphinx-docs-sync/SKILL.md)           | Compare and sync Sphinx docs across sibling projects                  |
| Maintenance | [`/translation-sync`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/translation-sync/SKILL.md)           | Detect stale translations and draft updates (awesome-list only)       |
| Release     | [`/repomatic-changelog`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-changelog/SKILL.md)     | Draft, validate, and fix changelog entries                            |
| Release     | [`/repomatic-cut-release`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-cut-release/SKILL.md) | Reconcile changelog/code/docs and prepare the release                 |

## Recommended workflow

The typical lifecycle for maintaining a downstream repository follows this sequence. Each skill suggests next steps after completing, creating a guided flow:

1. `/repomatic-init` — One-time setup: bootstrap workflows, labels, and configs
2. `/repomatic-deps` — As needed: visualize the dependency tree
3. `/repomatic-changelog` — Before release: draft and validate changelog entries
4. `/repomatic-cut-release` — Release time: reconcile the tree and prepare the release before merging the release PR

## Walkthrough: setup to first release

These steps take a downstream repository from a fresh checkout to its first release, running each skill interactively and approving its actions as you go:

1. Bootstrap your repository (one-time) with [`/repomatic-init`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-init/SKILL.md):

   ```text
   /repomatic-init
   ```

2. Add changelog entries as you work, with [`/repomatic-changelog`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-changelog/SKILL.md):

   ```text
   /repomatic-changelog add
   ```

3. Reconcile changelog, code, and docs, then prepare the release with [`/repomatic-cut-release`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-cut-release/SKILL.md):

   ```text
   /repomatic-cut-release
   ```

4. Commit and push to rebuild the [release PR](workflows.md#release-engineering) from the new `main`:

   ```shell-session
   $ git commit -am "…" && git push
   ```

5. Get `main` green with [`/babysit-ci`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/babysit-ci/SKILL.md), which also catches [Nuitka binary-build](workflows.md#github-workflows-release-yaml-jobs) breakage:

   ```text
   /babysit-ci
   ```

6. On GitHub, merge the release PR with ["Rebase and merge"](workflows.md#freeze-and-unfreeze-commits), never squash.

## Fully automated workflow

[`/babysit-ci`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/babysit-ci/SKILL.md) is built to run unattended: after a release push (step 5 of the [walkthrough](#walkthrough-setup-to-first-release)), it loops on CI feedback, fixes failures, and re-pushes until `main` is green. Every step is mechanical (fetch logs, match patterns, edit, commit), so nothing needs your approval.

To run it hands-off, launch Claude Code with `--dangerously-skip-permissions` so it never pauses for a `gh`, `git`, or `pytest` call. Sonnet is enough: the task doesn't call for deeper reasoning.

```shell-session
$ claude --dangerously-skip-permissions --model sonnet /babysit-ci
```

```{warning}
`--dangerously-skip-permissions` disables *every* permission prompt for the session: Claude Code runs shell commands, edits files, and pushes to your remote without asking first. Only use it when you trust the repository state and the operations the skill performs, ideally inside a sandbox or disposable environment.
```

Because the loop commits and pushes without human review, its commits carry a `Co-Authored-By` trailer so the autonomous work stays traceable.
