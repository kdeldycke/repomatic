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

"""Unified tool runner with managed config resolution.

Provides `repomatic run <tool>`: a single entry point that installs an
external tool at a pinned version, resolves its configuration through a strict
4-level precedence chain, translates `[tool.X]` sections from
`pyproject.toml` into the tool's native format, and invokes the tool with
the resolved config. The tool catalog it drives (`ToolSpec` entries, pinned
versions, checksums) lives in `tooling/registry.py`, and `tooling/binaries.py`
installs the binary tools.

```{important}
Config resolution precedence (first match wins, no merging):

1. **Native config file**: the tool's own config file in the repo.
2. **`[tool.X]` in `pyproject.toml`**: translated to the native format.
3. **Bundled default**: from `repomatic/data/`.
4. **Bare invocation**: no config at all.
```
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from click_extra import config_table_to_flags
from extra_platforms import is_github_ci
from packaging.version import Version

from .. import bundle
from ..cache import lock_path, store_config
from ..config import load_repomatic_config
from ..deps.pins import exclude_newer_cutoff, min_release_age_days
from ..deps.uv import UvError, uv_cmd, uvx_cmd
from ..file_inventory import FileInventory
from ..file_ops import normalize_text
from ..github.outputs import add_problem_matcher, remove_problem_matcher
from ..hashing import compute_file_sha256
from ..humanize import today_utc
from ..metadata.core import Metadata
from ..pyproject import read_pyproject_toml
from . import binaries
from .registry import (
    NPM_BOOTSTRAP_TOOL,
    NPM_MIN_VERSION_FOR_COOLDOWN,
    TOOL_REGISTRY,
    BinaryInstallError,
    NativeFormat,
    ToolBackend,
    ToolInstallError,
    ToolSpec,
    UnsupportedPlatformError,
    lookup_tool,
)

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence
    from typing import IO, Any


GIT_DIRECTORY = ".git"
"""Directory `_digest_targets` leaves out of the tree it walks.

No tool rewrites a file there, and another process can: an editor that
refreshes the index during a run would read as a file the tool rewrote.
"""

LOCK_POLL_SECONDS = 0.1
"""Delay between two attempts to take a lock Windows reported as held.

Windows has no blocking lock call that waits indefinitely: `msvcrt.LK_LOCK`
gives up after ten one-second attempts, so `_acquire_lock` polls instead.
"""

NPM_INSTALL_FLAGS = (
    "--ignore-scripts",
    "--no-progress",
    "--no-update-notifier",
    "--no-fund",
    "--no-audit",
)
"""Flags of every `npm install` repomatic runs.

`--ignore-scripts` skips the `preinstall`, `install` and `postinstall` scripts of
the package and of its whole transitive tree: the install-time vector behind
most npm supply-chain worms. It is safe here because the npm backend only
installs a repomatic-pinned, cooldown-gated tool, where no install script is
needed. `--no-audit` stops npm from sending the resolved dependency tree to the
audit endpoint of the registry on every install: the signature audit that
follows each install is a different check, see `_npm_audit_signatures`.

The install of the pinned npm that installs the tool carries them too: see
`_install_npm`.
"""

TOOL_CRASH_EXIT_CODE = 70
"""Exit code reported when a tool contradicts its own rewrite status.

`EX_SOFTWARE` from `sysexits.h`: an internal error in the tool being run.
Deliberately distinct from the code a formatter uses to say "I reformatted a
file", so a crash cannot pass for a rewrite. See
{attr}`~repomatic.tooling.registry.ToolSpec.rewrite_exit_code`.
"""


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class ConfigLevel(IntEnum):
    """Precedence level of a tool's configuration: the first one present wins."""

    NATIVE = 1
    """The tool's own config file in the repository."""

    PYPROJECT = 2
    """The tool's `[tool.X]` table in `pyproject.toml`."""

    BUNDLED = 3
    """The default repomatic bundles in `repomatic/data/`."""

    BARE = 4
    """No configuration: the tool runs on its own defaults."""


@dataclass(frozen=True)
class ConfigSource:
    """The configuration a tool resolves to, as the precedence walk finds it.

    ```{caution}
    The levels do not merge. The walk stops at its first hit, so a native
    config file or a `[tool.X]` section replaces the bundled default in full
    rather than layering on top of it. A downstream repo overriding one rule
    must restate every bundled rule it wants to keep, and gains nothing when
    the bundled default later grows a rule. {attr}`label` names such a
    shadowing config, so `repomatic run --list` shows the loss.
    ```
    """

    spec: ToolSpec
    """The tool the configuration is for."""

    level: ConfigLevel
    """The level that wins."""

    native_file: Path | None = None
    """The config file of the repository, at {attr}`ConfigLevel.NATIVE`."""

    table: dict[str, Any] | None = None
    """The `[tool.X]` table, at {attr}`ConfigLevel.PYPROJECT`.

    The live table of the parsed `pyproject.toml`, a `tomlrt.Table`. It keeps
    its comments for the formats that can carry them (see
    {meth}`~repomatic.tooling.registry.NativeFormat.serialize`). The parse is
    shared, so the table is read-only.
    """

    @classmethod
    def find(cls, spec: ToolSpec) -> ConfigSource:
        """Walk the precedence chain of *spec* in the working directory."""
        native = spec.find_native_config()
        if native is not None:
            return cls(spec, ConfigLevel.NATIVE, native_file=native)
        table = read_pyproject_toml().get("tool", {}).get(spec.pyproject_key)
        if table:
            return cls(spec, ConfigLevel.PYPROJECT, table=table)
        if spec.default_config:
            return cls(spec, ConfigLevel.BUNDLED)
        return cls(spec, ConfigLevel.BARE)

    @property
    def label(self) -> str:
        """Describe the source, as `repomatic run --list` shows it."""
        # A level-1 or level-2 config replaces the bundled default whole: every
        # bundled rule it does not restate is dropped, so the label says so.
        shadowed = " (replaces bundled default)" if self.spec.default_config else ""
        if self.native_file is not None:
            return f"{self.native_file.as_posix()}{shadowed}"
        if self.level is ConfigLevel.PYPROJECT:
            return f"[tool.{self.spec.pyproject_key}] in pyproject.toml{shadowed}"
        if self.level is ConfigLevel.BUNDLED:
            return "bundled default"
        return "(bare)"


def _config_filename(spec: ToolSpec) -> str:
    """Derive the canonical config filename for the cache.

    Uses the first `native_config_files` entry (without leading dots or path
    components) if available, otherwise constructs from the native format
    extension.
    """
    if spec.native_config_files:
        return Path(spec.native_config_files[0]).name.lstrip(".")
    return f"{spec.name}.{spec.native_format.value}"


def _store_config_to_cache(
    spec: ToolSpec,
    content: str,
) -> tuple[list[str], Path | None]:
    """Write config content to the cache and return CLI args.

    :return: `([flag, cache_path], None)` on success, or falls back to a temp
        file returning `([flag, tmp_path], tmp_path)` if the cache is not
        writable.
    """
    assert spec.config_flag is not None
    filename = _config_filename(spec)
    cached = store_config(spec.name, filename, content)
    if cached is not None:
        logging.info(
            f"{spec.name}: config cached at {cached}, passing via {spec.config_flag}."
        )
        return [spec.config_flag, str(cached)], None

    logging.warning(
        f"{spec.name}: cache directory not writable, falling back to temp file."
    )
    with tempfile.NamedTemporaryFile(
        encoding="UTF-8",
        mode="w",
        suffix=f".{spec.native_format.value}",
        prefix=f"repomatic-{spec.name}-",
        delete=False,
    ) as tmp:
        tmp.write(content)
    tmp_path = Path(tmp.name)
    return [spec.config_flag, str(tmp_path)], tmp_path


def _write_cwd_config(spec: ToolSpec, content: str, level: ConfigLevel) -> Path:
    """Write config content to the first native config path for CWD-discovery.

    For tools without a `--config` flag. The caller must clean up the file
    after the tool exits.

    :param spec: Tool specification (must have `native_config_files`).
    :param content: Config file content to write.
    :param level: Precedence level, for the log message.
    :return: Path to the written file.
    """
    target = Path(spec.native_config_files[0])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="UTF-8")
    logging.warning(
        f"{spec.name}: wrote config to repo at {target.resolve()} (level"
        f" {level.value}). This tool has no --config flag; the file will be removed"
        " after the run."
    )
    return target


def _deliver_config(
    spec: ToolSpec,
    content: str,
    level: ConfigLevel,
) -> tuple[list[str], Path | None]:
    """Deliver resolved config content via the appropriate mechanism.

    Tools with `config_flag` get a cached file passed via CLI. Tools with
    only `native_config_files` get a CWD file that must be cleaned up.

    :param spec: Tool specification.
    :param content: Config file content.
    :param level: Precedence level for logging.
    :return: Tuple of (extra CLI args, cleanup path or None).
    """
    if spec.config_flag:
        return _store_config_to_cache(spec, content)

    if spec.native_config_files:
        return [], _write_cwd_config(spec, content, level)

    msg = (
        f"{spec.name} has config content at level {level.value} but no config_flag "
        f"and no native_config_files to write to."
    )
    raise NotImplementedError(msg)


def resolve_config(spec: ToolSpec) -> tuple[list[str], Path | None]:
    """Resolve config for a tool using the 4-level precedence chain.

    See {class}`ConfigSource` for the walk, and why its levels do not merge.

    :param spec: Tool specification.
    :return: Tuple of (extra CLI args for config, path to clean up).
        The path is `None` when no cleanup is needed (cache-based configs
        persist across runs). Non-`None` paths are CWD files written for
        tools that have no `--config` flag.
    """
    source = ConfigSource.find(spec)

    if source.level is ConfigLevel.NATIVE:
        logging.info(
            f"{spec.name}: using native config file: {source.native_file} (level 1)."
        )
        return [], None

    if source.table is not None:
        if spec.reads_pyproject:
            logging.info(
                f"{spec.name}: using [tool.{spec.pyproject_key}] in pyproject.toml,"
                " read natively (level 2)."
            )
            return [], None

        if spec.native_format is NativeFormat.FLAGS:
            flags = config_table_to_flags(source.table)
            logging.info(
                f"{spec.name}: translated [tool.{spec.pyproject_key}] to {len(flags)}"
                " CLI flag(s) (level 2)."
            )
            return flags, None

        content = spec.native_format.serialize(source.table, tool_name=spec.name)
        logging.debug(
            f"Translated [tool.{spec.pyproject_key}] to"
            f" {spec.native_format.value}:\n{content}"
        )
        return _deliver_config(spec, content, source.level)

    if spec.default_config and source.level is ConfigLevel.BUNDLED:
        content = bundle.get_data_content(spec.default_config)
        return _deliver_config(spec, content, source.level)

    logging.info(f"{spec.name}: no config found, bare invocation (level 4).")
    return [], None


def _acquire_lock(handle: IO[bytes], *, blocking: bool) -> bool:
    """Take the exclusive OS lock on an open file.

    The operating system drops the lock with the process holding it, so a run
    that dies cannot leave the next one waiting forever.

    :param handle: File opened in binary mode.
    :param blocking: Wait for the lock instead of giving up when it is held.
    :return: `True` once the lock is held, `False` when it is held elsewhere and
        *blocking* is off.
    """
    if sys.platform == "win32":
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                if not blocking:
                    return False
                time.sleep(LOCK_POLL_SECONDS)
            else:
                return True
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        return False
    return True


def _release_lock(handle: IO[bytes]) -> None:
    """Release the lock {func}`_acquire_lock` took on *handle*."""
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _cwd_config_lock(spec: ToolSpec) -> Generator[None, None, None]:
    """Hold off other runs that would write the same working-directory config.

    A tool with no `--config` flag gets its config through
    {func}`_write_cwd_config`, as a file that exists only for the length of one
    run. Without this lock, a second run of the same tool starting in the same
    directory takes that file for the repository's own (level 1), then loses it
    halfway when the first run deletes it, and formats its remaining files with
    no config at all. Parallel agents sharing one checkout run into exactly that.

    The lock file lives in the cache, named after the config's absolute path,
    so only runs that would write the same file wait for each other. When it
    cannot be opened (a read-only cache), the run goes ahead unlocked.

    :param spec: Tool specification with `native_config_files`.
    """
    target = Path(spec.native_config_files[0]).resolve()
    digest = hashlib.sha256(str(target).encode("UTF-8")).hexdigest()[:16]
    path = lock_path(f"{spec.name}-{digest}")
    with ExitStack() as stack:
        handle: IO[bytes] | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(path.open("a+b"))
        except OSError:
            logging.debug(f"{spec.name}: cannot open {path}, running unlocked.")
        if handle is not None and not _acquire_lock(handle, blocking=False):
            logging.info(f"{spec.name}: waiting for another run that uses {target}.")
            _acquire_lock(handle, blocking=True)
        try:
            yield
        finally:
            if handle is not None:
                _release_lock(handle)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _npm_supports_cooldown(npm: str) -> bool:
    """Whether the npm at *npm* honors `min-release-age` (npm 11.10.0+).

    Runs `npm --version` and compares against
    {data}`NPM_MIN_VERSION_FOR_COOLDOWN`. Assumes support when the version cannot
    be determined, so a parse or exec failure never emits a spurious warning.
    """
    try:
        raw = subprocess.run(
            [npm, "--version"],
            capture_output=True,
            text=True,
            encoding="UTF-8",
            check=False,
        ).stdout.strip()
        return Version(raw) >= Version(NPM_MIN_VERSION_FOR_COOLDOWN)
    except (ValueError, OSError):
        return True


def _npm_install(npm: str, spec: ToolSpec, dest: Path, cooldown_days: int) -> Path:
    """Install the pinned *spec* into *dest* with *npm*, and return its executable.

    :param npm: The npm executable that runs the install.
    :param spec: The npm-backed tool to install.
    :param dest: Directory to install into (`dest/node_modules/.bin/<exe>`).
    :param cooldown_days: `minimum-release-age` in whole days; `0` omits the gate.
    :raises ToolInstallError: The install fails, or leaves no executable.
    """
    package_pin = f"{spec.install_target}@{spec.version}"
    cmd = [npm, "install", *NPM_INSTALL_FLAGS, "--prefix", str(dest), package_pin]
    if cooldown_days:
        cmd.append(f"--min-release-age={cooldown_days}")
    logging.info(f"Installing {spec.name} via npm: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        msg = f"npm install of {package_pin} failed with exit code {result.returncode}."
        raise ToolInstallError(msg)
    bin_path = dest / "node_modules" / ".bin" / spec.name
    if not bin_path.exists():
        msg = f"{spec.name!r} not found under {dest} after installing {package_pin}."
        raise ToolInstallError(msg)
    return bin_path


def _npm_audit_signatures(npm: str, prefix: Path) -> None:
    """Verify the registry signature and provenance of every package in *prefix*.

    `npm audit signatures` checks each installed package against the signing
    keys the registry publishes, and its Sigstore provenance attestation when
    the publisher produced one. A tarball whose integrity hash matched but
    whose signature does not is what this catches: a registry compromise, or
    a mirror serving altered content. The check reaches two hosts beside the
    registry: its keys endpoint, and the Sigstore TUF repository
    (`tuf-repo-cdn.sigstore.dev`).

    :param npm: The npm executable that runs the audit.
    :param prefix: The install prefix to audit.
    :raises ToolInstallError: A signature or an attestation did not verify,
        or the audit could not run, as when either host is unreachable.
    """
    cmd = [npm, "audit", "signatures", "--no-progress", "--prefix", str(prefix)]
    logging.info(f"Verifying npm signatures: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        msg = (
            f"npm audit signatures could not verify {prefix} (exit code "
            f"{result.returncode}): a signature or an attestation failed, or "
            "the registry keys or the Sigstore TUF repository were unreachable."
        )
        raise ToolInstallError(msg)


def _install_npm(spec: ToolSpec, dest: Path, cooldown_days: int) -> Path:
    """Install an npm tool into *dest* and return its executable path.

    The npm counterpart to {func}`~repomatic.tooling.binaries.install_binary`:
    installs `<package>@<version>` into a throwaway prefix with
    {data}`NPM_INSTALL_FLAGS`, relying on npm's own per-tarball integrity checks
    (no repomatic-pinned checksum), applies the `minimum-release-age`
    cooldown through npm's `min-release-age` when *cooldown_days* is non-zero,
    then verifies the registry signature and provenance of every installed
    package with `_npm_audit_signatures`.

    The npm on `PATH` only installs the pinned
    {data}`~repomatic.tooling.registry.NPM_BOOTSTRAP_TOOL` into `dest/npm`, and
    that npm installs the tool: see the rationale there. An npm older than
    {data}`NPM_MIN_VERSION_FOR_COOLDOWN` cannot enforce the gate, so the runner
    still checks the one that installs the tool, and warns (via
    {func}`_npm_supports_cooldown`) when it falls short.

    :param dest: Directory to install into (`dest/node_modules/.bin/<exe>`).
    :param cooldown_days: `minimum-release-age` in whole days; `0` omits the gate.
    :return: Path to the installed executable.
    :raises ToolInstallError: npm is absent, an install fails, or an
        executable is missing afterward.
    """
    npm = shutil.which("npm")
    if npm is None:
        msg = f"{spec.name} needs Node.js and npm on PATH, but npm was not found."
        raise ToolInstallError(msg)
    prefixes: list[Path] = []
    if spec.name != NPM_BOOTSTRAP_TOOL:
        pinned = TOOL_REGISTRY[NPM_BOOTSTRAP_TOOL]
        prefixes.append(dest / pinned.name)
        npm = str(_npm_install(npm, pinned, prefixes[0], cooldown_days))
    if cooldown_days and not _npm_supports_cooldown(npm):
        logging.warning(
            f"npm installing {spec.name} is older than "
            f"{NPM_MIN_VERSION_FOR_COOLDOWN}, so the minimum-release-age cooldown "
            "is not enforced; its transitive dependencies resolve without it."
        )
    bin_path = _npm_install(npm, spec, dest, cooldown_days)
    # The npm that installed the tool audits every prefix of the run, its own
    # bootstrap included: it is the pinned release, where the npm on PATH may
    # predate `audit signatures`.
    for prefix in (*prefixes, dest):
        _npm_audit_signatures(npm, prefix)
    return bin_path


def _build_install_args(spec: ToolSpec, exclude_newer: str | None = None) -> list[str]:
    """Build the command prefix for installing and running a tool.

    *exclude_newer* (a `YYYY-MM-DD` date) gates the isolated resolution by
    upload date, applying the `minimum-release-age` cooldown to the tool's
    transitive dependencies. It is ignored for `needs_venv` tools running from a
    lockfile, whose tree the frozen `uv.lock` already pins.

    ```{note}
    A `needs_venv` tool gets the project virtualenv only when the working
    directory holds a `uv.lock` to freeze it from. Without one, `uv run
    --frozen` aborts before the tool starts, and an unfrozen run would resolve
    the project live and write a brand-new `uv.lock` as a side effect. So the
    tool falls back to an isolated, cooldown-gated environment
    (`--no-project`), the same guarantee the `uvx` path provides: the tool
    still runs, it just cannot import the project's dependencies.
    ```

    A tool declaring
    {attr}`~repomatic.tooling.registry.ToolSpec.install_into_venv` is
    already in that virtualenv when a `uv.lock` exists, so its command carries
    no `--with` layer: see {func}`_install_into_venv`.
    """
    if spec.needs_venv:
        if _installs_into_venv(spec):
            # The project virtualenv already holds the tool, and was synced by
            # the install: see _install_into_venv.
            cmd = [*uv_cmd("run", frozen=True), "--no-sync"]
        elif Path("uv.lock").is_file():
            cmd = [*uv_cmd("run", frozen=True), "--with", spec.pypi_pin]
        else:
            cmd = uv_cmd("run", no_project=True, exclude_newer=exclude_newer)
            cmd.extend(["--with", spec.pypi_pin])
        if spec.module:
            cmd.extend(["--", "python", "-m", spec.module])
        else:
            cmd.extend(["--", spec.name])
    else:
        cmd = uvx_cmd(exclude_newer=exclude_newer)
        for pkg in spec.with_packages:
            cmd.extend(["--with", pkg])
        cmd.extend(["--from", spec.pypi_pin, spec.name])

    return cmd


def _installs_into_venv(spec: ToolSpec) -> bool:
    """Tell whether this run installs *spec* into the project virtualenv.

    Only a `uv.lock` gives the tool a project virtualenv to install into: see
    the note of {func}`_build_install_args`.
    """
    return spec.install_into_venv and Path("uv.lock").is_file()


def _venv_install_args(spec: ToolSpec, venv: str) -> list[str]:
    """Build the command that installs the pinned tool into *venv*.

    The command carries no cooldown flag of its own. Like the `uv run --frozen
    --with` form of the other `needs_venv` tools, it takes the window and its
    exemptions from the project's `[tool.uv]` section and from
    `UV_EXCLUDE_NEWER`.
    """
    return [*uv_cmd("pip"), "install", "--python", venv, spec.pypi_pin]


def _install_into_venv(spec: ToolSpec) -> None:
    """Install the pinned tool into the project virtualenv.

    `uv run --frozen` creates the virtualenv from the lockfile when it is
    missing, syncs it, and reports where it is. `uv pip install` then adds
    the tool to it. See
    {attr}`~repomatic.tooling.registry.ToolSpec.install_into_venv` for
    the reason.

    :raises ToolInstallError: uv cannot locate the virtualenv, or cannot
        install the tool into it.
    """
    locate = [
        *uv_cmd("run", frozen=True),
        "--",
        "python",
        "-c",
        "import sys; print(sys.prefix)",
    ]
    logging.info(f"Running: {' '.join(locate)}")
    # Only stdout is captured: what uv says about the sync stays in the log.
    located = subprocess.run(
        locate, stdout=subprocess.PIPE, text=True, encoding="UTF-8", check=False
    )
    venv = located.stdout.strip()
    if located.returncode != 0 or not venv:
        msg = (
            f"Cannot locate the project virtualenv to install {spec.name} into: "
            f"`{' '.join(locate)}` exited with code {located.returncode}."
        )
        raise ToolInstallError(msg)

    install = _venv_install_args(spec, venv)
    logging.info(f"Running: {' '.join(install)}")
    result = subprocess.run(install, check=False)
    if result.returncode != 0:
        msg = (
            f"Cannot install {spec.name} into {venv}: `{' '.join(install)}` "
            f"exited with code {result.returncode}."
        )
        raise ToolInstallError(msg)


def _path_tools_env(
    spec: ToolSpec, *, skip_checksum: bool, no_cache: bool
) -> dict[str, str] | None:
    """Build the child environment exposing *spec*'s companion binaries.

    Installs each {attr}`~repomatic.tooling.registry.ToolSpec.path_tools` entry
    through {func}`~repomatic.tooling.binaries.install_binary`, so it arrives
    at the registry-pinned version with its checksum verified, then prepends the
    directories holding them to `PATH`. Prepending (rather than appending) is
    what makes the pin authoritative: a system-installed build of the same tool
    further down `PATH` is shadowed instead of winning by accident.

    A companion publishing no binary for the running platform is skipped with a
    warning rather than failing the run, since no retry can make it exist:
    `shfmt` ships nothing for Windows ARM64, and hard-failing there would leave
    `repomatic run mdformat` unusable on the platform instead of merely leaving
    its shell blocks unformatted. Every other install failure still aborts.

    :param spec: Specification of the tool about to run.
    :param skip_checksum: Skip SHA-256 verification, as for the primary tool.
    :param no_cache: Bypass the binary cache when `True`.
    :return: The environment to hand the child, or `None` to inherit unchanged
        when the tool declares no companions.
    :raises BinaryInstallError: If a companion fails to install for any reason
        other than the platform having no binary at all.
    """
    if not spec.path_tools:
        return None

    prefixes = []
    for tool_name in spec.path_tools:
        companion = TOOL_REGISTRY[tool_name]
        try:
            bin_path = binaries.install_binary(
                companion, skip_checksum=skip_checksum, no_cache=no_cache
            )
        except UnsupportedPlatformError as exc:
            # The companion publishes nothing for this platform, so no run can
            # ever provision it here. Carrying on without it keeps the primary
            # tool working, degraded, instead of making it unusable on the
            # platform: mdformat still formats Markdown, leaving only its shell
            # blocks untouched.
            logging.warning(f"Skipping {tool_name} on PATH for {spec.name}: {exc}")
            continue
        except BinaryInstallError as exc:
            msg = f"Cannot provision {tool_name} on PATH for {spec.name}: {exc}"
            raise BinaryInstallError(msg) from exc
        logging.info(
            f"Exposing {tool_name} {companion.version} on PATH for {spec.name}."
        )
        prefixes.append(str(bin_path.parent))

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*prefixes, env.get("PATH", "")])
    return env


def _command_prefix(
    spec: ToolSpec, stack: ExitStack, *, skip_checksum: bool, no_cache: bool
) -> list[str]:
    """Install the tool and build the command that starts it.

    The command carries everything the run adds ahead of the caller's
    arguments: the default flags, the CI flags and the computed parameters.

    :param spec: The tool to install.
    :param stack: Owner of the scratch directory an npm install goes into.
    :param skip_checksum: Skip SHA-256 verification of a binary download.
    :param no_cache: Bypass the binary cache.
    :raises ToolInstallError: If the tool cannot be installed.
    """
    if spec.backend is ToolBackend.BINARY:
        binary = binaries.install_binary(
            spec, skip_checksum=skip_checksum, no_cache=no_cache
        )
        cmd = [str(binary)]
    elif spec.backend is ToolBackend.NPM:
        prefix = stack.enter_context(
            tempfile.TemporaryDirectory(prefix=f"repomatic-{spec.name}-npm-")
        )
        # Gate the transitive tree by the same minimum-release-age window the
        # sync jobs apply to pins, via npm's min-release-age.
        cooldown = min_release_age_days(load_repomatic_config().minimum_release_age)
        cmd = [str(_install_npm(spec, Path(prefix), cooldown))]
    else:
        # Gate the isolated uvx resolution (tool + transitive tree) by the
        # same minimum-release-age window the sync jobs apply to pins.
        cutoff = exclude_newer_cutoff(
            load_repomatic_config().minimum_release_age, today_utc()
        )
        if _installs_into_venv(spec):
            _install_into_venv(spec)
        cmd = _build_install_args(spec, exclude_newer=cutoff)

    cmd.extend(spec.default_flags)
    if spec.ci_flags and is_github_ci():
        cmd.extend(spec.ci_flags)
    if spec.computed_params:
        cmd.extend(spec.computed_params(Metadata()))
    return cmd


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


def resolve_default_args(spec: ToolSpec) -> list[list[str]] | None:
    """Build the argument batches for a bare `repomatic run <tool>`.

    Combines {attr}`~repomatic.tooling.registry.ToolSpec.default_args` with the
    file list named by
    {attr}`~repomatic.tooling.registry.ToolSpec.default_paths`, splitting into
    one batch per file when
    {attr}`~repomatic.tooling.registry.ToolSpec.per_file` is set.

    :param spec: The tool to resolve defaults for.
    :return: One argument list per invocation; a single empty-argument batch
        when the tool declares no defaults, so the caller runs it bare as
        before. `None` when the tool wants targets and the repository holds
        none, which means *skip the tool* rather than invoke it pathless.
    """
    if not spec.default_args and not spec.default_paths:
        return [[]]

    base = list(spec.default_args)
    if not spec.default_paths:
        return [base]

    paths = [str(path) for path in getattr(FileInventory(), spec.default_paths)]
    if not paths:
        logging.info(
            f"Skipping {spec.name}: the repository holds no {spec.default_paths}."
        )
        return None
    if spec.per_file:
        return [[*base, path] for path in paths]
    return [[*base, *paths]]


def _digest_targets(args: Sequence[str]) -> dict[str, str]:
    """Digest every existing file among a batch's arguments.

    Directories are walked, so a tool pointed at a tree is covered too. The
    walk never enters a {data}`GIT_DIRECTORY` below a directory argument.
    Non-path arguments (flags, their values) simply do not exist on disk and
    are skipped, which needs no flag parsing: a flag that happens to name a
    real file is digested harmlessly.

    :param args: One invocation's arguments.
    :return: Path string mapped to the SHA-256 of its content.
    """
    digests: dict[str, str] = {}
    for arg in args:
        path = Path(arg)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = []
            for root, folders, files in os.walk(path):
                # Removed in place, which is what keeps the walk out of it.
                if GIT_DIRECTORY in folders:
                    folders.remove(GIT_DIRECTORY)
                candidates.extend(
                    child for name in files if (child := Path(root, name)).is_file()
                )
        else:
            continue
        for candidate in candidates:
            digests[str(candidate)] = compute_file_sha256(candidate)
    return digests


def _run_batch(
    spec: ToolSpec,
    prefix: Sequence[str],
    config_args: Sequence[str],
    batch: Sequence[str],
    env: dict[str, str] | None,
) -> int:
    """Run the tool once, on one batch of arguments.

    :param spec: The tool to run.
    :param prefix: The command that starts the tool, from {func}`_command_prefix`.
    :param config_args: The config arguments from {func}`resolve_config`.
    :param batch: The arguments of this invocation.
    :param env: The environment of the child, `None` to inherit it.
    :return: The exit code of the tool, `0` when it reports a rewrite it did
        perform, or {data}`TOOL_CRASH_EXIT_CODE` when it reports a rewrite it
        did not perform.
    """
    cmd = [*prefix, *spec.splice_config_args(config_args, batch)]

    # See ToolSpec.output_flag for why the runner creates these.
    for destination in spec.output_paths(batch):
        destination.parent.mkdir(parents=True, exist_ok=True)

    # Snapshot the targets when the files have to say what the tool rewrote.
    before = _digest_targets(batch) if spec.tracks_rewrites else {}

    logging.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False, env=env)
    logging.info(f"{spec.name} exited with code {result.returncode}.")
    exit_code = result.returncode

    # The files settle what a rewrite status means. With a target rewritten,
    # the status reports the success the caller asked for, so it becomes 0 and
    # no caller has to tolerate it. With every target unchanged, it is a
    # contradiction, and the shape a crash takes: pyproject-fmt exits 1 on a
    # PanicException exactly as it does on a successful reformat.
    if spec.rewrite_exit_code is not None and exit_code == spec.rewrite_exit_code:
        if _digest_targets(batch) == before:
            logging.error(
                f"{spec.name} exited with code {exit_code}, which it uses "
                "to report rewritten files, but left every target unchanged. "
                f"Treating it as a crash and reporting {TOOL_CRASH_EXIT_CODE}; "
                "re-run it directly to see why."
            )
            exit_code = TOOL_CRASH_EXIT_CODE
        else:
            logging.info(
                f"{spec.name} rewrote its targets, which its exit code {exit_code}"
                " reports. Reporting success."
            )
            exit_code = 0

    # post_process is a write-mode-only fixup: it rewrites files on disk, so it
    # runs only after a successful write (return code 0), never in check or
    # dry-run mode, which writes nothing. The warning below covers that gap;
    # see ToolSpec.check_bypasses_post_process.
    if exit_code == 0 and spec.post_process:
        # A directory argument names no file, so add the ones this run
        # rewrote below it. A file the tool's own exclude list skipped is
        # unchanged, which keeps it out of the fixup as well.
        rewritten = [
            path
            for path, digest in _digest_targets(batch).items()
            if before.get(path) != digest and path not in batch
        ]
        spec.post_process([*batch, *rewritten])

    # A check/dry-run invocation of a post_process tool cannot be trusted: the
    # fixup never ran, so warn rather than report a misleading status.
    if spec.check_bypasses_post_process(batch):
        logging.warning(
            f"{spec.name} ran in check mode, but it relies on a repomatic "
            "post-processing step that only runs when files are written. Its "
            "exit status is unreliable here: it can flag drift the write path "
            "would reconcile, or miss drift the write path would introduce. "
            "Re-run in write mode and inspect the diff for an authoritative "
            f"result; from the CLI, `repomatic run {spec.name} --verify` does "
            "that against throwaway copies."
        )

    return exit_code


def _prepare(spec: ToolSpec) -> dict[str, str]:
    """Run the {attr}`~repomatic.tooling.registry.ToolSpec.prepare` hook of *spec*.

    :return: The environment variables the hook asks for, empty when the tool
        declares no hook.
    :raises ToolInstallError: When the hook cannot install what the run needs.
    """
    if spec.prepare is None:
        return {}
    try:
        return spec.prepare(Metadata())
    except UvError as exc:
        msg = f"Cannot prepare the {spec.name} run: {exc}"
        raise ToolInstallError(msg) from exc


def _remove_config_file(path: Path) -> None:
    """Delete a config file written for one run."""
    logging.debug(f"Cleaning up temp config: {path.resolve()}")
    path.unlink(missing_ok=True)


@contextmanager
def _problem_matcher(filename: str) -> Generator[None, None, None]:
    """Register a bundled problem matcher with the GitHub Actions runner, for one run.

    The runner reads the matcher file when it processes the registration, so
    the file goes to the temporary directory of the job, which outlives this
    process. The tool writes its findings to the stream the registration went
    to, so the removal, printed once the tool exited, reaches the runner after
    the last line the matcher has to read.

    :param filename: The matcher, in `repomatic/data/`.
    """
    content = bundle.get_data_content(filename)
    owners = [matcher["owner"] for matcher in json.loads(content)["problemMatcher"]]
    path = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / filename
    path.write_text(content, encoding="UTF-8")
    add_problem_matcher(path)
    try:
        yield
    finally:
        for owner in owners:
            remove_problem_matcher(owner)


def run_tool(
    name: str,
    extra_args: Sequence[str] = (),
    version: str | None = None,
    checksum: str | None = None,
    skip_checksum: bool = False,
    no_cache: bool = False,
) -> int:
    """Run an external tool with managed config resolution.

    With no *extra_args*, a tool declaring
    {attr}`~repomatic.tooling.registry.ToolSpec.default_args` or
    {attr}`~repomatic.tooling.registry.ToolSpec.default_paths` runs the
    invocation CI performs, resolved in-process by
    {func}`resolve_default_args`. Any explicit argument suppresses that
    entirely and is passed through as before.

    :param name: Tool name (must be in `TOOL_REGISTRY`).
    :param extra_args: Extra arguments passed through to the tool.
    :param version: Override the pinned version.
    :param checksum: Override the SHA-256 checksum for the current platform.
    :param skip_checksum: Skip SHA-256 verification entirely.
    :param no_cache: Bypass the binary cache when `True`.
    :return: The tool's exit code; the first non-zero one when the defaults
        resolved to several invocations. A tool declaring
        {attr}`~repomatic.tooling.registry.ToolSpec.rewrite_exit_code` reports
        `0` for a rewrite it performed, and {data}`TOOL_CRASH_EXIT_CODE` for one
        it did not.
    :raises ValueError: If no tool goes by *name*.
    :raises ToolInstallError: If the tool or one of its companions cannot be
        installed.
    """
    spec = lookup_tool(name).with_overrides(version=version, checksum=checksum)

    # A caller-supplied argument means the caller is driving: the registry
    # defaults apply only to a bare invocation. See ToolSpec.default_args.
    batches = [list(extra_args)] if extra_args else resolve_default_args(spec)
    if batches is None:
        return 0

    with ExitStack() as stack:
        # A tool with no `--config` flag may get its config written into the
        # working directory for the length of this run, so any other run that
        # would write the same file waits for this one: see _cwd_config_lock.
        # Entered first, the lock is released last, once the file is gone.
        config_lock = stack.enter_context(ExitStack())
        if spec.writes_config_in_cwd:
            config_lock.enter_context(_cwd_config_lock(spec))

        logging.info(f"Resolving config for {spec.name} {spec.version}...")
        config_args, config_file = resolve_config(spec)
        if config_file is None:
            # Nothing was written into the directory, so nothing to guard.
            config_lock.close()
        else:
            stack.callback(_remove_config_file, config_file)

        # Ready the machine before the install: a sync it runs into the project
        # virtualenv must not remove the tool installed there afterwards.
        prepared_env = _prepare(spec)
        prefix = _command_prefix(
            spec, stack, skip_checksum=skip_checksum, no_cache=no_cache
        )
        env = _path_tools_env(spec, skip_checksum=skip_checksum, no_cache=no_cache)
        if prepared_env:
            env = {**(env or os.environ), **prepared_env}
        # Each finding becomes an annotation on the line it names. See
        # ToolSpec.problem_matcher.
        if spec.problem_matcher and is_github_ci():
            stack.enter_context(_problem_matcher(spec.problem_matcher))

        exit_code = 0
        for batch in batches:
            batch_code = _run_batch(spec, prefix, config_args, batch, env)
            # Keep going after a failure, the way `xargs` does, but never let a
            # later success overwrite an earlier failure.
            if batch_code != 0 and exit_code == 0:
                exit_code = batch_code
        return exit_code


def verify_via_write_path(
    name: str,
    extra_args: Sequence[str] = (),
    **run_kwargs: Any,
) -> tuple[int, list[str]]:
    """Check a `post_process` tool's formatting without touching the tree.

    A tool pairing `post_process` with `check_flags` has no trustworthy check
    mode: the fixup only runs on the write path, so the check status can flag
    drift the write path would reconcile, or miss drift it would introduce (see
    {attr}`~repomatic.tooling.registry.ToolSpec.check_flags`). This runs the
    *write* path against throwaway copies instead, then compares, which is the
    only authoritative answer.

    ```{important}
    The copies are made **inside the working directory**, not in the system
    temp area. Formatters discover their config by walking up from each file,
    so a copy parked outside the repository resolves a different config and
    silently reports drift that does not exist.
    ```

    The working tree is never written to: only the copies are formatted, and
    they are removed before returning.

    :param name: Tool name, as in {func}`run_tool`.
    :param extra_args: Arguments for the tool. Any existing path among them is
        copied and rewritten to its copy; check flags are dropped, since they
        would defeat the write path this relies on. Every other argument is
        passed through untouched. Empty resolves the tool's registry defaults,
        the same set {func}`run_tool` would have run, flattened into one batch:
        the copies are per-path already, so a `per_file` split would only cost
        extra invocations.
    :param run_kwargs: Forwarded verbatim to {func}`run_tool`.
    :return: `(exit_code, drifted)`, where `exit_code` is `0` when every target
        is already formatted and `1` otherwise, and `drifted` names the paths
        the write path would have changed. A tool that fails on the copies
        yields its own exit code and no drift, since it measured nothing. So
        does a call with no existing path to copy, which yields `2`, and a
        path that cannot be copied, which is logged and yields `2` too.
    :raises ValueError: If no tool goes by *name*.
    """
    spec = lookup_tool(name)
    # An argument resolving to the working directory means "every file the tool
    # handles", which is exactly what the default batches below answer, and
    # answer cheaply. Copying the tree instead cannot work: the scratch root is
    # created *inside* the working directory (see the note above), so it is its
    # own copy destination, and `copytree` fails on it as an existing path.
    # Letting it through with `dirs_exist_ok` would be worse, recursing into
    # `.git`, the virtualenv, and the copy in progress.
    working_dir = Path.cwd().resolve()
    extra_args = tuple(
        arg
        for arg in extra_args
        if not (Path(arg).exists() and Path(arg).resolve() == working_dir)
    )
    if not extra_args:
        batches = resolve_default_args(spec)
        if batches is None:
            return 0, []
        # Deduplicate while preserving order: a non-per-file tool repeats its
        # `default_args` in no batch, but a per-file one repeats them in each.
        seen: dict[str, None] = {}
        for batch in batches:
            for arg in batch:
                seen.setdefault(arg, None)
        extra_args = tuple(seen)
    scratch = Path(tempfile.mkdtemp(prefix=".repomatic-verify-", dir=Path.cwd()))
    try:
        rewritten: list[str] = []
        # Map each copy back to its original, so the comparison below reads the
        # pair rather than re-deriving it from argument order.
        pairs: list[tuple[Path, Path]] = []
        for arg in extra_args:
            if arg in spec.check_flags:
                continue
            source = Path(arg)
            if not source.exists():
                rewritten.append(arg)
                continue
            # Mirror the path under the scratch root rather than flattening to
            # the basename. Two targets sharing a name would otherwise land on
            # the same copy, and every one but the last would be compared
            # against a neighbour's formatting: with 16 skills all entered
            # through a `SKILL.md`, that reports the whole set as drifted.
            try:
                relative = source.resolve().relative_to(Path.cwd().resolve())
            except ValueError:
                # Outside the working directory: nothing to mirror against, so
                # fall back to the basename.
                relative = Path(source.name)
            copy = scratch / relative
            try:
                copy.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, copy)
                else:
                    shutil.copy2(source, copy)
            except OSError as error:
                # A partial copy measures nothing: the tool would format half a
                # tree, and the comparison would report the rest as clean.
                logging.error(
                    f"{name}: cannot copy {source} to verify it ({error}), nothing"
                    " verified."
                )
                return 2, []
            pairs.append((source, copy))
            rewritten.append(str(copy))

        # Nothing copied means nothing measured, which must not read as clean. A
        # tool that walks the tree itself declares no default path to copy, and
        # the working directory cannot be copied (see above), so it needs paths.
        if not pairs:
            if extra_args:
                logging.error(
                    f"{name}: no existing path among {extra_args!r}, nothing to verify."
                )
            else:
                logging.error(
                    f"{name} declares no default path to copy: name the files or"
                    " directories to verify."
                )
            return 2, []

        # A tool that died on the copies formatted nothing, so the comparison
        # below would find no difference and report the tree as clean. Surface
        # the failure instead of laundering it into a passing verdict. A
        # formatter that reformatted a copy reports 0 here, whatever rewrite
        # status it exits with: run_tool checks that status against the files.
        run_code = run_tool(name, extra_args=rewritten, **run_kwargs)
        if run_code != 0:
            logging.error(
                f"{name} failed with code {run_code} on the throwaway copies; its "
                "formatting cannot be verified."
            )
            return run_code, []

        drifted = sorted(
            str(source_file)
            for source, copy in pairs
            for source_file, copied_file in _walk_pair(source, copy)
            if source_file.read_bytes() != copied_file.read_bytes()
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return (1 if drifted else 0), drifted


def _walk_pair(original: Path, copy: Path) -> Iterator[tuple[Path, Path]]:
    """Yield every `(original, copy)` file pair under a copied target."""
    if original.is_file():
        yield original, copy
        return
    for child in sorted(p for p in original.rglob("*") if p.is_file()):
        mirrored = copy / child.relative_to(original)
        if mirrored.is_file():
            yield child, mirrored


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def find_unmodified_configs(root: Path | None = None) -> list[tuple[str, str]]:
    """Find native config files identical to their bundled defaults.

    Iterates over every tool in
    {data}`~repomatic.tooling.registry.TOOL_REGISTRY` that has a
    `default_config`.  For each, checks whether any of its
    `native_config_files` exists on disk and is content-identical
    to the bundled default, both compared in the
    {func}`~repomatic.file_ops.normalize_text` form `init` writes.

    :param root: Directory the relative config paths resolve against.
        Defaults to the working directory; `run_init` passes its
        `output_dir` so the scan and the deletion the CLI derives from it
        (`--delete-unmodified`) agree on one tree.
    :return: List of `(tool_name, relative_path)` tuples for each
        unmodified file found.
    """
    base = root or Path()
    unmodified: list[tuple[str, str]] = []

    for name, spec in sorted(TOOL_REGISTRY.items()):
        if not spec.default_config:
            continue

        bundled = normalize_text(bundle.get_data_content(spec.default_config))

        for config_file in spec.native_config_files:
            path = base / config_file
            if not path.exists():
                continue
            native = normalize_text(path.read_text(encoding="UTF-8"))
            if native == bundled:
                unmodified.append((name, config_file))

    return unmodified
