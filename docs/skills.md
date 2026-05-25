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

| Phase | Skill | Description |
| :--- | :--- | :--- |
| Setup | [`/repomatic-init`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-init/SKILL.md) | Bootstrap a repository with reusable workflows |
| Development | [`/brand-assets`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/brand-assets/SKILL.md) | Create and export project logo/banner SVG assets to PNG variants |
| Development | [`/repomatic-deps`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-deps/SKILL.md) | Dependency graphs, tree analysis, and declaration audit |
| Development | [`/repomatic-topics`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-topics/SKILL.md) | Optimize GitHub topics for discoverability |
| Maintenance | [`/awesome-triage`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/awesome-triage/SKILL.md) | Triage issues and PRs on awesome-list repos (awesome-list only) |
| Maintenance | [`/file-bug-report`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/file-bug-report/SKILL.md) | Write a bug report for an upstream project |
| Maintenance | [`/repomatic-audit`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-audit/SKILL.md) | Audit downstream repo alignment with upstream reference |
| Maintenance | [`/sphinx-docs-sync`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/sphinx-docs-sync/SKILL.md) | Compare and sync Sphinx docs across sibling projects |
| Maintenance | [`/translation-sync`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/translation-sync/SKILL.md) | Detect stale translations and draft updates (awesome-list only) |
| Release | [`/repomatic-changelog`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-changelog/SKILL.md) | Draft, validate, and fix changelog entries |
| Release | [`/repomatic-cut-release`](https://github.com/kdeldycke/repomatic/blob/main/.claude/skills/repomatic-cut-release/SKILL.md) | Reconcile changelog/code/docs and prepare the release |

## Recommended workflow

The typical lifecycle for maintaining a downstream repository follows this sequence. Each skill suggests next steps after completing, creating a guided flow:

1. `/repomatic-init` — One-time setup: bootstrap workflows, labels, and configs
2. `/repomatic-deps` — As needed: visualize the dependency tree
3. `/repomatic-changelog` — Before release: draft and validate changelog entries
4. `/repomatic-cut-release` — Release time: reconcile the tree and prepare the release before merging the release PR

### Walkthrough: setup to first release

```text
# In Claude Code, bootstrap your repository
/repomatic-init

# Add changelog entries as you work
/repomatic-changelog add

# Reconcile changelog/code/docs and prep the release
/repomatic-cut-release

# Commit and push — CI rebuilds the release PR from the new main
git commit -am "…" && git push

# Get main green (also catches Nuitka binary-build breakage)
/babysit-ci

# On GitHub, merge the release PR with "Rebase and merge" (never squash)
```
