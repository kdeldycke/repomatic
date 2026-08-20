# {octicon}`dependabot` Claude Code agents

This repository includes [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) that run quality assurance checks against the repository. Unlike skills (which are user-invoked slash commands), agents are auto-invoked by Claude based on their `description:` frontmatter when the current task matches their role.

Downstream repositories can install them with:

```shell-session
$ uvx -- repomatic init subagents
```

To install a single subagent:

```shell-session
$ uvx -- repomatic init subagents/grunt-qa
```

Selectors use the same `component[/file]` syntax as the `exclude` config option in [`[tool.repomatic]`](configuration.md).

```{note}
The component is `subagents`, not `agents`: the name follows what it ships, one folder per subagent, and keeps it distinct from the agent runtime reading those definitions.
```

These same subagents are also published as a Claude Code plugin, which installs them without committing any copy to your repository: see [§ Claude Code plugin](plugin.md).

To deploy them to a non-default directory (like a dotfiles repository where `.claude/` is not at the root), set `subagents.location` in `[tool.repomatic]`:

```toml
[tool.repomatic]
include = ["subagents"]
subagents.location = "./dotfiles/.claude/agents/"
```

## Available agents

| Agent                                                                                           | Role                                                                                                           |
| :---------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------- |
| [`grunt-qa`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/grunt-qa.md)       | Hands-on worker that fixes typos, ordering, style, doc-sync issues, and other mechanical CLAUDE.md violations. |
| [`qa-engineer`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/qa-engineer.md) | Senior engineer that handles deep code analysis, bug-class sweeps, and design decisions.                       |
| [`sphinx-docs`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/sphinx-docs.md) | Documentation steward that keeps `docs/` in sync with code and enforces MyST and click-extra conventions.      |

## Self-containment

Like skills, agents must be self-contained for downstream portability. They reference [`claude.md`](https://github.com/kdeldycke/repomatic/blob/main/claude.md) sections rather than upstream `docs/` URLs, so a downstream repo's Claude can resolve every reference locally without network access.
