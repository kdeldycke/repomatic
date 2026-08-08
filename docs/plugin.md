# {octicon}`plug` Claude Code plugin

The [skills](skills.md) and [agents](agents.md) this repository ships are also published as a single [Claude Code plugin](https://code.claude.com/docs/en/plugins), so you can install them once and let Claude Code keep them up to date, instead of copying files into every repository.

Both distribution paths are supported and neither replaces the other:

| Path       | Command                               | What lands on disk                                      |
| :--------- | :------------------------------------ | :------------------------------------------------------ |
| **Files**  | `repomatic init skills agents`        | A copy of every skill and agent, committed to your repo |
| **Plugin** | `/plugin install repomatic@kdeldycke` | Nothing: Claude Code caches the plugin outside the repo |

## Install

Register the marketplace, then install the plugin:

```shell-session
$ claude plugin marketplace add kdeldycke/repomatic
$ claude plugin install repomatic@kdeldycke
```

The same two steps work as `/plugin marketplace add` and `/plugin install` from inside a session, where `/plugin` also offers an interactive browser.

Skills and agents arrive namespaced by the plugin, so `/repomatic-changelog` becomes `/repomatic:repomatic-changelog` and the QA agent is `repomatic:qa-engineer`.

```{important}
Installing needs **Claude Code 2.1.224 or later**. The marketplace fetches the plugin from a zip archive, and `archive` sources are only understood from that release on. Earlier versions report `This plugin uses a source type your Claude Code version does not support`, and versions before 2.1.120 fail to load the marketplace at all. Check yours with `claude --version`.
```

## What it ships

Every skill and agent, and nothing else: the 15 skills listed on the [skills page](skills.md) and the 3 agents on the [agents page](agents.md), read straight from `.claude/skills/` and `.claude/agents/` on `main`. Those directories stay the single source of truth, so there is no second copy of any skill in the repository and what you install is what you can read there.

The archive places them at the plugin spec's default `skills/` and `agents/` directories, so the manifest at [`.claude-plugin/plugin.json`](https://github.com/kdeldycke/repomatic/blob/main/.claude-plugin/plugin.json) declares metadata only and no component paths:

```text
repomatic-plugin.zip
└── repomatic/
    ├── .claude-plugin/plugin.json
    ├── agents/<name>.md
    └── skills/<name>/SKILL.md
```

```{warning}
Do not add an `agents` path to the manifest to keep the `.claude/` layout inside the archive. `claude plugin validate --strict` accepts one and Claude Code then loads **zero** agents, with no error anywhere. This was measured against Claude Code 2.1.220: `claude plugin details` reported `Agents (0)` for the custom-path layout and `Agents (3)` for the default one. A consequence worth knowing: the repository root is not itself an installable plugin, so test a change by packing it (see below) rather than by pointing `--plugin-dir` at your checkout.
```

## Where the archive comes from

Every GitHub release carries a `repomatic-plugin.zip` asset, and the marketplace entry installs from it. The archive is built by `repomatic pack-plugin` in the release workflow's `pack-plugin` job and attached by the release engine's [`extra-assets`](workflows.md#extra-release-assets-extra-assets) job, which also attests it.

The entry's URL is pinned to a release tag, not a `latest` redirect, and **ratchets forward**: each release's freeze commit rewrites it to that release's tag and nothing walks it back. Two consequences worth knowing:

- The default branch always names the newest *published* release, so `/plugin marketplace add kdeldycke/repomatic` is always installable.
- Adding the catalog at a tag installs that tag's plugin: `/plugin marketplace add kdeldycke/repomatic@vX.Y.Z` gives you `vX.Y.Z`'s skills and agents, which a moving redirect could not offer. Tags older than the release that introduced the plugin carry no marketplace entry.

The entry carries no `sha256` pin. Integrity comes from the release attestation instead:

```shell-session
$ gh release download --pattern repomatic-plugin.zip
$ gh attestation verify repomatic-plugin.zip \
    --repo kdeldycke/repomatic --signer-repo kdeldycke/repomatic
```

The version Claude Code compares against your installed copy is stamped into the archive's manifest at pack time, so it always matches the release the archive came from.

## Install without a marketplace

The archive is a plain plugin directory, so it works offline and without git:

```shell-session
$ gh release download --pattern repomatic-plugin.zip
$ unzip repomatic-plugin.zip -d ./plugins
$ claude --plugin-dir ./plugins/repomatic
```

`--plugin-dir` lasts for the session only, which also makes it the way to try a change to a skill before releasing it:

```shell-session
$ repomatic pack-plugin --output /tmp/repomatic-plugin.zip
$ unzip /tmp/repomatic-plugin.zip -d /tmp/plugins
$ claude plugin validate /tmp/plugins/repomatic --strict
$ claude --plugin-dir /tmp/plugins/repomatic
```

## Wire it into a repository

`repomatic init plugin` writes the marketplace and enablement keys into your repository's `.claude/settings.json`, so collaborators are prompted to install the plugin when they trust the folder:

```shell-session
$ uvx -- repomatic init plugin
```

It merges rather than overwrites: your own `permissions`, hooks, and any other marketplace you already registered are left alone, and re-running it is a no-op. Move the destination with `[tool.repomatic] settings.location` if `.claude/` is not at your repository root, the same way [`skills.location` and `agents.location`](configuration.md) work.

Like `skills` and `agents`, this component is **opt-in**: a bare `repomatic init` never touches your settings. Name it explicitly, or list it under `[tool.repomatic] include`.

```{note}
Claude Code only *prompts* each collaborator to install a plugin a project declares; it never installs one on their behalf. So declaring the plugin does not guarantee every collaborator has it, which is why `repomatic init skills` and `init agents` remain available and unchanged.
```

## Not the same as a Desktop skill upload

Claude Desktop's **Settings > Customize > Skills** panel takes one ZIP per skill, not a plugin. [`.claude/package-skills.sh`](https://github.com/kdeldycke/repomatic/blob/main/.claude/package-skills.sh) produces those, and its header documents why the two cannot be merged. See [kdeldycke/repomatic#2540](https://github.com/kdeldycke/repomatic/issues/2540).
