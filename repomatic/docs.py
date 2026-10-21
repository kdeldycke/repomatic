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

Backs the `update-docs` command, which runs the phases of {class}`DocsPhase`
in order: `sphinx-apidoc`, the RST-to-MyST conversion, the toctree wiring, the
project's `docs/docs_update.py` script, the reformat of a `pyproject.toml` that
script changed, and the self-updating directive-block refresh.
{func}`update_docs` returns one {class}`PhaseResult` per phase it ran, and the
command reports them. Configuration is read from `[tool.repomatic.docs]`.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from click_extra import convert_rst_files_in_directory

from .deps.uv import uv_cmd
from .file_inventory import FileInventory
from .metadata.core import Metadata
from .tooling.registry import ToolInstallError
from .tooling.runner import run_tool

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .config import Config


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

TOCTREE_ENTRY_RE = re.compile(r"^[ \t]*(\w+(?:\.\w+)+)\s*$", re.MULTILINE)
"""A `toctree` entry of a generated index page: a dotted module name alone on its line.

The reST directive that `click_extra` wraps in an ``{eval-rst}`` block indents
its entries. A native MyST ``{toctree}`` fence holds them at the margin.
"""


class DocsPhase(Enum):
    """One phase of `update-docs`, in the order the phases run.

    The help of the command lists the phases from this registry, so the two
    cannot drift apart.
    """

    APIDOC = (
        "sphinx-apidoc",
        "Run sphinx-apidoc to generate the RST stub of every module.",
    )
    CONVERSION = (
        "RST conversion",
        "Convert the RST stubs to MyST Markdown, if the project uses MyST-Parser.",
    )
    TOCTREE = (
        "Toctree wiring",
        "List every generated page in the toctree of its parent index page.",
    )
    SCRIPT = (
        "Docs update script",
        "Run docs/docs_update.py (docs.update-script), if it exists.",
    )
    PYPROJECT = (
        "pyproject.toml reformat",
        "Reformat with pyproject-fmt each pyproject.toml an earlier phase changed.",
    )
    DIRECTIVES = (
        "Directive-block refresh",
        "Refresh the {matrix} and :mirror: blocks of docs/ and readme.md.",
    )

    def __init__(self, label: str, description: str) -> None:
        self.label = label
        """Short name of the phase, for a report line."""
        self.description = description
        """What the phase does, in the plain text of the command's help."""

    @property
    def checks(self) -> bool:
        """Whether the phase has a check mode, so `update-docs --check` runs it.

        The other phases write files and have no dry-run mode.
        """
        return self in {DocsPhase.SCRIPT, DocsPhase.DIRECTIVES}


class PhaseStatus(Enum):
    """How one phase of `update-docs` ended."""

    DONE = auto()
    """The phase ran, and wrote or found everything up to date."""

    SKIPPED = auto()
    """The phase has nothing to do in this project."""

    DRIFT = auto()
    """In check mode, the phase found out-of-date content."""

    FAILED = auto()
    """The phase failed, which stops the phases after it."""


@dataclass(frozen=True)
class PhaseResult:
    """What one phase of `update-docs` did."""

    phase: DocsPhase
    """The phase that ran."""

    status: PhaseStatus
    """How it ended."""

    message: str = ""
    """What to tell the user: the work done, the reason for a skip, the
    out-of-date content, or the failure."""


def validate_docs_script_path(script: str, repo_root: Path) -> Path | None:
    """Validate and resolve a docs update script path.

    :param script: Configured `docs.update-script` path, relative to the repo.
    :param repo_root: Repository root the script path resolves against.
    :return: The resolved path, or `None` when the configured value is empty.
    :raises ValueError: If the path escapes the repository root or is not a
        `.py` file under `docs/`.
    """
    if not script:
        return None
    script_path = (repo_root / script).resolve()
    if not script_path.is_relative_to(repo_root):
        msg = f"docs.update-script escapes repository root: {script}"
        raise ValueError(msg)
    if not script_path.is_relative_to((repo_root / "docs").resolve()):
        msg = f"docs.update-script must be under docs/: {script}"
        raise ValueError(msg)
    if script_path.suffix != ".py":
        msg = f"docs.update-script must be a .py file: {script}"
        raise ValueError(msg)
    return script_path


def has_directive_block(path: Path) -> bool:
    """Whether *path* carries a self-updating block worth refreshing.

    :param path: Markdown file to scan.
    :return: `True` when any {data}`DIRECTIVE_BLOCK_MARKERS` entry appears.
    """
    text = path.read_text(encoding="UTF-8")
    return any(marker in text for marker in DIRECTIVE_BLOCK_MARKERS)


def _run_docs_tool(*args: str) -> int:
    """Run a tool from the `docs` dependency group through uv.

    The command runs with the frozen lockfile and the `docs` group installed.

    :param args: The command and its arguments, passed after `uv run --`.
    :return: The command's exit code.
    """
    cmd = [*uv_cmd("run", frozen=True), "--group", "docs", "--", *args]
    logging.info(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


def _tool_result(
    phase: DocsPhase, code: int, *, check: bool = False, label: str = ""
) -> PhaseResult:
    """Read the exit code of a phase's tool.

    :param phase: The phase the tool ran for.
    :param code: The tool's exit code.
    :param check: Whether the tool ran in its check mode, where a non-zero
        code means out-of-date content rather than a failure.
    :param label: The name of the phase in the messages. Defaults to its
        {attr}`DocsPhase.label`.
    """
    label = label or phase.label
    if not code:
        message = "" if check else f"{label} completed."
        return PhaseResult(phase, PhaseStatus.DONE, message)
    if check:
        return PhaseResult(phase, PhaseStatus.DRIFT, label)
    return PhaseResult(
        phase, PhaseStatus.FAILED, f"{label} failed with exit code {code}."
    )


def _toctree_entries(page: Path) -> set[str]:
    """Return the dotted names listed in every `toctree` of an index page."""
    return set(TOCTREE_ENTRY_RE.findall(page.read_text(encoding="UTF-8")))


def _insert_into_toctree(page: Path, section: str, entry: str) -> bool:
    """Add `entry` to the `toctree` under `section` of `page`, sorted.

    :param page: The index page to rewrite.
    :param section: Heading whose `toctree` receives the entry, like
        `Submodules`.
    :param entry: Dotted module name to insert.
    :return: `True` when the page was rewritten, `False` when it carries no
        such section.
    """
    lines = page.read_text(encoding="UTF-8").splitlines(keepends=True)
    try:
        start = lines.index(f"## {section}\n")
    except ValueError:
        return False

    # The entries are the dotted names of the first toctree below the heading.
    # Collect that one run, then splice the new name into it.
    run: list[int] = []
    for offset, line in enumerate(lines[start:], start=start):
        if TOCTREE_ENTRY_RE.fullmatch(line):
            run.append(offset)
        elif run:
            break
    if not run:
        return False

    first = lines[run[0]]
    indent = first[: len(first) - len(first.lstrip())]
    block = sorted([*lines[run[0] : run[-1] + 1], f"{indent}{entry}\n"])
    lines[run[0] : run[-1] + 1] = block
    page.write_text("".join(lines), encoding="UTF-8")
    return True


def _is_package_page(page: Path) -> bool:
    """Whether a generated page documents a package rather than a module.

    `sphinx-apidoc` titles a page `<name> package` or `<name> module`, and the
    title is the one place that says which. Without `--separate` the modules of
    a package sit inline in its page, so a package page has no page below it.
    """
    title = page.read_text(encoding="UTF-8").partition("\n")[0]
    return title.startswith("# ") and title.rstrip().endswith(" package")


def wire_api_pages_into_toctrees(docs_dir: Path) -> list[str]:
    """List every generated API page in the `toctree` of its parent index.

    `sphinx-apidoc --no-toc` writes one page per module and no package index,
    so a module added after an index was last written stays orphaned: the page
    exists, nothing points at it, and Sphinx reports
    `document isn't included in any toctree` as a warning the build survives.
    Wiring it here is what keeps the roster whole without a hand edit per
    module, and it is the half {func}`click_extra.convert_rst_files_in_directory`
    cannot do, since that one only declines to overwrite an existing page.

    A page goes under `Subpackages` when it documents a package: it is itself
    an index, or its title says so. It goes under `Submodules` otherwise. An
    index missing the section it needs is left alone and named in the return
    value, since inventing a heading would guess at a page layout this function
    does not own.

    :param docs_dir: The `docs/` directory holding the generated pages.
    :return: One `<page> -> <index>` line per entry inserted, plus a
        `<page>: <reason>` line per page that could not be wired.
    """
    stems = {path.stem for path in docs_dir.glob("*.md")}
    # An index is any page some other page's dotted name sits below.
    indexes = {
        stem
        for stem in stems
        if any(o.startswith(f"{stem}.") for o in stems if o != stem)
    }

    report: list[str] = []
    for stem in sorted(stems):
        if "." not in stem:
            continue
        parts = stem.split(".")
        parent = next(
            (
                ".".join(parts[:cut])
                for cut in range(len(parts) - 1, 0, -1)
                if ".".join(parts[:cut]) in indexes
            ),
            None,
        )
        if parent is None:
            report.append(f"{stem}: no parent index page")
            continue

        index = docs_dir / f"{parent}.md"
        if stem in _toctree_entries(index):
            continue

        is_package = stem in indexes or _is_package_page(docs_dir / f"{stem}.md")
        section = "Subpackages" if is_package else "Submodules"
        if _insert_into_toctree(index, section, stem):
            report.append(f"{stem} -> {parent}.md ({section})")
        else:
            report.append(f"{stem}: {parent}.md carries no {section} toctree")

    return report


def _apidoc(config: Config, meta: Metadata, docs_dir: Path) -> PhaseResult:
    """Run `sphinx-apidoc` over the project, when autodoc is active."""
    phase = DocsPhase.APIDOC
    if not meta.active_autodoc:
        return PhaseResult(phase, PhaseStatus.SKIPPED, "No active autodoc extension.")
    code = _run_docs_tool(
        "sphinx-apidoc",
        "--no-toc",
        "--module-first",
        "--output-dir",
        str(docs_dir),
        *config.docs.apidoc_extra_args,
        ".",
        *config.docs.apidoc_exclude,
    )
    return _tool_result(phase, code)


def _convert(meta: Metadata, docs_dir: Path) -> PhaseResult:
    """Convert the RST stubs to MyST Markdown, when the project uses MyST."""
    phase = DocsPhase.CONVERSION
    if not meta.uses_myst:
        return PhaseResult(phase, PhaseStatus.SKIPPED, "MyST-Parser not detected.")
    if not docs_dir.is_dir():
        return PhaseResult(phase, PhaseStatus.SKIPPED, f"No {docs_dir} directory.")
    converted = convert_rst_files_in_directory(docs_dir)
    if not converted:
        return PhaseResult(phase, PhaseStatus.SKIPPED, "No RST file to convert.")
    return PhaseResult(
        phase,
        PhaseStatus.DONE,
        f"Converted {len(converted)} RST file(s) to MyST Markdown.",
    )


def _wire(meta: Metadata, docs_dir: Path) -> PhaseResult:
    """List the generated pages in the toctree of their parent index."""
    phase = DocsPhase.TOCTREE
    if not meta.active_autodoc or not docs_dir.is_dir():
        return PhaseResult(phase, PhaseStatus.SKIPPED, "No generated API page.")
    wired = wire_api_pages_into_toctrees(docs_dir)
    if not wired:
        return PhaseResult(phase, PhaseStatus.DONE)
    lines = "".join(f"\n  {line}" for line in wired)
    return PhaseResult(
        phase, PhaseStatus.DONE, f"Wired API pages into their toctree:{lines}"
    )


def _script(config: Config, repo_root: Path, check: bool) -> PhaseResult:
    """Run the project's docs update script, if it exists."""
    phase = DocsPhase.SCRIPT
    script_path = validate_docs_script_path(config.docs.update_script, repo_root)
    if not script_path:
        return PhaseResult(
            phase, PhaseStatus.SKIPPED, "Docs update script disabled (empty path)."
        )
    if not script_path.is_file():
        return PhaseResult(
            phase, PhaseStatus.SKIPPED, f"Docs update script not found: {script_path}"
        )
    code = _run_docs_tool("python", str(script_path), *(["--check"] if check else []))
    return _tool_result(
        phase, code, check=check, label=f"{phase.label} ({script_path.name})"
    )


def _reformat_pyproject(before: Mapping[Path, bytes]) -> PhaseResult:
    """Reformat each `pyproject.toml` an earlier phase of this run changed.

    The docs update script may regenerate sections of `pyproject.toml`, like
    label rules or keywords. Reformatting what it wrote keeps the output of
    `update-docs` a `pyproject-fmt` fixpoint, so its pull request never fights
    the one of the `format-pyproject` job. A file this run left alone keeps
    any formatting drift it had: that drift belongs to `format-pyproject`.

    :param before: The content of each `pyproject.toml` before the first phase.
    """
    phase = DocsPhase.PYPROJECT
    changed = [
        str(path)
        for path, content in before.items()
        if path.is_file() and path.read_bytes() != content
    ]
    if not changed:
        return PhaseResult(phase, PhaseStatus.SKIPPED, "No pyproject.toml changed.")
    try:
        code = run_tool("pyproject-fmt", extra_args=changed)
    except ToolInstallError as error:
        return PhaseResult(phase, PhaseStatus.FAILED, str(error))
    return _tool_result(phase, code)


def _refresh(repo_root: Path, docs_dir: Path, check: bool) -> PhaseResult:
    """Refresh the self-updating blocks of the docs and the readme.

    Only files already carrying a block are passed, so repositories without
    any stay clear of the sphinx extra that `refresh-directives` requires.
    """
    phase = DocsPhase.DIRECTIVES
    candidates = sorted(docs_dir.rglob("*.md")) if docs_dir.is_dir() else []
    readme_path = repo_root / "readme.md"
    if readme_path.is_file():
        candidates.append(readme_path)
    directive_files = [path for path in candidates if has_directive_block(path)]
    if not directive_files:
        return PhaseResult(
            phase, PhaseStatus.SKIPPED, "No self-updating directive block found."
        )
    code = _run_docs_tool(
        "click-extra",
        "refresh-directives",
        *(["--check"] if check else []),
        *(str(path) for path in directive_files),
    )
    return _tool_result(phase, code, check=check)


def update_docs(config: Config, *, check: bool = False) -> list[PhaseResult]:
    """Run the phases of {class}`DocsPhase` in order, and say how each ended.

    A failed phase stops the phases after it. A project with no Sphinx
    configuration runs no phase.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param check: Report out-of-date content without writing, for CI drift
        detection. Only the phases whose {attr}`DocsPhase.checks` is true
        run, in their own check modes (`docs_update.py --check` and
        `refresh-directives --check`). The update script must accept a
        ``--check`` flag to participate: a script that ignores it will still
        write.
    :return: One result per phase that ran.
    :raises ValueError: When `docs.update-script` names a path outside
        `docs/`, or a file that is not Python.
    """
    repo_root = Path.cwd()
    docs_dir = repo_root / "docs"

    # Detect Sphinx capabilities from conf.py.
    meta = Metadata()
    if not meta.is_sphinx:
        logging.info("No Sphinx configuration found. Nothing to do.")
        return []

    # What the phases start from, for the reformat to tell what they changed.
    pyproject_before = {
        path: path.read_bytes() for path in FileInventory().pyproject_files
    }
    runners: dict[DocsPhase, Callable[[], PhaseResult]] = {
        DocsPhase.APIDOC: lambda: _apidoc(config, meta, docs_dir),
        DocsPhase.CONVERSION: lambda: _convert(meta, docs_dir),
        DocsPhase.TOCTREE: lambda: _wire(meta, docs_dir),
        DocsPhase.SCRIPT: lambda: _script(config, repo_root, check),
        DocsPhase.PYPROJECT: lambda: _reformat_pyproject(pyproject_before),
        DocsPhase.DIRECTIVES: lambda: _refresh(repo_root, docs_dir, check),
    }
    results: list[PhaseResult] = []
    for phase in DocsPhase:
        if check and not phase.checks:
            continue
        result = runners[phase]()
        results.append(result)
        if result.status is PhaseStatus.FAILED:
            break
    return results
