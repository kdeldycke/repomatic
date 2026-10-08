# {octicon}`cross-reference` Upstream

repomatic installs, configures and runs a long list of external tools and libraries. When one of them misbehaves, the fix goes upstream where possible, and into repomatic where it cannot wait. This page tracks that relationship: code contributed back, workarounds provided, issues reported, and features declined.

A dependency that a sibling project consumes more directly is tracked on that project's page: [`click-extra`](https://github.com/kdeldycke/click-extra/blob/main/docs/upstream.md) for Click, Cloup and `python-tabulate`, and [`meta-package-manager`](https://github.com/kdeldycke/meta-package-manager/blob/main/docs/upstream.md) for Nuitka and the package managers it wraps.

## Code contributed upstream

Pull requests authored by repomatic's maintainer and merged into upstream projects.

### [`arrow`](https://github.com/arrow-py/arrow)

- [`#246` - Fix overzealous time truncation in `span_range()`](https://github.com/arrow-py/arrow/pull/246)

### [`awesome-lint`](https://github.com/sindresorhus/awesome-lint)

repomatic lints awesome lists with it:

- [`#127` - Fix matching of opening curly quote in description](https://github.com/sindresorhus/awesome-lint/pull/127)
- [`#123` - Forbid License, Licence and Contribute sections](https://github.com/sindresorhus/awesome-lint/pull/123)
- [`#118` - Allow for a Footnotes section at the end of an Awesome list](https://github.com/sindresorhus/awesome-lint/pull/118)
- [`#111` - Fix location matching of `contributing.md` and `code-of-conduct.md` files](https://github.com/sindresorhus/awesome-lint/pull/111)
- [`#102` - Allow `.github` directory to contain contribution guidelines](https://github.com/sindresorhus/awesome-lint/pull/102)
- [`#101` - Check and enforce quotes with punctuations](https://github.com/sindresorhus/awesome-lint/pull/101)

### [`gitignore.io`](https://github.com/toptal/gitignore.io)

- [`#17` - Ignore rope files](https://github.com/toptal/gitignore.io/pull/17)

### [`lychee-action`](https://github.com/lycheeverse/lychee-action)

- [`#253` - Fix variable name](https://github.com/lycheeverse/lychee-action/pull/253)
- [`#197` - Add `*.rst` glob pattern to defaults](https://github.com/lycheeverse/lychee-action/pull/197)

### [`mdformat-deflist`](https://github.com/executablebooks/mdformat-deflist)

- [`#5` - Allow `mdit-py-plugins` v0.4](https://github.com/executablebooks/mdformat-deflist/pull/5)

### [`mdformat-gfm`](https://github.com/hukkin/mdformat-gfm)

- [`#28` - Removes cap on `mdit-py-plugins` dependency](https://github.com/hukkin/mdformat-gfm/pull/28)

### [`mdformat-myst`](https://github.com/executablebooks/mdformat-myst)

- [`#28` - Remove cap on `mdit-py-plugins` version](https://github.com/executablebooks/mdformat-myst/pull/28)

### [`mdformat-pelican`](https://github.com/gaige/mdformat-pelican)

- [`#11` - Fix link renderer conflict with `mdformat-gfm` 1.0.0 and other plugins](https://github.com/gaige/mdformat-pelican/pull/11)
- [`#6` - Fix module import](https://github.com/gaige/mdformat-pelican/pull/6)

### [`mdformat-pyproject`](https://github.com/csala/mdformat-pyproject)

- [`#22` - Modernize typing](https://github.com/csala/mdformat-pyproject/pull/22)
- [`#21` - Fix typos and update docs](https://github.com/csala/mdformat-pyproject/pull/21)
- [`#20` - Makes `.mdformat.toml` take precedence](https://github.com/csala/mdformat-pyproject/pull/20)
- [`#19` - Fix mdformat 1.0.0 compatibility](https://github.com/csala/mdformat-pyproject/pull/19)

### [`mdformat-recover-urls`](https://github.com/holy-two/mdformat-recover-urls)

- [`#2` - Fix compatibility with `mdformat-doc`](https://github.com/holy-two/mdformat-recover-urls/pull/2)

### [`mdformat-simple-breaks`](https://github.com/csala/mdformat-simple-breaks)

- [`#6` - Add missing description](https://github.com/csala/mdformat-simple-breaks/pull/6)
- [`#5` - Defer typing imports](https://github.com/csala/mdformat-simple-breaks/pull/5)
- [`#4` - Fix compatibility with mdformat 1.0.0](https://github.com/csala/mdformat-simple-breaks/pull/4)

### [`pipdeptree`](https://github.com/tox-dev/pipdeptree)

`pipdeptree` drew repomatic's dependency graph until `uv export --format cyclonedx1.5` replaced it:

- [`#201` - Unique IDs in Mermaid not conflicting with reserved keywords](https://github.com/tox-dev/pipdeptree/pull/201)
- [`#200` - Quote Mermaid node and edge labels](https://github.com/tox-dev/pipdeptree/pull/200)
- [`#196` - Fix mermaid option](https://github.com/tox-dev/pipdeptree/pull/196)
- [`#195` - Implements Mermaid output](https://github.com/tox-dev/pipdeptree/pull/195)
- [`#189` - Make the output of the dot format deterministic and stable](https://github.com/tox-dev/pipdeptree/pull/189)
- [`#65` - Pre-filter Dependency Tree before Rendering](https://github.com/tox-dev/pipdeptree/pull/65)

### [`sphinx`](https://github.com/sphinx-doc/sphinx)

- [`#13163` - Always print tracebacks in logs when Sphinx encounters an internal error](https://github.com/sphinx-doc/sphinx/pull/13163)

### [`sphinx-autodoc-typehints`](https://github.com/tox-dev/sphinx-autodoc-typehints)

- [`#687` - Show version in error tracebacks](https://github.com/tox-dev/sphinx-autodoc-typehints/pull/687)

### [`sphinxcontrib-mermaid`](https://github.com/mgaitan/sphinxcontrib-mermaid)

- [`#118` - Do not fail on empty class diagram generation](https://github.com/mgaitan/sphinxcontrib-mermaid/pull/118)

## Upstreamed from repomatic

Issues found while building repomatic and resolved upstream. Where repomatic carried a workaround in the meantime, the entry says what it was and when it went away.

- [`bump-my-version#331`](https://github.com/callowayproject/bump-my-version/issues/331) - `1.1.1` broke the `replace` command on `[[tool.bumpversion.files]]` entries that do not apply. repomatic held `bump-my-version` at `1.1.0` until a later release fixed it.
- [`lychee#1930`](https://github.com/lycheeverse/lychee/issues/1930) - `lychee` read its configuration only from its own file, so repomatic translated `[tool.lychee]` into that file. Since `0.24.0` ([`lychee#2104`](https://github.com/lycheeverse/lychee/pull/2104)), `lychee` reads the section from `pyproject.toml` itself, and repomatic no longer translates it.
- [`mdformat-myst#45`](https://github.com/executablebooks/mdformat-myst/issues/45) / [`mdformat-myst#46`](https://github.com/executablebooks/mdformat-myst/pull/46) - `mdformat-myst` depended on `mdformat-frontmatter`, which made it incompatible with `mdformat` `1.0.0`. `#46` replaced that dependency with `mdformat-front-matters`. repomatic ran `mdformat-myst` from its `master` branch, then from a fork, until a release carried the change.
- [`mdformat-pelican#3`](https://github.com/gaige/mdformat-pelican/issues/3) / [`mdformat-pelican#4`](https://github.com/gaige/mdformat-pelican/pull/4) - `mdformat-pelican` and `mdformat-gfm` could not run together, also reported as [`mdformat-gfm#38`](https://github.com/hukkin/mdformat-gfm/issues/38). repomatic depended on a personal fork that carried the fix, then on unreleased upstream, then on the release that shipped it.
- [`mdformat-web#5`](https://github.com/hukkin/mdformat-web/pull/5) - The HTML formatter added an extra doctype to `html` code blocks. repomatic ran a patched `mdformat-web` from June 2023 until `0.2.0` shipped the fix.
- [`tomlrt#171`](https://github.com/dimbleby/tomlrt/issues/171) - `Table.section()` dropped standalone comments and inline-array padding from the source document. repomatic rebuilt each section with a helper to keep them, and removed the helper once `tomlrt` `2.2.4` fixed the clone path.
- [`zizmor#1664`](https://github.com/zizmorcore/zizmor/issues/1664) - The `template-injection` audit failed on a multiline YAML block. repomatic turned the audit off until `zizmor` `1.23.0` fixed it, then turned it back on.

Also reported while building repomatic, and closed upstream:

- [`setup-python#807` - Feature request: Do not fail when caching on non-Python repositories](https://github.com/actions/setup-python/issues/807)
- [`ruff#9186` - docstring code formatter: add a `--docstring-code-format` parameter to `ruff format` CLI](https://github.com/astral-sh/ruff/issues/9186)
- [`ruff#2857` - Add `--target-version pyXX` option to CLI](https://github.com/astral-sh/ruff/issues/2857)
- [`uv#8774` - Equivalent command to `check-wheel-contents`](https://github.com/astral-sh/uv/issues/8774)
- [`uv#8770` - CLI reported by `uv run` on Windows is not found on invocation](https://github.com/astral-sh/uv/issues/8770)
- [`uv#5185` - Feature request: option to not update `uv.lock` file](https://github.com/astral-sh/uv/issues/5185)
- [`uv#4762` - `uv pip install --all-extras .` fail to locate `pyproject.toml`](https://github.com/astral-sh/uv/issues/4762)
- [`uv#1481` - `uv pip install` cannot fetch requirement file from remote URL](https://github.com/astral-sh/uv/issues/1481)
- [`bump-my-version#410` - Unlock dependency on `click` 8.4.1](https://github.com/callowayproject/bump-my-version/issues/410)
- [`bump-my-version#300` - Nuitka-compilation fails with: `PydanticUndefinedAnnotation: name 'SCMInfo' is not defined`](https://github.com/callowayproject/bump-my-version/issues/300)
- [`bump-my-version#148` - Read configuration from remote URL](https://github.com/callowayproject/bump-my-version/issues/148)
- [`bump-my-version#145` - Add support for `ignore_missing_files` global option in configuration file](https://github.com/callowayproject/bump-my-version/issues/145)
- [`bump-my-version#117` - Empty string replacement (`--replace ""`) is interpreted as absence of parameter](https://github.com/callowayproject/bump-my-version/issues/117)
- [`bump-my-version#38` - Ignore missing files](https://github.com/callowayproject/bump-my-version/issues/38)
- [`bump-my-version#35` - Date-based search and replace](https://github.com/callowayproject/bump-my-version/issues/35)
- [`bump-my-version#34` - Traceback on `--search` parameter with plain string](https://github.com/callowayproject/bump-my-version/issues/34)
- [`bump-my-version#25` - `--no-configured-files` should not update `current_version`](https://github.com/callowayproject/bump-my-version/issues/25)
- [`bump-my-version#19` - Decouple search & replace from version bump](https://github.com/callowayproject/bump-my-version/issues/19)
- [`bump-my-version#15` - Get current version](https://github.com/callowayproject/bump-my-version/issues/15)
- [`labelmaker#126` - No global `on-rename-clash` parameter](https://github.com/jwodder/labelmaker/issues/126)
- [`labelmaker#125` - Multiple file support in `labelmaker apply`](https://github.com/jwodder/labelmaker/issues/125)
- [`mdformat-admon#15` - Latest `mdformat-admon` and `mdformat-gfm` conflicts](https://github.com/kyleking/mdformat-admon/issues/15)
- [`mdformat-deflist#7` - Uncap `mdformat < 0.8.0` requirement](https://github.com/executablebooks/mdformat-deflist/issues/7)
- [`mdformat-deflist#4` - Latest `mdformat-deflist` and `mdformat-admon` conflicts](https://github.com/executablebooks/mdformat-deflist/issues/4)
- [`mdformat-gfm#27` - Latest `mdformat-gfm` and `mdformat-admon` conflicts](https://github.com/hukkin/mdformat-gfm/issues/27)
- [`mdformat-myst#43` - Replace dependency of `mdformat-tables` to `mdformat-gfm`](https://github.com/executablebooks/mdformat-myst/issues/43)
- [`mdformat-pyproject#18` - Uncap `mdformat < 1.dev0` requirement](https://github.com/csala/mdformat-pyproject/issues/18)
- [`mdformat-simple-breaks#3` - Uncap `mdformat < 0.8.0` requirement](https://github.com/csala/mdformat-simple-breaks/issues/3)
- [`mdformat-toc#19` - `mdformat-toc` forces URL-encoding of non-ASCII characters](https://github.com/hukkin/mdformat-toc/issues/19)
- [`Nuitka#3449` - Why does nuitka reports `x86_64` architecture on `windows-11-arm` runner?](https://github.com/nuitka/Nuitka/issues/3449)
- [`pipdeptree#188` - Graph stability: random order in Graphiz content](https://github.com/tox-dev/pipdeptree/issues/188)
- [`tomlrt#172` - Reassigning a section relocates its AoT children to the end of the document](https://github.com/dimbleby/tomlrt/issues/172)

## Addressed by repomatic

Issues still open or unfixed upstream. repomatic provides the solution.

### GitHub Actions

The reusable workflows work around a set of GitHub Actions limitations. The [GitHub Actions limitations](workflows.md#github-actions-limitations) table lists each one, its status and the mechanism that addresses it.

### PyPI

- [`warehouse#11096` - Trusted publishing: Support for GitHub reusable workflows](https://github.com/pypi/warehouse/issues/11096): the `publish-pypi` job stays in each repository's own `release.yaml`, so the OIDC claim names the file that the repository registered as its Trusted Publisher.
- [`warehouse#1388` - 404 for registered package with no release](https://github.com/pypi/warehouse/issues/1388) and [`warehouse#9536` - Unable to get a consistent view of existing/changed packages from the various APIs](https://github.com/pypi/warehouse/issues/9536): PyPI's JSON API answers `404` for some registered projects. repomatic does not read that `404` as proof that a package is missing.

### Tool configuration in `pyproject.toml`

[`repomatic run`](tool-runner.md) translates a `[tool.X]` section into the native configuration file of a tool that cannot read `pyproject.toml`:

- [`actionlint#623` - Support configuration in `[tool.actionlint]` section of `pyproject.toml`](https://github.com/rhysd/actionlint/issues/623)
- [`biome#9239`](https://github.com/biomejs/biome/discussions/9239) (discussion)
- [`gitleaks#2066` - Support configuration from `pyproject.toml`](https://github.com/gitleaks/gitleaks/issues/2066)
- [`zizmor#322`](https://github.com/orgs/zizmorcore/discussions/322#discussioncomment-15919620) (discussion comment)
- [`mdformat#432` - Option to specify path to configuration file](https://github.com/hukkin/mdformat/issues/432) and [`mdformat#562` - Add `--config` option to specify explicit configuration file path](https://github.com/hukkin/mdformat/issues/562): `mdformat` finds its configuration only by discovery, so repomatic writes a temporary `.mdformat.toml` for the run and deletes it after.

### Markdown formatting

repomatic post-processes the output of `mdformat` to undo two `mdformat-myst` rewrites, and changes one `mdformat` default:

- [`mdformat-myst#21` - myst formatting directive with only 1 parameter](https://github.com/executablebooks/mdformat-myst/issues/21): `mdformat-myst` turns `:key: value` directive options into a YAML block. repomatic restores the field list. The open fix is [`mdformat-myst#49`](https://github.com/executablebooks/mdformat-myst/pull/49).
- [`mdformat-myst#13` - colon fence syntax is broken by escape character](https://github.com/executablebooks/mdformat-myst/issues/13): `mdformat-myst` escapes `:::{name}` colon fences. repomatic removes the escape. The open fixes are [`mdformat-myst#36`](https://github.com/executablebooks/mdformat-myst/pull/36) and [`mdformat-myst#48`](https://github.com/executablebooks/mdformat-myst/pull/48).
- [`mdformat#312` - Needless URL encoding of link destinations](https://github.com/hukkin/mdformat/issues/312): repomatic turns off URL validation and bundles [`mdformat-recover-urls`](https://github.com/holy-two/mdformat-recover-urls), which decodes percent-encoded non-ASCII characters back.

### Awesome lists

- [`mdformat-toc#17` - Feature request: ignore specific Headings](https://github.com/hukkin/mdformat-toc/issues/17) / [`mdformat-toc#20`](https://github.com/hukkin/mdformat-toc/pull/20): `mdformat-toc` cannot leave a heading out of the table of contents, and `awesome-lint` forbids some entries there. The `fix-awesome-toc` command deletes them.

### Python formatting

- [`ruff#7414` - Formatter: wrap comments (`E501`)](https://github.com/astral-sh/ruff/issues/7414): Ruff does not wrap long comments. The `format-python` job also runs `autopep8`, limited to that one fix.

### Shell formatting

- [`sh#1203` - zsh: support inline loop syntax](https://github.com/mvdan/sh/issues/1203): `shfmt` cannot parse some Zsh constructs, so repomatic keeps Zsh files away from it.

### Workflow linting

- [`actionlint#711` - Support the new `$/` self-repository `uses:` syntax](https://github.com/rhysd/actionlint/issues/711) / [`actionlint#732`](https://github.com/rhysd/actionlint/pull/732): `actionlint` rejects the `$/` self-repository syntax of GitHub. repomatic's workflows call their own reusable workflows through workspace-relative `./` paths instead.

### Test matrix

- [`uv#12906` - uv installs x86_64 Python on arm64 Windows](https://github.com/astral-sh/uv/issues/12906): `tests.yaml` forces a native ARM64 interpreter on the Windows ARM64 runner, which would otherwise test an emulated x86_64 Python.

### Nuitka binary builds

[Nuitka integration](nuitka.md) details the workaround for each of these:

- [`Nuitka#3879` - `--main-entry-point` doesn't populate the `_main_module` and `_main_paths` internal state](https://github.com/Nuitka/Nuitka/issues/3879) and [`Nuitka#4024` - `--main-entry-point` whose CLI name matches its package builds a binary that fails at startup](https://github.com/Nuitka/Nuitka/issues/4024): repomatic compiles a `__main__.py` entry point as its package directory, with `--python-flag=-m`.
- [`Nuitka#3909` - With `--project` the `[tool.nuitka]` section of `pyproject.toml` is ignored](https://github.com/Nuitka/Nuitka/issues/3909) and [`Nuitka#4025` - `--project` refuses to build over a `py.typed` and over a dependency's package data](https://github.com/Nuitka/Nuitka/issues/4025): Nuitka `4.2` reads `[tool.nuitka]` only under `--project`, a mode repomatic cannot use, so the tool runner turns the section into command-line flags. [`Nuitka#2136`](https://github.com/Nuitka/Nuitka/issues/2136) asked for the same support earlier.
- [`Nuitka#3994` - `--include-data-dir` package dangling symlinks: silent on Linux, `FileNotFoundError` in macOS signing](https://github.com/Nuitka/Nuitka/issues/3994): the tool runner stages a copy of the data directory with no symlinks before it calls Nuitka.
- [`Nuitka#3996` - `ccache` never hits across CI machines](https://github.com/Nuitka/Nuitka/issues/3996): the release workflow writes `base_dir` into `ccache.conf`.
- [`Nuitka#3997` - Tool downloads are fetched and executed without integrity verification](https://github.com/Nuitka/Nuitka/issues/3997): the macOS builds turn ccache off, which keeps its unverified download out of the build.

### Dependency cooldowns

- [`uv#20995` - Allow sharing exclude-newer-package across workspaces: file reference, env var, or repo-level config](https://github.com/astral-sh/uv/issues/20995): uv has no configuration or environment knob for `--exclude-newer-package`, so repomatic adds the exemption to every frozen command line. Glob exemptions ([`uv#20788`](https://github.com/astral-sh/uv/issues/20788)) or pin-based bypasses ([`uv#19864`](https://github.com/astral-sh/uv/issues/19864), [`uv#18921`](https://github.com/astral-sh/uv/pull/18921)) would also remove the need.
- [`uv#18792` - Prune stale `exclude-newer-package` entries on `uv lock`](https://github.com/astral-sh/uv/issues/18792): repomatic removes the stale entries itself after it relocks.

### Dependency graph

- [`mermaid#4182` - `click` cannot be used for node IDs](https://github.com/mermaid-js/mermaid/issues/4182): a Mermaid keyword breaks a graph when used as a node ID. repomatic keeps keywords out of the node IDs of its dependency graph, as [`pipdeptree#201`](https://github.com/tox-dev/pipdeptree/pull/201) does upstream.

### Dependency choices

Two dependencies stand in for a feature that a preferred library lacks:

- [`whenever#277`](https://github.com/ariebovenberg/whenever/discussions/277) (discussion): `whenever` has no humanizer, so repomatic uses `arrow` for its human-readable times.
- [`wcmatch#226` - Possibly add a gitignore parser](https://github.com/facelessuser/wcmatch/issues/226): `wcmatch` cannot read `.gitignore` files, so repomatic also depends on `py-walk`.

### Issue reporting

- [`create-issue-from-file#298` - Avoid creating duplicate issues](https://github.com/peter-evans/create-issue-from-file/issues/298): the action opens a new issue on every run. repomatic manages the issues its jobs open, so a new run does not duplicate them.

### Documentation

- [`furo#921`](https://github.com/pradyunsg/furo/discussions/921) (discussion): Furo shows no icons in toctree entries. repomatic's documentation adds them with CSS.

## Declined by upstream

Pull requests and features that upstream maintainers rejected.

- [`mypy#13294` - Auto-detect minimal Python for `--python-version` option](https://github.com/python/mypy/issues/13294): repomatic derives `--python-version` from `requires-python` when it runs `mypy`.
- [`pyupgrade#688` - Auto-detect minimal Python version](https://github.com/asottile/pyupgrade/issues/688): repomatic computed the minimal Python version from `requires-python` while it still ran `pyupgrade`.
- [`sh#1268` - Support configuration in `[tool.shfmt]` section of `pyproject.toml`](https://github.com/mvdan/sh/issues/1268): `shfmt` reads its style from `.editorconfig` only, so repomatic has no `[tool.shfmt]` section.

## Open upstream

Pull requests and issues still pending upstream.

- [`awesome-lint#229` - Fix false positives in `balanced-punctuation`](https://github.com/sindresorhus/awesome-lint/pull/229)

- [`bump-my-version#146` - Non-matching glob patterns don't raise `FileNotFoundError`](https://github.com/callowayproject/bump-my-version/issues/146)

- [`lychee#2249` - Recognize `{#id}` heading anchors in `--include-fragments`](https://github.com/lycheeverse/lychee/issues/2249): closed by [`lychee#2250`](https://github.com/lycheeverse/lychee/pull/2250), which no stable release carries yet.

- [`lychee#1772` - Suggest alternative frontend services from LibRedirect](https://github.com/lycheeverse/lychee/issues/1772)

- [`markdown-it-py#445` - Image `alt` text loses backslash escapes and entities](https://github.com/executablebooks/markdown-it-py/issues/445): the cause of `mdformat#599` and `MyST-Parser#1210`.

- [`mdformat#599` - Backslash escapes and entities are deleted from image `alt` text](https://github.com/hukkin/mdformat/issues/599): the `format-markdown` job applies that loss to every image it formats. See [A known mdformat defect](tool-runner.md#a-known-mdformat-defect).

  ```{todo}
  When a `markdown-it-py` release carries the fix for [executablebooks/markdown-it-py#445](https://github.com/executablebooks/markdown-it-py/issues/445), run the reproducer from [hukkin/mdformat#599](https://github.com/hukkin/mdformat/issues/599) through `repomatic run mdformat`. Then update this entry and [A known mdformat defect](tool-runner.md#a-known-mdformat-defect) to match the result.
  ```

- [`mdformat-myst#25` - Block attributes formatting introduce extra empty line](https://github.com/executablebooks/mdformat-myst/issues/25)

- [`mdformat-web#8` - `format_xml` drops comments and the XML declaration](https://github.com/hukkin/mdformat-web/issues/8): since `0.2.0`, formatting an `xml` code block deletes its comments and its `<?xml ...?>` declaration. repomatic pins `mdformat-web` `0.2.0`, so the `format-markdown` job applies that loss to every `xml` block it formats.

  ```{todo}
  When `sync-tool-versions` moves the `mdformat-web` pin past `0.2.0`, run the reproducer from [hukkin/mdformat-web#8](https://github.com/hukkin/mdformat-web/issues/8) through `repomatic run mdformat`. Then update this entry to match the result.
  ```

- [`Nuitka#3998` - `enableCcache` overwrites a user-set `CCACHE_SLOPPINESS`, unlike `CCACHE_DIR` which is honored](https://github.com/Nuitka/Nuitka/issues/3998)

- [`MyST-Parser#1210` - Image `alt` text loses backslash escapes](https://github.com/executablebooks/MyST-Parser/issues/1210): the same loss in the `alt` attribute of a page that Sphinx builds.

- [`MyST-Parser#1151` - Cross-reference to an explicit target emits a false-positive `myst.xref_missing` warning](https://github.com/executablebooks/MyST-Parser/issues/1151)

- [`ruff#19421` - Failure to create cache key on invalid symlink](https://github.com/astral-sh/ruff/issues/19421)

- [`sphinx#14623` - `todolist` produce duplicate titles for admonitions](https://github.com/sphinx-doc/sphinx/issues/14623)

- [`sphinx-design#304` - Implements vertical tabs](https://github.com/executablebooks/sphinx-design/pull/304)

<!-- typos:off -->

- [`typos#998` - False positive: `hda` → `had`](https://github.com/crate-ci/typos/issues/998)

<!-- typos:on -->
