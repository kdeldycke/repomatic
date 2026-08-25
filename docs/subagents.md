# {octicon}`dependabot` Subagents

This repository includes [subagents](https://code.claude.com/docs/en/sub-agents) that run quality assurance checks against the repository. Unlike skills, which a user invokes as a slash command, a subagent is auto-invoked by the agent from its `description:` frontmatter when the current task matches its role.

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

These same subagents are also published as a Claude Code plugin, which installs them without committing any copy to your repository: see [§ Claude Code plugin](claude-code-plugin.md).

To deploy them to a non-default directory (like a dotfiles repository where `.claude/` is not at the root), set `subagents.location` in `[tool.repomatic]`:

```toml
[tool.repomatic]
include = ["subagents"]
subagents.location = "./dotfiles/.claude/agents/"
```

Where they land by default follows [`[tool.repomatic.flavor] agent`](configuration.md), so the layout is the agent's, not a fixed `.claude/` path.

## Available subagents

| Subagent                                                                                        | Role                                                                                                             |
| :---------------------------------------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------------------- |
| [`grunt-qa`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/grunt-qa.md)       | Hands-on worker that fixes typos, ordering, style, doc-sync issues, and other mechanical `claude.md` violations. |
| [`qa-engineer`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/qa-engineer.md) | Senior engineer that handles deep code analysis, bug-class sweeps, and design decisions.                         |
| [`sphinx-docs`](https://github.com/kdeldycke/repomatic/blob/main/.claude/agents/sphinx-docs.md) | Documentation steward that keeps `docs/` in sync with code and enforces MyST and click-extra conventions.        |

## Self-containment

Like skills, subagents must be self-contained for downstream portability. They reference [`claude.md`](https://github.com/kdeldycke/repomatic/blob/main/claude.md) sections rather than upstream `docs/` URLs, so a downstream repository's agent resolves every reference locally, with no network access.
