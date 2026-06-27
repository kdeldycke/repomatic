---
args: [diff_table]
title: Fix vulnerable dependencies
footer: false
---

### Description

Upgrades packages with known security vulnerabilities detected by [`uv audit`](https://docs.astral.sh/uv/reference/cli/#uv-audit) against the [Python Packaging Advisory Database](https://github.com/pypa/advisory-database). Uses [`--exclude-newer-package`](https://docs.astral.sh/uv/reference/settings/#exclude-newer-package) to bypass the [`exclude-newer`](https://docs.astral.sh/uv/reference/settings/#exclude-newer) cooldown for security fixes. See the [`fix-vulnerable-deps` job documentation](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) for details.

\$diff_table

### Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`vulnerable-deps.sources`](https://kdeldycke.github.io/repomatic/configuration.html#vulnerable-deps-sources)
- [`vulnerable-deps.sync`](https://kdeldycke.github.io/repomatic/configuration.html#vulnerable-deps-sync)
