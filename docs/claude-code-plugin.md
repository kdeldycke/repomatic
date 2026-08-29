# {octicon}`plug` Claude Code plugin

The [skills](agent-skills.md) and [subagents](subagents.md) this repository ships are also published as a single [Claude Code plugin](https://code.claude.com/docs/en/plugins), so you can install them once and let Claude Code keep them up to date, instead of copying files into every repository.

Both distribution paths are supported and neither replaces the other:

| Path       | Command                               | What lands on disk                                      |
| :--------- | :------------------------------------ | :------------------------------------------------------ |
| **Files**  | `repomatic init skills agents`        | A copy of every skill and agent, committed to your repo |
| **Plugin** | `/plugin install repomatic@kdeldycke` | Nothing: Claude Code caches the plugin outside the repo |

The plugin also reaches further than the files do. Claude Code discovers skills in `~/.claude/skills/` and a repository's `.claude/skills/`, but [Cowork and cloud sessions do not read either](https://code.claude.com/docs/en/skills#skills-in-cowork-and-cloud-sessions): they load what is enabled for your account. A plugin is the only form those surfaces accept, so `init skills` cannot reach them by design. See [§ Install in Claude Desktop](#install-in-claude-desktop).

## Install

Register the marketplace, then install the plugin:

```shell-session
$ claude plugin marketplace add kdeldycke/repomatic
$ claude plugin install repomatic@kdeldycke
```

The same two steps work as `/plugin marketplace add` and `/plugin install` from inside a session, where `/plugin` also offers an interactive browser.

Skills and agents arrive namespaced by the plugin, so `/repomatic-changelog` becomes `/repomatic:repomatic-changelog` and the QA agent is `repomatic:qa-engineer`.

```{important}
The marketplace reads the plugin directory straight from this repository through a `git-subdir` source, so git must be on your `PATH` (2.25 or later, for sparse-checkout cone mode). A Claude Code too old to know the type reports `This plugin uses a source type your Claude Code version does not support`, and versions before 2.1.120 fail to load the marketplace at all. Check yours with `claude --version`; 2.1.236 handles it.
```

## What it ships

Every skill and agent, and nothing else: the 17 skills listed on the [skills page](agent-skills.md) and the 3 subagents on the [subagents page](subagents.md), read straight from `.claude/skills/` and `.claude/agents/` on `main`. Those directories stay the single source of truth, so there is no second copy of any skill in the repository and what you install is what you can read there.

`.claude/` **is** the plugin: it already holds `skills/` and `agents/` at the locations the plugin spec scans, so the manifest at [`.claude/.claude-plugin/plugin.json`](https://github.com/kdeldycke/repomatic/blob/main/.claude/.claude-plugin/plugin.json) declares metadata only and no component paths:

```text
.claude/
├── .claude-plugin/plugin.json
├── agents/{name}.md
└── skills/{name}/SKILL.md
```

That directory is what the marketplace publishes and what the archive mirrors, so `claude --plugin-dir .claude` in a checkout loads exactly what an install gives you. A marketplace install shows that tree back to you, read-only:

![The installed plugin's Contents tab, listing plugin.json, three agents and seventeen skill folders](assets/desktop-plugin-contents.png)

```{warning}
Do not add an `agents` path to the manifest to publish the assets from somewhere else. `claude plugin validate --strict` accepts one and Claude Code then loads **zero** agents, with no error anywhere. This was measured against Claude Code 2.1.220: `claude plugin details` reported `Agents (0)` for the custom-path layout and `Agents (3)` for the default one.
```

## How the pin moves

The entry's two halves move differently, and the difference is the point:

- **`ref` round-trips.** A release commit's freeze writes `vX.Y.Z`; the post-release unfreeze walks it back to `main`. So `/plugin marketplace add kdeldycke/repomatic` tracks the default branch and gets each skill fix as it lands, while `/plugin marketplace add kdeldycke/repomatic@vX.Y.Z` installs exactly that release. Tags older than the release that introduced the plugin carry no marketplace entry.
- **`version` ratchets forward.** It names the last published release and is never walked back, because it is the string update detection compares. Left on a `.devN` value it would advertise a release nobody can install.

Pinning the tag everywhere would be the tidier-looking choice and is the wrong one twice over. A tag never moves, so an entry frozen to one makes the app's `Sync automatically` a no-op between releases. And a pin naming the last release breaks outright whenever the plugin's own layout changes, which is what happened when the manifest moved into `.claude/`: the previous tag's tree has no manifest for the catalog to find.

## Where the archive comes from

Every GitHub release also carries a `repomatic-claude-plugin.zip` asset, built by `repomatic pack-plugin` in the release workflow's `pack-plugin` job and attached by the release engine's [`extra-assets`](workflows.md#extra-release-assets-extra-assets) job, which also attests it. The marketplace does not fetch it: the archive is for offline installs and for the Claude Desktop upload below.

Integrity comes from the release attestation:

```shell-session
$ gh release download --pattern repomatic-claude-plugin.zip
$ gh attestation verify repomatic-claude-plugin.zip \
    --repo kdeldycke/repomatic --signer-repo kdeldycke/repomatic
```

The version Claude Code compares against your installed copy is stamped into the archive's manifest at pack time, so it always matches the release the archive came from. The checked-in manifest carries one too, written by the same freeze that moves the catalog pin, because a `git-subdir` install reads that file rather than a packed copy. Keep the version out of the archive's filename as well: the app-side upload path derives the plugin name from the uploaded filename, so a versioned name like `repomatic-claude-plugin-v7.13.0.zip` installs as a duplicate plugin instead of updating the existing one (reported for Cowork in [anthropics/claude-code#20697](https://github.com/anthropics/claude-code/issues/20697)). The release asset is version-free for this reason, with the version carried by the release tag and the manifest instead.

## Install without a marketplace

The archive is a plain plugin directory, so it works offline and without git:

```shell-session
$ gh release download --pattern repomatic-claude-plugin.zip
$ unzip repomatic-claude-plugin.zip -d ./plugins
$ claude --plugin-dir ./plugins/repomatic
```

`--plugin-dir` lasts for the session only, which also makes it the way to try a change to a skill before releasing it:

```shell-session
$ repomatic pack-plugin --output /tmp/repomatic-claude-plugin.zip
$ unzip /tmp/repomatic-claude-plugin.zip -d /tmp/plugins
$ claude plugin validate /tmp/plugins/repomatic --strict
$ claude --plugin-dir /tmp/plugins/repomatic
```

`--plugin-dir` also takes the archive itself, so checking what a build actually exposes needs no unpacking. This is the quickest way to catch a manifest that loads fewer components than intended:

```shell-session
$ claude --plugin-dir /tmp/repomatic-claude-plugin.zip plugin details repomatic
```

```{caution}
`claude plugin validate` does **not** accept an archive. Handed one, it parses the zip header as JSON and fails with `Invalid JSON syntax: JSON Parse error: Unexpected identifier "PK"`. Unpack first, as above.
```

## Wire it into a repository

`repomatic init plugin` writes the marketplace and enablement keys into your repository's `.claude/settings.json`, so collaborators are prompted to install the plugin when they trust the folder:

```shell-session
$ uvx -- repomatic init plugin
```

It merges rather than overwrites: your own `permissions`, hooks, and any other marketplace you already registered are left alone, and re-running it is a no-op. Move the destination with `[tool.repomatic] settings.location` if `.claude/` is not at your repository root, the same way [`skills.location` and `subagents.location`](configuration.md) work.

Like `skills` and `subagents`, this component is **opt-in**: a bare `repomatic init` never touches your settings. Name it explicitly, or list it under `[tool.repomatic] include`.

```{note}
Claude Code only *prompts* each collaborator to install a plugin a project declares; it never installs one on their behalf. So declaring the plugin does not guarantee every collaborator has it, which is why `repomatic init skills` and `init subagents` remain available and unchanged.
```

## Install in Claude Desktop

The same archive installs into the Claude Desktop app, covering its **Chat** tab, [Cowork](https://claude.com/product/cowork), and claude.ai. None of those read the skill directories on your machine, so the plugin is the only way to reach them.

Grab the asset from a release, then in the app open **Customize > Plugins** and choose **Add > Upload plugin**:

```shell-session
$ gh release download --repo kdeldycke/repomatic --pattern repomatic-claude-plugin.zip
```

Every skill and agent arrives in that single upload, listed under the plugin's **Skills** and **Agents** tabs and invocable by typing `/` in chat. The marketplace route below populates the same two tabs:

![The Skills tab listing seventeen slash commands](assets/desktop-plugin-skills.png)

![The Agents tab listing grunt-qa, qa-engineer and sphinx-docs](assets/desktop-plugin-agents.png)

```{note}
An uploaded plugin carries no update channel: its `⋮` menu offers `Disable` and `Remove` and nothing else, with no version shown and no way to check for one, so each new release needs a fresh upload and a stale copy announces itself nowhere. That is the cost of this route compared to the marketplace one, which Claude Code keeps current on its own.
```

The app's **Add > Add marketplace** flow takes the same `kdeldycke/repomatic` catalog, and is the better route: it carries the version, the update check and the source listing an upload has none of. It rejects an `archive` source outright, with `External plugin source type 'archive' is not supported. Supported types: git-subdir, github, url` in `~/Library/Logs/Claude/main.log` behind a bare "Marketplace sync failed" in the dialog, which is why this catalog publishes a `git-subdir` source.

Adding and installing needs no GitHub App, on a public repository. Keeping the plugin current on every push does:

![A warning reading "Auto-sync requires the Claude GitHub App to have access to this repository", with a Grant access link](assets/desktop-autosync-warning.png)

The same refusal reaches the log as `Automatic sync on push requires the Claude GitHub App to be installed on this repository`, with error code `github_repo_not_accessible`. Install it from the link in that warning, or from [https://github.com/apps/claude/installations/new](https://github.com/apps/claude/installations/new), and scope it to the repositories whose catalogs you sync. Without it the plugin still installs and still updates when you ask it to; it just will not notice a push on its own.

This supersedes the per-skill archives [`.claude/package-skills.sh`](https://github.com/kdeldycke/repomatic/blob/main/.claude/package-skills.sh) builds for the **Customize > Skills** panel: one plugin upload carries every skill *and* every agent, which separate skill archives cannot do. See [kdeldycke/repomatic#2540](https://github.com/kdeldycke/repomatic/issues/2540).
