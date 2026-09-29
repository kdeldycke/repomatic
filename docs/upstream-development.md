# {octicon}`gear` Upstream development

This page collects rules that apply only when working inside the `kdeldycke/repomatic` source repository itself. Repos that *use* the `repomatic` CLI through reusable workflows do not need to follow these rules.

## Documentation sync

The following documentation artifacts must stay in sync with the code in this repository. When changing any of these, update the others:

- **Version samples in `docs/workflows.md`**: Two samples must name the latest released tag: the `uses: kdeldycke/repomatic/.github/workflows/*.yaml@vX.Y.Z` reference in the example-usage section, and the `Upgrade repomatic to vX.Y.Z` pull request title in the `sync-repomatic` job description. Bump both by hand during the docs reconciliation pass. The `docs/install.md` version pins are covered under the auto-generated docs below.
- **Workflow job descriptions in `docs/workflows.md`**: Each `.github/workflows/*.yaml` workflow section must document all jobs by their actual job ID, with accurate descriptions of what they do, their requirements, and skip conditions.
- **PAT permissions**: `PAT_PERMISSION_PROBES` in `repomatic/github/token.py` is the single source of truth, one `PatProbe` row per fine-grained permission, run by `check_all_pat_permissions` for both `lint-repo` and `setup-guide` (so a new probe row reaches every consumer automatically). When changing permissions, update: the probe table and module docstring, the permission table and pre-filled URL in `repomatic/templates/setup-guide-token.md`, the `lint-repo` CLI docstring in `repomatic/cli/lint.py`, and the `lint-repo` job description in `docs/workflows.md`.
- **Repository configuration expectations**: The `lint-repo` job enforces repo settings described in the setup guide. When adding new setup steps, add a corresponding `RepoCheck` entry to `REPO_CHECKS` in `repomatic/lint_repo.py`, which `run_repo_lint()` walks. If the check cannot be automated, document the limitation in a comment.
- **PAT permission review**: When adding or removing workflow jobs that use `REPOMATIC_PAT`, review `PAT_PERMISSION_PROBES` to verify the permission set is still minimal and complete. Check `secrets.REPOMATIC_PAT` references across all workflow files to audit actual usage.
- **Module reference pages**: `[tool.repomatic] docs.apidoc-extra-args` passes `--separate`, so every module gets its own `docs/{module}.md` page. `repomatic update-docs` writes a missing page but never rewrites an existing one. It also lists each new page in the toctree of its package page (`docs/repomatic.md` for a top-level module, `docs/repomatic.{subpackage}.md` otherwise), so a new module needs no hand edit there. That wiring only adds: a module that is renamed or removed leaves its old page and toctree entry to delete by hand. `tests/test_readme.py` fails on a module that has no page. The file inventories that `tests/test_metadata.py` checks come from `git ls-files`, so a new tracked file needs no fixture edit, while an untracked file in the checkout fails them.

**Auto-generated docs (no manual sync needed):**

- **CLI parameters** in `docs/cli.md`: rendered live at build time from Click via the `{click:tree}` directive.
- **Configuration table** in `docs/configuration.md`: rendered live at build time from the `Config` dataclass via the `{click:config}` directive.
- **Binary download URLs and `Specific version` CLI pin** in `docs/install.md`: both version-pinned, both ratcheted forward to the new release automatically by `prepare-release`'s `freeze_install_download_urls` and `freeze_install_cli_version`. No manual bump needed.
- **Plugin marketplace pin** in `.claude-plugin/marketplace.json`, and the plugin manifest version in `.claude/.claude-plugin/plugin.json`: `prepare-release`'s `freeze_marketplace_pin` writes both the entry's `ref` and its `version` on the release commit. `freeze_plugin_manifest_version` stamps the same version into the manifest, which is the string the Claude Code CLI compares to detect an update (see [§ How the pin moves](claude-code-plugin.md#how-the-pin-moves)). Then `unfreeze_marketplace_ref` returns the `ref` to the default branch and leaves both versions on the release. No manual bump needed, and deliberately not a `[[tool.bumpversion.files]]` entry: that would rewrite the version on the post-release bump too, advertising a `vX.Y.Z.devN` release that never exists.

## Tool runner: flags vs config

When adding or modifying a tool in `TOOL_REGISTRY`, choose the right mechanism for each default based on whether downstream repos should be able to override it:

**`default_flags`**: operational/cosmetic flags that are always applied and not overridable: output formatting (`--color`), operational mode (`--write-changes`, `--in-place`), enforcement level (`--strict`), network policy (`--offline`), tool-specific quirks with no config-file equivalent.

**`default_config`** (bundled file in `repomatic/data/`): behavioral preferences a downstream repo might legitimately want to override via its own config: lint rule selection, formatting preferences (numbering, line length), spell-check dictionaries, tool-specific rule configuration (severity, thresholds).

The test: if a downstream repo might reasonably want the opposite setting, it belongs in a config file. CLI flags take precedence over config files in most tools, so an overridable preference in `default_flags` silently prevents downstream customization.

**Config delivery has two paths** depending on whether the tool accepts a `--config` flag:

- Tools with `config_flag`: the bundled default is passed via that flag at invocation time.
- Tools without `config_flag` (CWD-discovery only): the bundled default is written to the first `native_config_files` path in CWD and cleaned up after invocation. A second run of the same tool in that directory waits for the first to finish, so it never reads a config the first run is about to remove.

## Release checklist

The release process is automated by the `release.yaml` workflow. See [§ Release engineering](workflows.md#release-engineering) for the complete list (git tag, GitHub release, binaries, PyPI, changelog) and design rationale for the workflow itself, including the `workflow_run` checkout pitfall, immutable-release semantics, concurrency strategies, and freeze/unfreeze commit structure.

### PyPI Trusted Publisher registration

The upstream `kdeldycke/repomatic` package publishes to PyPI via OIDC Trusted Publishing. The publisher is registered against the upstream's own `release.yaml` workflow file: the `publish-pypi` job inside that file runs only on the `push` trigger (self-release), so its OIDC `job_workflow_ref` claim resolves to `kdeldycke/repomatic/.github/workflows/release.yaml`. Downstream repos invoking the workflow via `workflow_call` skip that job and run their own caller-side `publish-pypi` job instead, which uses the [`publish-pypi`](https://github.com/kdeldycke/repomatic/blob/main/.github/actions/publish-pypi/action.yaml) composite action.
