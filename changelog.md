# Changelog

## [`7.15.0.dev0` (unreleased)](https://github.com/kdeldycke/repomatic/compare/v7.14.0...main)

> [!WARNING]
> This version is **not released yet** and is under active development.

- **Breaking:** the `manpages` release job now renders through `click-extra wrap --help-format man`, so a project opting into man pages needs `click-extra >= 9`. The `--man` flag it used no longer writes roff.
- **Breaking:** `show-test-matrix` now lists one row per job by default, and `--flat` is gone. Pass the new `--grid` for the compact two-axis pivot the command used to print.
- `lint-repo` gains a `manpages-toolchain` check, warning when a project opting into man pages locks a `click-extra` its release job cannot render with.
- `show-test-matrix --row-axis` and `--col-axis` now list the job keys of your own matrix in `--help`, and complete them in the shell.
- Add a `runner_arch` metadata key, mapping every runner label in the full test matrix to its CPU architecture.
- `pr-body --part` and `pr-sync --part` now accept only `minor` or `major`, and name both in `--help`.
- `update-dep-graph --group`, `--no-group` and `--only-group` now list the project's own `[dependency-groups]` in `--help`, and their `--extra` counterparts its `[project.optional-dependencies]`.
- `ci-status --workflow` and `job-timings --workflow` now accept only a workflow the repository has, and complete them in the shell.
- The `scan-virustotal` pull request is now titled `Update released binaries database`, with a one-line body replacing its scan-records commentary.
- The `sync-runner-images` pull request body is now shorter and uses simpler sentences.
- Trim the prose of the `bump-version`, `detect-squash-merge`, `prepare-release`, `sample-metrics`, `setup-guide-cloudflare-pages`, `setup-guide-token` and `sync-dep-sources` templates.
- `sync-runner-images` now rewrites a literal `runs-on:` as soon as a newer image supersedes it, instead of waiting for the old one to be deprecated.
- The Claude Code plugin marketplace now installs `.claude/` through a `git-subdir` source, which Claude Desktop and Cowork accept where the previous `archive` source failed to sync.
- The marketplace entry now tracks the default branch between releases, so an installed plugin picks up skill fixes as they land instead of at the next release. Adding the catalog at a tag still installs that release.
- The plugin manifest moved to `.claude/.claude-plugin/plugin.json`, so `claude --plugin-dir .claude` loads the plugin straight from a checkout.
- Help screens now list each section's subcommands alphabetically.
- Render each command's worked examples in a dedicated `Examples` section of its help screen and man page, with the command lines highlighted.
- Illustrate the readme with a capture of the CLI help screen, refreshed by the `update-docs` lane and following the reader's color scheme.
- Animate the readme's `sync-deps --dry-run` example with a recorded terminal session.
- Illustrate the docs with a capture of the test-matrix grid and a syntax-highlighted configuration example.
- Fix `repomatic run` printing a Python traceback for an unknown tool name. It is now a usage error listing every registered tool.
- Fix the `plugin` component rewriting a settings file `format-json` had already settled, which kept `sync-repomatic` and the autofix lane undoing each other. The wiring now compares the parsed document, not its text.
- Fix the generated `release.yaml` ending on a stray blank line, alone among the workflow callers.
- Fix `lint-repo` asking for GitHub topics a project already declares: topics now match `[project] keywords` case-insensitively, since GitHub lowercases every topic it stores.
- Fix `sync-runner-images` proposing a probe for a runner image the repository already runs, which marked every test-matrix cell on that image `continue-on-error`.
- Fix `run {tool} --verify` crashing with a `FileExistsError` on a path resolving to the working directory, which now verifies the tool's own default file set.

## [`7.14.0` (2026-08-27)](https://github.com/kdeldycke/repomatic/compare/v7.13.0...v7.14.0)

> [!NOTE]
> `7.14.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.14.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.14.0).

- **Breaking:** the `agent` component is gone, with the `agent.location` config key and the bundled `claude.md` reference document. `repomatic init` no longer writes to a repository's instructions file, which each repository now owns outright.
- **Breaking:** `CLOUDFLARE_ACCOUNT_ID` is gone: the account derives from `CLOUDFLARE_API_TOKEN` alone. `cloudflare-pages --account-id` and `lint-repo --has-cloudflare-account-id` are removed with it.
- **Breaking:** the `debug.yaml` workflow is now opt-in, gated by a new `[tool.repomatic] debug.sync` key. A repository already carrying it keeps it by setting `debug.sync = true`.
- **Breaking:** the `arch` field of `repomatic metadata`'s `build_targets` and `nuitka_matrix` output now reads `x86_64` and `aarch64`, not `x64` and `arm64`. Update any workflow branching on `matrix.arch`.
- **Breaking:** `build_targets` and `nuitka_matrix` replace the `glibc_floor` and `min_os` keys with a single `floor`, holding the same versions. A workflow reading `matrix.min_os` needs `matrix.floor`.
- `lint-repo` gains two checks: `setup-uv-checksum-coverage`, warning when CI installs the pinned uv unverified, and `classic-branch-protection`, reporting a branch protection rule left beside a ruleset.
- `show-test-matrix` gains `--row-axis`, `--col-axis` and `--flat`, laying the grid out on any job key the matrix carries or listing one row per job.
- `show-test-matrix` states how many jobs a cell stands for (`✅ stable ×5`), and sorts its Python rows by release instead of job order.
- `scan-virustotal` and `sample-metrics` publish through one long-lived pull request that each run appends to, instead of committing to the default branch. No file-modifying job pushes there any more.
- Both gain `--carry-from`, restoring the store from a remote branch first, so records pending in an open pull request are appended to rather than dropped.
- `sample-metrics` names the phase (`forward`, `reconstruct`, `import` or `wayback`) in a new column of its report table, so one subject reads as one outcome per phase.
- `cloudflare-pages --create` is now idempotent: an existing Pages project is reused and reconciled against the declared settings, instead of failing on the API's `409` duplicate-name refusal.
- The Todo list documentation page lists the project's pending work: each deferred change is written as a `todo` admonition beside the code or prose it acts on.
- The `repomatic-ship` skill now sweeps those admonitions: pending work the cycle introduced is written as one, and a todo whose upstream trigger fired is retired with the shim it guards.
- `metadata` and every glob-driven check now walk the repository tree once and skip `.git/`, instead of re-walking it on every lookup.
- `ci-status` locates every workflow's newest run through one branch-wide listing instead of one query per workflow.
- Every HTTP fetch now identifies itself with a `repomatic/{version}` user agent.
- `sync-workflow-pins` no longer bumps the uv pin past what the pinned `astral-sh/setup-uv` can checksum-verify: the action silently skips validation for a version its bundled table does not list.
- `show-config` wraps its widest columns instead of running past 240 characters. A format unable to hold a wrapped cell, like `github` or `csv`, stays unwrapped.
- `list-skills` renders a table honoring `--table-format`, replacing the hand-padded lines that ran to 500 characters.
- `sync-runner-images` takes `--live` like every other command with a dry-run mode, replacing its odd `--no-dry-run` spelling.
- Dependency-update pull requests no longer list yanked or pre-release versions in their intermediate release notes.
- The bundled `[tool.typos]` config no longer checks SVG content: a terminal capture splits words across `<text>` runs, so correcting a fragment corrupts the image.
- The `lint-repo` stale gh-pages check now passes only on a confirmed `404`: any other API failure reports as skipped instead of green.
- Fix `lint-repo` flagging every `test-matrix.exclude` entry as stale on a `full-include` matrix, and the finding now names the missing axis values.
- Fix `lint-deps` missing a dependency floor declared with `>` when checking floors against the cooldown window.
- Fix `lint-deps` asking an aggregate extra selecting the project's own extras for a version floor, which a project can never declare on itself.
- Fix `metadata` leaving the checkout on a past commit with changes stashed when a mid-scan git command fails.
- Fix the `sample-metrics` chart crashing on a series whose peak is `0` or `1`.
- Fix `sample-metrics` abandoning the tail of a sampling pass to GitHub's secondary rate limit: the `gh` plumbing now retries the refusal on a bounded backoff.
- `sample-metrics --backfill-wayback` gives up after ten consecutive refusals and reports to retry later, instead of spending every remaining capture's retry schedule against an exhausted per-IP budget.
- Fix `show-test-matrix` leaving a cell bare when several jobs share it: every state of a `stable, unstable` cell now carries its own glyph.
- Fix the `plugin` component writing tab-indented settings into a repository whose Biome config asks for spaces, which had `sync-repomatic` and `format-json` fighting each other. The indent now follows that config.
- Fix `init` leaving behind the folder of a removed skill whose `SKILL.md` was already gone: the tombstone addresses the file, so an empty folder outlived every later run.
- Fix `format-images` dropping its optimization summary table from the PR body, lost in `7.11.0` when its two PR-publishing steps collapsed into one.
- The skills, subagents and plugin documentation pages group under an Agent tooling section and move to `/agent-skills`, `/subagents` and `/claude-code-plugin`, with the old URLs redirecting.
- The generated API reference moves off the guide pages and splits one page per module (`/repomatic.awesome_toc`), instead of stacking a whole package onto one page. The guide pages keep their prose and link to it.
- The Python compatibility table of the install page marks a version a release neither declared nor excluded with `–`, instead of counting it as unsupported.
- The `commit-messages` page and the bundled skills now hold a commit body to three cases: bundled orthogonal work, a link to a public record, or a `Closes #N` pointer.
- Fix the `exclude` example of the configuration page naming `zizmor`, which is not a component: copying it made `repomatic init` fail.
- Fix stale guidance in the bundled skills: two module pointers the regroup broke, the scan history now published through a pull request, and a retired upstream contribution target.
- Fix the dead `claude.md` section cross-references the bundled skills and subagents carried, and guard against new ones.
- Sphinx linkcheck no longer reports every release-asset download URL as broken: `conf.py` withholds its github.com credential from those URLs, which GitHub redirects to a host that answers 401.
- Fix the dead links on the install and operation-contracts pages, and the API reference documenting every regrouped module twice.

## [`7.13.0` (2026-08-17)](https://github.com/kdeldycke/repomatic/compare/v7.12.1...v7.13.0)

> [!NOTE]
> `7.13.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.13.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.13.0).

- **Breaking:** the `agents` component is renamed `subagents`, its config key `agents.location` becoming `subagents.location`. Naming the old component in `[tool.repomatic] include` or `exclude` fails the init; the old config key is ignored with a warning.
- **Breaking:** `repomatic init claude` is renamed `repomatic init agent`, and its destination follows `[tool.repomatic] agent.location` (defaulting per `[tool.repomatic.flavor] agent`) rather than a fixed root-level `claude.md`, reaching instructions kept as `AGENTS.md` or outside the root.
- **Breaking:** the `runner-images` command, its workflow job and its `[tool.repomatic] runner-images` config key are gone, along with the issue they maintained; `sync-runner-images` closes that issue on its first run.
- **Breaking:** the Cloudflare Pages deploy needs only `CLOUDFLARE_API_TOKEN`: a token scoped to `Cloudflare Pages: Edit` resolves its own account, so `CLOUDFLARE_ACCOUNT_ID` becomes an override for a credential spanning several accounts; `cloudflare-pages --account-id` prints the identifier for that case.
- **Breaking:** the VirusTotal scan history moves from `docs/assets/virustotal-scans.json` to `virustotal-scans.csv`. The first `scan-virustotal` run after upgrading converts the old JSON on its own; delete the stale `.json` afterwards.
- New `[tool.repomatic] site.deploy` config choosing where the built site publishes: `github-pages`, the default, or `cloudflare-pages` through `wrangler pages deploy`, with `site.cloudflare-project` naming a Pages project that predates the repository.
- New `cloudflare-pages` command reconciling the live Pages project against the declared `site.*` state: `--check`, `--apply`, `--dump`, and `--create` for rebuilding from nothing. Warns a month before the API token expires, which Cloudflare never signals.
- New `cloudflare-pages --attach-domain` serving a project at a custom domain, creating the proxied `CNAME` record the bare API leaves missing.
- New `cloudflare-config-drift` job in the Docs workflow running that check apart from the deploy, so drift is loud without holding up publishing.
- `lint-repo` now fails a committed `_redirects` file that would lose rules to the Cloudflare Pages engine's silent budget accounting, naming each URL a dropped rule leaves dead or silently misredirected.
- `lint-repo` now warns when `wrangler.toml` contradicts the declared Cloudflare Pages project name or compatibility date, and checks a migrated site keeps its GitHub Pages custom domain pointing at the new host so published `github.io` URLs keep redirecting.
- The Cloudflare deploy job now drops files over Direct Upload's 25 MiB per-file limit, naming each in the log, instead of failing the whole upload on the first one.
- The Docs workflow now runs monthly, mirrored into downstream callers: a lapsed Cloudflare token becomes a red run, and link rot surfaces between pushes.
- The Docs workflow no longer publishes Sphinx's parse cache: doctrees now build in the runner's temp directory instead of riding the Pages upload, 118 MB of a 182 MB artifact on an autodoc-heavy project.
- Every setup guide step is rewritten to the actions it asks for; the guide and `lint-repo` now ask about whichever host `site.deploy` names, and the Cloudflare step hands out a pre-filled account-owned token form.
- New `sample-metrics` command recording what forges say about the repositories a project tracks into one committed CSV: star count, newest release or tag, and newest commit, read from GitHub, GitLab and Forgejo instances.
- `sample-metrics` renders the accumulated history as themeable SVG charts, replacing the star-history embeds GitHub's 2026 stargazer restriction broke. Star history recovers from per-star timestamps, archived pages with `--backfill-wayback`, or a star-history.com export with `--import-csv`.
- New `[tool.repomatic.metrics] charts` option `scale = "logarithmic"`, measuring the vertical axis in powers of ten so series orders of magnitude apart stay legible on one chart.
- New opt-in `metrics.yaml` workflow running the sampler weekly and committing its store. Enable with `[tool.repomatic] metrics.sync = true` and declare the repositories to follow in `[tool.repomatic.metrics] subjects`.
- New `sync-runner-images` command and weekly job opening a pull request that moves a deprecated runner image onto its successor, or probes a strictly newer one as `continue-on-error`. Decline a proposal for good with `[tool.repomatic.sync-runner-images] ignore`.
- New `job-timings` command reporting median whole-job wall-clock per runner image, read from recent successful runs.
- `lint-deps` now warns about a dependency floor comment running past `[tool.repomatic] lint-deps.comment-word-threshold` words, 40 by default.
- New `lint-anchors` command checking every authored same-page fragment link against the anchors the built site actually carries, run by the Docs workflow before each deploy.
- New `pr-body` and `pr-sync` option `--template-arg-file KEY=PATH`, reading a template value from a file for a value with no ceiling on its size.
- New `git-commit-push` option `--all-changes`, staging everything the working tree carries instead of a named path list.
- New bundled `repomatic-test-matrix` skill deciding which Python versions, operating systems and runner images earn a test-matrix cell.
- Bundled guidance moves its release-repair, label-retirement and test-matrix procedures out of `claude.md` and into the skills that run them, leaving the always-loaded file carrying rules rather than checklists.
- The bundled `repomatic-ship` skill now names all three shapes an unanswered hardware-key signing prompt produces, treating a silent hang as a missed prompt rather than a slow command.
- The bundled `sphinx-docs` agent's page roster gains the `plugin`, `commit-messages` and `history` pages.
- The release PR's unshippable-dependency warning is now a single line naming each package, each linked to the line declaring it, instead of a paragraph and a four-column table.
- The binaries catalog now leaves the VirusTotal cell empty unless a scan record backs it, instead of linking every binary to an analysis page that was never created.
- The bundled `[tool.typos]` config now accepts `PNGs` and `PATCHed`, which typos otherwise splits and rewrites to `ONGs` and `PATCHead`.
- `repomatic init` no longer drops the comment documenting a bundled template's first key: six of the nine templates were losing their opening comment, the `[tool.typos]` proper-noun map among them.
- Fix `ci-status --help` collapsing everything after its first example into one garbled line.
- `update-dep-graph` now reads the root's direct dependencies from `uv.lock`: uv's SBOM export promoted a dependency-group package to a primary dependency whenever an extra pulled it in transitively.
- New Cloudflare Pages documentation page covering Direct Upload, token scoping and rotation, the drift check, and the redirects engine's undocumented accounting. It opens on a runbook taking a bare domain name to a published site in seven commands.
- This project's documentation moves to [repomatic.net](https://repomatic.net), and its links move with it: every reference now names the extensionless URL Cloudflare Pages serves. The old `kdeldycke.github.io/repomatic` addresses keep redirecting.

## [`7.12.1` (2026-08-15)](https://github.com/kdeldycke/repomatic/compare/v7.12.0...v7.12.1)

> [!NOTE]
> `7.12.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.12.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.12.1).

- New `[tool.repomatic] sphinx.builder` config choosing the Sphinx builder the Docs workflow deploys, so a project can publish extension-less URLs with `dirhtml`.
- The bundled `repomatic-ship` skill now reads a green CI run as stale when supersession cancelled every run between it and `HEAD`, and ships a release's own reflection findings in that same release.
- Bundled guidance now requires a duration baseline and per-job timestamps before calling a CI run hung, and `repomatic-ship` no longer holds a green release waiting for a binary build.
- Fix stale guidance in the bundled agents and skills: the `update-docs` job credited to the wrong workflow, Furo logo and OpenGraph settings contradicting a working configuration, and a `repomatic` invocation that fails outside the canonical repository.
- New `repomatic init claude` component projecting the audience-tagged sections of the bundled `claude.md` into a repository, leaving every section that repository wrote for itself untouched.
- Bundled guidance now declares which repositories each `claude.md` section applies to, and documents what a repository consuming repomatic owns: its workflow content, its pin, and its configuration.
- The bundled `repomatic-audit` skill now reads those tags to tell a synced section from a repository's own, instead of classifying each one by hand.
- Fix two stale claims in the bundled guidance: the workflow permissions contract is generated rather than hand-written, and PAT-gated `lint-repo` checks report as skipped rather than failing the job.

## [`7.12.0` (2026-08-14)](https://github.com/kdeldycke/repomatic/compare/v7.11.0...v7.12.0)

> [!NOTE]
> `7.12.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.12.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.12.0).

- New `ci-status` command reporting each workflow's latest run and which of its failing jobs gate a merge, read from jobs rather than run conclusions.
- New `repomatic run <tool> --verify` reporting which files a formatter would rewrite, without touching the working tree.
- `repomatic run` now resolves a tool's targets itself when given no arguments, and skips a tool with no matching file instead of invoking it pathless.
- New repeatable `pr-sync --add-path` option limiting what the pull request commits to a git pathspec list.
- `lint-deps` now warns about declarations departing from version policy: upper bounds, missing floors, unsorted lists, misplaced type stubs, uncommented floors. Disabled with `--no-policy`.
- `lint-deps` now names the `lint-deps.allow` exemption in the remedy it prints for a git source.
- `sync-workflow-pins` now backfills a missing `--exclude-newer-package` exemption on a run that moves no version, instead of computing the splice and discarding it.
- `lint-repo` now fails when an inline upstream pin resolving under a cooldown carries no `--exclude-newer-package` exemption, which takes every `needs: metadata` job down with it.
- `lint-repo` now warns when an `astral-sh/setup-uv` step declares no `version:` input, or when steps across the repository pin more than one uv version.
- `lint-repo` now fails, and `repomatic init` now warns, when a workflow asks `repomatic metadata` for a key that no longer exists.
- `lint-repo` now warns when a Sphinx project's GitHub website field differs from the documentation URL declared in `[project.urls]`.
- `lint-changelog` now warns about a released section holding no entry.
- `runner-images` now watches every image the workflows name literally, not just the curated test axes.
- A bare `repomatic init` now re-syncs a tool config the repository already carries, so `[tool.typos]`, `[tool.uv]` and `[tool.bumpversion]` follow the bundled template once adopted.
- Every job now caps its runtime with `timeout-minutes`, so a hung job frees its runner in minutes instead of the platform's 6-hour ceiling. Downstream callers inherit the caps.
- The Docs workflow now triggers on `readme.*.md`, so a push touching only a translated readme no longer skips link checking.
- `repomatic run actionlint` now ships a bundled config declaring the `ubuntu-26.04` runner labels actionlint `1.7.12` predates.
- The bundled `lychee.toml` now excludes `bitdefender.com`, `npmjs.com`, `star-history.com` and `githubstatus.com`, which answer bots with 403, 405 or JavaScript rather than a link.
- `cancel-runs` now spares a run whose head commit carries `[changelog] Release`, so a sweep of the default branch cannot kill a release matrix.
- Fix `gh` re-downloading on every command instead of once per version: a binary nested in an archive subdirectory was stored under one cache key and looked up under another.
- Fix `repomatic init` realigning a workflow's inline `repomatic==X.Y.Z` pin without the cooldown exemption beside it, leaving a command that cannot resolve the version just written.
- Fix `audit --fix` dirtying `uv.lock` with a cooldown-override record when no upgrade was reachable, which opened a `fix-vulnerable-deps` pull request carrying no fix.
- Fix `repomatic init` restyling a locally added array item when it re-syncs a tool config, which had `sync-repomatic` and `format-pyproject` endlessly opening pull requests undoing each other.
- A formatter exiting with its rewrite status while leaving every target unchanged is now reported as a crash, instead of passing for a successful reformat that never happened.
- Fix stale guidance across the bundled skills: wrong `[tool.repomatic.workflow]` key names, wrong workflow and job names for `lint-deps` and `lint-changelog`, which tools a bundled default actually covers, and downstream audits comparing against `main` rather than the adopted release.
- Fix the bundled `babysit-ci` skill classifying a CI job by a leading `✅`, which dropped every required job whose name carries no stability glyph from the failures it collects.

## [`7.11.0` (2026-08-13)](https://github.com/kdeldycke/repomatic/compare/v7.10.0...v7.11.0)

> [!NOTE]
> `7.11.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.11.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.11.0).

- **Breaking:** `labels.content-rules` and `labels.file-rules` are now tables mapping each label to its patterns, like `"📚 docs" = ["docs/**"]`. The array-of-tables form and its `actions/labeler` v5 matcher schema are gone; an un-migrated config is ignored with a warning.
- **Breaking:** the `file-labeller` and `content-labeller` jobs merged into a single `apply-labels` job. Update any required check or `needs:` edge naming them.
- **Breaking:** `repomatic init` and `repomatic workflow lint` take `--upstream-repo` for the upstream toolkit; `--repo` now means the `owner/name` slug everywhere, including on `sync-labels`.
- New `pr-sync` command replacing `peter-evans/create-pull-request` in every job: opens, refreshes or retires an automation PR from one `--template` flag. Its PR closes even when the job's `if:` gate skips the rest of the step.
- New `lock-threads` command replacing `dessant/lock-threads` in the autolock job. It skips threads carrying the `🤖 ci` label, so an issue repomatic maintains is no longer locked out of reopening when its condition recurs.
- New `apply-labels` command replacing `actions/labeler` and `github/issue-labeler` in the labeller job. A rule's entry overrides the bundled default for that label, and an empty list disables it.
- The debug job's context dump no longer runs `crazy-max/ghaction-dump-context`, and no longer installs `cgroup-tools` and `cpuid` to read them. It now reports the kernel, disk, CPU and memory of every runner using only what the image already ships.
- A bare content pattern is now a keyword, matched case-insensitively on word boundaries, so `"🐛 bug" = ["bug", "error"]` works as written; the `/…/flags` form passes a regex through. Keyword lists used to be AND-joined and never fired.
- `repomatic init labels` no longer writes `.github/labeller-content-based.yaml` or `.github/labeller-file-based.yaml`: the default rules live in the package, and `repomatic init` now prunes an unmodified committed copy automatically. Move customizations to `[tool.repomatic.labels]`; a hand-edited copy is left for review instead of deleted.
- The `sponsor-label` command now applies the `💖 sponsor` label the registry defines, instead of a plural variant that exists in no repository and failed every labelling attempt.
- `pr-sync` now clears a conversation lock standing in the way of retiring a stale automation PR, instead of dying on the refused close comment.
- `sync-labels` now hands labelmaker the canonical token (`REPOMATIC_PAT`, then `GH_TOKEN`, then `GITHUB_TOKEN`), so an environment carrying only the PAT syncs authenticated.
- Every re-lock (`sync-dep-sources`, `audit --fix`) now passes the project's own `[tool.uv] exclude-newer` explicitly, so CI's ambient `UV_EXCLUDE_NEWER` can no longer retime the lock.
- `lint-repo` now runs the branch-ruleset and immutable-releases checks it already defined but never invoked.
- `apply-labels` no longer applies `💖 sponsor` from the words "funding" or "sponsor", or from a pull request touching `.github/funding.yml`: only `sponsor-label` sets it, from actual sponsorship.
- Every `gh` call now runs the registry-pinned, checksum-verified binary, falling back to `$PATH` with a warning when it cannot be installed.
- Test jobs running an unreleased Python are now titled `py3.15-dev`, so a `continue-on-error` cell states why it is allowed to fail.

## [`7.10.0` (2026-08-12)](https://github.com/kdeldycke/repomatic/compare/v7.9.0...v7.10.0)

> [!NOTE]
> `7.10.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.10.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.10.0).

- **Breaking:** the `update-dep-graph` job moved from `autofix.yaml` into the release engine, its only firing moment being a release push. A required check or `needs:` edge naming it under Autofix must follow.
- **Breaking:** manual version-bump commits now read `` [changelog] Bump minor version to `vX.Y.0` ``: every version-machinery commit carries the `[changelog] ` prefix, and anything matching the old unprefixed titles must follow.
- **Breaking:** the default test matrix moved to `ubuntu-26.04-arm` and `ubuntu-26.04`, retiring `ubuntu-slim`: every job now runs on a test-matrix runner. Pin the old images back with `test-matrix.replace.os = { "ubuntu-26.04-arm" = "ubuntu-24.04-arm" }`.
- **Breaking:** a report emitted with `--output-format github-actions` now travels as a file: the step output is named `<key>_file` and holds a path, so a large report reaches a PR body intact. A workflow reading `steps.<id>.outputs.diff_table` must read `.diff_table_file` instead.
- New `lint-deps` command and release-lane job, blocking a release whose dependencies do not all resolve from PyPI: git branches, forks, local paths, direct URLs and private indexes. Exempt a package with `[tool.repomatic] lint-deps.allow`.
- New `runner-images` job and CLI command, opening an issue that lists GitHub's open runner-image announcements and flags the ones retiring an image the repo runs on. Opt out with `runner-images = false`.
- New `is_python_package` metadata key. `sync-bumpversion` now gates on it instead of `is_python_project`, so the job no longer opens `[tool.bumpversion]` PRs against a uv virtual project (`[tool.uv] package = false`).
- New `--prefix-file` option on `pr-body`, and a matching `GHA_PR_BODY_PREFIX_FILE` variable, reading the body prefix from a file.
- Autofix jobs no longer run on version-bump pushes, which only re-checked machine-generated commits; drift stays covered by the next push and the weekly sweep.
- `repomatic init` no longer writes the running version's workflow content beside a pin the cooldown held back: a repository that already carries workflows keeps them untouched until the release is adopted.
- The `repomatic-ship` skill now reconciles bundled skills and agents as a third pass, judges a false-positive autofix PR against current `main`, and distinguishes a superseded intra-cycle measurement from a genuine contradiction.
- macOS Nuitka builds now hit their compile cache: ccache hashes paths relative to the runner root (`base_dir`), so uv's randomly-named cache path no longer changes the hashed compiler arguments every run.
- The tool runner now retries a download up to 3 times on transient network failures, instead of failing the job on a one-off TLS or truncation error.
- Standalone binary tests now run for every healthy target when a sibling build fails, instead of being skipped wholesale.
- Oversized step outputs are trimmed instead of killing the step that reads them with `Argument list too long`.
- The setup guide issue now reopens when Actions SHA pinning is turned off, instead of closing while reporting every repository setting complete.

## [`7.9.0` (2026-08-10)](https://github.com/kdeldycke/repomatic/compare/v7.8.0...v7.9.0)

> [!WARNING]
> The `windows-x64` binary shipped without its `.attestation.json` sidecar: a transient TLS failure on the runner skipped the upload, and immutable releases lock the asset list. The attestation itself is registered, so `gh attestation verify repomatic-7.9.0-windows-x64.exe --repo kdeldycke/repomatic` still verifies against GitHub's attestation service.

> [!NOTE]
> `7.9.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.9.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.9.0).

- **Breaking:** `REPOMATIC_PAT` now requires `Administration: Read-only`, and `lint-repo` fails without it. Regenerate the token with the setup guide's pre-filled link; steps that cannot be verified now say so instead of vanishing.
- New `[tool.repomatic] nuitka.dev-targets` option: an ordinary push now compiles binaries only for a canary subset, `["linux-arm64"]` by default. Release commits, a new weekly schedule and manual dispatches build the full 6-target fleet.
- `lint-repo` now also verifies versionless `releases/latest/download` URLs in `docs/install.md`, catching a renamed asset that leaves the guide pointing at a 404.
- `sync-workflow-pins` now walks `npx pkg@1.2.3` version literals forward, not just `npm install` and `uvx` pins.
- `repomatic init` now realigns a workflow's inline `repomatic==X.Y.Z` literal onto the pin it writes into the `uses:` refs, in either direction.
- Nuitka compile caches now persist across runs, so a warm build skips most of the C compilation. Release commits and macOS builds opt out, so no published binary is influenced by cached objects.
- Compile, binary-test and VirusTotal-scan jobs now carry execution timeouts, so a hung job frees its runner slot instead of squatting it for 6 hours.
- Scheduled and manually dispatched release runs get their own concurrency group, so a later push no longer cancels them mid-build.
- Binary self-tests on non-release pushes now run inside the compile job; the standalone per-target test jobs run on release commits only.
- `sync-gitignore` no longer discards rules added by hand to the committed `.gitignore`: it refuses to write when the rebuild would lose one, naming the rules. New `--drop-orphans` flag confirms an intended loss.
- The setup guide no longer asks a uv virtual project to register a PyPI Trusted Publisher, and `lint-repo` no longer checks one: both now gate on whether the project builds a distributable.
- Fix the broken-links issue never being filed: `docs.yaml` ran lychee through `xargs`, which reported its "broken links found" exit status as a crash.
- A `Bad credentials` 401 now fails immediately instead of retrying a token that cannot recover, so a revoked `REPOMATIC_PAT` reports itself plainly rather than as a chain of unrelated check failures.
- New [Nuitka compilation guide](https://kdeldycke.github.io/repomatic/nuitka.html) covering build targets, fleet cadence, compile caching, the LTO stance, and upstream workarounds.
- New *Build backends* section in the packaging guide, covering the `[tool.setuptools]` package-discovery shim a distribution needs when it builds a `uv-build` project with setuptools.
- The documentation sidebar logo now follows Furo's light and dark toggle, so the wordmark no longer renders near-black against the dark theme.

## [`7.8.0` (2026-08-09)](https://github.com/kdeldycke/repomatic/compare/v7.7.0...v7.8.0)

> [!NOTE]
> `7.8.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.8.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.8.0).

- **Breaking:** the Codecov integration is removed, along with the `codecov` component, its `.github/codecov.yaml`, and the `coverage_cells` metadata key. `repomatic init` prunes an untouched orphaned config; delete the `CODECOV_TOKEN` secret and coverage badge by hand.
- **Breaking:** attestation bundles now carry the full filename of the asset they attest, so `repomatic-manpages.attestation.json` becomes `repomatic-manpages.tar.gz.attestation.json`. A repository declaring one extra asset gets `<filename>.attestation.json` in place of `<package-name>-extra-assets.attestation.json`.
- New `pack-attestation` command names an attestation bundle after the asset it attests and prints the release upload list.
- New `coverage` component: `repomatic init coverage` writes a `[tool.coverage]` section carrying branch coverage, report precision and a `report.fail_under` ratchet, shipped disabled.
- `lint-repo` now warns when a release download URL in `docs/install.md` names a file its release does not carry.
- The bundled pytest config drops `--cov-branch` and `--cov-precision` from `addopts`, now carried by the `coverage` component. Existing `[tool.pytest]` sections are left alone; run `repomatic init coverage` to pick the settings back up.
- The release lane's `publish-release` job now checks out the repository, so uploading binaries to the release no longer fails with `Failed to spawn: repomatic`. `7.7.0` shipped with no standalone executables because of it.
- `lint-changelog` re-confirms a release live before dropping its availability admonition, so a day-old cache no longer reports a just-published version as missing.
- The readme's logo is now an absolute URL, so it renders on the PyPI project page instead of 404ing.
- The install guide's executable table points at the last release that carries binaries, instead of a version whose upload lane failed.

## [`7.7.0` (2026-08-09)](https://github.com/kdeldycke/repomatic/compare/v7.6.0...v7.7.0)

> [!NOTE]
> `7.7.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.7.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.7.0).

- **Breaking:** drop the `check-version` command: nothing invoked it, and workflows read the `minor_bump_allowed` and `major_bump_allowed` metadata keys instead.
- **Breaking:** drop the `clean-unmodified-configs` command, superseded by `repomatic init --delete-unmodified`.
- **Breaking:** `repomatic metadata` no longer emits the `toml_files`, `changelog_bullet_word_threshold`, `nuitka_enabled` and `nuitka_nofollow_imports` keys. No workflow consumed them; the three config fields are read directly by their subcommands.
- New `github-housekeeping` skill backfills and curates labels and milestones across a repository's full issue and PR history: taxonomy design, cache-backed bulk classification with review gates, AI-slop detection from closed-without-comment signals, and milestone assignment by changelog, git-tag, and release-date archaeology.
- New `pack-binaries` command materializes the versionless binary aliases and prints the release upload list, replacing the release engine's shell loop: the asset naming convention now has one Python definition, shared with the install-guide freeze.
- New `--default-branch` option on the `changelog` command, so a repository whose default branch is not `main` gets a comparison URL that round-trips through the release freeze.
- `init` no longer writes the release lane (`changelog.md`, `changelog.yaml`, `release.yaml`, the PyPI publish action) into a uv virtual project (`[tool.uv] package = false`). Dependency locking, coverage and test tooling still apply.
- `init` now lists each `awesome-template` file it writes instead of one summary line, and `init labels` no longer reports its regenerated files as unmodified configs to clean up.
- Rename the Claude Code plugin release asset to `repomatic-claude-plugin.zip`, as `repomatic-plugin.zip` read as a plugin for repomatic. `/plugin install` is unaffected.
- Attestation bundles are now named after the asset they cover (`repomatic-manpages.attestation.json`) rather than the job that produced them, so the eight binary bundles no longer overwrite each other down to a single file.
- The VirusTotal detections chart now pins its CDN script with a Subresource Integrity digest, so a tampered copy is refused by the browser.
- `sync-workflow-pins` now splices the `--exclude-newer-package` cooldown exemption beside the inline `repomatic==X.Y.Z` pin it realigns, in the single spelling the release freeze writes and the post-release unfreeze recognizes.
- The changelog's `[!CAUTION]` admonition for a yanked release now quotes the reason PyPI recorded for the yank, when there is one.
- Changelog links are discovered under any capitalization of the PyPI `project_urls` key, matching how source URLs were already resolved.
- PyPI release lookups order same-day publications by PEP 440, so `1.10.0` is no longer ranked below `1.9.0`.
- Dependency-update PR bodies omit the version comparison link when the upstream repository publishes no matching tags, instead of linking to a 404.
- `format-images` now installs and verifies its `oxipng` binary once per run instead of once per optimized image.
- Registry binary downloads and `update-checksums` now carry a stall timeout, and `update-checksums` rejects truncated bodies instead of recording their digest as the new canonical checksum.
- GitHub token validation now times out instead of hanging forever when the API is unreachable.
- The `zsh_files` metadata key now lists only shell scripts whose shebang names zsh, so a bash `.sh` file is no longer linted as Zsh.
- `repomatic metadata` no longer aborts on a src-layout project: a Nuitka entry point with no module file at the repository root is skipped with a warning.
- `lint-repo` now reports an unreadable rulesets API as a skipped branch-protection check rather than a failed one.
- The broken-links issue no longer claims broken links when lychee itself failed to run.
- The release freeze now warns when it finds no changelog section for the version being released, instead of silently doing nothing.
- `lint-changelog --fix` now exits non-zero when it could not repair every problem it reported, instead of reporting success as soon as any one fix landed.
- `lint-changelog --fix` no longer stamps an undatable orphaned version with a `0000-00-00` placeholder date, and files a datable one in order even when every existing heading is a `.devN` development section.
- `sync-uv-lock` parses `uv.lock` once per phase instead of once per lookup, cutting several hundred milliseconds off each run.
- Fix downstream tool caches frozen at their first write: the reusable workflows' cache keys hashed a file that only exists upstream. Keys now rotate with the reusable workflow's own commit SHA.
- Fix `init` misreading a thin caller's trailing comment or odd-indented job line as extra downstream jobs, which silently flipped the file onto the explicit-permissions contract or duplicated the managed job's tail below the regenerated lanes.
- Fix `init` mishandling a consumer's extra release jobs: the extra `needs:` edges declared on the `release` lane were dropped, and a blank line accumulated above the jobs on every sync.
- Fix `init --output-dir` scanning the current directory for unmodified configs, which made `--delete-unmodified` act on the wrong tree.
- Fix the generated `release.yaml` emitting a bare `needs:` (which GitHub rejects at startup) when every canonical edge is filtered out.
- Fix a `[tool.repomatic.workflow] paths` override emitting entries like `**/*.py` unquoted when the canonical block held no quoted entry to copy, which parsed as a YAML alias and left the workflow silently ignored.
- Fix `repomatic metadata` crashing on a repository carrying a tag the version parser refuses, like `v1.2.3_hotfix`.
- Fix `sync-action-pins` and `sync-workflow-pins` silently reverting each other's edits when one `sync-deps` run bumps both kinds of pin in the same workflow file.
- Fix a mid-resolve failure of `sync-uv-lock` leaving synced policy pins beside a stale lockfile for the CI job to commit: the project is now restored to its pre-run state on any error.
- Fix `sync-mailmap` crashing with `UnboundLocalError` on a repository that has no `.mailmap` yet, the exact bootstrap its default `--create-if-missing` advertises.
- Fix the sponsor labeller treating an empty `pull_request` event payload as a pull request.
- Fix issue-filing jobs failing to record a new issue when `gh` prints a notice above the issue URL.
- Fix `update-dep-graph` failing on Windows when a dependency's SBOM metadata contains non-ASCII characters.
- Fix `cache clean` scoping: `--namespace` no longer wipes binaries and tool configs, `--tool` no longer wipes HTTP responses, and `--max-age` now applies to cached configs instead of deleting them all.
- Fix `repomatic run mypy` aborting with `Unable to find lockfile` in a repository without a `uv.lock`: the tool now runs in an isolated, cooldown-gated environment when there is no lockfile to freeze.
- Fix binary tools executing a just-deleted staging copy when the cache write is lost (Docker overlay runners) or the cache root is unwritable: the fallback copy now survives for the whole process.
- Fix the published plugin manifest declaring the post-release `.devN` version rather than the release it ships with.
- Fix image optimization leaving a `.bak` file in the working tree when interrupted.

## [`7.6.0` (2026-08-08)](https://github.com/kdeldycke/repomatic/compare/v7.5.0...v7.6.0)

> [!NOTE]
> `7.6.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.6.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.6.0).

- **Breaking:** the `labels` component is now ephemeral: `labels.toml` and the two labeller YAMLs are staged only when named explicitly (`repomatic init labels`). A repository that committed them sees them reported as excluded files on disk, removable with `init --delete-excluded`.
- New `fix-awesome-toc` command deletes the table-of-contents entries awesome-lint forbids from `readme.md` and every `readme.{lang}.md` beside it, matching translated sections by position rather than by their English name.
- The bundled skills and agents are now published as a Claude Code plugin: `/plugin marketplace add kdeldycke/repomatic` then `/plugin install repomatic@kdeldycke`. Needs Claude Code 2.1.224 or later. Addresses [#2378](https://github.com/kdeldycke/repomatic/issues/2378).
- New `pack-plugin` command builds the attested `repomatic-plugin.zip` asset every GitHub release now carries, with the marketplace URL pinned to the release tag.
- New `init plugin` component merges the plugin's marketplace and enablement keys into a repository's `.claude/settings.json`, leaving every other setting untouched. New `[tool.repomatic] settings.location` moves the destination.
- `lint-repo` now checks a repository's own `pr-body --template-file` templates: they belong in `.github/pr-templates/`, must exist, and must carry a `title` and a bare `footer: false`.
- A failed `extra-assets` job now leaves the GitHub release as a draft instead of publishing it without the declared assets. A skipped job still publishes, so repositories declaring no extra assets are unaffected.
- `sync-labels` now exports to a scratch directory instead of the repository root, so it no longer leaves three untracked files behind. Both committed and downloaded `extra-labels/` files are applied, a download shadowing a committed file of the same name.
- The generated downstream `release.yaml` now carries the canonical deny-by-default `permissions: {}`, so a consumer job appended below the managed lanes no longer runs with the repository's default token scopes.
- The generated `release.yaml` keeps the extra `needs:` edges a consumer declares on its `release` lane, so a caller-side asset build can gate the engine. Edges naming a managed lane, a vanished job, or an upstream-only job are still dropped.
- Fix `init` downgrading the upstream `uses:` pins of a repository that adopted a release still inside the `minimum-release-age` window. The cooldown now gates adoptions only, and a rollback to an older repomatic is honored.
- `init` no longer closes with "commit the generated files and push" on a run that produced nothing but ephemeral output.
- Fix the broken-links and setup-guide jobs dying on a recurring issue that the `autolock` workflow had locked. A conversation lock blocking a close or reopen comment is now cleared and the write retried.
- Fix `repomatic run` finding no binary on a Linux distribution `extra-platforms` cannot name, which broke every registry tool inside the AlmaLinux-based manylinux build container.
- Fix a crash when `pyproject.toml` is removed while being read, which now falls back to an empty configuration as documented.
- Re-lock `uv.lock` in the release freeze commit, so a release no longer ships with its version ahead of its own lock entry. The desync made the Windows binary builds fail.

## [`7.5.0` (2026-08-07)](https://github.com/kdeldycke/repomatic/compare/v7.4.1...v7.5.0)

> [!NOTE]
> `7.5.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.5.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.5.0).

- **Breaking:** bundled skills drop `model` and `disable-model-invocation`, so every skill is now model-invocable and runs on the session model. The recommended model moved to the spec's `compatibility` field.
- **Breaking:** workflows now run the CLI from `uv.lock` with `uv run --frozen` instead of `uvx --from .`, so installs are hash-verified. The release freeze still pins downstream to `uvx 'repomatic==X.Y.Z'`.
- `minimum-release-age` now defaults to `1 week` (was `8 days`) and `[tool.uv] exclude-newer` tracks it, closing the band where `uv.lock` could pin a version CI then refused to install.
- Reject a dependency floor naming a release still inside the cooldown window, which would ship a package that downstream repos and `uvx` users cannot resolve.
- Fix the `sync-*` updaters adopting a release published on the cutoff day itself, which uv then refused to resolve because it applies the window to the hour rather than the day.
- `sync-workflow-pins` now warns when a pin already in the tree sits inside the cooldown, instead of only judging the version it is about to write.
- A `lint-repo` check that cannot read its GitHub API response now reports one "could not query API" outcome instead of separate unreachable-API and unparsable-JSON messages.
- Fix `prepare-release` and `sync-github-releases` reading `./changelog.md` instead of the configured `changelog.location`.
- Fix a PR body template's `footer: false` being ignored, appending the attribution footer to a template that opted out of it.
- Fix template and skill frontmatter truncating at a value that embeds `---`, dropping every field below it.
- Trim the hand-maintained example dumps from `metadata`'s module and `nuitka_matrix` docstrings, which had drifted from the values they claimed to show.
- Fix docstrings where a brace placeholder against a closing backtick rendered as a spurious role, swallowing the prose after it.
- Every workflow now gates package installs behind the `minimum-release-age` cooldown, so no `uvx`, `uv pip install`, `npm install` or `npx` resolves a release published inside the window.
- A thin caller carrying extra downstream jobs now gets a top-level `permissions: {}`, and on its managed job the union of the scopes the reusable workflow's jobs declare. Callers with no extra jobs are untouched.
- `astral-sh/setup-uv` steps now pin the uv version, bumped by `sync-workflow-pins` once a release clears the cooldown, instead of installing the newest build satisfying `required-version`.
- `repomatic run mdformat` provisions its own `shfmt` at the registry-pinned version, so formatting shell blocks in Markdown no longer needs a system `shfmt`.
- New `path_tools` field on a tool spec, naming registry tools whose binary must be on `PATH` while it runs.
- Fix `repomatic run mdformat` aborting on Windows ARM64, where `shfmt` publishes no binary: a companion missing for the platform is now skipped with a warning.
- Fix the macOS binary builds, which crashed in code signing once skills joined the bundled data: an `--include-data-dir` source holding symlinks is now staged symlink-free. Reported upstream as [Nuitka/Nuitka#3994](https://github.com/Nuitka/Nuitka/issues/3994).
- The `format-markdown` job drops back to the lean `ubuntu-slim` runner, and its awesome-list fixup uses `sed` instead of `gawk`.
- `apt-get` replaces `apt` in every workflow step, with `--no-install-recommends` throughout.
- Add `gh` to the `repomatic run` registry. The release engine's attestation check now uses it instead of adding GitHub's RPM repository to the build container and installing an unpinned `gh`.
- A tool's `strip_components` accepts a per-platform mapping, like `archive_format` already did.
- Add `oxipng` to the `repomatic run` registry, bumped to `10.1.1`. `format-images` now uses the pinned, checksum-verified build instead of a hand-fetched `.deb` installed with `dpkg`.
- Dependency-updater reports now close on the `Held back by cooldown` section, below `Cooldown bypasses`, so a PR opens on what the run changed instead of what it left alone.
- `sync-tool-versions` leaves `autofix.yaml` for a new upstream-only `self-maintenance.yaml`, and polls daily instead of weekly. Downstream `autofix.yaml` loses the four steps it could never run.
- `sync-tool-versions` now also bumps the packages pinned alongside a tool in its `uvx` environment, like mdformat's plugin set, reporting them like every other row.
- Fix `get_source_url` returning a bug-tracker or changelog sub-path instead of the repository root, and matching `project_urls` keys case-sensitively.
- The bundled ruff config sets `output-prefer-rule-codes`, so diagnostics report `ISC004` instead of `implicit-string-concatenation-in-collection-literal`. Requires ruff `0.16.1`.
- `repomatic init` holds the derived upstream workflow pin back to the newest release past the `minimum-release-age` cooldown; `--no-cooldown` pins the running version immediately.
- Commands honoring `--output` now log their destination uniformly, always naming what is written instead of mixing `Save updated results to` and a subject-less `Write to`.
- Align bundled skills with the [Agent Skills specification](https://agentskills.io/specification): `babysit-ci` gains its required `name` field, and `allowed-tools` moves to the spec's space-separated form.
- Skills are now installed as whole folders, so one can ship the spec's optional `scripts/`, `references/` and `assets/` directories alongside its `SKILL.md`.
- Add `[tool.repomatic.flavor]` with `agent` and `ci` keys, declaring the ecosystems a repository targets. Values are [extra-platforms](https://github.com/kdeldycke/extra-platforms) trait IDs.
- `[tool.repomatic.labels.extra]` entries now carry `labelmaker`'s full per-label specification, adding `rename-from` in-place renames, multi-color lists, and the `create`, `update`, `enforce-case` and `on-rename-clash` knobs. An unknown field now warns instead of being dropped silently.
- New `release-assets` filename list in `[tool.repomatic]`: each named asset is built by a caller-side job, attested like the compiled binaries, and attached to the release draft before publication locks it.
- The man-page tarball is now attested like the compiled binaries, with its sigstore bundle attached as a `manpages.attestation.json` release asset.
- `skills.location` and `agents.location` now default to the layout of the configured `flavor.agent`, and still win when set explicitly.
- `repomatic run mdformat` drops the `mdformat-ruff` plugin and its separate ruff pin, and the bundled ruff config drops `extend-include`: ruff formats fenced Python blocks in Markdown on its own.
- Teach the `repomatic-ship` and `babysit-ci` skills to act on the first failing CI job instead of waiting out full matrices, pay down pre-existing and `⁉️`-probe test debt before the first push, and hold prose-only pushes while binary matrices drain.
- Chart the `/repomatic-ship` convergence loop (first-failing-job fixes, push timing, debt paydown) on the skills docs page.
- Re-base the release PR onto the current `main` HEAD after every build by also running `prepare-release` on `workflow_run`, so a reconciliation commit that misses `changelog.yaml`'s `paths:` filter no longer leaves the PR stale.
- `/repomatic-topics` pre-approves the `Agent` tool instead of the retired `Task` name.
- Add a `history` documentation page retracing the project from its 2021 reusable-workflow origins through the `gha-utils` CLI to the `repomatic` rename.
- Recenter the readme and the workflows page on the CLI-first design: workflows only trigger CLI commands, with local-run examples and links to the standalone binaries and history pages.
- Document Agent Skills spec conformance, and `argument-hint` as the single accepted deviation, on the skills documentation page.
- Correct the readme and benchmark page: both carried stale reusable-workflow counts, and listed `jpegoptim` among the tools `repomatic run` manages when it never was.

## [`7.4.1` (2026-08-01)](https://github.com/kdeldycke/repomatic/compare/v7.4.0...v7.4.1)

> [!NOTE]
> `7.4.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.4.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.4.1).

- Add two review links to the release PR's `How-to release` checklist: the draft dev pre-release and the full changes against `main`.
- Teach the `repomatic-ship` pre-push gate to read the latest conclusive ancestor CI run when HEAD's own runs are still in-flight, catching a pre-existing platform-gated failure before the first push.
- Lower the uv `required-version` floor from `>=0.12` to `>=0.11.15`, the actual resolver minimum.
- Extend the `uvx` script-equals-package guard to the `pipx run <script>` install check, so `📦 Package install` no longer fails permanently for projects whose CLI script differs from their package name.

## [`7.4.0` (2026-07-31)](https://github.com/kdeldycke/repomatic/compare/v7.3.1...v7.4.0)

> [!NOTE]
> `7.4.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.4.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.4.0).

- **Breaking:** Rename `update-deps-graph` to `update-dep-graph` across the CLI command, autofix job, PR branch, and body template, aligning with the `dependency-graph` config key. Close any open `update-deps-graph` pull request; the next run reopens it on the new branch.
- **Breaking:** `repomatic init` component selectors are now case-sensitive, validated by the same code path as the `exclude` and `include` configuration entries.
- Lower the compiled-binary OS floors: Linux glibc `2.28`, built and self-tested in `manylinux_2_28` containers (RHEL 8, Debian 10, Ubuntu 20.04 and later), and macOS `11.0` (Apple silicon) / `10.15` (Intel) via uv's embedded python-build-standalone interpreter.
- Enforce each binary's OS floor at build time: `verify-binary` parses ELF, Mach-O and PE headers natively and no longer needs exiftool.
- Keep `tkinter` and its Tcl/Tk stack out of compiled binaries via the new `[tool.repomatic]` `nuitka.nofollow-imports` setting (default `["tkinter"]`, set to `[]` to bundle it).
- Emit man pages for repomatic's own CLI on docs builds and attach a `repomatic-manpages.tar.gz` asset to each release.
- Add `update-docs --check` to report out-of-date self-updating content and exit non-zero without writing, for CI drift detection. The docs update script must accept its own `--check` flag to participate.
- Add a `check_sha_pinning_required` lint check and setup-guide step for GitHub's `sha_pinning_required` Actions setting, the platform-enforced backstop for action SHA pinning.
- Unify the `lint-repo` checks on one tri-state result protocol: skipped checks now print `ℹ` instead of a misleading `✓`.
- Key the CI tool-binary caches on `tool_registry.py` instead of the whole runner module, so engine-only changes stop invalidating cached tools.
- Report `sync-deps` and `update-checksums` progress as a `✓`/`✘` trail with a running tally and a timed summary.
- Warn about unknown `[tool.repomatic]` keys once per project and process, instead of on every configuration re-load.
- Loosen the uv `required-version` pin to a lower bound (`>=0.12`), dropping the per-minor upper cap so uv can update across minors without a manual bump.
- Move the docs link checker from `ubuntu-slim` to `ubuntu-24.04-arm`: the crawl outgrew the slim runner's 15-minute job cap.
- Extend the bundled lychee configuration with generic excludes: GitHub issue-comment fragments, release binary downloads, and DOI-to-Zenodo redirects.
- Declare least-privilege permissions on the canonical `release.yaml`, clearing the workflow `check_workflow_permissions` lint.
- Extend the `check_workflow_permissions` lint to flag a reusable-workflow call inheriting an empty top-level `permissions: {}` without its own grants: the misconfiguration that aborts a run at startup.
- Block install-time scripts and apply the `minimum-release-age` cooldown on every npm install of `awesome-lint`, hardening both the runtime and CI-provisioning paths against supply-chain attacks.
- Surface `uv audit`'s stderr when it exits without emitting JSON, replacing the bare `produced no output` error.
- Keep `metadata` from crashing when git refuses the repository (dubious ownership, unresolvable range): it now logs git's stderr and continues.
- Accept `sur` (macOS Big Sur, Homebrew's `big_sur` bottle tag) as a valid word in the bundled typos configuration, so `fix-typos` stops correcting it to `sure`.
- Document the minimum OS requirement of each binary target, and the distributions it opens execution to, in a new [Minimum OS requirements](https://kdeldycke.github.io/repomatic/binaries.html#minimum-os-requirements) section that downstream binaries pages link to.
- Document how to verify a downloaded binary's build-provenance attestation with `gh attestation verify` on the installation page.
- Order the installation docs' Python-compatibility table newest-first, so the latest release and Python version read from the upper-left.
- Rename the binaries page chart markers to `binaries-chart`/`binaries-chart-end`, aligning on click-extra's `<!-- name --> / <!-- name-end -->` marker grammar; pages carrying older markers are migrated on their next refresh.
- Fix the click-extra `{matrix}` directive link on the installation page, and realign page octicons with the `sphinx-docs` agent's extended icon registry.
- Emit an absolute `og:image` URL for social previews: `ogp_site_url` now backs `ogp_image` in the docs configuration.
- Pin the install guide's versioned CLI examples (`pkg@X.Y.Z`, `pkg==X.Y.Z`) to the release in the prepare-release freeze step.
- Direct the `babysit-ci` skill to announce its early exit and name the still-unverified `release.yaml` binary run, instead of stopping on a silent idle.
- Harden the `repomatic-ship` skill: forbid detached `Monitor` polling, read the whole unreleased changelog section at invocation, and align every convention description its docs pass corrects.
- Extend the `sphinx-docs` agent and `sphinx-docs-sync` skill: the `{click:run}` `--version` trap, thin-schema combined CLI page, Cloudflare-blocked intersphinx probes, mdformat seed-block collapse, plus release-asset, self-healing-marker, and linkcheck audit guards.
- Note in the `repomatic-ship` skill that changelog released sections are immutable, and that a workflow cache-key line-length fix cannot lift `hashFiles()` into a workflow-level `env:`.

## [`7.3.1` (2026-07-28)](https://github.com/kdeldycke/repomatic/compare/v7.3.0...v7.3.1)

> [!NOTE]
> `7.3.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.3.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.3.1).

- Refresh the bundled pytest defaults to the canonical configuration: `importlib` import mode, `tests/`-restricted collection, the `once` marker, parallel runs via `pytest-xdist`, and `--cov-report=xml` left to the test workflow's command line.
- Mark the `validate-arch` job of `tests.yaml` as canonical-repository-only: downstream repos drop the job and its `build_targets` metadata field when adapting the workflow.
- Point the `lint-changelog` `not found on PyPI` warning at its remedy: list intentionally-unpublished releases under `[tool.repomatic] abandoned-versions`.
- Fix the post-release re-trigger of `changelog.yaml`: its `workflow_run` filter still watched the pre-emoji `Build & release` workflow name and never fired.
- Fix `lint-changelog --fix` treating a published pre-release (`X.Y.Z.dev0`, `rc`, `alpha`, `beta`) as a missing changelog entry, which inserted a spurious section and rewrote the adjacent release's comparison URL.
- Fix the `update-docs` autofix job opening a duplicate of the `format-pyproject` pull request: it now reformats `pyproject.toml` only when `update-docs` changed it.
- Skip the bare `uvx <script>` invocation of the package-install smoke job when the CLI script is not named after its package.
- Restructure the `repomatic-ship` skill: rules shared by every spawned agent move to a single section, and accumulated incident notes compress into their operative rules.
- Harden the `repomatic-ship` release checks: dispatch `release.yaml` past a content-skipped binary matrix, revert formatter moves whole, read the freeze scope from the regenerated release PR, and verify `{click:run}` blocks against the live CLI.
- Direct the `repomatic-ship` docs pass to advance version samples lagging the released tag up to it.
- Note in the `repomatic-ship` and `babysit-ci` skills that CI log fetches write under `~/.cache/gh` and need the sandbox off.

## [`7.3.0` (2026-07-23)](https://github.com/kdeldycke/repomatic/compare/v7.2.0...v7.3.0)

> [!NOTE]
> `7.3.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.3.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.3.0).

- **Breaking:** Move the MyST docstring toolchain upstream to click-extra, now floored at `8.5`: the `repomatic.myst_docstrings` Sphinx extension becomes `click_extra.sphinx.myst_docstrings` (point `conf.py` at the new module path), and the `convert-to-myst` command becomes `click-extra convert-to-myst`.
- Warn on unknown `[tool.repomatic]` keys through click-extra's schema layer, covering nested tables too; the warning now names keys in snake_case.
- Upload each release binary under a versionless alias, so the stable `releases/latest/download` URLs keep resolving across releases.
- Mark the upstream toolkit's lockstep-aligned pin with a `⛓️ lockstep` docs link in the `sync-workflow-pins` PR table, instead of an empty `Released` cell.
- Add a ⚙️ emoji to the `Configuration` section heading of PR bodies, and swap the `Held back by cooldown` section's 🔜 emoji for ⏸️.
- Disable ruff's `unsafe-fixes` in the bundled defaults, so `--fix` and the autofix workflow only apply semantics-preserving fixes.
- Expose `GITHUB_TOKEN` to the Sphinx linkcheck step of the docs workflow, so a repo's `conf.py` can authenticate its github.com checks via `linkcheck_request_headers`.
- Space out the Windows exiftool install with step-level retries, absorbing Chocolatey community-feed outages that punch through choco's own `--retry-count`.
- Fix the `exclude` and `include` configuration reference to list `agents` among the default-excluded components.
- Exclude `once`-marked tests from every test-matrix cell and run them in a dedicated single-runner `once-tests` job with its own coverage upload.
- Teach the `repomatic-ship` and `babysit-ci` skills that the Nuitka binary matrix only exists on projects enabling `[tool.repomatic] nuitka.enabled`, and how to verify binary-less releases.
- Fix the `repomatic-ship` local-gate tool recipes: pass `biome` and `shfmt` their args forms, and smoke checksum-pinned tools with no matching files via `--version` only.
- Broaden the `repomatic-ship` review scopes: version samples are audited against the last freeze commit's file list, and platform-gated tests are reviewed with the inputs they consume.
- Require the `repomatic-ship` sweep agents to message their final reports to the orchestrator, with one chase on a silent idle.
- Point the changelog over-length warning and the `repomatic-changelog` skill at the canonical entry-length guideline URL, which downstream `CLAUDE.md` copies lack.

## [`7.2.0` (2026-07-16)](https://github.com/kdeldycke/repomatic/compare/v7.1.0...v7.2.0)

> [!NOTE]
> `7.2.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.2.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.2.0).

- **Breaking:** the `release-prep` command is renamed `prepare-release`, matching the job, template, and PR branch it drives.
- **Breaking:** the `version-check` command is renamed `check-version`.
- Add `cancel-runs`: cancels a branch's in-progress and queued workflow runs, replacing the bash block in `cancel-runs.yaml`; run listings now paginate past the first page.
- Add `[tool.repomatic] binaries.sync`: set to `false` to stop the release pipeline from committing the binaries catalog and scan records to the default branch.
- Add `sync-dep-sources`, a fifth `sync-deps` updater: once the release named by a git-tracked dependency's `.dev` floor ships on PyPI, it drops the `[tool.uv.sources]` override, tightens the floor, and freezes the adopted release through the cooldown. Disable with `[tool.repomatic] dep-sources.sync`.
- `Dialect`, `ArchiveFormat`, and `WorkflowFormat` now carry their dispatch as enum methods (`serialize`, `extract`, `write_workflow`).
- Rename cross-module internals to public names: `COMPONENTS_BY_NAME`, `is_source_repo`, `format_released`, `format_upload_date`, `date_to_utc_cutoff`.
- The `sponsor-labeller` job in `labels.yaml` is renamed `sponsor-label`, matching the CLI command it runs.
- Remove the dead `get_default_repo` and `list_open_issues` helpers and the unused `REQUIRED_PAT_PERMISSIONS` constant.
- `update-deps-graph` now keeps only directly-declared dependencies inside the `--group` and `--extra` boxes, renders transitive dependencies as plain ovals, and counts an extra's transitives as depth 2 under `--level`.
- The unsubscribe workflow's GraphQL phase now re-validates each item's staleness client-side and reports the items it holds back.
- The `unsubscribe.yaml` workflow now streams per-thread progress to the job log.
- Generated PR and issue bodies now use `##` section headings, with `Release notes` nested as a `###` subsection; release bodies embedded in dropdowns get their headings demoted below the per-version heading.
- Generated PR bodies drop the boilerplate `Description` section, and the `Workflow metadata` block becomes a compact list led by a `Documentation` link to the job's section of the workflows reference.
- The `Cooldown bypasses` PR section is now a single table: `🧹 cleared:`, `📌 frozen:`, and `🚧 unreleased:` rows with a `Held until` expiry column.
- Diff tables label added and removed packages with `🆕 new:` and `🗑️ removed:` prefixes ahead of the version.
- `sync-action-pins` and `sync-workflow-pins` PR bodies now report an action or package pinned at several versions as a single row spanning from the oldest pin.
- The `update-docs` job now re-formats `pyproject.toml` files with `pyproject-fmt` after running the project's update script.
- GitHub Releases API reads now resolve their token like every other GitHub access (`REPOMATIC_PAT` first), instead of hitting the anonymous rate limit.
- PyPI, npm, and GitHub API lookups now retry once on a truncated response instead of crashing with `IncompleteRead`.
- The release freeze and unfreeze steps now cover `.yml` workflow files alongside `.yaml`.
- The gitignore.io template download now times out after 10 seconds instead of hanging on a stalled connection.
- Version bumps no longer overwrite the `cff-version:` schema field in `citation.cff` when it coincides with the package version.
- `sync-action-pins` no longer rewrites `uses:` pins inside files `repomatic init` deploys verbatim, like the `publish-pypi` composite action.
- The `run typos` guidance now recommends `extend-ignore-re` guards for encoded hashes and intentional-typo examples.
- Document the scan job contract and the release-lane direct-commit exception.

## [`7.1.0` (2026-07-08)](https://github.com/kdeldycke/repomatic/compare/v7.0.0...v7.1.0)

> [!NOTE]
> `7.1.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.1.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.1.0).

- **Breaking:** `scan-virustotal` no longer writes scan tables into GitHub release notes; the `--update-release` and `--repo` options are removed, and `--binaries-dir` is now required.
- Add `sync-binaries`: regenerates `docs/assets/binaries.csv` and its `docs/binaries.md` page, a catalog of every released binary with download links, VirusTotal analyses, and a detection trend chart.
- `sync-binaries --backfill-records` recovers detection snapshots from the VirusTotal tables of legacy release notes into the scan history file.
- The release pipeline now records each binary's `flagged / total` snapshot in `docs/assets/virustotal-scans.json` and refreshes the binaries page instead of editing release notes.
- The documentation build gains `sphinx-datatables`, rendering the binaries catalog as a searchable, sortable table; downstream repos can opt in with the same extension.
- Add `git-commit-push`: commits files and pushes them, rebasing and retrying on rejection, for release jobs publishing generated files to the default branch.
- Add a global `--jobs` option controlling how many parallel workers commands may use, defaulting to one fewer than the host's logical CPUs.
- `update-checksums`, `sync-tool-versions`, and `sync-deps` now download artifacts and resolve updates concurrently, sized by `--jobs`; Ctrl+C aborts the fan-out promptly and `--verbosity DEBUG` collapses it to sequential.
- `sync-uv-lock` PR bodies gain a `Cooldown bypasses` section: each active `exclude-newer-package` freeze with the date it expires and is cleared from `pyproject.toml`, plus the entries the run froze or pruned.
- The `Held back by cooldown` table now also lists releases blocked by an `exclude-newer-package` freeze, not only those inside the global `exclude-newer` window.
- `update-docs` gains a fourth phase refreshing self-updating `{matrix}` directive blocks in `docs/` and `readme.md`; the Python compatibility matrix in the installation docs now renders from those markers instead of an in-repo generator.
- The CLI, configuration, and tool-runner references in the docs render live through the `click:tree`, `click:config`, and `{python:render}` directives; the checked-in generated tables and `docs/docs_update.py` are removed.
- Each tool section in the tool-runner reference shows Stars and Last release badges; the separate `Comparison` table is removed.
- Require `click-extra >= 8.3`, adding the `--export-config` option, `--theme auto` terminal-background detection, and the `click:config` Sphinx directive.
- Label added and removed packages in dependency report tables consistently after the version, with 🆕 and 🗑️ status emoji.
- `sync-workflow-pins` now aligns the inline `repomatic` pin to the newest `uses:` ref version, bypassing the release-age cooldown.
- `sync-action-pins` now converges actions pinned at several versions onto the highest pin, even when no newer release clears the cooldown.
- Trim oversized PR and issue bodies to GitHub's 65536-character limit, preserving the refresh tip, metadata block, and attribution footer.
- Add the humanized age next to `Released` dates in `fix-vulnerable-deps` reports, matching the other dependency updaters.
- `update-deps-graph` now places a package declared by several groups or extras in the box where most of its dependents live, drawing the duplicates with a dashed border and a dotted identity link to the real node.
- The setup guide and `lint-repo` now flag a missing `REPOMATIC_NOTIFICATIONS_PAT` secret when `notification.unsubscribe` is enabled.
- Exclude VirusTotal analysis links from lychee broken-link checks.
- Update the `av-false-positive` skill to start from the scan history file and to record post-submission re-scans into it.
- The `babysit-ci` and `repomatic-ship` skills now mandate sleeps between CI polls and document GitHub API rate-limit exhaustion, whose symptoms masquerade as PAT permission errors.
- Fix `sync-uv-lock` reporting `No dependency changes` and writing no PR body when a run only prunes or freezes cooldown bypasses in `pyproject.toml`.
- Fix downstream manual dispatches of the `unsubscribe` workflow ignoring their inputs and always running live: generated thin callers now forward `workflow_dispatch` inputs to the reusable workflow.
- The `publish-pypi` composite action and the `unsubscribe` workflow no longer trigger setup-uv's cache-invalidation and `Empty workdir detected` warnings on downstream runs, which execute without a checkout.
- The PR-creation steps of the changelog workflow now time out after 10 minutes instead of hanging when the GitHub API is rate-limit starved.
- Fix the binary cache purging fresh entries whose release archive carries an old build date: cached binaries are now aged by their store time, not the archive's mtime.
- `repomatic run` now reports a truncated tool download as `got X of Y bytes` instead of a SHA-256 mismatch, which read as a stale checksum or a tampered artifact.
- Rebuild binaries on pushes that only touch `.github/workflows/_release-engine.yaml`: the release workflow split left the engine lane outside the binary-affecting paths.
- Disable mouse zoom on the class inheritance diagrams of the documentation's API sections, so they no longer hijack page scrolling; the fullscreen viewer keeps zoom.

## [`7.0.0` (2026-07-02)](https://github.com/kdeldycke/repomatic/compare/v6.31.0...v7.0.0)

> [!NOTE]
> `7.0.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/7.0.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v7.0.0).

- **Breaking:** Remove the Renovate integration: the `renovate.yaml` workflow, the bundled `renovate.json5`, the `check-renovate` command, and the Dependabot-to-Renovate migration are gone, replaced by self-hosted dependency updates (below). Downstream repos prune the orphaned files on their next `repomatic init`.
- **Breaking:** `repomatic update-checksums` is now registry-only: the workflow-file argument and the `--registry` flag are removed, so it only refreshes the binary tool checksums.
- Add `sync-deps`, the single entry point for dependency updates: runs every enabled updater or a named subset in parallel and applies them in order, with a `--dry-run` preview.
- Add `sync-tool-versions`: bumps each `repomatic run` tool to its latest release past the cooldown and refreshes the binary tool checksums in the same pass.
- Add `sync-action-pins`: bumps SHA-pinned GitHub Actions to their latest release past the cooldown, resolving each release tag to its commit SHA.
- Add `sync-workflow-pins`: bumps the npm and PyPI version literals embedded in workflow YAML past the cooldown.
- Add `[tool.repomatic] minimum-release-age` (default `8 days`), the shared stabilization cooldown for the sync updaters, plus per-updater `tool-versions.sync`, `action-pins.sync`, and `workflow-pins.sync` toggles.
- `repomatic run` now runs npm-backed tools, starting with `awesome-lint`, installed from the npm registry with per-tarball integrity and the `minimum-release-age` cooldown; the `lint-awesome` job now calls `repomatic run awesome-lint`.
- Add a `[tool.repomatic] changelog.archive-location` option pointing at an archive file for older release sections, so `lint-changelog` treats archived versions as documented instead of flagging them as orphans.
- Dependency-updater PR bodies now share `sync-uv-lock`'s format: a cooldown cutoff date, a `Held back by cooldown` section, and a `Release notes` dropdown between them.
- `repomatic run` now applies the `minimum-release-age` cooldown to the transitive dependencies of its `uvx`-installed tools; binary and `uv run` tools stay pinned as before.
- The autofix workflow now runs the dependency updaters weekly on a schedule, so quiet repositories still pick up dependency, tool, and action-pin updates.
- `REPOMATIC_PAT` no longer requires the `Commit statuses` permission; `lint-repo` now warns when a token still grants it so it can be tightened.
- Drop the `pydriller` dependency: Git history operations now invoke the `git` CLI directly, shrinking the install and compiled-binary footprint.
- `repomatic run` now animates a spinner while downloading a tool whose server omits a `Content-Length`, where it previously showed nothing.
- The generated Python compatibility matrix in `install.md` now covers pre-classifier releases, falling back to `requires-python`, Poetry, or `setup.py` metadata to infer supported versions.
- Fix `update-deps-graph` rendering only one of several extras or dependency groups that share a directly-declared dependency; each now gets its own subgraph.
- Fix broken documentation links: the standalone-binary downloads (versionless 404s), the `pipx` installation guide, and the GitHub matrix-strategy reference.

## [`6.31.0` (2026-06-27)](https://github.com/kdeldycke/repomatic/compare/v6.30.0...v6.31.0)

> [!NOTE]
> `6.31.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.31.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.31.0).

- `sync-uv-lock` PRs now list newer releases held back by the `exclude-newer` cooldown, with the date each ages out of the window.
- `sync-uv-lock` package tables now annotate each release date with a relative hint (`2 days ago`, `in 3 days`).
- Autofix PR bodies now document every relevant `[tool.repomatic]` option in their Configuration section.
- The test workflow uploads coverage to Codecov from one runner per OS, not from every matrix cell.
- Drop the Codecov Test Analytics (test results) upload and the `junit.xml` file it generated.
- `update-checksums --registry` now maintains binary tool checksums in a dedicated `repomatic/tool_checksums.py` module.
- Update `pyproject-fmt` to `2.25.1`, which keeps comments inside inline tables when `format-pyproject` reorders their keys.
- Fix the PyPI availability admonition missing from GitHub release notes.
- Fix `repomatic run biome` failing with a SHA-256 mismatch: Biome `2.5.0`'s binary checksums were stale, breaking JSON, JavaScript, and TypeScript format jobs.
- Fix `[tool.repomatic] workflow.sync = false` being ignored: it now skips workflow sync like the other `*.sync` toggles.

## [`6.30.0` (2026-06-24)](https://github.com/kdeldycke/repomatic/compare/v6.29.0...v6.30.0)

> [!NOTE]
> `6.30.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.30.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.30.0).

- **Breaking:** The test and release workflows now run `click-extra test-suite` (the `test-plan` engine renamed in click-extra `8.1`), reading the suite from `./tests/cli-test-suite.toml`. Requires `click-extra >= 8.1`.
- `repomatic lint-repo` now fails when a workflow's inline `repomatic==X.Y.Z` pin lags the version of its `uses:` ref.
- The test workflow skips the Codecov upload on free-threaded Python (`3.14t`), where codecov-cli cannot build its `test-results-parser` extension.
- In generated dependency graphs, thick arrows now mark only the root package's direct dependencies; a transitive edge that points at a primary dependency stays thin, so optional extras no longer read as a primary dependency chain.
- The release workflow now cancels superseded runs on rapid non-release pushes to `main`, so intermediate commits no longer pile up redundant binary builds; release commits still run to completion.

## [`6.29.0` (2026-06-22)](https://github.com/kdeldycke/repomatic/compare/v6.28.1...v6.29.0)

> [!NOTE]
> `6.29.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.29.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.29.0).

- **Breaking:** Remove the `repomatic test-plan` command and `[tool.repomatic] test-plan` config. The declarative test-plan engine moved upstream to [click-extra](https://kdeldycke.github.io/click-extra/test-suite.html); run `click-extra test-plan` instead, configured via `[tool.click-extra.test-plan]`.
- Add `repomatic show-test-matrix` to render the CI test matrix as a Python-version by OS grid in any `--table-format`.
- Add `repomatic init uv` to sync the canonical `[tool.uv]` pins (`required-version`, `exclude-newer`) into `pyproject.toml`; `sync-uv-lock` applies the same sync, so every machine resolves `uv.lock` with the same uv.
- Require `click-extra >= 8`; the `manpages` release job now uses `click-extra wrap --man` to generate man pages.
- The binary download progress bar now respects `--no-progress` and `--accessible`, hiding it when progress output is turned off.
- Move the Sphinx linkcheck output to `docs/_linkcheck/` (mirroring `docs/_build/`); `broken-links --output-json` now defaults there and the generated `.gitignore` excludes it.
- `repomatic run` now warns when `--check` targets a post-processed formatter (currently `mdformat`): check mode bypasses the fixup, so its exit status can mislead.
- `sync-uv-lock` now reverts a re-lock that changed no package versions, so uv's machine-dependent re-spelling of equivalent `uv.lock` environment markers no longer opens empty sync PRs that ping-pong between contributors and CI.
- Documentation pages that cover a Python module now end with that module's API reference.
- Test the free-threaded `3.14t` build as a stable single-runner smoke test instead of across the full cross-platform matrix; `3.15` stays `continue-on-error`.

## [`6.28.1` (2026-06-19)](https://github.com/kdeldycke/repomatic/compare/v6.28.0...v6.28.1)

> [!NOTE]
> `6.28.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.28.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.28.1).

- Fix `test-matrix.full-include` matrices emitting combinations that `exclude` should have removed; they now follow GitHub's documented include/exclude algorithm.

## [`6.28.0` (2026-06-19)](https://github.com/kdeldycke/repomatic/compare/v6.27.0...v6.28.0)

> [!NOTE]
> `6.28.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.28.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.28.0).

- `repomatic test-plan` runs its cases in parallel by default (one fewer than the CPU count); pass `--jobs 1` for sequential execution.
- Add `[tool.repomatic] test-matrix.full-include` config: declare full-matrix-only job rows as explicit combinations (each merged onto the shipped-config defaults), a readable alternative to a long `test-matrix.exclude` list.
- Run the pull-request test matrix on `ubuntu-24.04-arm` for faster Linux CI; the full test matrix still covers x86 Linux.
- `repomatic metadata` no longer prints spurious `--overwrite` or `$GITHUB_OUTPUT` warnings when writing to stdout.
- Add a [test-matrix guide](https://kdeldycke.github.io/repomatic/test-matrix.html) to the docs: choosing matrix targets, a GitHub-runner speed inventory, and a worked example.

## [`6.27.0` (2026-06-18)](https://github.com/kdeldycke/repomatic/compare/v6.26.0...v6.27.0)

> [!NOTE]
> `6.27.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.27.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.27.0).

- **Breaking:** Replace `fix-vulnerable-deps` with `audit`. `repomatic audit` reports vulnerable dependencies read-only; `repomatic audit --fix` performs the previous upgrade behavior.
- Stop forcing `pyproject-fmt` table expansion: `project.urls`, `project.scripts`, and similar sections now use its default compact (dotted-key) form.
- Recognize each bundled tool's native config files more accurately (`biome`, `gitleaks`, `ruff`, `typos`, `zizmor`, and others) and their config-file CLI flags.
- Preserve comments when materializing a `[tool.X]` section from `pyproject.toml` to a tool's native TOML config file (like `.gitleaks.toml`), instead of dropping them.
- Update `pyproject-fmt` to `2.25.0`, fixing the `format-pyproject` job writing invalid TOML when it reformats `[tool.repomatic.labels]` rule tables.
- Align the bundled `[tool.bumpversion]` and `[tool.lychee]` templates with `pyproject-fmt`'s canonical output, ending the reformatting pull-request loops they triggered.
- Fix cooldown bypasses (`[tool.uv] exclude-newer-package`) never expiring: `sync-uv-lock` now freezes each one at its locked version instead of a latest-tracking `"0 day"` span, and prunes it once that version ages past `exclude-newer`.
- Fix `uv.lock` ping-ponging on every `sync-uv-lock` run: `exclude-newer-package` freezes are now explicit UTC timestamps, not bare dates that uv re-expands in the locking machine's timezone.
- Fix the `repomatic.myst_docstrings` Sphinx extension corrupting two adjacent inline-code spans in a docstring when the second span starts with an underscore.
- Fix the `manpages` release job: attach the man-page tarball to the release draft before publishing, so it no longer fails under GitHub immutable releases.

## [`6.26.0` (2026-06-17)](https://github.com/kdeldycke/repomatic/compare/v6.25.1...v6.26.0)

> [!NOTE]
> `6.26.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.26.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.26.0).

- Add `[tool.repomatic] nuitka.extras` config to sync listed `[project.optional-dependencies]` extras into the venv before the Nuitka build, so optional features land in the binary.
- Add `[tool.repomatic.labels]` `extra`, `file-rules`, and `content-rules` config for inline label definitions and labeller rules, replacing the silently-ignored `extra-file-rules` and `extra-content-rules` fields.
- Stop version-bump PRs from upgrading dependencies: the bump and release jobs now run plain `uv lock`, leaving dependency refreshes to the `sync-uv-lock` job.
- Add a `[tool.repomatic] changelog.bullet-word-threshold` config: `lint-changelog` warns (non-fatally) about unreleased changelog bullets longer than the threshold (40 words by default).

## [`6.25.1` (2026-06-13)](https://github.com/kdeldycke/repomatic/compare/v6.25.0...v6.25.1)

> [!NOTE]
> `6.25.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.25.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.25.1).

- Fix `uvx repomatic@X.Y.Z` failing for end users with `No solution found` by dropping the `bump-my-version` dependency and reading the current version natively from `.bumpversion.toml` or `[tool.bumpversion]`.
- Remove the `uv-overrides.txt` file and all `UV_OVERRIDE` workflow env blocks.
- Render the Mermaid dependency graph in `docs/install.md` under a new `Default dependencies` section.

## [`6.25.0` (2026-06-13)](https://github.com/kdeldycke/repomatic/compare/v6.24.0...v6.25.0)

> [!NOTE]
> `6.25.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.25.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.25.0).

- Add man page generation to the release and docs pipelines via a `manpages` job, activated by `[tool.repomatic.manpages]` config keys (`script`, `asset-name`); requires `click-extra>=7.19`.
- Validate `[project.scripts]` entries when building the Nuitka matrix, rejecting path-shaped, empty, or malformed script names up front with a clear error.
- Replace the `tomlkit` and `tomli` dependencies with [`tomlrt`](https://dimbleby.github.io/tomlrt/) for all TOML reads and comment-preserving writes.
- Annotate `gh` and PAT permission check failures with the current [githubstatus.com](https://www.githubstatus.com) summary, and surface raw stderr on non-403 failures instead of misreporting missing scopes.
- Recognize friendly durations (`24 hours`, `30 minutes`) and ISO 8601 durations (`PT24H`, `P7D`) in `[tool.uv].exclude-newer` when computing the `repomatic sync-uv-lock` cooldown.
- Fix `UV_OVERRIDE` not reaching Renovate's child processes during `update-checksums`.
- Add `[tool.repomatic] abandoned-versions` to `lint-changelog`, reporting listed versions as skipped instead of warning that they are missing from PyPI.
- Tighten `/repomatic-ship`'s pre-push gate with `ruff format --check` and a `repomatic --version` dependency-resolution smoke run.

## [`6.24.0` (2026-05-28)](https://github.com/kdeldycke/repomatic/compare/v6.23.0...v6.24.0)

> [!NOTE]
> `6.24.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.24.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.24.0).

- Publish to PyPI right after the wheel builds instead of waiting for the full release engine, by splitting the build into a `_release-build.yaml` lane that `release.yaml`'s `publish-pypi` job depends on.

## [`6.23.0` (2026-05-28)](https://github.com/kdeldycke/repomatic/compare/v6.22.0...v6.23.0)

> [!NOTE]
> `6.23.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.23.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.23.0).

- Split `release.yaml` into a thin entry workflow and a new reusable `_release-engine.yaml` engine; the entry keeps `publish-pypi` so PyPI Trusted Publisher OIDC resolves to each repo's own `release.yaml`.
- Remove the `release-publish-pypi-job.yaml` data fragment; `release.yaml` is now the single source for the `publish-pypi` job.
- Require `uv` >= `0.11.15` for the vulnerability scan and parse `uv audit --output-format json` directly, raising a clear error on unsupported `uv` versions and deduplicating advisories across sources by alias.
- Refine `/repomatic-ship` to re-consolidate the changelog after the babysit phase and re-dispatch `changelog.yaml` after a code-only fix push so the release PR stays current.
- Extend `/babysit-ci` to also monitor `autofix.yaml`, diagnosing and fixing crashed mechanical-fix jobs instead of leaving them red on `main`.
- Fix `release.yaml`'s `compile-binaries` and `test-binaries` jobs aborting every non-release run with `Unexpected value ''` on projects with `nuitka.enabled = false`.

## [`6.22.0` (2026-05-25)](https://github.com/kdeldycke/repomatic/compare/v6.21.0...v6.22.0)

> [!NOTE]
> `6.22.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.22.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.22.0).

- `repomatic init` now prunes downstream orphans of renamed or removed skills, agents, and workflows; locally modified copies are reported for manual review, never deleted. Pass `--keep-removed` to report without deleting, or `--delete-removed-modified` to also delete modified ones.
- `/repomatic-ship` now closes with a reflect step that reviews the session for friction and proposes fixes to the upstream `repomatic` source.
- Fix the downstream caller's `publish-pypi` job aborting every non-release `release.yaml` run with `Unexpected value ''` when its `strategy.matrix` is empty.
- Fix `/repomatic-ship` and `/babysit-ci` dropping the `Co-Authored-By: Claude` trailer on their autonomous commits; both skills now require it self-containedly.
- `/babysit-ci` now treats a workflow run that fails with no individual job failure as a real workflow-level error to investigate.
- Fix the documentation site's live CLI-help and example blocks rendering empty since `click-extra` 7.15.0 made execution directives opt-in.
- Seed each tool section in the [tool-runner docs](https://kdeldycke.github.io/repomatic/tool-runner.html#available-tools) with a runnable `repomatic run` example and a minimal `[tool.X]` snippet.

## [`6.21.0` (2026-05-25)](https://github.com/kdeldycke/repomatic/compare/v6.20.0...v6.21.0)

> [!NOTE]
> `6.21.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.21.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.21.0).

- Replace the `repomatic-release` skill with `repomatic-ship`, a release orchestrator that reconciles changelog, code, and docs, then commits, pushes, and babysits CI until the release PR is ready. Review-gated by default, fully autonomous under `--dangerously-skip-permissions`.
- Add a `modernize` mode to the `repomatic-deps` skill that reads upgraded dependencies' changelogs and refactors code to adopt their new features, gating each change on the test suite.
- Extend the `babysit-ci` skill to also monitor and triage the Nuitka `compile-binaries` job in `release.yaml`.
- Decouple the downstream caller's `publish-pypi` job from the run's overall result: it now runs under `always()` and gates on a new `package_built` output, so a cleanly built wheel publishes even when an unrelated job fails.
- Remove the `repomatic-sync`, `repomatic-lint`, and `repomatic-test` skills, which only wrapped CLI commands CI already runs on every push.
- Fix the `bump-version` job in `changelog.yaml` leaving an orphan version-bump PR open after a competing bump merged into `main`.
- Enable myst-parser's `alert` extension so GitHub-style alerts (`> [!NOTE]`, `> [!IMPORTANT]`) render as admonitions on the documentation site.
- Give each tool section in the [tool-runner docs](https://kdeldycke.github.io/repomatic/tool-runner.html) a hand-maintained extra-docs region preserved across regenerations, seeded for Nuitka, and add Nuitka to the page's `[tool.X]`-support table.

## [`6.20.0` (2026-05-24)](https://github.com/kdeldycke/repomatic/compare/v6.19.0...v6.20.0)

> [!NOTE]
> `6.20.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.20.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.20.0).

- **Breaking:** remove `[tool.repomatic] nuitka.extra-args`. Configure Nuitka flags through `[tool.nuitka]` in `pyproject.toml` instead (`--include-data-files=SRC=DEST` becomes `include-data-files = ["SRC=DEST"]`).
- `repomatic run nuitka` now installs the pinned Nuitka, reads `[tool.nuitka]` from `pyproject.toml`, and passes the section as CLI flags; Nuitka appears in `repomatic run --list`.
- Build Nuitka binaries on Python 3.14.
- Switch `[tool.typos]` sync to `ONGOING`: canonical proper-noun identifiers merge into a pre-existing `[tool.typos]` section instead of skipping it, preserving local keys and entries.
- Add `[[tool.bumpversion.files]]` rules to the bundled template so downstream Python repos sync `[tool.nuitka]`'s numeric version keys without rewriting them on `[project]` bumps.
- Add `test-matrix.unstable` config: matrix-key dicts (like `{click-version = "main"}`) that mark matching full-matrix combinations `continue-on-error` in CI.
- Add a `lint-repo` check warning when a `[tool.repomatic.test-matrix] exclude` entry references a runner or Python version absent from the live matrix axes.
- Add `workflow_dispatch` triggers to `release.yaml` and `update-checksums.yaml` for manual re-runs.

## [`6.19.0` (2026-05-21)](https://github.com/kdeldycke/repomatic/compare/v6.18.4...v6.19.0)

> [!NOTE]
> `6.19.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.19.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.19.0).

- Add `repomatic close-stale-bump-pr --part minor|major` to close orphan version-bump PRs left by races between the `changelog.yaml` schedule and a competing push.
- Expand sponsor benefits in the awesome template's `contributing.md`: sponsors get a dedicated entry in the matching section and a waiver on the licensing-marker requirement.
- Switch the `Sync uv.lock` steps in `changelog.yaml` from `uv sync` to `uv lock --upgrade`, folding pending transitive refreshes into the bump commit.
- Make `lint-changelog --fix` refuse to rewrite admonitions when an upstream GitHub or PyPI lookup looks unhealthy, instead of applying a corrupted view.
- Skip CI for automated version-bump operations across `tests.yaml`, `lint.yaml`, `labels.yaml`, and `release.yaml` via a unified `metadata` gate.
- Reduce CI scheduling with `paths-ignore`/`paths:` filters and per-job gates that skip lint jobs when no relevant files changed.
- Bump Biome from `2.4.14` to `2.4.15`.

## [`6.18.4` (2026-05-14)](https://github.com/kdeldycke/repomatic/compare/v6.18.3...v6.18.4)

> [!NOTE]
> `6.18.4` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.4/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.4).

- Replace `RepoScope.NON_AWESOME` with `PYTHON_ONLY`, gating Python-flavored components on a PEP 621 `[project].name` so dotfiles repos carrying `pyproject.toml` only for `[tool.*]` config skip them by default.
- The bundled `release-publish-pypi-job.yaml` fragment now participates in the `@main` to `@vX.Y.Z` rewrite, so wheels built from a freeze commit ship with the pinned action ref.
- Bump Biome from `2.4.13` to `2.4.14` and Lychee from `0.24.1` to `0.24.2`.
- Fix `fix-vulnerable-deps` placing `exclude-newer-package` at the end of `[tool.uv]`, which triggered a spurious `format-pyproject` PR on the next run.

## [`6.18.3` (2026-05-11)](https://github.com/kdeldycke/repomatic/compare/v6.18.2...v6.18.3)

> [!NOTE]
> `6.18.3` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.3).

- Fix `autofix.yaml`'s `setup-guide` job being skipped on `workflow_dispatch` re-runs.
- Fix `release.yaml`'s `publish-pypi` job running against downstream callers and failing PyPI trusted publishing with a `job_workflow_ref` mismatch.
- Switch the `compile-binaries` job from `--onefile` to `--mode=onefile`, the documented spelling since Nuitka 4.0.

## [`6.18.2` (2026-05-08)](https://github.com/kdeldycke/repomatic/compare/v6.18.1...v6.18.2)

> [!NOTE]
> `6.18.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.2).

- Fix `release.yaml` uploading distributions to PyPI without PEP 740 attestations; the build job now signs each dist file and ships the `.publish.attestation` sidecars alongside it.

## [`6.18.1` (2026-05-08)](https://github.com/kdeldycke/repomatic/compare/v6.18.0...v6.18.1)

> [!NOTE]
> `6.18.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.18.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.1).

- Fix the `publish-pypi` composite action verifying build attestations on every workspace file instead of just the downloaded distribution artifacts.

## [`6.18.0` (2026-05-07)](https://github.com/kdeldycke/repomatic/compare/v6.17.0...v6.18.0)

> [!NOTE]
> `6.18.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.18.0).

> [!WARNING]
> `6.18.0` is **not available** on 🐍 PyPI.

- **Breaking:** drop `PYPI_TOKEN` from the `release.yaml` `workflow_call.secrets:` interface. Regenerate the thin-caller workflow with `repomatic init workflows` and register a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) for your own `release.yaml`.
- Add the [`publish-pypi`](https://github.com/kdeldycke/repomatic/blob/main/.github/actions/publish-pypi/action.yaml) composite action that publishes via OIDC Trusted Publishing with build-attestation verification; each downstream thin-caller now runs a generated `publish-pypi` job.
- Add a `check_pypi_trusted_publisher` probe to `lint-repo` and a `setup-guide-pypi-trusted-publisher` step that points to a pre-filled PyPI publisher settings URL and stays open until the first OIDC-attested upload.
- Add `release_commits_matrix` and `package_name` outputs to the reusable `release.yaml` so callers can drive their own matrix and gate jobs on a release commit.
- New composite actions under `.github/actions/` now participate in `@main` ↔ `@vX.Y.Z` ref freeze/unfreeze without code changes.
- Fix `sync-repomatic` proposing to delete `.github/actions/publish-pypi/action.yaml` when it matched the bundled default; the file must stay on disk for GitHub Actions to resolve the `uses:` path.

## [`6.17.0` (2026-05-04)](https://github.com/kdeldycke/repomatic/compare/v6.16.0...v6.17.0)

> [!NOTE]
> `6.17.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.17.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.17.0).

- Add `--template-file <path>` and `--template-arg KEY=VALUE` flags to `repomatic pr-body` so downstream repos can render project-specific PR templates without forking. `--template` and `--template-file` are mutually exclusive.
- Fix backslash-escaped brackets rendering literally in `docs/configuration.md` `**Type:**` lines (like `list\[dict[str, str]\]`).
- Fix doubled heading anchors on `docs/configuration.html` and `docs/workflows.html` (like `#dev-release-sync-dev-release-sync`).
- Collapse the most recent Python compatibility matrix row in `docs/install.md` to a major-version wildcard (like `6.x`) so the table stays stable across minor releases.

## [`6.16.0` (2026-04-29)](https://github.com/kdeldycke/repomatic/compare/v6.15.0...v6.16.0)

> [!NOTE]
> `6.16.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.16.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.16.0).

- Add the `sphinx-docs` agent to the `agents` component, deployed by `repomatic init agents` or via `[tool.repomatic] include = ["agents"]`.
- Add three `[tool.repomatic.workflow]` knobs for customizing `paths:` filters in generated thin callers: `extra-paths` appends repo-specific entries, `ignore-paths` strips canonical entries absent downstream, and `paths` replaces a filter wholesale per workflow.
- Add a Python compatibility matrix to `docs/install.md`, auto-generated from the `Programming Language :: Python` classifiers declared at every release tag.
- Render each command's `--help` live in `docs/cli.md` via `{click:run}` directives instead of captured plain-text help blocks.
- Replace the `Type` column in the `docs/configuration.md` summary table with a one-line description derived from each option's docstring, and lead each per-option section with that one-liner.
- Detect vulnerable dependencies from the GitHub Advisory Database alongside the PyPA database: `fix-vulnerable-deps` now unions `uv audit` with Dependabot alerts and credits each entry's source. Configurable via `[tool.repomatic] vulnerable-deps.sources`.
- Fix generated thin-caller fidelity: triggers mirror the canonical workflow verbatim instead of always injecting `workflow_dispatch`, universal path entries are preserved, and `repomatic workflow lint` now flags extra triggers absent upstream.
- Fix `sync-uv-lock` and `fix-vulnerable-deps` PR bodies showing `1-01-01` as the `exclude-newer` cutoff when `pyproject.toml` configures a relative span like `"1 week"`.
- Fix broken documentation links in all 18 PR body templates, now pointing at the published `configuration.html` and `workflows.html` anchors with each option name linked to its own anchor.
- Fix `release.yaml` discarding healthy binaries when one matrix cell crashed: the `compile-binaries` matrix sets `fail-fast: false` and `publish-release` uploads whatever built.
- Fix `update-docs` ↔ `format-markdown` ping-pong on `docs/cli.md` and `docs/configuration.md`.
- Bump pinned `uv` to `0.11.8` and `mdformat-pelican` to `1.0.0`, fixing non-ASCII anchor links being percent-encoded on every `format-markdown` run.

## [`6.15.0` (2026-04-27)](https://github.com/kdeldycke/repomatic/compare/v6.14.0...v6.15.0)

> [!NOTE]
> `6.15.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.15.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.15.0).

- Decode percent-encoded non-ASCII characters in Markdown link destinations back to their original form, so non-ASCII anchors no longer get rewritten to `%XX` on every `format-markdown` run.
- Add 💸/🆓 licensing markers to the awesome-list contributing guide, issue template, and PR template (English and Chinese mirrors): 💸 for a paid version atop an OSS core, 🆓 for fully open-source.
- Add the `agents` component to `repomatic init` for deploying Claude Code agents (`grunt-qa`, `qa-engineer`) downstream. Excluded by default; opt in via `[tool.repomatic] include = ["agents"]`. Destination set by `[tool.repomatic] agents.location`.
- Add `docs/benchmark.md` comparing repomatic against ten alternatives across template sync, repo governance, release automation, and changelog lifecycle.
- Switch MyST admonitions to backtick fences (```` ```{note} ````) instead of colon fences project-wide so `mdformat` preserves them; the `convert-to-myst` command now emits backtick fences.
- Expand the `myst_docstrings` Sphinx extension: convert plain triple-backtick code fences and footnotes to reST, and run MyST-to-reST conversion before `sphinx_autodoc_typehints`.
- Upgrade lychee to `0.24.1`, which reads its `[tool.lychee]` config directly from `pyproject.toml` so repomatic drops the TOML translation bridge.
- Fix `repomatic init` reporting unchanged files as updated; re-running against an unchanged tree is now a true no-op.
- Fix `update-docs` ↔ `format-markdown` ping-pong on `docs/tool-runner.md`.

## [`6.14.0` (2026-04-20)](https://github.com/kdeldycke/repomatic/compare/v6.13.0...v6.14.0)

> [!NOTE]
> `6.14.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.14.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.14.0).

- Add a Sphinx documentation site (Furo theme, MyST-Parser) splitting the monolithic `readme.md` into focused pages: installation, configuration, CLI parameters, reusable workflows, security, skills, and a tool runner tutorial. Deployed via `docs.yaml`.
- Add the `repomatic.myst_docstrings` Sphinx extension and `repomatic.myst_converter` utility, converting MyST markdown in docstrings to reST at build time so `sphinx.ext.autodoc` works unmodified. `convert-to-myst` rewrites source files in place.
- Add a `--sort-by` option to the `show-config`, `metadata --list-keys`, `run --list`, and `cache show` commands; each defaults to a natural sort column and accepts any column name.
- Add an incremental mode to the `brand-assets` skill: when base SVGs already exist, skip the design menu and fill gaps directly.
- Add a `check_stale_gh_pages_branch` lint check and setup-guide instructions for deleting leftover `gh-pages` branches after switching to GitHub Actions deployment.
- Fix `Matrix.prune()` keeping exclude directives that reference keys absent from the matrix axes, which GitHub Actions rejects.
- Fix the setup-guide Pages step for Sphinx projects: reopen the issue when Pages is unconfigured, and offer both first-time-enable and update commands.
- Fix the `sponsor-label` job in `labels.yaml` missing an `actions/checkout` step, which caused it to fail.

## [`6.13.0` (2026-04-15)](https://github.com/kdeldycke/repomatic/compare/v6.12.0...v6.13.0)

> [!NOTE]
> `6.13.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.13.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.13.0).

- Add `nuitka.entry-points` config option to select which `[project.scripts]` entries produce Nuitka binaries; aliases pointing to the same callable are deduplicated by default.
- Add two-phase VirusTotal scanning: an initial table with scan links, then a `--poll` pass that fills in a Detections column of `flagged / total` engine counts.
- Add `av-false-positive` skill to scan release binaries on VirusTotal and generate per-vendor false-positive submission files for flagged artifacts.
- Add `update-checksums.yaml` workflow that recomputes SHA-256 checksums for binary tools bumped by Renovate and commits the fix to the PR branch.
- Include release notes for every intermediate version in `sync-uv-lock` PR bodies, not just the target version.
- Config `include` entries now bypass `RepoScope` filtering, matching explicit CLI component naming; qualified entries like `skills/awesome-triage` implicitly select their parent component.
- Add baseline criteria for GitHub repositories in awesome list contributing guidelines: minimum 50 stars, not archived, and updated within 3 years.
- Add `--min-savings-bytes` option to `format-images` (default 1024) to skip images whose absolute byte savings are negligible.
- Add cross-platform binary support (macOS arm64/x64, Linux arm64/x64, Windows x64) for actionlint, biome, gitleaks, labelmaker, lychee, shfmt, and typos, plus ZIP archive extraction.
- Show a progress bar during binary tool downloads when the server reports `Content-Length`; interactive terminals only, silent in CI.
- Verify cached binaries with a two-layer integrity model: the registry checksum at download time and a `.sha256` sidecar on every cache hit.
- Enable `[tool.actionlint]` config support, translating it to `.github/actionlint.yaml` at invocation time.
- Cache downloaded tool binaries across CI runs with `actions/cache`, keyed per tool, OS, and architecture.
- Replace `peaceiris/actions-gh-pages` with GitHub's native `actions/upload-pages-artifact` and `actions/deploy-pages` for documentation deployment, plus a `lint-repo` check that the Pages source is set to GitHub Actions.
- Add `benchmark-update` skill to create and maintain competitive benchmark pages (`docs/benchmark.md`) with `audit`, `init`, `add`, and `refresh-badges` modes.
- Add `upstream-audit` skill to create and maintain upstream contribution tracking pages (`docs/upstream.md`) with `audit`, `init`, `refresh`, and `sync-git` modes.
- Upgrade the macOS Intel runner from `macos-15-intel` to `macos-26-intel` across binary builds, the test matrix, and Nuitka compilation.
- Run the `lint-repo` workflow job on all repositories, not just Python projects, so generic checks apply to awesome lists too.
- Centralize GitHub token resolution with priority `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN` and automatic fallback to `GITHUB_TOKEN` on an expired PAT; `--has-pat` on `setup-guide` and `lint-repo` now auto-detects from `REPOMATIC_PAT`.
- Fix `exclude-newer-package` pruning in `pyproject.toml` to remove orphaned comments and emit `pyproject-fmt`-compatible inline tables.
- Give a clear error when exiftool is not installed instead of a bare `FileNotFoundError`, and verify it is on PATH after the Windows install step.
- Create parent directories for `--output` file paths in `repomatic run`, fixing lychee write errors when the output directory is missing.
- Sanitize `@mentions`, `#issue` references, and `github.com` URLs in Lychee and Sphinx linkcheck output before embedding them in the broken-links issue.

## [`6.12.0` (2026-04-13)](https://github.com/kdeldycke/repomatic/compare/v6.11.3...v6.12.0)

> [!NOTE]
> `6.12.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.12.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.12.0).

- **Breaking:** rename the `shell_files` metadata key to `shfmt_files`, and exclude Zsh files and `.sh` files with a Zsh shebang from `shfmt` processing.
- Add `repomatic cache` subcommands (`show`, `clean`, `path`) and a global binary cache for downloaded tools; cached binaries are re-verified against their checksum and auto-purged after 30 days (configurable via `REPOMATIC_CACHE_MAX_AGE`). Add `--no-cache` to `repomatic run` to bypass it.
- Add an HTTP response cache for PyPI metadata and GitHub release bodies to avoid redundant API calls, plus `--namespace` on `repomatic cache clean` for targeted cleanup.
- Route generated tool configs through the cache directory and pass them explicitly via `--config`, instead of writing to `/tmp` or the repository root.
- Add `--version`, `--checksum`, and `--skip-checksum` options to `repomatic run` to override the pinned tool version and SHA-256 verification at invocation time.
- Add structured logging to `repomatic run`: `--verbosity INFO` reports config precedence, the full command, and exit code; `DEBUG` adds parsed config details.
- Add `skills.location` config option to override the Claude Code skills directory (default `./.claude/skills/`).
- Add `changelog.location` config option to override the changelog file path (default `./changelog.md`), honored by all CLI commands.
- Add `.claude/package-skills.sh` to package each Claude Code skill as a ZIP for manual upload to Claude Desktop.
- Sanitize `@mentions`, `#issue` references, and `github.com` URLs in upstream release notes embedded in `sync-uv-lock` PR bodies to prevent auto-linking and backlink cross-references.
- Use the `REPOMATIC_PAT` token in all `peter-evans/create-pull-request` steps so created PRs trigger other workflows.
- Make the `uv sync` step in `lint-types` conditional on `is_python_project`, so repos with Python files but no lockfile can still be type-checked.
- Fix `format-json` failing with a `--config-path` error when a `[tool.biome]` section exists.
- Improve the `file-bug-report` skill to check organization-level community health files before per-repo files.

## [`6.11.3` (2026-04-09)](https://github.com/kdeldycke/repomatic/compare/v6.11.2...v6.11.3)

> [!NOTE]
> `6.11.3` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.3/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.3).

- Add a `lint-repo` check warning when the GitHub Actions fork PR approval policy is weaker than `first_time_contributors`, with a setup guide step to fix it.
- Add a `readme.md` supply chain security section mapping Astral's security practices to concrete repomatic implementations.
- Fix `rst_to_myst` conversion leaving RST backslash escapes in headings and not wrapping dotted module names in backticks.
- Fix the `format-pyproject` autofix job failing with exit code 123.
- Disable the uv cache in the `publish-pypi` release job, which has no checkout and emitted spurious cache-miss warnings.

## [`6.11.2` (2026-04-08)](https://github.com/kdeldycke/repomatic/compare/v6.11.1...v6.11.2)

> [!NOTE]
> `6.11.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.2).

- Add the `shfmt` shell formatter to the tool runner (`repomatic run shfmt`).
- Add a `format-shell` autofix job to auto-format shell scripts with `shfmt`.
- Replace the `crazy-max/ghaction-virustotal` action with a native `repomatic scan-virustotal` command, fixing the silently skipped release-body update.
- Deduplicate release attestations: Python packages are now attested once in `build-package` instead of three times, and `.gitignore` is no longer accidentally attested.

## [`6.11.1` (2026-04-08)](https://github.com/kdeldycke/repomatic/compare/v6.11.0...v6.11.1)

> [!NOTE]
> `6.11.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.11.1).

- Parallelize the release workflow: `compile-binaries` starts right after `metadata`, and `publish-pypi` runs concurrently with `create-tag` and `create-release`, with binary and attestation uploads deferred to `publish-release`.
- Fall back to the PyPI `project_urls` changelog link when a package has no GitHub Release, so release notes render a `[Changelog]` link instead of omitting the package.
- Fix the release workflow uploading the attestation bundle before the GitHub release draft existed.
- Skip `exclude-newer-package` exemptions for packages whose fixed version already falls within the `exclude-newer` cooldown window.
- Fix `--delete-excluded` not detecting scope-excluded component files that still exist on disk.
- Fix awesome-template sync overwriting `pyproject.toml` instead of merging, which stripped user-managed `[tool.*]` sections.
- Fix `repomatic init <component>` silently ignoring an explicitly requested component when its scope did not match the repo.
- Fix `--delete-excluded` removing opt-in workflow files in the source repo by skipping config-key exclusions there.
- Fix the `format-pyproject` autofix step running with no input files and masking tool errors.

## [`6.11.0` (2026-04-07)](https://github.com/kdeldycke/repomatic/compare/v6.10.0...v6.11.0)

> [!NOTE]
> `6.11.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.11.0/).

> [!WARNING]
> `6.11.0` is **not available** on 🐙 GitHub.

- Preserve extra downstream jobs when syncing thin-caller workflows; the managed job is regenerated in place while project-specific jobs, comments, and blank lines are kept.
- Add a VirusTotal scanning job to the release workflow that uploads compiled binaries to seed AV databases. Requires the optional `VIRUSTOTAL_API_KEY` repository secret.
- Verify each attestation in CI right after `actions/attest` with `gh attestation verify`.
- Upload Sigstore attestation bundles (`.jsonl`) as GitHub release assets for compiled binaries and Python packages, enabling offline verification.
- Add a `lint-repo` warning when `VIRUSTOTAL_API_KEY` is missing and Nuitka binary compilation is active.
- Add a VirusTotal API key setup step to the setup guide issue, shown only when Nuitka compilation is active.
- Remove the one-time bumpversion dev-versioning migration code now that all downstream repos use PEP 440 dev versioning.

## [`6.10.0` (2026-04-03)](https://github.com/kdeldycke/repomatic/compare/v6.9.0...v6.10.0)

> [!NOTE]
> `6.10.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.10.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.10.0).

- **Breaking:** Remove the `-o` short option from `pr-body` and `format-images`; use `--output`.
- Add `brand-assets` skill to create and export project logo/banner SVG assets to light/dark PNG variants.
- Add `babysit-ci` skill to monitor CI test workflows, diagnose failures, fix code, and loop until stable jobs pass.
- Add `file-bug-report` skill to write upstream bug reports from contribution guidelines, issue templates, and community norms.
- Add `test-matrix.replace` and `test-matrix.remove` config to swap or drop axis values in the test matrices.
- Add `sync_mode=ONGOING` for tool configs to repeatedly sync while preserving local additions, starting with `sync-bumpversion` keeping local `[[tool.bumpversion.files]]` entries.
- Add `--output-format [markdown|github-actions]` to `sync-uv-lock`, `fix-vulnerable-deps`, `pr-body`, and `format-images`, replacing implicit `$GITHUB_OUTPUT` detection.
- Add `.claude/scheduled_tasks.lock` to the default `.gitignore` extra content.
- Add a collapsible workflow metadata table (trigger, actor, commit, job, workflow, run link) to issue lifecycle comments.
- Make the `setup-guide` issue body a set of collapsible per-step sections with status indicators, and close it only once PAT, permissions, vulnerability alerts, and branch protection are all verified.
- Add `--release-notes/--no-release-notes` and `--table/--no-table` flags to `sync-uv-lock`, defaulting to a terminal table and reserving markdown for `--output`.
- Prune stale `exclude-newer-package` entries from `pyproject.toml` before relocking in `sync-uv-lock`.
- Make the `renovate` component opt-in, and exclude `renovate` and `codecov` from awesome-list repositories.
- Remove Python `3.15t` (free-threaded) from the default test matrix.
- Warn instead of crashing on unknown `[tool.repomatic]` configuration keys.
- Echo `metadata` output to stderr when `--output` targets a file, so computed matrices stay visible in CI logs.
- Add the `repomatic update-docs` command to run `sphinx-apidoc`, RST-to-MyST conversion, and `docs/docs_update.py` in one step.
- Add `docs.apidoc-extra-args`, `docs.apidoc-exclude`, and `docs.update-script` configuration options.
- Move the `sync-uv-lock` job from `renovate.yaml` to `autofix.yaml` so it runs on every push to `main`.
- Fix a CLI crash when `test-matrix.variations` or `test-matrix.replace` contain nested keys.

## [`6.9.0` (2026-03-31)](https://github.com/kdeldycke/repomatic/compare/v6.8.0...v6.9.0)

> [!NOTE]
> `6.9.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.9.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.9.0).

- **Breaking:** Rename the `config` subcommand to `show-config` (it now resolves typed `[tool.repomatic]` config via click-extra).
- **Breaking:** Remove the `prebake-version` and `prebake-tag-sha` commands; use `click-extra prebake` instead.
- Add per-project test matrix configuration via `[tool.repomatic.test-matrix]`, supporting `exclude`, `include`, and `variations`.
- Replace the `audit-deps` lint job with a `fix-vulnerable-deps` autofix job that opens PRs upgrading vulnerable packages.
- Add a `codecov` bundled component that syncs `.github/codecov.yaml` to suppress noisy PR comments.
- Support tool-runner config for tools that discover config from the working directory rather than a `--config` flag.
- Move the mdformat `number` default to a bundled `mdformat.toml` so downstream repos can override it.
- Expand PAT validation in `lint-repo` and `check-renovate` with repository scope, tag ruleset, and permission checks.
- Auto-exclude `changelog.md` for awesome-list repositories.
- Migrate from `actions/attest-build-provenance` to `actions/attest`.
- Run granular PAT permission checks in `setup-guide`, keeping the issue open with a diagnostic table when permissions are incomplete.
- Fix the `setup-guide` job so PAT detection works everywhere.
- Fix an infinite cycle between the `migrate-to-renovate` and `sync-repomatic` jobs.
- Include git stderr in `git-tag` CLI error messages.

## [`6.8.0` (2026-03-27)](https://github.com/kdeldycke/repomatic/compare/v6.7.0...v6.8.0)

> [!NOTE]
> `6.8.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.8.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.8.0).

- **Breaking:** Rename `repomatic init --delete-redundant` to `--delete-unmodified`, which now also removes config files identical to bundled defaults.
- **Breaking:** Remove the deprecated `WORKFLOW_UPDATE_GITHUB_PAT` secret and its fallbacks; downstream repos must use `REPOMATIC_PAT`.
- **Breaking:** Stop persisting `[tool.ruff]` defaults into downstream `pyproject.toml`; bundled ruff config is now injected at runtime when none exists.
- **Breaking:** Remove the `sync-renovate` command, autofix job, `renovate.sync` config toggle, and PR body template; `sync-repomatic` and runtime materialization replace them.
- **Breaking:** Merge `/repomatic-deps-review` into `/repomatic-deps`, which now supports `graph` and `review` modes.
- Move the test matrix definition into `repomatic metadata` so it is available in job-level `if:` conditions.
- Reduce CI jobs on pull requests by skipping release builds, experimental Python versions, and redundant verification tests; the full matrix still runs on push to `main`.
- Make `exclude` config additive to the default exclusions (`labels`, `skills`), and add an `include` config to force-include default-excluded components.
- Auto-exclude the `awesome-triage` skill for non-awesome repositories.
- Add `--delete-excluded` to `repomatic init` to remove excluded files that still exist on disk.
- Replace the `sync-workflows` and `clean-unmodified-configs` autofix jobs with a single `sync-repomatic` job that syncs and prunes managed files in one PR.
- Add PAT capability and repo configuration checks to `lint-repo` (Renovate config, Dependabot security updates off, vulnerability alerts on, PAT permissions).
- Add stale draft release detection to `lint-repo`, warning about draft releases whose tag does not end with `.dev0`.
- Relax the abandoned-dependency threshold from 1 year to 2 years in the Renovate config.
- Fix thin-caller generation rendering `workflow_dispatch` inputs as Python dicts instead of YAML.
- Add the `/sphinx-docs-sync` skill for cross-project Sphinx documentation comparison and synchronization.
- Add the `/translation-sync` skill to detect and draft fixes for stale `readme.*.md` and `contributing.*.md` translations; auto-excluded for non-awesome repos.
- Streamline Dependabot guidance in the setup-guide issue.
- Allow `repomatic init` to accept qualified `component/file` selectors (like `repomatic init skills/repomatic-topics`).
- Only auto-include the `awesome-template` component for `awesome-*` repos when no explicit components are given.
- Add a package version diff table to `sync-uv-lock` PRs, listing updated, added, and removed packages with PyPI links and collapsible release notes.
- Document file naming conventions in `claude.md`: prefer `.yaml` over `.yml` and lowercase filenames, with a table of GitHub exceptions.
- Fix awesome-template URL rewriting to also process `.yml` files in `.github/`.
- Auto-exclude the `changelog.yaml`, `debug.yaml`, and `release.yaml` workflows for `awesome-*` repositories.
- Materialize the bundled `renovate.json5` at runtime when absent, so downstream repos can safely delete their own copy.
- Pin GitHub Actions to SHA digests via Renovate's `helpers:pinGitHubActionDigestsToSemver` preset.
- Add top-level `permissions: {}` to all workflow files, requiring each job to declare its own minimal permissions.
- Fix `sync-repomatic` deleting the upstream repo's own skills.
- Generalize the `opt_in_key` config option into `config_key`/`config_default`.

## [`6.7.0` (2026-03-24)](https://github.com/kdeldycke/repomatic/compare/v6.6.0...v6.7.0)

> [!NOTE]
> `6.7.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.7.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.7.0).

- **Breaking:** Remove the `sync-skills`, `workflow create`, and `workflow sync` commands; `repomatic init` handles all three.
- Bundle awesome-template boilerplate files in `repomatic` instead of cloning `kdeldycke/awesome-template` at runtime.
- Format every `pyproject.toml` in the repo in the `format-pyproject` job, not just the root file.
- Add a branch protection checklist to the setup-guide issue, linking to a pre-filled ruleset creation form.
- Add an opt-in `unsubscribe.yaml` reusable workflow for scheduled cleanup of closed notification threads, enabled via `notification.unsubscribe = true` and requiring `REPOMATIC_NOTIFICATIONS_PAT`.
- Surface actual `gh` CLI error messages in `unsubscribe-threads` warnings.
- Enable `delete-branch: true` on all `peter-evans/create-pull-request` invocations so stale automation PRs auto-close.
- Add `gitleaks` to the tool runner with binary download and `[tool.gitleaks]` config bridge, and migrate `lint-secrets` to `repomatic run gitleaks`.
- Move lychee config from `lychee.toml` to `[tool.lychee]` in `pyproject.toml`.
- Fix the `format-images` job by installing `oxipng` from its GitHub release `.deb` so it runs on `ubuntu-slim`.

## [`6.6.0` (2026-03-23)](https://github.com/kdeldycke/repomatic/compare/v6.5.0...v6.6.0)

> [!NOTE]
> `6.6.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.6.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.6.0).

- **Breaking:** downstream repos with `yamllint` or `zizmor` in their `[tool.repomatic] exclude` list must remove those entries.
- Remove `yamllint` and `zizmor` init components; the tool runner falls back to bundled default configs at runtime. Default `exclude` is now `["labels", "skills"]`.
- Add `repomatic clean-redundant-configs` command and autofix job that removes native config files identical to bundled defaults; `repomatic init` warns about redundant configs on disk.
- Rename the `WORKFLOW_UPDATE_GITHUB_PAT` secret to `REPOMATIC_PAT`; workflows accept both names. Old-name repos get a migration issue that auto-closes once `REPOMATIC_PAT` is detected.
- Add a `setup-guide` toggle to `[tool.repomatic]` to suppress the setup guide issue.
- Pre-fill the fine-grained PAT creation form via URL and provide `gh` CLI commands for adding the secret, configuring Dependabot, and triggering a verify run.
- Add a `lint-repo` check that warns when the owner has GitHub Sponsors enabled but `.github/FUNDING.yml` is missing.

## [`6.5.0` (2026-03-23)](https://github.com/kdeldycke/repomatic/compare/v6.4.1...v6.5.0)

> [!NOTE]
> `6.5.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.5.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.5.0).

- **Breaking:** the old `init.exclude` and `workflow.sync-exclude` keys are no longer recognized and raise a hard error.
- **Breaking:** remove legacy `[tool.gha-utils]` and `[tool.repokit]` config migration; rename old sections to `[tool.repomatic]` manually.
- Replace `init.exclude` and `workflow.sync-exclude` with a unified `exclude` key: bare names exclude whole components, `component/identifier` entries exclude specific files.
- Add `repomatic run <tool>` for unified tool invocation with managed config resolution (native file, `[tool.X]`, bundled default, bare); use `--list` to see managed tools and their active config source.
- Register actionlint, autopep8, biome, bump-my-version, labelmaker, lychee, mdformat, mypy, pyproject-fmt, ruff, typos, yamllint, and zizmor with `repomatic run`, and migrate all workflow tool invocations to it.
- Add a `yamllint` init component, excluded from init by default like `zizmor`.
- Add `repomatic update-checksums --registry` to refresh SHA-256 hashes for binary tools.
- Add `[tool.lychee]` and `[tool.biome]` config translation, so downstream repos can configure lychee and biome from `pyproject.toml` without separate config files.

## [`6.4.1` (2026-03-11)](https://github.com/kdeldycke/repomatic/compare/v6.4.0...v6.4.1)

> [!NOTE]
> `6.4.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.4.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.4.1).

- Add a `github-json` output dialect to `repomatic metadata` that bundles all keys into a single `metadata` output, accessed via `fromJSON(needs.metadata.outputs.metadata).key_name`.
- Add key filtering to `repomatic metadata`: pass key names as arguments to output only those values.
- Add a `--list-keys` flag to `repomatic metadata` to list all available keys with descriptions.
- Rename the `project-metadata` job and step IDs to `metadata` across all workflows.
- Rename the `linters` init component to `zizmor`; default `init.exclude` is now `["labels", "skills", "zizmor"]`.
- Remove the `sync-zizmor` job, CLI command, and `zizmor.sync` toggle; `zizmor.yaml` is now user-owned and created by `repomatic init zizmor` if missing.
- Rename the `bump-versions` job to `bump-version` in `changelog.yaml`.
- Upgrade zizmor to `1.23.0` and re-enable the `template-injection` audit.
- Fix `repomatic metadata` list values breaking GitHub Actions `${{ }}` interpolation: lists are now pre-formatted (file lists as quoted strings, plain lists space-separated, dict lists as JSON).
- Fix `repomatic workflow sync --format header-only` erroring when a target workflow file is absent downstream; missing default files are skipped and named missing files warn instead.
- Enable parallel test execution by default via `--numprocesses=auto`.

## [`6.4.0` (2026-03-10)](https://github.com/kdeldycke/repomatic/compare/v6.3.2...v6.4.0)

> [!NOTE]
> `6.4.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.4.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.4.0).

- Rename `optimize-images` to `format-images`, aligning it with the `format-*` naming convention, and add a matching PR body template.
- Allow `--prefix` and `--template` to be combined in `repomatic pr-body`; the prefix is prepended before the rendered template.
- Add `awesome-template-sync`, `bumpversion-sync`, `dev-release-sync`, `gitignore-sync`, `labels-sync`, `mailmap-sync`, `uv-lock-sync`, and `zizmor-sync` toggles to `[tool.repomatic]`, so each sync operation can be individually disabled.
- Rename `sync-linter-configs` to `sync-zizmor` (and `linter-sync` to `zizmor-sync`), naming the sync job after the tool it syncs.
- Add a `repomatic sync-labels` command wrapping `labelmaker` with toggle check, profile detection, and extra label file handling.
- Replace `AndreasAugustin/actions-template-sync` with a native `repomatic sync-awesome-template` command.
- Add `repomatic init typos` to sync the shared typos spell-checker config into `pyproject.toml`, with proper-noun corrections and `<!-- typos:off -->` / `<!-- typos:on -->` block markers.
- Skip Ruff config injection in `format-python` for non-Python projects, and skip `sync-bumpversion` for non-Python projects.
- Use TOML sub-keys for grouped `[tool.repomatic]` options (like `nuitka.enabled`, `gitignore.location`, `test-plan.file`); only `pypi-package-history` stays flat.
- Add a `workflow-source-paths` option to `[tool.repomatic]`: thin-caller and header-only workflows gain `paths:` filters for the project's source directory, auto-derived from `[project.name]`.
- Add a `repomatic config` command that renders the `[tool.repomatic]` reference table.
- Add `### Configuration` sections to PR body templates listing the relevant `[tool.repomatic]` options.

## [`6.3.2` (2026-03-08)](https://github.com/kdeldycke/repomatic/compare/v6.3.1...v6.3.2)

> [!NOTE]
> `6.3.2` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.2/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.2).

- Add `--all-extras` to the `uv sync` step in `tests.yaml` to catch incompatibilities between optional dependency groups.
- Add a `test-package-install` job to `tests.yaml` that verifies every `[project.scripts]` entry point installs and runs via `uvx`, `uv run --with`, module invocation, `uv tool install`, and `pipx run`, from PyPI and GitHub. Add a `cli_scripts` metadata output.
- Sync `customManagers` to downstream `renovate.json5` so Renovate can update inline version pins in workflow files.
- Fix thin-caller generation stripping `paths` and `paths-ignore` filters, which incorrectly restricted CI triggers downstream.
- Fix the `optimize-images` job failing on `ubuntu-slim` where `oxipng` is unavailable.
- Add a `citation.cff` `date-released` update to the bundled `bumpversion.toml` template so downstream repos keep their release date in sync on version bumps.

## [`6.3.1` (2026-03-07)](https://github.com/kdeldycke/repomatic/compare/v6.3.0...v6.3.1)

> [!NOTE]
> `6.3.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.1).

- Sync the `repomatic-audit` skill to downstream repos.

## [`6.3.0` (2026-03-06)](https://github.com/kdeldycke/repomatic/compare/v6.2.1...v6.3.0)

> [!NOTE]
> `6.3.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.3.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.3.0).

- `repomatic init` now always overwrites managed files (workflows, configs, skills) by default; remove the `--overwrite` flag. `changelog.md` is never overwritten once it exists.
- `repomatic init` output now distinguishes created, updated, and skipped files, and warns about excluded files still on disk.
- Auto-remove legacy `.claude/skills/gha-*/` skill directories during `repomatic init`, completing the `gha-utils` to `repomatic` rename.
- `sync-bumpversion`, `sync-linter-configs`, and `sync-skills` now report both created and updated files.
- `sync-bumpversion` now replaces the whole `[tool.bumpversion]` section from the bundled template instead of applying incremental migrations.
- Use the short SHA in release workflow job names instead of the full commit hash.

## [`6.2.1` (2026-03-06)](https://github.com/kdeldycke/repomatic/compare/v6.2.0...v6.2.1)

> [!NOTE]
> `6.2.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.2.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.2.1).

- Fix `actions/checkout` wiping downloaded Python package artifacts before `gh release create` could attach them, so release drafts now include the distribution files.
- Fix `fix-changelog` marking releases as not available on GitHub while the release was still a draft.

## [`6.2.0` (2026-03-05)](https://github.com/kdeldycke/repomatic/compare/v6.1.0...v6.2.0)

> [!NOTE]
> `6.2.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.2.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.2.0).

- Add the `repomatic optimize-images` CLI command (lossless `oxipng` for PNG, `jpegoptim` for JPEG), replacing `calibreapp/image-actions`.
- Add the `sync-dev-release` CLI command and workflow job to maintain a rolling dev pre-release on GitHub with the latest binaries and Python package.
- Add the `repomatic-topics` skill for optimizing GitHub repository topics for discoverability.
- Add a `lint-repo` check that warns when GitHub topics are not a subset of `pyproject.toml` keywords.
- Add the `init-exclude` config option to skip components during `repomatic init`, defaulting to `["labels", "linters", "skills"]`; `workflow-sync-exclude` now also applies to `repomatic init`.
- Add `rename-from` rules to migrate all 9 default GitHub labels.
- Add the package version to compiled binary filenames (`repomatic-6.2.0-linux-arm64.bin`).
- Automatically migrate `[tool.gha-utils]` and `[tool.repokit]` config sections to `[tool.repomatic]` during `repomatic init`; commands fall back to legacy section names when `[tool.repomatic]` is absent.
- Support GitHub immutable releases by drafting releases then publishing them.
- Replace `softprops/action-gh-release` with `gh release create`; all release operations now use the `gh` CLI.
- Freeze readme binary download URLs to versioned `/releases/download/vX.Y.Z/` paths during releases.
- GitHub releases now include PyPI and GitHub availability links at creation time.
- Fix Nuitka-compiled binaries silently producing no output when the entry point is a `__main__.py` inside a package.
- Fix `update-checksums` leaving stale SHA-256 hashes when the hash and `sha256sum --check` keyword span multiple lines.
- Fix Windows ARM64 test runners using x86_64 emulation by forcing native ARM64 Python via `UV_PYTHON`.
- Fix `fix-changelog` producing a trailing blank line when the last changelog section is modified.

## [`6.1.0` (2026-02-27)](https://github.com/kdeldycke/repomatic/compare/v6.0.1...v6.1.0)

> [!NOTE]
> `6.1.0` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.1.0/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.1.0).

- Add the `unsubscribe-threads` CLI command to unsubscribe from closed, inactive GitHub notification threads.
- Add the `prebake-version` CLI command to inject the Git commit hash into `__version__` before Nuitka compilation, so binaries report the exact commit they were built from (e.g., `6.1.0.dev0+abc1234`).
- Add the `list-skills` CLI command to display all available Claude Code skills grouped by lifecycle phase.
- Add the `sync-github-releases` CLI command to sync GitHub release notes from `changelog.md`.
- Add the `pypi-package-history` config option so `lint-changelog` fetches releases from former package names and generates correct PyPI URLs for renamed projects.
- `lint-changelog` now detects orphaned versions (git tags, GitHub releases, or PyPI packages with no changelog entry) and inserts placeholder sections in `--fix` mode.
- Rename the `lint-changelog` workflow job to `fix-changelog`; the CLI command remains `lint-changelog`.
- Make changelog entries and GitHub release bodies template-driven via `release-notes.md` and `github-releases.md`, so editing one template affects only its destination.
- Group CLI commands into sections (Project setup, Release & versioning, Sync, Linting & checks, GitHub issues & PRs) in help output.
- Add next-step handoff suggestions to all Claude Code skills, and document skills with a grouped table and walkthrough in `readme.md`.
- Generate thin caller workflows with explicit secret forwarding instead of `secrets: inherit`.
- Move zizmor config from `.github/zizmor.yml` to `zizmor.yaml` at repo root.

## [`6.0.1` (2026-02-24)](https://github.com/kdeldycke/repomatic/compare/v6.0.0...v6.0.1)

> [!NOTE]
> First release under the [`repomatic`](https://pypi.org/project/repomatic/) name on PyPI, after `repokit` was rejected for typo-squatting ([see `6.0.0` below](#600-2026-02-24)). The GitHub repository is [`kdeldycke/repomatic`](https://github.com/kdeldycke/repomatic).

> [!NOTE]
> `6.0.1` is available on [🐍 PyPI](https://pypi.org/project/repomatic/6.0.1/) and [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.0.1).

- Rename project from `repokit` to `repomatic`. Rename GitHub repository from `kdeldycke/repokit` to `kdeldycke/repomatic`.

## [`6.0.0` (2026-02-24)](https://github.com/kdeldycke/repomatic/compare/v5.14.1...v6.0.0)

> [!CAUTION]
> This release was deleted from PyPI. It was supposed to be published as `repokit`, but PyPI flagged the name as typo-squatting the pre-existing [`repo-kit`](https://pypi.org/project/repo-kit/) package.

> [!NOTE]
> `6.0.0` is available on [🐙 GitHub](https://github.com/kdeldycke/repomatic/releases/tag/v6.0.0).

> [!WARNING]
> `6.0.0` is **not available** on 🐍 PyPI.

- Rename project from `gha-utils` to `repokit`. Rename GitHub repository from `kdeldycke/workflows` to `kdeldycke/repokit`.

## Earlier releases

> [!NOTE]
> Releases `5.14.1` and earlier are recorded in the [changelog archive](https://kdeldycke.github.io/repomatic/changelog-archive.html).
