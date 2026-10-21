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

"""Build `uv` and `uvx` command lines, and run them.

The leaf of the dependency package: it imports nothing else from it, so the
tool runner and the docs builder reach the builders without loading the lock
and report machinery. The lock views live in {mod}`repomatic.deps.lockfile`,
the cooldown lifecycle in {mod}`repomatic.deps.cooldown`.
"""

from __future__ import annotations

import logging
import subprocess

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def uv_cmd(
    subcommand: str,
    *,
    frozen: bool = False,
    no_project: bool = False,
    exclude_newer: str | None = None,
) -> list[str]:
    """Build a `uv <subcommand>` command prefix with standard flags.

    Always includes `--no-progress`. Adds `--frozen` when requested
    (appropriate for `run`, `export`, `sync`, never for `lock`). Adds
    `--no-project` to skip project discovery entirely, and `--exclude-newer`
    to gate a resolution by a cooldown, mirroring {func}`uvx_cmd`.

    :param subcommand: The `uv` subcommand.
    :param frozen: Add `--frozen`.
    :param no_project: Add `--no-project`.
    :param exclude_newer: The `--exclude-newer` value, in any form uv accepts.
        Omitted when empty.
    """
    cmd = ["uv", "--no-progress", subcommand]
    if frozen:
        cmd.append("--frozen")
    if no_project:
        cmd.append("--no-project")
    if exclude_newer:
        cmd += ["--exclude-newer", exclude_newer]
    return cmd


def uvx_cmd(exclude_newer: str | None = None) -> list[str]:
    """Build a `uvx` command prefix with standard flags.

    When *exclude_newer* is set (a `YYYY-MM-DD` date), adds `--exclude-newer` so
    the isolated resolution honors the `minimum-release-age` cooldown, gating the
    tool's transitive dependencies by upload date.
    """
    cmd = ["uvx", "--no-progress"]
    if exclude_newer:
        cmd += ["--exclude-newer", exclude_newer]
    return cmd


def uv_lock_command(
    *extra: str,
    exclude_newer: str = "",
    exclude_newer_packages: Mapping[str, str] | None = None,
) -> list[str]:
    """Build a `uv lock` argv carrying a cooldown window.

    The one builder behind every re-lock this package runs (`sync-uv-lock`,
    the dep-sources swap, `audit --fix`, the held-back probe), so none of them
    can forget the explicit `--exclude-newer` that keeps CI's ambient
    `UV_EXCLUDE_NEWER` from retiming the lock: see
    {attr}`repomatic.deps.cooldown.UvSettings.window`.

    :param extra: Extra `uv lock` arguments (`--upgrade`,
        `--upgrade-package`, ...), placed before the cooldown flags.
    :param exclude_newer: The window, in any form uv accepts. Empty leaves the
        flag off.
    :param exclude_newer_packages: Package name to its own window, for this run
        only. A flag on the command line beats the `pyproject.toml` entry of
        the same package.
    :return: The argv to run, with `cwd` set to the project directory.
    """
    cmd = [*uv_cmd("lock"), *extra]
    for name, value in sorted((exclude_newer_packages or {}).items()):
        cmd += ["--exclude-newer-package", f"{name}={value}"]
    if exclude_newer:
        cmd += ["--exclude-newer", exclude_newer]
    return cmd


class UvError(subprocess.CalledProcessError):
    """A `uv` command that exited non-zero, carrying what it printed on stderr."""

    def __str__(self) -> str:
        detail = str(self.stderr or "").strip()
        return f"{super().__str__()}\n{detail}" if detail else super().__str__()


def run_uv(
    cmd: Sequence[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a `uv` command and capture its output.

    The one runner behind every `uv` call of this package. The output is
    captured, never streamed: a resolution summary printed while a progress
    trail is on screen lands on the trail's own row. The decoding is pinned to
    UTF-8, since an SBOM carries package descriptions and author names that
    the platform default cannot always decode.

    :param cmd: The argv, from one of the builders above.
    :param cwd: The directory to run in. Defaults to the working directory.
    :param check: Raise on a non-zero exit.
    :return: The completed process, `stdout` and `stderr` as text.
    :raises UvError: When the command fails and *check* is set. Its message
        carries the command's stderr.
    """
    logging.debug(f"Running {' '.join(cmd)}" + (f" in {cwd}." if cwd else "."))
    result = subprocess.run(
        list(cmd), capture_output=True, encoding="UTF-8", check=False, cwd=cwd
    )
    if check and result.returncode:
        raise UvError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def relock() -> None:
    """Refresh `uv.lock` after a version bump, and nothing else in it.

    A plain `uv lock`, on purpose. `bump-my-version` rewrites the project's
    version in `pyproject.toml` but not in `uv.lock`, and a bump committed
    without the re-lock leaves the project ahead of its own lock entry: the
    next `uv run --frozen` then reinstalls the project, and two nested syncs of
    a release build can delete a console script the other one is running, a
    sharing violation on Windows.

    `uv lock --upgrade` would re-resolve every dependency instead. With a
    `"0 day"` `exclude-newer-package` bypass in place, it fast-forwards that
    dependency across a release still inside its cooldown, on an unreviewed
    bump pull request. `uv sync` would leave the lock in a state the
    `sync-uv-lock` job rewrites into a redundant follow-up pull request.
    Dependency upgrades stay the job of `sync-uv-lock`, in their own pull
    request.

    :raises UvError: When uv cannot lock the project.
    """
    cmd = uv_cmd("lock")
    logging.info(f"Running: {' '.join(cmd)}")
    run_uv(cmd)
