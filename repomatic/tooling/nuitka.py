# Copyright Kevin Deldycke <kevin@deldycke.com> and contributors.
#
# This program is Free Software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""What `repomatic run nuitka` adds to a Nuitka build.

The `nuitka` entry of {data}`~repomatic.tooling.registry.TOOL_REGISTRY` reads
two hooks from here:

- {func}`nuitka_params`, its computed parameters: the plugin that guards the
  entry point, the modules the binary leaves out, and the options of the build
  target this machine compiles for.
- {func}`prepare_nuitka_build`, its preparation: the plugin to write, the extras
  to install, the compile cache to trim and configure, and the environment of
  the target.

Each of them reads the machine it runs on, so the release workflow hands a
compile job nothing but the entry point to build.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from ..deps.uv import run_uv, uv_cmd
from ..file_ops import atomic_write
from ..release.binary import current_build_target

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Final

    from ..metadata.core import Metadata

MEBIBYTE: Final[int] = 1024 * 1024
"""Bytes in a mebibyte, the unit the cache size is logged in."""

COMPILE_CACHE_CAP: Final[int] = 2048 * MEBIBYTE
"""Size past which {func}`prepare_compile_cache` resets the compile cache.

Nuitka's internal clcache prunes by age alone and has no size cap (ccache
prunes itself at 5 GiB), so a restore-then-save cycle can still grow the
cache. Past this size, the cache starts a fresh lineage.
"""

CCACHE_CONFIG: Final[str] = "ccache.conf"
"""The file ccache reads its settings from, inside its cache directory."""

ENTRY_POINT_GUARD: Final[str] = "repomatic-entry-point-guard.py"
"""The file {func}`write_entry_point_guard` writes the Nuitka plugin to."""

ENTRY_POINT_GUARD_PLUGIN: Final[str] = r'''
"""Nuitka user plugin that `repomatic run nuitka` writes and loads.

It adds a `__name__ == "__main__"` guard to the launcher Nuitka writes for
`--main-entry-point`.
"""

from nuitka.plugins.PluginBase import NuitkaPluginBase


class EntryPointGuard(NuitkaPluginBase):
    plugin_name = "repomatic-entry-point-guard"
    plugin_desc = "Call the --main-entry-point function in the main process only."

    def onModuleSourceCode(self, module_name, source_filename, source_code):
        lines = source_code.splitlines()
        if (
            len(lines) == 3
            and lines[0] == "import sys"
            and lines[1].endswith(" as user_main")
            and lines[2].startswith("sys.exit(user_main.")
        ):
            lines[2:] = ['if __name__ == "__main__":', "    " + lines[2]]
            return "\n".join(lines) + "\n"
        return source_code
'''
"""Nuitka plugin calling the `--main-entry-point` function in the main process only.

```{note}
Nuitka writes the launcher of `--main-entry-point` with no `__name__ ==
"__main__"` guard: it imports the module of the entry point, then calls the
function at once. Nuitka compiles that launcher twice: as the main program, and
as the `__parents_main__` module that each multiprocessing worker runs before
its task. So each worker runs the whole CLI again. A CLI that starts workers then
fails, or starts more workers from each worker.

The plugin adds the guard to both copies, through the `onModuleSourceCode` hook
of Nuitka, and leaves every other source as it is.
```

```{todo}
Drop this plugin and its `--user-plugin` option once a Nuitka release fixes
[Nuitka#4136](https://github.com/Nuitka/Nuitka/issues/4136).
```
"""


def entry_point_guard_path() -> Path:
    """Where {func}`write_entry_point_guard` writes the plugin.

    In the temporary directory of the job on a runner, which outlives this
    process, and in the temporary directory of the system elsewhere. The name
    is fixed, so each build replaces the file of the build before it.
    """
    temp_dir = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(temp_dir) / ENTRY_POINT_GUARD


def write_entry_point_guard() -> Path:
    """Write {data}`ENTRY_POINT_GUARD_PLUGIN` where `--user-plugin` reads it.

    The write renames a full temporary file into place, so a build that starts
    at the same time never reads half a plugin.

    :return: The path of the plugin.
    """
    path = entry_point_guard_path()
    atomic_write(
        path,
        f".{ENTRY_POINT_GUARD}.",
        lambda tmp: tmp.write_text(ENTRY_POINT_GUARD_PLUGIN, encoding="UTF-8"),
    )
    return path


def nuitka_params(metadata: Metadata) -> list[str]:
    """Build the options `repomatic run nuitka` passes after the shared ones.

    :param metadata: The project the binary is built from.
    :return: The `--user-plugin` option of the plugin that
        {func}`prepare_nuitka_build` writes, a `--nofollow-import-to` option
        per module of `[tool.repomatic] nuitka.nofollow-imports`, then the
        options of the build target this machine compiles for (see
        {attr}`~repomatic.release.binary.BuildTarget.nuitka_flags`).
    """
    params = [f"--user-plugin={entry_point_guard_path()}"]
    params.extend(
        f"--nofollow-import-to={module}"
        for module in metadata.config.nuitka_nofollow_imports
    )
    target = current_build_target()
    if target is not None:
        params.extend(target.nuitka_flags)
    return params


def install_extras(extras: Sequence[str]) -> None:
    """Install the optional dependency groups *extras* into the project venv.

    The binary only bundles what the build virtualenv holds, which by default
    is the base dependencies of the project. An extra listed in
    `[tool.repomatic] nuitka.extras` adds the packages of an optional feature.

    The sync is inexact: it adds the extras and removes nothing, so a
    developer's virtualenv keeps the dependency groups it had.

    :param extras: Names of `[project.optional-dependencies]` groups.
    :raises UvError: When uv cannot install them.
    """
    if not extras:
        return
    cmd = [*uv_cmd("sync", frozen=True), "--inexact"]
    for extra in extras:
        cmd.extend(["--extra", extra])
    logging.info(f"Running: {' '.join(cmd)}")
    run_uv(cmd)


def _directory_size(directory: Path) -> int:
    """Total size in bytes of the files below *directory*."""
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def prepare_compile_cache(
    cache_dir: Path, ccache_dir: Path, work_root: Path | None
) -> None:
    """Trim the Nuitka compile cache, then write the settings of ccache.

    Two duties. The trim resets a cache past {data}`COMPILE_CACHE_CAP`. The
    settings land in the {data}`CCACHE_CONFIG` file of *ccache_dir*, which
    ccache always reads, and which the cache step of the workflow saves with
    the objects:

    - `compiler_check = content` hashes the compiler binary's content. The
      default check reads its modification time and size, which runner VMs do
      not keep stable.
    - `base_dir` makes ccache hash every path below *work_root* as a relative
      path, so a work root that moves between machines keeps its keys. Nuitka
      sets no `base_dir` of its own.

    :param cache_dir: The directory `NUITKA_CACHE_DIR` names.
    :param ccache_dir: The directory `CCACHE_DIR` names.
    :param work_root: The directory holding every checkout of the runner, or
        `None` to leave `base_dir` unset.
    """
    if cache_dir.is_dir():
        size = _directory_size(cache_dir)
        logging.info(f"Nuitka cache restored at {size // MEBIBYTE} MiB.")
        if size > COMPILE_CACHE_CAP:
            logging.info(
                f"Cache exceeds {COMPILE_CACHE_CAP // MEBIBYTE} MiB, resetting."
            )
            shutil.rmtree(cache_dir)
    ccache_dir.mkdir(parents=True, exist_ok=True)
    settings = ["compiler_check = content"]
    if work_root is not None:
        settings.append(f"base_dir = {work_root}")
    (ccache_dir / CCACHE_CONFIG).write_text(
        "".join(f"{line}\n" for line in settings), encoding="UTF-8"
    )


def prepare_nuitka_build(metadata: Metadata) -> dict[str, str]:
    """Ready this machine for the Nuitka build `repomatic run nuitka` starts.

    Writes the plugin that {func}`nuitka_params` loads, and installs the
    `[tool.repomatic] nuitka.extras`. Then prepares the compile cache when the
    job relocated it under `NUITKA_CACHE_DIR` and the target keeps one (see
    {attr}`~repomatic.release.binary.BuildTarget.compile_cache`). Outside CI
    the variable is unset, and the cache is left alone.

    :param metadata: The project the binary is built from.
    :return: The environment variables the build of this machine's target
        needs, from {attr}`~repomatic.release.binary.BuildTarget.build_env`.
    """
    write_entry_point_guard()
    install_extras(metadata.config.nuitka_extras)

    target = current_build_target()
    cache_dir = os.environ.get("NUITKA_CACHE_DIR")
    if cache_dir and (target is None or target.compile_cache):
        workspace = os.environ.get("RUNNER_WORKSPACE")
        prepare_compile_cache(
            Path(cache_dir),
            Path(os.environ.get("CCACHE_DIR") or Path(cache_dir) / "ccache"),
            Path(workspace).parent if workspace else None,
        )

    return dict(target.build_env) if target is not None else {}
