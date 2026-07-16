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

from click_extra import ClickException, echo

from .metadata import Metadata
from .rst_to_myst import convert_rst_files_in_directory

TYPE_CHECKING = False
if TYPE_CHECKING:
    from .config import Config


def validate_docs_script_path(script: str, repo_root: Path) -> Path | None:
    """Validate and resolve a docs update script path.

    Returns the resolved path if the script exists, or `None` if the
    configured value is empty. Raises `ClickException` if the path
    escapes the repository root or is not under the `docs/` directory.
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


def _run_docs_tool(label: str, *args: str) -> None:
    """Run a tool from the `docs` dependency group through uv.

    Shared invocation shape for every `update-docs` phase: the command runs
    with the frozen lockfile and the `docs` group installed, failures raise a
    `ClickException` naming the phase, and success is echoed.

    :param label: Human-readable phase name for logs and errors.
    :param args: The command and its arguments, passed after `uv run --`.
    """
    cmd = ["uv", "--no-progress", "run", "--frozen", "--group", "docs", "--", *args]
    logging.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode:
        raise ClickException(f"{label} failed with exit code {result.returncode}")
    echo(f"{label} completed.")


def update_docs(config: Config) -> None:
    """Regenerate Sphinx autodoc stubs and run the project's update script.

    Orchestrates four phases:

    1. Run `sphinx-apidoc` to generate RST stubs for all modules.
    2. If MyST-Parser is detected, convert the RST stubs to MyST markdown
       with `{eval-rst}` blocks.
    3. Run the project-specific `docs/docs_update.py` script (if present)
       to generate dynamic content.
    4. Refresh self-updating directive blocks (like `{matrix}` compatibility
       tables) found in `docs/` pages and `readme.md`, via
       `click-extra refresh-directives`.

    :param config: The resolved `[tool.repomatic]` configuration.
    """
    repo_root = Path.cwd()
    docs_dir = repo_root / "docs"

    # Detect Sphinx capabilities from conf.py.
    meta = Metadata()

    if not meta.is_sphinx:
        logging.info("No Sphinx configuration found. Nothing to do.")
        return

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
        _run_docs_tool(
            f"Docs update script ({script_path.name})", "python", str(script_path)
        )
    elif script_path:
        logging.info(f"Docs update script not found: {script_path}")
    else:
        logging.info("Docs update script disabled (empty path).")

    # Phase 4: self-updating directive blocks. Both forms are refreshed: the
    # `{matrix}` MyST fence (live-rendered by Sphinx) and the `<!-- matrix -->`
    # comment region (whose embedded table renders on GitHub too). Only files
    # already carrying a block are passed, so repositories without any stay
    # clear of the sphinx extra that `refresh-directives` requires.
    def has_directive_block(path: Path) -> bool:
        text = path.read_text(encoding="UTF-8")
        return "{matrix}" in text or "<!-- matrix" in text

    candidates = sorted(docs_dir.rglob("*.md")) if docs_dir.is_dir() else []
    readme_path = repo_root / "readme.md"
    if readme_path.is_file():
        candidates.append(readme_path)
    directive_files = [path for path in candidates if has_directive_block(path)]
    if directive_files:
        _run_docs_tool(
            "Directive-block refresh",
            "click-extra",
            "refresh-directives",
            *(str(path) for path in directive_files),
        )
    else:
        logging.info("No self-updating directive blocks found. Skipping refresh.")
