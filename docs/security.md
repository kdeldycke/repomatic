# {octicon}`shield-lock` Security

## Supply chain security

`repomatic` implements most of the practices described in Astral's [Open Source Security at Astral](https://astral.sh/blog/open-source-security-at-astral) post, baked into a drop-in setup that any maintainer can inherit by pointing their workflows at the reusable callers.

| Astral practice                                                | How `repomatic` covers it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| :------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Ban dangerous triggers (`pull_request_target`, `workflow_run`) | The [lint-workflow-security](workflows.md#github-workflows-lint-yaml-jobs) job runs [`zizmor`](https://docs.zizmor.sh) on every push: see [`.github/workflows/lint.yaml`](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/lint.yaml)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Minimal workflow permissions                                   | [`check_workflow_permissions`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) parses every workflow file and warns when a custom-step workflow omits the top-level `permissions` key                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Pinned actions                                                 | All `uses:` refs pinned to full commit SHAs (with the semver tag preserved as a trailing comment) via the [`sync-action-pins`](workflows.md#github-workflows-autofix-yaml-jobs) autofix job. [`check_sha_pinning_required`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) warns when the repository's `sha_pinning_required` setting is off, and the [setup guide](https://github.com/kdeldycke/repomatic/blob/main/repomatic/templates/setup-guide-sha-pinning-required.md) ships a fix: GitHub itself then refuses to run any workflow referencing an action by a mutable tag, closing the gap where a hand-edited workflow slips one past an inline-suppressed `zizmor` finding                                                                                                                                                                                                                                                                     |
| No force-pushes to `main`                                      | [`check_branch_ruleset_on_default`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) verifies an active branch ruleset exists, and the [setup guide](https://github.com/kdeldycke/repomatic/blob/main/repomatic/templates/setup-guide-branch-ruleset.md) walks users through creating one. [`check_classic_branch_protection`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) reports any branch protection rule left beside that ruleset: GitHub still supports both and [applies them together](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets), so a rule surviving a migration splits the branch policy across two settings pages                                                                                                                                                                                                              |
| Immutable release tags                                         | [`check_immutable_releases`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) verifies [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) is enabled, and the release workflow rewrites `@main` refs to `@vX.Y.Z` during freeze: see [tagged workflow URLs](workflows.md#tagged-workflow-urls)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Dependency cooldowns                                           | `minimum-release-age` shared cooldown for `sync-tool-versions`, `sync-action-pins`, and `sync-workflow-pins`; `uv --exclude-newer` for Python packages via `sync-uv-lock`, with a per-package escape hatch for CVE fixes: see [`minimum-release-age`](configuration.md#minimum-release-age) and [cooldowns](workflows.md#cooldowns)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Install-time cooldowns                                         | Every workflow exports `UV_EXCLUDE_NEWER` and `NPM_CONFIG_MIN_RELEASE_AGE` at workflow level, so every `uvx`, `uv pip install`, `uv tool install`, `npm install` and `npx` in every job refuses a package published inside the window, transitive dependencies included. This covers the ad-hoc installs no pin or lockfile describes, including debugging steps and jobs added later: see [install-time cooldown](workflows.md#install-time-cooldown)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Trusted Publishing                                             | PyPI uploads via OIDC with no long-lived token. The [`publish-pypi`](workflows.md#github-workflows-release-yaml-jobs) job in each downstream caller workflow invokes the upstream [`publish-pypi`](https://github.com/kdeldycke/repomatic/blob/main/.github/actions/publish-pypi/action.yaml) composite action, which inherits the caller's OIDC context. This sidesteps [pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096), where reusable workflows mint an OIDC token whose `job_workflow_ref` does not match the downstream's PyPI Trusted Publisher config. [`check_pypi_trusted_publisher`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) reads PEP 740 provenance for the latest published file and warns when no bundle names this repo's own `release.yaml`; the [setup guide](https://github.com/kdeldycke/repomatic/blob/main/repomatic/templates/setup-guide-pypi-trusted-publisher.md) walks through the registration |
| Cryptographic attestations                                     | Every binary and wheel is attested to the workflow run that built it via `attest-build-provenance`: see the `Generate build attestations` steps in [`.github/workflows/_release-engine.yaml`](https://github.com/kdeldycke/repomatic/blob/main/.github/workflows/_release-engine.yaml)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Checksums in installer scripts                                 | The [`update-checksums`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/cli/main.py) CLI command regenerates SHA-256 checksums for every binary tool; invoked automatically by [`sync-tool-versions`](workflows.md#github-workflows-sync-tool-versions-yaml-jobs) whenever a tool version is bumped                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Fork PR approval policy                                        | [`check_fork_pr_approval_policy`](https://github.com/kdeldycke/repomatic/blob/main/repomatic/lint_repo.py) warns when the policy is weaker than `first_time_contributors`, and the [setup guide](https://github.com/kdeldycke/repomatic/blob/main/repomatic/templates/setup-guide-fork-pr-approval.md) ships a pre-filled `gh api` one-liner to fix it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |

> [!WARNING]
> **Known gap: multi-person release approval.** Astral gates releases behind a dedicated [GitHub deployment environment](https://docs.github.com/en/actions/managing-workflow-runs/reviewing-deployments) with required reviewers, so that a single compromised account cannot publish. `repomatic` does not enforce this, but if the repository has multiple maintainers, I recommend adding an `environment: release` key to the caller-side `publish-pypi` job (and to the upstream `create-release` job, if the caller exposes it) in the downstream workflow and configuring required reviewers on that environment in repo settings.

> [!IMPORTANT]
> **One-time PyPI Trusted Publisher setup.** Each downstream repository must register a Trusted Publisher entry on PyPI for its own caller workflow. The publisher config matches against the OIDC `job_workflow_ref` claim, which names the downstream's workflow file (typically `.github/workflows/release.yaml`). Without this registration, the first PyPI upload after migration fails cleanly with a publisher mismatch error. See the [PyPI Trusted Publishers documentation](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) for the registration steps.

### Third-party action minimization

Every third-party GitHub Action executes with access to `GITHUB_TOKEN` and repository secrets. Each action is a trust delegation: you depend on the maintainer's security practices, their CI pipeline, and their transitive dependencies. A compromised action can steal secrets, inject code into builds, or tamper with releases.

`repomatic` has systematically eliminated 23 third-party actions since late 2025, replacing them with internal CLI commands, SHA-256-verified binary downloads, and runner built-in tools:

| Removed action                           | Replacement                 | Strategy                |
| :--------------------------------------- | :-------------------------- | :---------------------- |
| `calibreapp/image-actions`               | `repomatic format-images`   | Internal CLI            |
| `crazy-max/ghaction-virustotal`          | `repomatic scan-virustotal` | Internal CLI            |
| `AndreasAugustin/actions-template-sync`  | `repomatic init`            | Internal CLI            |
| `JasonEtco/is-sponsor-label-action`      | `repomatic sponsor-label`   | Internal CLI            |
| `dessant/lock-threads`                   | `repomatic lock-threads`    | Internal CLI            |
| `actions/labeler`                        | `repomatic apply-labels`    | Internal CLI            |
| `github/issue-labeler`                   | `repomatic apply-labels`    | Internal CLI            |
| `peter-evans/create-pull-request`        | `repomatic pr-sync`         | Internal CLI            |
| `lycheeverse/lychee-action`              | `repomatic run lychee`      | Direct binary + SHA-256 |
| `crate-ci/typos`                         | `repomatic run typos`       | Direct binary + SHA-256 |
| `biomejs/setup-biome`                    | `repomatic run biome`       | Direct binary + SHA-256 |
| `gitleaks/gitleaks-action`               | `repomatic run gitleaks`    | Direct binary + SHA-256 |
| `julb/action-manage-label`               | `repomatic run labelmaker`  | Direct binary + SHA-256 |
| `taiki-e/install-action`                 | Direct `curl` + checksum    | Direct binary + SHA-256 |
| `softprops/action-gh-release`            | `gh release create`         | Runner built-in         |
| `actions/github-script`                  | Bash + `gh` CLI             | Runner built-in         |
| `crazy-max/ghaction-dump-context`        | Bash + runner built-ins     | Runner built-in         |
| `actions-rust-lang/setup-rust-toolchain` | Runner built-in Rust        | Runner built-in         |
| `actions/setup-python`                   | `astral-sh/setup-uv`        | Consolidated            |
| `peaceiris/actions-gh-pages`             | `actions/deploy-pages`      | First-party replacement |
| `codecov/codecov-action`                 | None (integration dropped)  | Removed entirely        |
| `codecov/test-results-action`            | None (feature dropped)      | Removed entirely        |
| `GitHubSecurityLab/actions-permissions`  | Explicit `permissions:` key | Removed entirely        |

The only remaining third-party action (1 of 8 total) is `astral-sh/setup-uv`, which installs the toolchain every other job runs through.

That makes its own pin do double duty: it fixes the action's code, and it fixes which uv builds arrive checksum-verified, since `setup-uv` validates a download only against the table its release bundles and silently skips verification for anything else. `sync-workflow-pins` therefore never walks the uv pin past that table, and `lint-repo` reports a repository already sitting past it.

Every other `uses:` in the tree is GitHub's own: `actions/checkout`, `actions/cache`, `actions/upload-artifact`, `actions/download-artifact`, `actions/attest`, `actions/upload-pages-artifact` and `actions/deploy-pages`. All but `actions/checkout` speak the Actions cache, artifact, attestation and Pages backplanes, and are kept deliberately: those are internal service protocols GitHub reserves the right to change, so a reimplementation would break silently rather than loudly.

#### Issue and pull-request labelling

`repomatic apply-labels` covers what the two retired labeller actions did: file globs over a pull request's changed paths (`actions/labeler`) and content patterns over its title and body (`github/issue-labeler`). One job runs it on every issue and pull request opened; {mod}`repomatic.labels` holds the matcher, `tests/test_labels.py` pins the semantics, including the `minimatch` glob dialect.

The supply-chain gain is the usual one, and modest: both actions were SHA-pinned with cooldown-gated bumps. The concrete win is that the rules stopped being files. Each job used to run `repomatic init labels` to stage a YAML config purely so its action could read it back out of the checkout, and the actions' schemas dictated the config's shape all the way up into `[tool.repomatic.labels]`. Owning the matcher retired the staging step, the two bundled YAML files, the serializers that produced them, and the schema itself: a rule is now one label mapped to the keywords or globs that apply it, with the defaults living in {data}`repomatic.labels.DEFAULT_CONTENT_RULES` and {data}`~repomatic.labels.DEFAULT_FILE_RULES` as plain Python data.

It also fixed a silent bug the old shape could only warn about. `github/issue-labeler` AND-joined a label's `patterns`, so the obvious "any of these keywords" rule matched nothing; any pattern matching now applies the label, and keywords are matched case-insensitively on word boundaries instead of each rule hand-rolling `/\bword\b/i`.

#### Pull-request creation

`repomatic pr-sync` opens, refreshes and retires every pull request CI produces, across all 22 call sites that used to run `peter-evans/create-pull-request`. It is a deliberate port of that action's algorithm rather than a fresh design, and {mod}`repomatic.github.pr` records the mapping.

That action was the last third-party one entrusted with `REPOMATIC_PAT`, a token carrying workflow-write scope, which made it the largest remaining trust delegation on this page. It was nonetheless SHA-pinned with bumps gated on [`minimum-release-age`](dependencies.md) by `sync-action-pins`, so the supply-chain gain is real but bounded: a compromised release could not have reached a build until it had been public for the cooldown window and a maintainer had merged the bump.

The concrete win is elsewhere. The action's cleanup only ran when the action ran, and a job behind an `if:` gate skips its own steps, leaving a branch and pull request nobody retires: `repomatic close-stale-bump-pr` exists to patch exactly that hole. `pr-sync` decides internally instead, so one command covers the create, update, no-op and close cases.

Owning both ends also collapsed the plumbing: each job used to render its body with `repomatic pr-body`, squeeze it through a `$GITHUB_OUTPUT` step output (a 32 KiB ceiling), and relay it into the action via `env:`. `pr-sync --template X` renders internally, so the relay, its size ceiling, and the per-step `env:` blocks are gone, and the branch, labels and draft state derive from the template's own frontmatter.

Two conformance tests hold this in place: `test_pull_requests_are_opened_by_the_cli_not_an_action` fails when any workflow reaches for the action again, and `test_detached_head_pr_sync_steps_name_a_base` fails when a job checking out a raw SHA forgets to name `--base`.

Replacement strategies, ordered from most to least isolated:

1. **Internal CLI**: the operation runs inside `repomatic` Python code with no external process.
2. **Direct binary download**: checksummed binary fetched from a GitHub release URL, no action code path involved.
3. **Runner built-in**: uses tools pre-installed on the GitHub Actions runner (`gh`, Rust toolchain).
4. **First-party replacement**: swaps a community action for an official `actions/*` equivalent maintained by GitHub.

### Ruff consolidation

Ten separate Python linters and formatters, two of them mdformat plugins, have been absorbed into `ruff`, eliminating ten runtime or dev dependencies:

| Removed tool     | What it did                                                                            | Replaced |
| :--------------- | :------------------------------------------------------------------------------------- | :------- |
| `pylint`         | Static analysis and linting                                                            | Feb 2023 |
| `pydocstyle`     | Docstring convention enforcement                                                       | Feb 2023 |
| `pycln`          | Unused import removal                                                                  | Feb 2023 |
| `pyupgrade`      | Python syntax modernization                                                            | Feb 2023 |
| `isort`          | Import sorting                                                                         | Feb 2023 |
| `black`          | Code formatting                                                                        | Sep 2023 |
| `docformatter`   | Docstring formatting                                                                   | Jan 2024 |
| `mdformat-black` | Python formatting in Markdown code blocks, as an mdformat plugin                       | Aug 2024 |
| `blacken-docs`   | Python formatting in Markdown code blocks                                              | Feb 2026 |
| `mdformat-ruff`  | Same as `mdformat-black`, through a second ruff pinned inside the mdformat environment | Aug 2026 |

`autopep8` is the only legacy formatter still in use: it handles long-line comment wrapping that ruff does not yet cover ([astral-sh/ruff#7414](https://github.com/astral-sh/ruff/issues/7414)).

### uv consolidation

Five separate packaging and install tools have been absorbed into `uv`, which now handles dependency management, builds, publishing, auditing, and Python version installation:

| Removed tool                | What it did                                             | Replaced |
| :-------------------------- | :------------------------------------------------------ | :------- |
| `poetry`                    | Dependency management, lock files, virtual environments | Jun 2024 |
| `build` / `python -m build` | Package building (wheels and sdists)                    | Sep 2024 |
| `twine`                     | PyPI uploads                                            | Jan 2025 |
| `check-wheel-contents`      | Wheel validation                                        | Jan 2025 |
| `pip-audit`                 | Vulnerability scanning                                  | Mar 2026 |

`uv` also consolidated command-line usage that previously required separate tools: `pip install` became `uv pip install` / `uv sync`, `pipx` became `uvx`, and `actions/setup-python` was replaced by `astral-sh/setup-uv` (counted in the [action minimization table](#third-party-action-minimization) above).

Two other Python packages were eliminated outside the ruff/uv consolidations: `pipdeptree` (replaced by an internal `dep-graph` implementation) and `gitignore-parser` (replaced by `py-walk`).

## Permissions and token

Several workflows need a `REPOMATIC_PAT` secret to create PRs that modify files in `.github/workflows/` and to trigger downstream workflows. Without it, those jobs silently fall back to the default `GITHUB_TOKEN`, which lacks the required permissions.

After your first push, the [`setup-guide` job](workflows.md#github-workflows-autofix-yaml-jobs) automatically opens an issue with [step-by-step instructions](https://github.com/kdeldycke/repomatic/blob/main/repomatic/templates/setup-guide.md) to create and configure the token.

## Concurrency and cancellation

All workflows use a `concurrency` directive to prevent redundant runs and save CI resources. When a new commit is pushed, any in-progress workflow runs for the same branch or PR are automatically cancelled.

Workflows are grouped by:

- **Pull requests**: `{workflow-name}-{pr-number}` — Multiple commits to the same PR cancel previous runs
- **Branch pushes**: `{workflow-name}-{branch-ref}` — Multiple pushes to the same branch cancel previous runs

`release.yaml` uses a stronger protection: release commits get a **unique concurrency group** based on the commit SHA, so they can never be cancelled. This ensures tagging, PyPI publishing, and GitHub release creation complete successfully.

Additionally, [`cancel-runs.yaml`](workflows.md#github-workflows-cancel-runs-yaml-jobs) actively cancels in-progress and queued runs when a PR is closed. This complements passive concurrency groups, which only trigger cancellation when a *new* run enters the same group — closing a PR doesn't produce such an event.

> [!TIP]
> For implementation details on how concurrency groups are computed and why `release.yaml` needs special handling, see the {mod}`repomatic.github.actions` module docstring.

## AV false-positive submissions

Compiled Python binaries (built with [Nuitka](https://nuitka.net/) `--onefile`) are frequently flagged as malicious by heuristic AV engines. The onefile packaging technique (self-extracting archive with embedded Python runtime) triggers generic "packed/suspicious" signatures. This is a known issue across the Nuitka ecosystem.

The [`scan-virustotal`](workflows.md#github-workflows-release-engine-yaml-jobs) job in `_release-engine.yaml` uploads all compiled binaries to [VirusTotal](https://www.virustotal.com/) on every release. This seeds AV vendor databases to reduce false positive rates for downstream distributors (Chocolatey, Scoop, etc.). Each release's `flagged / total` snapshot is recorded in `docs/assets/virustotal-scans.csv` and rendered, along with the full catalog of released binaries and their analysis links, on the [binaries page](binaries.md). Detection counts are deliberately kept out of GitHub release notes, where they read as a malware verdict without context (see [kdeldycke/meta-package-manager#1911](https://github.com/kdeldycke/meta-package-manager/issues/1911)).

When a release is flagged, the `/av-false-positive` [skill](agent-skills.md) generates per-vendor submission files with pre-written text and form field mappings. The vendor details below document the process for manual reference.

### Why binaries get flagged

Nuitka `--onefile` creates a self-extracting archive that decompresses an embedded Python runtime to a temporary directory and executes it at launch. This "drop and execute from temp" pattern is behaviorally identical to trojan droppers, which triggers heuristic and ML-based detections. Two more factors compound it: Nuitka is popular with malware authors for source code protection, which poisons AV heuristics for all Nuitka-compiled binaries, and Microsoft has gone as far as [suspending an Artifact Signing account](https://github.com/Nuitka/Nuitka/issues/3842) over Nuitka onefile binaries.

The detection profile is consistent across projects: Linux binaries scan clean, macOS ones pick up the occasional ML false positive, Windows ARM64 stays low (fewer ARM64 heuristics in AV engines), and Windows x64 attracts the bulk of the detections through generic signatures like `Gen:Variant.Application.tedy` (BitDefender family), [`Trojan:Win32/Sabsik`](https://www.microsoft.com/en-us/wdsi/threats/malware-encyclopedia-description?Name=Trojan:Win32/Sabsik.EN.A!ml&threatId=-2147156305) (Microsoft), `Python/Packed.Nuitka.AL` (ESET), and various ML classifiers. Pure-Python `.whl` and `.tar.gz` distributions scan clean.

The Nuitka project tracks the situation in [Nuitka/Nuitka#2685](https://github.com/Nuitka/Nuitka/issues/2685), [Nuitka/Nuitka#2495](https://github.com/Nuitka/Nuitka/issues/2495), [Nuitka/Nuitka#2757](https://github.com/Nuitka/Nuitka/issues/2757), and [Nuitka/Nuitka#3842](https://github.com/Nuitka/Nuitka/issues/3842).

### Vendor portals

| Vendor      | Engines covered                                                                     | Portal                                                                                                | Format                                                                             | Turnaround             |
| :---------- | :---------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------------------------- | :--------------------------------------------------------------------------------- | :--------------------- |
| Microsoft   | `Microsoft`                                                                         | [WDSI file submission](https://www.microsoft.com/en-us/wdsi/filesubmission?persona=SoftwareDeveloper) | One file per form, 1900 char limit on additional info                              | Fastest                |
| BitDefender | `BitDefender`, `ALYac`, `Arcabit`, `Emsisoft`, `GData`, `MicroWorld-eScan`, `VIPRE` | [bitdefender.com/submit](https://www.bitdefender.com/submit/)                                         | One file per form, screenshot mandatory                                            | Fast                   |
| ESET        | `ESET-NOD32`                                                                        | Email to `samples@eset.com`                                                                           | Single email, password-protected ZIP (`infected`), ~24 MB limit                    | Reliable               |
| Symantec    | `Symantec`                                                                          | [symsubmit.symantec.com](https://symsubmit.symantec.com/false_positive)                               | Hash submission only (no `.exe`/`.bin` upload), one hash per form, 5000 char limit | 3-7 business days      |
| Avast/AVG   | `Avast`, `AVG`                                                                      | [avast.com/submit-a-sample](https://www.avast.com/submit-a-sample)                                    | One file per form, shared engine                                                   | Medium                 |
| Sophos      | `Sophos`                                                                            | [sophos.com filesubmission](https://support.sophos.com/support/s/filesubmission)                      | One file per form, 25 MB max per submission                                        | Up to 15 business days |

Complete directories of vendor false-positive contacts are maintained by [VirusTotal](https://docs.virustotal.com/docs/false-positive-contacts) and [False-Positive-Center](https://github.com/yaronelh/False-Positive-Center).

### Submission priority

Submit in this order to maximize impact:

1. **Microsoft**: most influential engine. ML detections (`Sabsik`, `Wacatac`) have the broadest downstream effect.
2. **BitDefender**: powers ~6 downstream vendor engines. Highest detection-removal-per-submission ratio.
3. **ESET**: email-based channel with no portal dependency. The most reliable submission path.
4. **Symantec**: ML detections (`ML.Attribute.*`) may take longer to process.
5. **Avast/AVG**: shared engine, so one submission covers both.
6. **Sophos**: PUA detections require justification of the software's legitimate purpose.

### Submission content

Every false-positive submission should include:

- The binary's VirusTotal report link.
- VirusTotal links for the clean `.whl` and `.tar.gz` source distributions (as comparison evidence).
- The GitHub release link and direct download URL for the binary.
- Project homepage and PyPI URL.
- License from `pyproject.toml`.
- Reference to any prior false-positive issue in the repository.

All submission text should mention that the binary is compiled with Nuitka `--onefile` from an open-source project.

### Known portal issues

- **Microsoft**: CORS errors or stuck progress modals during upload (auth session expiring). Workaround: sign out, clear cookies for `microsoft.com`, sign back in, submit immediately.
- **BitDefender**: form sometimes returns "Your request could not be registered!" with no details. Retry later.
- **Avast**: form sometimes returns "An internal error occurred while sending the form." Retry later.

### Long-term mitigations

False-positive submissions are a per-release moving target. The structural fixes:

- **Code signing with an EV certificate** would reduce heuristic detections across the board, especially from Microsoft and Symantec ML models.
- **Switching from `--onefile` to `--standalone`** would eliminate the self-extracting pattern entirely, at the cost of distributing a directory instead of a single `.exe`.
- **[Nuitka Commercial](https://nuitka.net/doc/commercial.html)** claims proprietary AV-mitigation techniques but offers no guarantees.
