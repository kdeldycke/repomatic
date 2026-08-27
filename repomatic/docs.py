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

"""Regenerate Sphinx API docs and dynamic documentation content.

Backs the `update-docs` command: orchestrates `sphinx-apidoc`, the RST-to-MyST
conversion, the project's `docs/docs_update.py` script, and the self-updating
directive-block refresh. Configuration is read from `[tool.repomatic.docs]`.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from click_extra import ClickException, convert_rst_files_in_directory, echo

from .deps.uv import uv_cmd
from .metadata.core import Metadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from .config import Config


def validate_docs_script_path(script: str, repo_root: Path) -> Path | None:
    """Validate and resolve a docs update script path.

    :param script: Configured `docs.update-script` path, relative to the repo.
    :param repo_root: Repository root the script path resolves against.
    :return: The resolved path, or `None` when the configured value is empty.
    :raises ClickException: If the path escapes the repository root or is not
        a `.py` file under `docs/`.
    """
    if not script:
        return None
    script_path = (repo_root / script).resolve()
    # Must be under the repository root.
    try:
        script_path.relative_to(repo_root)
    except ValueError:
        raise ClickException(f"docs.update-script escapes repository root: {script}")
    # Must be under docs/ and be a Python file.
    docs_dir = (repo_root / "docs").resolve()
    try:
        script_path.relative_to(docs_dir)
    except ValueError:
        raise ClickException(f"docs.update-script must be under docs/: {script}")
    if script_path.suffix != ".py":
        raise ClickException(f"docs.update-script must be a .py file: {script}")
    return script_path


DIRECTIVE_BLOCK_MARKERS: tuple[str, ...] = (
    "{matrix}",
    "<!-- matrix",
    ":mirror:",
    "<!-- mirror",
)
"""Markers of a self-updating block `click-extra refresh-directives` rewrites.

Every form the refresh recognizes: the ``{matrix}`` MyST fence (live-rendered by
Sphinx), the `<!-- matrix -->` comment region (whose embedded table renders on
GitHub too), and the `python:render` `:mirror:` region (`<!-- mirror -->`, whose
generator Python the refresh executes).
"""


def has_directive_block(path: Path) -> bool:
    """Whether *path* carries a self-updating block worth refreshing.

    :param path: Markdown file to scan.
    :return: `True` when any {data}`DIRECTIVE_BLOCK_MARKERS` entry appears.
    """
    text = path.read_text(encoding="UTF-8")
    return any(marker in text for marker in DIRECTIVE_BLOCK_MARKERS)


def _run_docs_tool(label: str, *args: str, check: bool = False) -> int:
    """Run a tool from the `docs` dependency group through uv.

    Shared invocation shape for every `update-docs` phase: the command runs
    with the frozen lockfile and the `docs` group installed, failures raise a
    `ClickException` naming the phase, and success is echoed.

    :param label: Human-readable phase name for logs and errors.
    :param args: The command and its arguments, passed after `uv run --`.
    :param check: In drift-detection mode a non-zero exit means "out of date"
        rather than "failed", so the code is returned instead of raised and the
        caller aggregates drift across phases.
    :return: The command's exit code.
    """
    cmd = [*uv_cmd("run", frozen=True), "--group", "docs", "--", *args]
    logging.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if check:
        return result.returncode
    if result.returncode:
        raise ClickException(f"{label} failed with exit code {result.returncode}")
    echo(f"{label} completed.")
    return result.returncode


def update_docs(config: Config, *, check: bool = False) -> None:
    """Regenerate Sphinx autodoc stubs and run the project's update script.

    Orchestrates four phases:

    1. Run `sphinx-apidoc` to generate RST stubs for all modules.
    2. If MyST-Parser is detected, convert the RST stubs to MyST markdown
       with ``{eval-rst}`` blocks.
    3. Run the project-specific `docs/docs_update.py` script (if present)
       to generate dynamic content.
    4. Refresh self-updating blocks (``{matrix}`` compatibility tables and
       `python:render` `:mirror:` regions) found in `docs/` pages and
       `readme.md`, via `click-extra refresh-directives`.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param check: Report out-of-date content without writing, for CI drift
        detection. Phases 1–2 regenerate files and have no dry-run mode, so
        they are skipped; the self-updating phases run in their own check modes
        (`docs_update.py --check` and `refresh-directives --check`) and any
        drift raises a `ClickException`. The update script must accept a
        ``--check`` flag to participate: a script that ignores it will still
        write.
    """
    repo_root = Path.cwd()
    docs_dir = repo_root / "docs"

    # Detect Sphinx capabilities from conf.py.
    meta = Metadata()

    if not meta.is_sphinx:
        logging.info("No Sphinx configuration found. Nothing to do.")
        return

    # Names of the self-updating phases found out of date, collected in check
    # mode and reported at the end.
    drift: list[str] = []

    # Phases 1-2 write files and have no dry-run mode, so they are skipped when
    # only checking for drift.
    if not check:
        # Phase 1: sphinx-apidoc.
        if meta.active_autodoc:
            _run_docs_tool(
                "sphinx-apidoc",
                "sphinx-apidoc",
                "--no-toc",
                "--module-first",
                "--output-dir",
                str(docs_dir),
                *config.docs.apidoc_extra_args,
                ".",
                *config.docs.apidoc_exclude,
            )
        else:
            logging.info("No active autodoc extensions. Skipping sphinx-apidoc.")

        # Phase 2: RST → MyST conversion.
        if meta.uses_myst and docs_dir.is_dir():
            converted = convert_rst_files_in_directory(docs_dir)
            if converted:
                echo(f"Converted {len(converted)} RST file(s) to MyST markdown.")
            else:
                logging.info("No RST files to convert.")
        elif not meta.uses_myst:
            logging.info("MyST-Parser not detected. Skipping RST conversion.")

    # Phase 3: docs update script.
    script_path = validate_docs_script_path(config.docs.update_script, repo_root)
    if script_path and script_path.is_file():
        label = f"Docs update script ({script_path.name})"
        script_args = [str(script_path), *(["--check"] if check else [])]
        code = _run_docs_tool(label, "python", *script_args, check=check)
        if check and code:
            drift.append(label)
    elif script_path:
        logging.info(f"Docs update script not found: {script_path}")
    else:
        logging.info("Docs update script disabled (empty path).")

    # Phase 4: self-updating blocks. Only files already carrying a block are
    # passed, so repositories without any stay clear of the sphinx extra that
    # `refresh-directives` requires.
    candidates = sorted(docs_dir.rglob("*.md")) if docs_dir.is_dir() else []
    readme_path = repo_root / "readme.md"
    if readme_path.is_file():
        candidates.append(readme_path)
    directive_files = [path for path in candidates if has_directive_block(path)]
    if directive_files:
        refresh_args = [
            "click-extra",
            "refresh-directives",
            *(["--check"] if check else []),
            *(str(path) for path in directive_files),
        ]
        code = _run_docs_tool("Directive-block refresh", *refresh_args, check=check)
        if check and code:
            drift.append("self-updating directive blocks")
    else:
        logging.info("No self-updating directive blocks found. Skipping refresh.")

    if check:
        if drift:
            raise ClickException(
                "Documentation is out of date ("
                + ", ".join(drift)
                + "). Run `repomatic update-docs`."
            )
        echo("Documentation is up to date.")
