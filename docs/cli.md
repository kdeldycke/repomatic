# {octicon}`command-palette` CLI

> [!TIP]
> Each `[tool.repomatic]` config option maps to CLI behavior. See the [configuration reference](configuration.md) for project-level defaults.

<!-- cli-reference-start -->

| Command                                                                     | Description                                              |
| :-------------------------------------------------------------------------- | :------------------------------------------------------- |
| [`repomatic audit`](#repomatic-audit)                                       | Report (and optionally fix) vulnerable dependencies      |
| [`repomatic broken-links`](#repomatic-broken-links)                         | Manage broken links issue lifecycle                      |
| [`repomatic cache`](#repomatic-cache)                                       | Manage the download cache                                |
| [`repomatic cache clean`](#repomatic-cache-clean)                           | Remove cached entries                                    |
| [`repomatic cache help`](#repomatic-cache-help)                             | Show help for a command                                  |
| [`repomatic cache path`](#repomatic-cache-path)                             | Print the cache directory path                           |
| [`repomatic cache show`](#repomatic-cache-show)                             | List cached entries                                      |
| [`repomatic changelog`](#repomatic-changelog)                               | Maintain a Markdown-formatted changelog                  |
| [`repomatic clean-unmodified-configs`](#repomatic-clean-unmodified-configs) | Remove config files that match bundled defaults          |
| [`repomatic close-stale-bump-pr`](#repomatic-close-stale-bump-pr)           | Close a stale version-bump PR                            |
| [`repomatic convert-to-myst`](#repomatic-convert-to-myst)                   | Convert reST docstrings to MyST in Python files          |
| [`repomatic format-images`](#repomatic-format-images)                       | Format images with lossless optimization                 |
| [`repomatic git-commit-push`](#repomatic-git-commit-push)                   | Commit files and push, rebasing on rejection             |
| [`repomatic git-tag`](#repomatic-git-tag)                                   | Create and push a Git tag                                |
| [`repomatic help`](#repomatic-help)                                         | Show help for a command                                  |
| [`repomatic init`](#repomatic-init)                                         | Bootstrap a repository to use reusable workflows         |
| [`repomatic lint-changelog`](#repomatic-lint-changelog)                     | Check changelog dates against release dates              |
| [`repomatic lint-repo`](#repomatic-lint-repo)                               | Run repository consistency checks                        |
| [`repomatic list-skills`](#repomatic-list-skills)                           | List available Claude Code skills                        |
| [`repomatic metadata`](#repomatic-metadata)                                 | Output project metadata                                  |
| [`repomatic pr-body`](#repomatic-pr-body)                                   | Generate PR body with workflow metadata                  |
| [`repomatic release-prep`](#repomatic-release-prep)                         | Prepare files for a release                              |
| [`repomatic run`](#repomatic-run)                                           | Run an external tool with managed config                 |
| [`repomatic scan-virustotal`](#repomatic-scan-virustotal)                   | Upload release binaries to VirusTotal                    |
| [`repomatic setup-guide`](#repomatic-setup-guide)                           | Manage setup guide issue lifecycle                       |
| [`repomatic show-config`](#repomatic-show-config)                           | Print [tool.repomatic] configuration reference           |
| [`repomatic show-test-matrix`](#repomatic-show-test-matrix)                 | Render the CI test matrix as a grid                      |
| [`repomatic sponsor-label`](#repomatic-sponsor-label)                       | Label issues/PRs from GitHub sponsors                    |
| [`repomatic sync-action-pins`](#repomatic-sync-action-pins)                 | Bump SHA-pinned GitHub Actions to their latest release   |
| [`repomatic sync-binaries`](#repomatic-sync-binaries)                       | Regenerate the binaries catalog page                     |
| [`repomatic sync-bumpversion`](#repomatic-sync-bumpversion)                 | Sync bumpversion config from bundled template            |
| [`repomatic sync-deps`](#repomatic-sync-deps)                               | Update dependencies, all or a named subset               |
| [`repomatic sync-dev-release`](#repomatic-sync-dev-release)                 | Sync rolling dev pre-release on GitHub                   |
| [`repomatic sync-github-releases`](#repomatic-sync-github-releases)         | Sync GitHub release notes from changelog                 |
| [`repomatic sync-gitignore`](#repomatic-sync-gitignore)                     | Sync .gitignore from gitignore.io templates              |
| [`repomatic sync-labels`](#repomatic-sync-labels)                           | Sync repository labels via labelmaker                    |
| [`repomatic sync-mailmap`](#repomatic-sync-mailmap)                         | Sync Git's .mailmap file with missing contributors       |
| [`repomatic sync-tool-versions`](#repomatic-sync-tool-versions)             | Bump registry tool versions from upstream releases       |
| [`repomatic sync-uv-lock`](#repomatic-sync-uv-lock)                         | Re-lock dependencies and roll cooldown overrides forward |
| [`repomatic sync-workflow-pins`](#repomatic-sync-workflow-pins)             | Bump npm/PyPI version literals in workflow YAML          |
| [`repomatic unsubscribe-threads`](#repomatic-unsubscribe-threads)           | Unsubscribe from closed, inactive notification threads   |
| [`repomatic update-checksums`](#repomatic-update-checksums)                 | Recompute SHA-256 checksums for the binary tool registry |
| [`repomatic update-deps-graph`](#repomatic-update-deps-graph)               | Generate dependency graph from uv lockfile               |
| [`repomatic update-docs`](#repomatic-update-docs)                           | Regenerate Sphinx API docs and run update script         |
| [`repomatic verify-binary`](#repomatic-verify-binary)                       | Verify binary architecture using exiftool                |
| [`repomatic version-check`](#repomatic-version-check)                       | Check if a version bump is allowed                       |
| [`repomatic workflow`](#repomatic-workflow)                                 | Lint downstream workflow caller files                    |
| [`repomatic workflow help`](#repomatic-workflow-help)                       | Show help for a command                                  |
| [`repomatic workflow lint`](#repomatic-workflow-lint)                       | Lint workflow files for common issues                    |

```{click:source}
:hide-source:
from repomatic.cli import repomatic
```

## Help screen

```{click:run}
invoke(repomatic, args=['--help'])
```

## `repomatic audit`

```{click:run}
invoke(repomatic, args=['audit', '--help'])
```

## `repomatic broken-links`

```{click:run}
invoke(repomatic, args=['broken-links', '--help'])
```

## `repomatic cache`

```{click:run}
invoke(repomatic, args=['cache', '--help'])
```

### `repomatic cache clean`

```{click:run}
invoke(repomatic, args=['cache', 'clean', '--help'])
```

### `repomatic cache help`

```{click:run}
invoke(repomatic, args=['cache', 'help', '--help'])
```

### `repomatic cache path`

```{click:run}
invoke(repomatic, args=['cache', 'path', '--help'])
```

### `repomatic cache show`

```{click:run}
invoke(repomatic, args=['cache', 'show', '--help'])
```

## `repomatic changelog`

```{click:run}
invoke(repomatic, args=['changelog', '--help'])
```

## `repomatic clean-unmodified-configs`

```{click:run}
invoke(repomatic, args=['clean-unmodified-configs', '--help'])
```

## `repomatic close-stale-bump-pr`

```{click:run}
invoke(repomatic, args=['close-stale-bump-pr', '--help'])
```

## `repomatic convert-to-myst`

```{click:run}
invoke(repomatic, args=['convert-to-myst', '--help'])
```

## `repomatic format-images`

```{click:run}
invoke(repomatic, args=['format-images', '--help'])
```

## `repomatic git-commit-push`

```{click:run}
invoke(repomatic, args=['git-commit-push', '--help'])
```

## `repomatic git-tag`

```{click:run}
invoke(repomatic, args=['git-tag', '--help'])
```

## `repomatic help`

```{click:run}
invoke(repomatic, args=['help', '--help'])
```

## `repomatic init`

```{click:run}
invoke(repomatic, args=['init', '--help'])
```

## `repomatic lint-changelog`

```{click:run}
invoke(repomatic, args=['lint-changelog', '--help'])
```

## `repomatic lint-repo`

```{click:run}
invoke(repomatic, args=['lint-repo', '--help'])
```

## `repomatic list-skills`

```{click:run}
invoke(repomatic, args=['list-skills', '--help'])
```

## `repomatic metadata`

```{click:run}
invoke(repomatic, args=['metadata', '--help'])
```

## `repomatic pr-body`

```{click:run}
invoke(repomatic, args=['pr-body', '--help'])
```

## `repomatic release-prep`

```{click:run}
invoke(repomatic, args=['release-prep', '--help'])
```

## `repomatic run`

```{click:run}
invoke(repomatic, args=['run', '--help'])
```

## `repomatic scan-virustotal`

```{click:run}
invoke(repomatic, args=['scan-virustotal', '--help'])
```

## `repomatic setup-guide`

```{click:run}
invoke(repomatic, args=['setup-guide', '--help'])
```

## `repomatic show-config`

```{click:run}
invoke(repomatic, args=['show-config', '--help'])
```

## `repomatic show-test-matrix`

```{click:run}
invoke(repomatic, args=['show-test-matrix', '--help'])
```

## `repomatic sponsor-label`

```{click:run}
invoke(repomatic, args=['sponsor-label', '--help'])
```

## `repomatic sync-action-pins`

```{click:run}
invoke(repomatic, args=['sync-action-pins', '--help'])
```

## `repomatic sync-binaries`

```{click:run}
invoke(repomatic, args=['sync-binaries', '--help'])
```

## `repomatic sync-bumpversion`

```{click:run}
invoke(repomatic, args=['sync-bumpversion', '--help'])
```

## `repomatic sync-deps`

```{click:run}
invoke(repomatic, args=['sync-deps', '--help'])
```

## `repomatic sync-dev-release`

```{click:run}
invoke(repomatic, args=['sync-dev-release', '--help'])
```

## `repomatic sync-github-releases`

```{click:run}
invoke(repomatic, args=['sync-github-releases', '--help'])
```

## `repomatic sync-gitignore`

```{click:run}
invoke(repomatic, args=['sync-gitignore', '--help'])
```

## `repomatic sync-labels`

```{click:run}
invoke(repomatic, args=['sync-labels', '--help'])
```

## `repomatic sync-mailmap`

```{click:run}
invoke(repomatic, args=['sync-mailmap', '--help'])
```

## `repomatic sync-tool-versions`

```{click:run}
invoke(repomatic, args=['sync-tool-versions', '--help'])
```

## `repomatic sync-uv-lock`

```{click:run}
invoke(repomatic, args=['sync-uv-lock', '--help'])
```

## `repomatic sync-workflow-pins`

```{click:run}
invoke(repomatic, args=['sync-workflow-pins', '--help'])
```

## `repomatic unsubscribe-threads`

```{click:run}
invoke(repomatic, args=['unsubscribe-threads', '--help'])
```

## `repomatic update-checksums`

```{click:run}
invoke(repomatic, args=['update-checksums', '--help'])
```

## `repomatic update-deps-graph`

```{click:run}
invoke(repomatic, args=['update-deps-graph', '--help'])
```

## `repomatic update-docs`

```{click:run}
invoke(repomatic, args=['update-docs', '--help'])
```

## `repomatic verify-binary`

```{click:run}
invoke(repomatic, args=['verify-binary', '--help'])
```

## `repomatic version-check`

```{click:run}
invoke(repomatic, args=['version-check', '--help'])
```

## `repomatic workflow`

```{click:run}
invoke(repomatic, args=['workflow', '--help'])
```

### `repomatic workflow help`

```{click:run}
invoke(repomatic, args=['workflow', 'help', '--help'])
```

### `repomatic workflow lint`

```{click:run}
invoke(repomatic, args=['workflow', 'lint', '--help'])
```

<!-- cli-reference-end -->

## `repomatic.cli` API

```{eval-rst}
.. autoclasstree:: repomatic.cli
   :strict:

.. automodule:: repomatic.cli
   :members:
   :undoc-members:
   :show-inheritance:
```
