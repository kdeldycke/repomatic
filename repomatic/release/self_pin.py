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

"""The pin a workflow holds on the upstream toolkit itself.

A workflow reaches the upstream toolkit from a `run:` command through an inline
pin: `uvx 'repomatic==1.2.3' show-metadata`. That pin moves in lockstep with the
`uses:` refs of the same workflows, whatever the cooldown, so the command also
carries a cooldown exemption for the package. On `main` of the source
repository, the workflows run the CLI from the lockfile instead, and the release
freeze swaps the one form for the other.

Every spelling of those commands lives here, for each of their writers and
checkers: the release freeze and unfreeze, the realignment of `repomatic init`,
the rewrite of `sync-workflow-pins`, and the checks of `lint-repo`. Each one
takes the package name: `repomatic init --upstream-repo` can point a repository
at a renamed fork, whose package is not `repomatic`.
"""

from __future__ import annotations

import re
from functools import cache


def cooldown_exemption(package: str) -> str:
    """The uv flag letting a just-published *package* install under the cooldown.

    Every workflow exports a `UV_EXCLUDE_NEWER` covering all package resolution
    (see `docs/workflows.md` § Install-time cooldown), and it applies to the
    frozen `'repomatic==X.Y.Z'` self-pin like any other requirement. That pin
    moves in lockstep with the `uses:` refs pointing at the same tag, so the
    version it names is always minutes old: without an exemption every
    downstream repository would fail to resolve it until the window elapsed. A
    zero-length window sets the cutoff of that one package to "now", and leaves
    the rest of the tree gated.

    uv exposes no environment variable for `--exclude-newer-package`, so the
    exemption has to ride on the command line. That is why it sits beside the
    pin instead of the workflows declaring it once.

    ```{note}
    A `uvx` resolution reads no project configuration at all, so moving the
    exemption into `[tool.uv]` or an adjacent `uv.toml` would not work either:
    both are ignored (verified against uv `0.12.3`). The one knob that does
    reach it, `--config-file` / `UV_CONFIG_FILE`, replaces discovered
    configuration instead of merging with it: set for a whole CI environment, it
    silently drops `required-version`, `exclude-newer`, `dependency-groups` and
    `build-backend` from every other uv command there, unless a complete mirror
    of `[tool.uv]` sits beside it.
    ```

    ```{todo}
    Declare the exemption once, instead of splicing it onto every frozen command
    line, as soon as uv grows a configuration or environment knob for
    `--exclude-newer-package`:
    [astral-sh/uv#20995](https://github.com/astral-sh/uv/issues/20995). Glob
    exemptions ([astral-sh/uv#20788](https://github.com/astral-sh/uv/issues/20788))
    and pin-based bypasses
    ([astral-sh/uv#19864](https://github.com/astral-sh/uv/issues/19864),
    [astral-sh/uv#18921](https://github.com/astral-sh/uv/pull/18921)) could settle
    it too.
    ```

    Spelled as the ISO 8601 `P0D` rather than the `"0 day"` used in
    `pyproject.toml`'s `exclude-newer-package` table: the flag travels through
    YAML folded scalars into a shell, where the space in `0 day` would need
    quoting that survives both. `P0D` needs none.

    ```{caution}
    Every character here lands on 80-odd already-long workflow lines at freeze
    time. `tests/test_prepare_release.py` simulates a freeze and fails if the
    result breaches the 120-column cap of yamllint, so lengthening this string
    means reflowing the workflows that no longer fit.
    ```

    :param package: The distribution name of the upstream toolkit.
    :return: The flag and its value, as one shell-safe string.
    """
    return f"--exclude-newer-package {package}=P0D"


def local_cli_invocation(package: str) -> str:
    """How every workflow on `main` runs the CLI, before the freeze rewrites it.

    Resolving from `uv.lock` rather than from the index is what keeps the
    cooldown off the critical path here. A lockfile entry is pinned *and*
    hash-verified, so it is strictly stronger than a publication-age gate, and
    it cannot be made unsatisfiable by one: `uvx --from .` re-resolves
    `[project.dependencies]` on every call, and reads neither `uv.lock` nor
    `[tool.uv] exclude-newer-package`, so a floor naming a release younger than
    the window took every workflow down at once with nowhere to record the
    bypass.

    `--frozen` uses the lockfile as-is instead of asserting it is current, which
    is deliberate: `--locked` would fail every job the moment `pyproject.toml`
    drifted ahead of `uv.lock`, including the `sync-uv-lock` job whose whole
    purpose is to close that gap.

    :param package: The distribution name of the upstream toolkit, which is
        also the name of its command.
    :return: The command prefix, ready to take the arguments of the CLI.
    """
    return f"uv --no-progress run --frozen -- {package}"


def frozen_cli_invocation(package: str, version: str) -> str:
    """Render the frozen, cooldown-exempt `uvx` invocation of *package*.

    The one spelling both writers emit: the release freeze writes it whole,
    and {func}`add_cooldown_exemption` converges an exemption-less command onto
    the same bytes, so {func}`frozen_cli_re` has one shape to recognize.

    :param package: The distribution name of the upstream toolkit.
    :param version: The release the invocation pins.
    """
    return f"uvx --no-progress {cooldown_exemption(package)} '{package}=={version}'"


@cache
def frozen_cli_re(package: str) -> re.Pattern[str]:
    """Match {func}`frozen_cli_invocation` at any version.

    What the release unfreeze turns back into {func}`local_cli_invocation`.
    """
    return re.compile(
        rf"uvx --no-progress {re.escape(cooldown_exemption(package))} "
        rf"'{re.escape(package)}==[\d.]+'"
    )


@cache
def inline_pin_re(package: str) -> re.Pattern[str]:
    """Match an inline `{package}==X.Y.Z` pin, in any shape a workflow writes it.

    The one matcher of the pinned version: the `lint-repo` lockstep check reads
    it, and `repomatic init` and `sync-workflow-pins` rewrite it, so the check
    never flags a pin the rewrite cannot fix. Quoted or bare, with or without
    extras: `uvx 'repomatic==1.2.3'`, `--with 'repomatic[cli]==1.2.3'`,
    `pip install repomatic==1.2.3`.

    The lookbehind leaves out a longer name ending in *package*, like
    `papaya-repomatic==1.0`. The exemption of {func}`cooldown_exemption` never
    matches: it joins the package to its window with a single `=`.

    Memoized: every caller asks for the same package, once per workflow file.
    """
    return re.compile(
        rf"(?<![\w.-]){re.escape(package)}(?P<extras>\[[^\]]+\])?"
        r"==(?P<version>[0-9]+(?:\.[0-9]+)*)"
    )


def find_inline_pins(content: str, package: str) -> list[str]:
    """The versions the inline pins of *package* name, in the order they appear.

    :param content: The workflow file text.
    :param package: The distribution name of the upstream toolkit.
    """
    return [match["version"] for match in inline_pin_re(package).finditer(content)]


def align_inline_pins(
    content: str, package: str, version: str
) -> tuple[str, list[tuple[str, str, str]]]:
    """Rewrite every inline pin of *package* onto *version*, in either direction.

    The refs are the source of truth, so a pin ahead of them is drift the same
    way one behind them is.

    :param content: The workflow file text.
    :param package: The distribution name of the upstream toolkit.
    :param version: The version to pin, without the `v` of a tag.
    :return: The updated text, and one `(package, old, new)` triple per pin
        that moved.
    """
    changes: list[tuple[str, str, str]] = []

    def realign(match: re.Match[str]) -> str:
        old = match["version"]
        if old == version:
            return match.group(0)
        changes.append((package, old, version))
        return f"{package}{match['extras'] or ''}=={version}"

    return inline_pin_re(package).sub(realign, content), changes


@cache
def _uvx_pin_re(package: str) -> re.Pattern[str]:
    """Match a `uvx` command pinning *package*, capturing the flags before the pin.

    The one matcher of the exemption, which {func}`cooldown_exemptions` reads
    and {func}`add_cooldown_exemption` writes. It reads a quoted pin only, the
    form the release freeze writes: the splice lands right before the pin, and
    before a bare one it could split an option from its value, as in
    `--with repomatic==1.2.3`.

    Memoized: the splice runs once per workflow file for the same package.
    """
    return re.compile(
        r"(?P<prefix>uvx\s+)(?P<flags>[^'\n]*)"
        rf"(?P<pin>'{re.escape(package)}(?:\[[^\]]+\])?==[0-9][0-9.]*')"
    )


def cooldown_exemptions(content: str, package: str) -> list[bool]:
    """Whether each `uvx` command pinning *package* carries its exemption.

    :param content: The workflow file text.
    :param package: The distribution name of the upstream toolkit.
    :return: One flag per command, in the order they appear.
    """
    exemption = cooldown_exemption(package)
    return [
        exemption in match["flags"] for match in _uvx_pin_re(package).finditer(content)
    ]


def add_cooldown_exemption(content: str, package: str) -> str:
    """Splice the exemption into every `uvx` command pinning *package*.

    The inline pin moves in lockstep with the `uses:` refs, regardless of the
    cooldown, so the version it names can be minutes old. Every workflow exports
    a `UV_EXCLUDE_NEWER` covering all resolution, and `uvx` reads no
    per-package exemption from the environment or from `pyproject.toml`, so
    without the flag on the command line the freshly aligned pin fails to
    resolve until the window elapses.

    Idempotent: a command already carrying the exemption is left untouched.

    :param content: The workflow file text.
    :param package: The distribution name of the upstream toolkit.
    :return: The updated text.
    """
    exemption = cooldown_exemption(package)

    def splice(match: re.Match[str]) -> str:
        if exemption in match["flags"]:
            return match.group(0)
        # The exemption lands after the existing flags, right before the pin,
        # so the result is the same bytes the release freeze writes, and the
        # unfreeze only ever meets one spelling.
        return f"{match['prefix']}{match['flags']}{exemption} {match['pin']}"

    return _uvx_pin_re(package).sub(splice, content)
