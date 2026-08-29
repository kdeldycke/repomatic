# {octicon}`gear` Upstream development

This page collects rules that apply only when working inside the `kdeldycke/repomatic` source repository itself. Repos that *use* the `repomatic` CLI through reusable workflows do not need to follow these rules.

## Documentation sync

The following documentation artifacts must stay in sync with the code in this repository. When changing any of these, update the others:

- **Example workflow ref in `docs/workflows.md`**: The `uses: kdeldycke/repomatic/.github/workflows/*.yaml@vX.Y.Z` reference in the example-usage section must reflect the latest released tag; bump it by hand during the docs reconciliation pass (the `docs/install.md` version pins are covered under the auto-generated docs below).
- **Workflow job descriptions in `docs/workflows.md`**: Each `.github/workflows/*.yaml` workflow section must document all jobs by their actual job ID, with accurate descriptions of what they do, their requirements, and skip conditions.
- **PAT permissions**: `PAT_PERMISSION_PROBES` in `repomatic/github/token.py` is the single source of truth, one `PatProbe` row per fine-grained permission, run by `check_all_pat_permissions` for both `lint-repo` and `setup-guide` (so a new probe row reaches every consumer automatically). When changing permissions, update: the probe table and module docstring, the permission table and pre-filled URL in `repomatic/templates/setup-guide.md`, the `lint-repo` CLI docstring in `repomatic/cli/main.py`, and the `lint-repo` job description in `docs/workflows.md`.
- **Repository configuration expectations**: The `lint-repo` job enforces repo settings described in the setup guide. When adding new setup steps, add a corresponding check to `run_repo_lint()` in `repomatic/lint_repo.py`. If the check cannot be automated, document the limitation in a comment.
- **PAT permission review**: When adding or removing workflow jobs that use `REPOMATIC_PAT`, review `PAT_PERMISSION_PROBES` to verify the permission set is still minimal and complete. Check `secrets.REPOMATIC_PAT` references across all workflow files to audit actual usage.
- **`test_metadata` file-inventory fixtures**: `tests/test_metadata.py` pins the exact set of tracked files that `repomatic metadata` enumerates. Adding a file the command emits requires updating the matching hardcoded list, or the `test_metadata_*_format` tests fail: a new module (`*.py`) needs the `python_files` list, plus an automodule block on the page of the package that holds it (`docs/repomatic.md` for a top-level module, `docs/repomatic.{subpackage}.md` otherwise); a new `docs/*.md` page, `repomatic/templates/*.md` template, or skill `SKILL.md` needs the two markdown-file lists (alphabetical). The drift surfaces only in CI, since `repomatic metadata` auto-globs the file while the fixture does not.

**Auto-generated docs (no manual sync needed):**

- **CLI parameters** in `docs/cli.md`: rendered live at build time from Click via the `{click:tree}` directive.
- **Configuration table** in `docs/configuration.md`: rendered live at build time from the `Config` dataclass via the `{click:config}` directive.
- **Binary download URLs and `Specific version` CLI pin** in `docs/install.md`: both version-pinned, both ratcheted forward to the new release automatically by `prepare-release`'s `freeze_install_download_urls` and `freeze_install_cli_version`. No manual bump needed.
- **Plugin marketplace pin** in `.claude-plugin/marketplace.json`: `prepare-release`'s `freeze_marketplace_pin` writes both the entry's `ref` and its `version` on the release commit, then `unfreeze_marketplace_ref` returns the `ref` to the default branch and leaves the `version` on the release. No manual bump needed, and deliberately not a `[[tool.bumpversion.files]]` entry: that would rewrite the version on the post-release bump too, advertising a `vX.Y.Z.devN` release that never exists.

## Tool runner: flags vs config

When adding or modifying a tool in `TOOL_REGISTRY`, choose the right mechanism for each default based on whether downstream repos should be able to override it:

**`default_flags`**: operational/cosmetic flags that are always applied and not overridable: output formatting (`--color`), operational mode (`--write-changes`, `--in-place`), enforcement level (`--strict`), network policy (`--offline`), tool-specific quirks with no config-file equivalent.

**`default_config`** (bundled file in `repomatic/data/`): behavioral preferences a downstream repo might legitimately want to override via its own config: lint rule selection, formatting preferences (numbering, line length), spell-check dictionaries, tool-specific rule configuration (severity, thresholds).

The test: if a downstream repo might reasonably want the opposite setting, it belongs in a config file. CLI flags take precedence over config files in most tools, so an overridable preference in `default_flags` silently prevents downstream customization.

**Config delivery has two paths** depending on whether the tool accepts a `--config` flag:

- Tools with `config_flag`: the bundled default is passed via that flag at invocation time.
- Tools without `config_flag` (CWD-discovery only): the bundled default is written to the first `native_config_files` path in CWD and cleaned up after invocation.

## Release checklist

The release process is automated by the `release.yaml` workflow. See [§ Release engineering](workflows.md#release-engineering) for the complete list (git tag, GitHub release, binaries, PyPI, changelog) and design rationale for the workflow itself, including the `workflow_run` checkout pitfall, immutable-release semantics, concurrency strategies, and freeze/unfreeze commit structure.

### PyPI Trusted Publisher registration

The upstream `kdeldycke/repomatic` package publishes to PyPI via OIDC Trusted Publishing. The publisher is registered against the upstream's own `release.yaml` workflow file: the `publish-pypi` job inside that file runs only on the `push` trigger (self-release), so its OIDC `job_workflow_ref` claim resolves to `kdeldycke/repomatic/.github/workflows/release.yaml`. Downstream repos invoking the workflow via `workflow_call` skip that job and run their own caller-side `publish-pypi` job instead, which uses the [`publish-pypi`](https://github.com/kdeldycke/repomatic/blob/main/.github/actions/publish-pypi/action.yaml) composite action.
