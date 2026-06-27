---
title: Update docs
footer: false
---

### Description

Regenerates API documentation with [sphinx-apidoc](https://www.sphinx-doc.org/en/master/man/sphinx-apidoc.html), converts RST stubs to [MyST markdown](https://myst-parser.readthedocs.io/) if applicable, and runs the project's `docs_update.py` script if present. See the [`update-docs` job documentation](https://kdeldycke.github.io/repomatic/workflows.html#github-workflows-autofix-yaml-jobs) for details.

### Configuration

Relevant [`[tool.repomatic]`](https://kdeldycke.github.io/repomatic/configuration.html) options:

- [`docs.apidoc-exclude`](https://kdeldycke.github.io/repomatic/configuration.html#docs-apidoc-exclude)
- [`docs.apidoc-extra-args`](https://kdeldycke.github.io/repomatic/configuration.html#docs-apidoc-extra-args)
- [`docs.update-script`](https://kdeldycke.github.io/repomatic/configuration.html#docs-update-script)
