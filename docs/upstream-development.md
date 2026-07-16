# {octicon}`tools` Upstream development

This page collects rules that apply only when working inside the `kdeldycke/repomatic` source repository itself. Repos that *use* the `repomatic` CLI through reusable workflows do not need to follow these rules.

## Documentation sync

The following documentation artifacts must stay in sync with the code in this repository. When changing any of these, update the others:

- **Example workflow ref in `docs/workflows.md`**: The `uses: kdeldycke/repomatic/.github/workflows/*.yaml@vX.Y.Z` reference in the example-usage section must reflect the latest released tag; bump it by hand during the docs reconciliation pass (the `docs/install.md` version pins are covered under the auto-generated docs below).
- **Workflow job descriptions in `docs/workflows.md`**: Each `.github/workflows/*.yaml` workflow section must document all jobs by their actual job ID, with accurate descriptions of what they do, their requirements, and skip conditions.
- **PAT permissions**: `PAT_PERMISSION_PROBES` in `repomatic/github/token.py` is the single source of truth, one `PatProbe` row per fine-grained permission, run by `check_all_pat_permissions` for both `lint-repo` and `setup-guide` (so a new probe row reaches every consumer automatically). When changing permissions, update: the probe table and module docstring, the permission table and pre-filled URL in `repomatic/templates/setup-guide.md`, the `lint-repo` CLI docstring in `repomatic/cli.py`, and the `lint-repo` job description in `docs/workflows.md`.
- **Repository configuration expectations**: The `lint-repo` job enforces repo settings described in the setup guide. When adding new setup steps, add a corresponding check to `run_repo_lint()` in `repomatic/lint_repo.py`. If the check cannot be automated, document the limitation in a comment.
- **PAT permission review**: When adding or removing workflow jobs that use `REPOMATIC_PAT`, review `PAT_PERMISSION_PROBES` to verify the permission set is still minimal and complete. Check `secrets.REPOMATIC_PAT` references across all workflow files to audit actual usage.

**Auto-generated docs (no manual sync needed):**

- **CLI parameters** in `docs/cli.md`: rendered live at build time from Click via the `{click:tree}` directive.
- **Configuration table** in `docs/configuration.md`: rendered live at build time from the `Config` dataclass via the `{click:config}` directive.
- **Binary download URLs** in `docs/install.md`: version-pinned, ratcheted forward to the new release by `prepare-release`'s `freeze_install_download_urls`. The `Specific version` install tab example is a separate, manually-bumped pin: update it by hand to the last released version during the docs reconciliation pass.

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
