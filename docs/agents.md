# {octicon}`dependabot` Claude Code agents

This repository includes [Claude Code subagents](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/sub-agents) that run quality assurance checks against the repository. Unlike skills (which are user-invoked slash commands), agents are auto-invoked by Claude based on their `description:` frontmatter when the current task matches their role.

Downstream repositories can install them with:

```shell-session
$ uvx -- repomatic init agents
```

To install a single agent:

```shell-session
$ uvx -- repomatic init agents/grunt-qa
```

Selectors use the same `component[/file]` syntax as the `exclude` config option in [`[tool.repomatic]`](configuration.md).

To deploy agents to a non-default directory (like a dotfiles repository where `.claude/` is not at the root), set `agents.location` in `[tool.repomatic]`:

```toml
[tool.repomatic]
include = ["agents"]
agents.location = "./dotfiles/.claude/agents/"
```

## Available agents

| Agent                                                                                           | Role                                                                                                           |
| :---------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------- |
| [`grunt-qa`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/grunt-qa.md)       | Hands-on worker that fixes typos, ordering, style, doc-sync issues, and other mechanical CLAUDE.md violations. |
| [`qa-engineer`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/qa-engineer.md) | Senior engineer that handles deep code analysis, bug-class sweeps, and design decisions.                       |
| [`sphinx-docs`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/sphinx-docs.md) | Documentation steward that keeps `docs/` in sync with code and enforces MyST and click-extra conventions.      |

## Self-containment

Like skills, agents must be self-contained for downstream portability. They reference [`claude.md`](https://github.com/kdeldycke/repomatic/blob/main/claude.md) sections rather than upstream `docs/` URLs, so a downstream repo's Claude can resolve every reference locally without network access.
