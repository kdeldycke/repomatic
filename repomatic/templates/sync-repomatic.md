---
title: Sync repomatic-managed files
footer: false
---

### Description

Syncs all [repomatic](https://github.com/kdeldycke/repomatic)-managed files: [thin-caller workflows](https://kdeldycke.github.io/repomatic/workflows.html#example-usage), configuration files, and skill definitions. Also removes redundant config files identical to bundled defaults and cleans up excluded or stale files.

### Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`agents.location`](https://kdeldycke.github.io/repomatic/configuration.html#agents-location)
- [`awesome-template.sync`](https://kdeldycke.github.io/repomatic/configuration.html#awesome-template-sync)
- [`exclude`](https://kdeldycke.github.io/repomatic/configuration.html#exclude)
- [`include`](https://kdeldycke.github.io/repomatic/configuration.html#include)
- [`notification.unsubscribe`](https://kdeldycke.github.io/repomatic/configuration.html#notification-unsubscribe)
- [`skills.location`](https://kdeldycke.github.io/repomatic/configuration.html#skills-location)
- [`workflow.extra-paths`](https://kdeldycke.github.io/repomatic/configuration.html#workflow-extra-paths)
- [`workflow.ignore-paths`](https://kdeldycke.github.io/repomatic/configuration.html#workflow-ignore-paths)
- [`workflow.paths`](https://kdeldycke.github.io/repomatic/configuration.html#workflow-paths)
- [`workflow.source-paths`](https://kdeldycke.github.io/repomatic/configuration.html#workflow-source-paths)
- [`workflow.sync`](https://kdeldycke.github.io/repomatic/configuration.html#workflow-sync)
