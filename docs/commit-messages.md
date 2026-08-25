# {octicon}`git-commit` Commit messages

Most of what a repository writes to its own history is machine-generated: `sync-*` and `format-*` jobs commit their output, the release lane commits the freeze and unfreeze, and dependency bots open their own pull requests. Those messages are read back by other machines, which makes the commit subject a small shared namespace rather than free text.

## Square brackets are reserved

```{important}
A `[bracketed]` prefix in a commit subject is reserved for a **load-bearing mechanism that parses it back**. Never add one for decoration, categorization, or as a substitute for saying what changed.
```

The rule exists because a bracket prefix is not a label: it is an interface. Something downstream matches on it, and inventing a new one either collides with an existing matcher or trains readers to expect a meaning nothing enforces. The test to apply before writing one: *name the code that reads it.* If nothing does, write plain prose instead.

One prefix family is load-bearing in a repomatic-managed repository: every machine-authored version-machinery commit starts with {data}`repomatic.git_ops.CHANGELOG_COMMIT_PREFIX` (`[changelog] `), which is what lets workflow gates skip machinery pushes on a single `startsWith` clause. The members, all matched literally:

| Prefix                                   | Emitted by                      | Parsed by                                                                                                    |
| :--------------------------------------- | :------------------------------ | :----------------------------------------------------------------------------------------------------------- |
| `[changelog] Release vX.Y.Z`             | The release freeze commit       | The auto-tagging job, which locates the commit to tag **by its message**: a squash merge breaks it           |
| `[changelog] Post-release bump …`        | The unfreeze commit             | {data}`repomatic.git_ops.VERSION_BUMP_COMMIT_PREFIXES`, gating whether workflows run for a version-bump push |
| `[changelog] Bump major/minor version …` | The `bump-version` job's merges | {data}`repomatic.git_ops.MANUAL_VERSION_BUMP_COMMIT_PREFIXES`, the subset `tests.yaml` also skips on         |

Everything else repomatic commits carries no prefix. Each `sync-*`, `format-*` and `fix-*` job takes its subject from its pull request template's `title:` field, giving `Sync action pins`, `` Sync `uv.lock` ``, `Format Markdown`, `Fix vulnerable dependencies`: imperative, capitalized, no trailing period, identifiers backticked, no prefix.

## Who else reads or writes commit messages

An inventory of what this project depends on, so you know which parts of the subject line are already claimed. Only GitHub Actions and git itself *parse* a commit message; the rest either write one you control or never look.

| Tool or service                                                                                                        | Interaction          | Bracket convention                                                                                                                                                                                                                              |
| :--------------------------------------------------------------------------------------------------------------------- | :------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [GitHub Actions](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs)                   | **Parses**           | Skips the run when `[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]` or `[actions skip]` appears **anywhere** in the message. Also honors a `skip-checks:true` trailer. `push` and `pull_request` only, so `pull_request_target` still runs |
| git                                                                                                                    | **Parses**           | No brackets, but `squash!`, `fixup!` and `amend!` subject prefixes drive `rebase --autosquash`, and `git revert` generates `Revert "<subject>"`                                                                                                 |
| repomatic                                                                                                              | Writes and parses    | `[changelog]` only, per the table above                                                                                                                                                                                                         |
| [Dependabot](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference) | Writes               | No bracket default: it mimics the patterns it detects in the repository. A configured `commit-message.prefix` gets a colon appended automatically when it ends in a letter, digit, `)` or `]`, so `[deps]` becomes `[deps]:`                    |
| [Renovate](https://docs.renovatebot.com/configuration-options/)                                                        | Writes               | No prefix by default (`commitMessagePrefix` is `null`); a semantic prefix like `chore(deps):` appears only with `semanticCommits` enabled. Its docs use `[skip ci]` as a `commitBody` example                                                   |
| `bump-my-version`                                                                                                      | Writes, when told to | Free-form `message` template. repomatic does not use it to commit: it edits files and the workflow commits them                                                                                                                                 |
| `repomatic pr-sync`                                                                                                    | Writes               | Commits what it renders from the PR template, so the convention is the template's                                                                                                                                                               |
| Every `repomatic run` tool                                                                                             | Neither              | Formatters and linters (`ruff`, `typos`, `mdformat`, `biome`, `shfmt`, `yamllint`, `actionlint`, `zizmor`, `gitleaks`, `lychee`, `oxipng`, `mypy`) read and write files, never git history                                                      |

```{warning}
The skip tokens are the reason this matters beyond style. They match **anywhere in the message**, not just at the start, and they are not limited to the subject: a token quoted in a commit body silently skips CI. Worse, a skipped required check sits in "Pending" forever and blocks the merge instead of failing loudly. Never paste one into a message, not even as an example.
```

## Writing the subject

One line under 72 characters, imperative mood, capitalized, no trailing period, every identifier backticked. Name what changed, not the category it belongs to.

This is deliberately **not** [Conventional Commits](https://www.conventionalcommits.org): no `feat:`, `fix:` or `chore:` prefixes. The verb already carries that information, and the repository's automated operations follow the same [`verb-noun` naming](operation-contracts.md) as their commits.

Avoid the bare one-word subject (`Typo`, `Lint`, `Fix`). It costs the next reader a `git show` to learn anything, and it reads identically to the fifty other commits that say the same word.

## Writing the body

Omit the body by default, even when the *why* is not evident from the diff. A body is not the place to explain the change, defend the approach, or restate what the diff already shows. Write one only in these three cases, and write no more than the case needs:

1. **The commit bundles orthogonal work.** It carries several unrelated tasks, or spans different domains, and one subject cannot name them all. Give one short line per strand.
2. **A public record holds the context.** Link it: the upstream issue or pull request, a commit in another repository, the specification or documentation page that forced the behavior, the discussion thread. Point at the commit being reverted or followed up on the same way.
3. **The commit resolves or references a tracked item.** Use `Closes #N` when merging into the default branch must close the issue, and `Related to #N` when it must not.

Forges render commit messages as HTML, so a link is the cheapest path from `git log` to the full story. A body carrying one is where accountability and traceability actually live.

Everything else belongs somewhere durable instead: a code comment, a docstring, `docs/`, or the pull request body. Never narrate the work in sequence or enumerate the files touched, since `git log --stat` lists the files and the diff shows the rest.
