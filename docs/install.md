# {octicon}`download` Installation

```{sidebar}
[![Packaging status](https://repology.org/badge/vertical-allrepos/python%3Arepomatic.svg)](https://repology.org/project/python%3Arepomatic/versions)
```

## Quick start

```shell-session
$ cd my-project
$ uvx -- repomatic init
$ git add .
$ git commit -m "Update repomatic files"
$ git push
```

Works for both new and existing repositories. Run `repomatic init --help` to see available components and options: the workflows then take it from there, opening issues and PRs to guide any remaining setup.

## Try it

Thanks to `uv`, you can run it in one command, without installation or venv:

`````{tab-set}

````{tab-item} Latest release
```shell-session
$ uvx -- repomatic --help
```

```{click:run}
from repomatic.cli import repomatic
invoke(repomatic, args=['--help'])
```
````

````{tab-item} Specific version
```shell-session
$ uvx -- repomatic==7.13.0 --help
```
````

````{tab-item} Development version
```shell-session
$ uvx --from "repomatic @ git+https://github.com/kdeldycke/repomatic" -- repomatic --help
```
````

`````

## Install methods

`repomatic` is available on a couple of package managers:

`````{tab-set}

````{tab-item} uv
Easiest way is to [install `uv`](https://docs.astral.sh/uv/getting-started/installation/), then install `repomatic` system-wide with the [`uv tool`](https://docs.astral.sh/uv/guides/tools/#installing-tools) command:

```{code-block} shell-session
$ uv tool install repomatic
```
````

````{tab-item} pip
You can install the latest stable release and its dependencies with a simple `pip` call:

```{code-block} shell-session
$ python -m pip install repomatic
```

If you have difficulties to use `pip`, see [`pip`'s own installation instructions](https://pip.pypa.io/en/stable/installation/).
````

````{tab-item} pipx
[`pipx`](https://pipx.pypa.io/stable/how-to/install-pipx/) is a great way to install Python applications globally:

```{code-block} shell-session
$ pipx install repomatic
```
````

````{tab-item} Arch Linux
A `repomatic` package is [available on AUR](https://aur.archlinux.org/packages/python-repomatic) and can be installed with any AUR helper:

```{code-block} shell-session
$ yay -S python-repomatic
```

```{code-block} shell-session
$ paru -S python-repomatic
```

```{code-block} shell-session
$ pacaur -S python-repomatic
```
````

`````

## Python compatibility

The table below shows which Python versions each `repomatic` release range supports, derived from the declarations in each git tag's `pyproject.toml`. It is refreshed by [click-extra's `{matrix}` directive machinery](https://kdeldycke.github.io/click-extra/sphinx.html#the-matrix-directive) through the `update-docs` job. Releases prior to `4.0.0` did not declare Python version support in any form and are not represented.

<!-- matrix python -->

| `repomatic`         | Released   | `3.14` | `3.13` | `3.12` | `3.11` | `3.10` | `3.9` | `3.8` |
| :------------------ | :--------- | :----: | :----: | :----: | :----: | :----: | :---: | :---: |
| `4.25.x` → `7.x`    | 2025-12-05 |   ✅   |   ✅   |   ✅   |   ✅   |   ✅   |  ❌   |  ❌   |
| `4.20.x` → `4.24.x` | 2025-10-10 |   ✅   |   ✅   |   ✅   |   ✅   |   ❌   |  ❌   |  ❌   |
| `4.15.x` → `4.19.x` | 2025-03-05 |   –    |   ✅   |   ✅   |   ✅   |   ❌   |  ❌   |  ❌   |
| `4.7.x` → `4.14.x`  | 2024-11-03 |   –    |   ✅   |   ✅   |   ✅   |   ✅   |  ❌   |  ❌   |
| `4.4.x` → `4.6.x`   | 2024-07-27 |   –    |   –    |   ✅   |   ✅   |   ✅   |  ✅   |  ❌   |
| `4.0.x` → `4.3.x`   | 2024-06-29 |   –    |   –    |   ✅   |   ✅   |   ✅   |  ✅   |  ✅   |

<!-- matrix-end -->

## Executables

To ease deployment, standalone executables of `repomatic`'s latest version are available as direct downloads for several platforms and architectures:

| Platform    | `arm64`                                                                                                                                              | `x86_64`                                                                                                                                         |
| :---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Linux**   | [Download `repomatic-7.13.0-linux-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v7.13.0/repomatic-7.13.0-linux-arm64.bin)     | [Download `repomatic-7.13.0-linux-x64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v7.13.0/repomatic-7.13.0-linux-x64.bin)     |
| **macOS**   | [Download `repomatic-7.13.0-macos-arm64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v7.13.0/repomatic-7.13.0-macos-arm64.bin)     | [Download `repomatic-7.13.0-macos-x64.bin`](https://github.com/kdeldycke/repomatic/releases/download/v7.13.0/repomatic-7.13.0-macos-x64.bin)     |
| **Windows** | [Download `repomatic-7.13.0-windows-arm64.exe`](https://github.com/kdeldycke/repomatic/releases/download/v7.13.0/repomatic-7.13.0-windows-arm64.exe) | [Download `repomatic-7.13.0-windows-x64.exe`](https://github.com/kdeldycke/repomatic/releases/download/v7.13.0/repomatic-7.13.0-windows-x64.exe) |

That way you have a chance to try it out without installing Python or `uv`. Or embed it in your CI/CD pipelines running on minimal images. Or run it on old platforms without worrying about dependency hell.

Binaries of all past releases, with their VirusTotal analyses, are cataloged on the [binaries page](binaries.md).

## Release verification

Every binary is signed with a [build provenance attestation](https://docs.github.com/en/actions/security-guides/using-artifact-attestations) at release time. After downloading one, verify it with the [`gh` CLI](https://cli.github.com):

```shell-session
$ gh attestation verify repomatic-7.13.0-linux-x64.bin --repo kdeldycke/repomatic --signer-repo kdeldycke/repomatic
```

`--signer-repo kdeldycke/repomatic` is required because the release runs from the reusable `_release-engine.yaml` workflow whose signing identity is `kdeldycke/repomatic`. Downstream projects that build binaries through the same reusable workflow verify with their own `--repo` but keep `--signer-repo kdeldycke/repomatic`.

The PyPI distributions carry their own [PEP 740](https://peps.python.org/pep-0740/) attestations, visible and verifiable on the [PyPI project page](https://pypi.org/project/repomatic/).
