# {octicon}`package-dependents` Packaging

Distribution and packaging reference for `repomatic`, aimed at downstream packagers.

## Dependencies

This is a graph of the Python package's dependencies. Boxes hold the directly-declared dependencies (main set and each development group), drawn as hexagons; transitive dependencies render outside the boxes as ovals:

```mermaid assets/dependencies.mmd
:align: center
```

## Build backends

A repomatic-managed project declares `uv_build` as its build backend. Distribution channels do not all have it: a packager whose toolchain predates `uv-build`, or whose policy forbids fetching it, falls back to setuptools, and setuptools then runs its own automatic package discovery over the repository root.

That discovery fails on any repository holding more than one top-level directory, which is every project of this shape:

```text
error: Multiple top-level packages discovered in a flat-layout: ['docs', 'tests', 'my_package'].
```

The fix is one section naming the package explicitly:

```toml
[tool.setuptools]
# Distributors lacking uv-build fall back to setuptools, whose automatic
# discovery then sees docs/, tests/ and every other top-level directory as a
# package and fails. uv-build ignores this section entirely, relying on
# [tool.uv] build-backend instead, so it costs a released wheel nothing.
packages.find.include = [ "my_package*" ]
```

Two things make this worth knowing rather than obvious. It is invisible upstream: `uv build`, `uv sync` and every wheel published from CI ignore the section, so the failure only ever appears in a downstream packager's build log, usually weeks after the release that introduced the second top-level directory. And the trailing `*` matters, since it is what keeps subpackages in.

`repomatic init` does not write this section. The value it must carry is the package's own import name, and a bundled template can only approximate that with a list of directories to exclude, which is a worse default than the one line a project can write once and never revisit.
